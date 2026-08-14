"""[8/15 E-3] Memory.recall_stats() — 召回质量分析, 让主人看清现状.

主人 DESIGN §1.2 #6 短板修复: recall_log.recall_details_json 写满
method/rank/distance/rrf_score/importance, 但无人消费. 现在加
recall_stats() 方法聚合, MCP tool 暴露.

设计目标 (按主人 1.1 次/日召回量, 必须看清现状):
  - 各 method (vector / graph / meta / entity) 命中率 + 平均 rank + 平均 score
  - 召回空窗率 (results_json = '[]' 占比)
  - latency p50/p95/p99
  - 时间序列 (按日聚合, days 参数控制窗口)
  - 整体 totals (总召回次数, 唯一 query 数, 唯一命中 chunk 数)

[测试矩阵]
  1. 空 recall_log → 全 0, 不崩
  2. 1 条 recall, 1 个 vector 命中 → method 分布正确
  3. 多 method (vector + graph + meta + entity) → 各 method 计数对
  4. 空结果 (results_json='[]') → 计入 empty_results
  5. latency 聚合正确 (p50/p95/p99)
  6. group_by=method 模式输出每 method 详情
  7. days=N 窗口过滤
  8. recall_details_json NULL (老数据) → 不崩, 跳过 details 聚合
"""
import importlib.util as _ilu
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _load_from_repo(mod_name: str):
    target = str(_REPO / f'{mod_name}.py')
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, '__file__', None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_validation_repo = _load_from_repo('validation')
_memory_repo = _load_from_repo('memory')
_memory_repo.ValidationError = _validation_repo.ValidationError  # type: ignore[attr-defined]


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory with tmp_path db + usearch backend."""
    import config as _cfg_mod
    monkeypatch.setattr(_cfg_mod.config, 'search_backend', 'usearch', raising=True)
    db_path = tmp_path / 'test.db'
    monkeypatch.setattr(_cfg_mod.config, 'db_path', db_path, raising=False)

    schema_path = _REPO / 'schema.sql'
    import sqlite3 as _sqlite
    import re
    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    sql = re.sub(r'PRAGMA[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'INSTALL[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'LOAD[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(
        r'CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)',
        '', sql, flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        conn.executescript(sql)
    except Exception as e:
        if 'already exists' not in str(e):
            raise
    conn.commit()
    conn.close()

    m = _memory_repo.Memory(db_path=db_path)
    yield m
    try:
        m._conn.execute("DELETE FROM recall_log")
        m._conn.commit()
    finally:
        m.close()


def _seed_recall(
    mem,
    query: str,
    results: list,
    latency_ms: float = 10.0,
    created_at: str = None,
    graph_hops: int = 2,
    include_details: bool = True,
):
    """Helper: 直接 INSERT 一条 recall_log, 模拟真实 recall() 写的格式."""
    now_iso = created_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    details = None
    if include_details:
        details = json.dumps([
            {
                "rank": i + 1,
                "chunk_id": r.get("chunk_id"),
                "method": r.get("method", "vector"),
                "distance": r.get("distance", 0.5),
                "rrf_score": r.get("rrf_score", 0.01),
                "importance": r.get("importance", 0.5),
            }
            for i, r in enumerate(results[:5])
        ], ensure_ascii=False)
    mem._conn.execute(
        """
        INSERT INTO recall_log
            (query, results_json, graph_hops, latency_ms, created_at, recall_details_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            query,
            json.dumps([r.get("chunk_id") for r in results]),
            graph_hops,
            latency_ms,
            now_iso,
            details,
        ),
    )
    mem._conn.commit()


