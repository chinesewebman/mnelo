import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server


def _make_fake_memory(hygiene_values: dict):
    """[8/9 P1 follow-up] mcp_server.py:1017 直接用 target._conn.execute 拿 PII 24h count.

    FakeMemory mock 必须提供 _conn. 用真 Memory() instance + 临时 db_path, 走
    schema.sql (audit_log 表存在) 避免 pii_24h query 抛 "no such table".
    check_same_thread=False 跟 memory.py:282 Memory() 一样, health endpoint 在
    starlette 另一 thread 跑.
    """

    class FakeMemory:
        def __init__(self):
            self.values = hygiene_values
            # [8/9] 用真 Memory() instance (非空 :memory: db) — 避免 pii_24h query
            # 抛 "no such table: audit_log" 整 try 死. check_same_thread=False
            # 跟 memory.py:282 Memory() 一样, 因为 health endpoint 在 starlette 另一 thread 跑.
            import tempfile
            from pathlib import Path
            from memory import Memory

            tmpdir = tempfile.mkdtemp(prefix="health_fake_")
            self._real_mem = Memory(db_path=Path(tmpdir) / "memory.db")
            self._conn = self._real_mem._conn

        def stats(self):
            return {"hygiene": self.values}

    return FakeMemory()


def test_health_route_is_registered_and_reports_hygiene():
    app = mcp_server._build_sse_app("test-token")
    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/health" in paths


def test_health_endpoint_returns_json():
    from starlette.testclient import TestClient

    app = mcp_server._build_sse_app("test-token")
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert "hygiene" in body
    assert "purge_backlog" in body["hygiene"]
    assert "importance_below_floor" in body["hygiene"]
    assert "freshness" in body["hygiene"]


def test_health_recommends_maintenance_when_degraded(monkeypatch):
    from starlette.testclient import TestClient

    fake = _make_fake_memory({"purge_backlog": 200, "decay_floor_chunks": 150, "freshness": 0.5})
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", fake)
    monkeypatch.setattr(mcp_server.config, "health_purge_backlog_threshold", 100)
    monkeypatch.setattr(mcp_server.config, "health_floor_chunks_threshold", 100)
    body = TestClient(mcp_server._build_sse_app("test-token")).get("/health").json()
    assert body["status"] == "degraded"
    assert "recommendations" in body
    by_tool = {r["tool"]: r for r in body["recommendations"]}
    assert by_tool["memory_maintenance"]["safe"] is True
    assert by_tool["memory_maintenance"]["args"] == {
        "passes": ["hygiene"],
        "dry_run": True,
        "confirm_destructive": False,
    }
    assert by_tool["memory_audit_list"]["safe"] is True
    assert by_tool["memory_audit_list"]["args"] == {"pass_name": "hygiene", "limit": 20}
    assert "purge backlog" in by_tool["memory_maintenance"]["reason"]


def test_health_recommendation_payload_contract(monkeypatch):
    from starlette.testclient import TestClient

    fake = _make_fake_memory({"purge_backlog": 0, "decay_floor_chunks": 0, "freshness": 1.0})
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", fake)
    monkeypatch.setattr(mcp_server.config, "health_purge_backlog_threshold", 100)
    monkeypatch.setattr(mcp_server.config, "health_floor_chunks_threshold", 100)
    body = TestClient(mcp_server._build_sse_app("test-token")).get("/health").json()
    assert body["status"] == "ok"
    assert "recommendations" in body
    assert body["recommendations"] == []


def test_health_threshold_boundary_is_configurable(monkeypatch):
    from starlette.testclient import TestClient

    fake = _make_fake_memory({"purge_backlog": 10, "decay_floor_chunks": 20, "freshness": 0.5})
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", fake)
    monkeypatch.setattr(mcp_server.config, "health_purge_backlog_threshold", 10)
    monkeypatch.setattr(mcp_server.config, "health_floor_chunks_threshold", 20)
    client = TestClient(mcp_server._build_sse_app("test-token"))
    assert client.get("/health").json()["status"] == "ok"
    fake.values = {**fake.values, "purge_backlog": 11}
    assert client.get("/health").json()["status"] == "degraded"
    fake.values = {"purge_backlog": 5, "decay_floor_chunks": 21, "freshness": 0.5}
    assert client.get("/health").json()["status"] == "degraded"
    fake.values = {"purge_backlog": 9, "decay_floor_chunks": 19, "freshness": 0.5}
    assert client.get("/health").json()["status"] == "ok"


def test_health_endpoint_error_schema_is_stable(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", None)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_get_mem", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    response = TestClient(mcp_server._build_sse_app("test-token")).get("/health")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "hygiene": {
            "purge_backlog": None,
            "importance_below_floor": None,
            "freshness": None,
        },
        "recommendations": [],
    }


def test_health_endpoint_reuses_singleton(monkeypatch):
    from starlette.testclient import TestClient

    fake = _make_fake_memory({"purge_backlog": 0, "decay_floor_chunks": 0, "freshness": 1.0})
    calls = []
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", None)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_get_mem", lambda: calls.append(1) or fake)
    client = TestClient(mcp_server._build_sse_app("test-token"))
    assert client.get("/health").status_code == 200
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", fake)
    assert client.get("/health").status_code == 200
    assert len(calls) == 1