class TestRecallStats:
    """[8/15 E-3] Memory.recall_stats() 聚合查询 + MCP tool."""

    def test_empty_recall_log_returns_zeros(self, mem):
        """[E-3.1] 空 recall_log → 全 0, 不崩."""
        result = mem.recall_stats(days=30)
        assert result["totals"]["total_recalls"] == 0
        assert result["totals"]["unique_queries"] == 0
        assert result["latency_ms"]["p50"] == 0.0
        assert result["methods"] == {}  # 空 dict, 不崩

    def test_single_recall_single_method(self, mem):
        """[E-3.2] 1 条 recall, 1 个 vector 命中 → method 分布正确."""
        _seed_recall(mem, "test query 1", [
            {"chunk_id": "chunk_1", "method": "vector", "distance": 0.3, "rrf_score": 0.02},
        ], latency_ms=15.0)
        result = mem.recall_stats(days=30)
        assert result["totals"]["total_recalls"] == 1
        assert result["totals"]["total_hits"] == 1
        assert "vector" in result["methods"]
        assert result["methods"]["vector"]["hit_count"] == 1
        assert result["methods"]["vector"]["avg_rank"] == 1.0
        assert result["latency_ms"]["p50"] == 15.0

    def test_multi_method_distribution(self, mem):
        """[E-3.3] 多 method 混合 → 各 method 计数对."""
        results_mixed = [
            {"chunk_id": "c1", "method": "vector", "distance": 0.2},
            {"chunk_id": "c2", "method": "graph", "rrf_score": 0.015},
            {"chunk_id": "c3", "method": "meta", "rrf_score": 0.012},
            {"chunk_id": "c4", "method": "entity", "rrf_score": 0.01},
        ]
        _seed_recall(mem, "mixed query", results_mixed, latency_ms=20.0)
        result = mem.recall_stats(days=30)
        for m in ("vector", "graph", "meta", "entity"):
            assert m in result["methods"], f"{m} missing"
            assert result["methods"][m]["hit_count"] == 1

    def test_empty_results_counted(self, mem):
        """[E-3.4] 空结果 (results_json='[]') → 计入 empty_results."""
        _seed_recall(mem, "q1", [], latency_ms=5.0)
        _seed_recall(mem, "q2", [{"chunk_id": "c1", "method": "vector"}], latency_ms=10.0)
        result = mem.recall_stats(days=30)
        assert result["totals"]["total_recalls"] == 2
        assert result["totals"]["empty_results"] == 1
        assert result["totals"]["empty_rate"] == 0.5

    def test_latency_aggregation(self, mem):
        """[E-3.5] latency 聚合正确 (p50/p95/p99).

        numpy default percentile method='linear' (interpolation between
        closest ranks): for n=7 values, p50 index = 0.5 * 6 = 3.0 (exact)
        → 20.0. p95 index = 0.95 * 6 = 5.7 → 30 + 0.7*(100-30) = 79.0.
        p99 index = 0.99 * 6 = 5.94 → 30 + 0.94*70 ≈ 95.8.
        """
        for i, lat in enumerate([5, 10, 15, 20, 25, 30, 100]):
            _seed_recall(
                mem, f"q{i}", [{"chunk_id": f"c{i}", "method": "vector"}],
                latency_ms=lat,
            )
        result = mem.recall_stats(days=30)
        lat = result["latency_ms"]
        # 7 个值 sorted: [5, 10, 15, 20, 25, 30, 100]
        # p50 = idx 3.0 (exact rank) = 20.0
        assert lat["p50"] == 20.0, f"expected 20.0, got {lat['p50']}"
        # p95 = idx 5.7 (interp) ≈ 79.0
        assert abs(lat["p95"] - 79.0) < 0.5, f"expected ~79, got {lat['p95']}"
        # p99 ≈ 95.8
        assert lat["p99"] > 90.0
        # 数量正确
        assert lat["n"] == 7
        # min/max
        assert lat["min"] == 5.0
        assert lat["max"] == 100.0

    def test_days_window_filter(self, mem):
        """[E-3.6] days=N 窗口过滤 — 老 recall 不计入."""
        now = datetime.now()
        old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
        recent = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        _seed_recall(
            mem, "old_q", [{"chunk_id": "c_old", "method": "vector"}],
            created_at=old,
        )
        _seed_recall(
            mem, "new_q", [{"chunk_id": "c_new", "method": "vector"}],
            created_at=recent,
        )
        result_7d = mem.recall_stats(days=7)
        result_60d = mem.recall_stats(days=60)
        assert result_7d["totals"]["total_recalls"] == 1
        assert result_60d["totals"]["total_recalls"] == 2

    def test_null_recall_details_does_not_crash(self, mem):
        """[E-3.7] recall_details_json NULL (老数据 / pre-7.18 schema) → 不崩."""
        mem._conn.execute(
            """
            INSERT INTO recall_log
                (query, results_json, graph_hops, latency_ms, created_at, recall_details_json)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            ("legacy q", json.dumps(["c1"]), 2, 8.0, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
        )
        mem._conn.commit()
        # 不应抛异常
        result = mem.recall_stats(days=30)
        assert result["totals"]["total_recalls"] == 1
        # methods 字典可能空或只有 hits=0, 但不能崩
        assert "methods" in result
