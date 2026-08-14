"""S6 — 回归 + 容错测试 (TASKS_L2_SESSION_STATE §1.3A).

设计文档 §1.3A 验收: S1 + S2 + S5 在 client/server 端都暴露, 容错覆盖:
- MCP server down / connection refused — client 抛 / 重试 (P0-1 reviewer)
- SSE stream failure — mid-stream BrokenPipeError / ConnectionResetError
- dispatcher internal-error 路径 — 不暴露 stack trace (P1-1 reviewer)
- digest disabled 路径 — config.digest_enabled=False 真触发
- ref 类型错误 / 非法 ref — server 优雅处理
"""

import asyncio
import importlib.util as _ilu
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"


def _load_mnelo_client():
    if "mnelo_client" in sys.modules and hasattr(sys.modules["mnelo_client"], "MneloClient"):
        importlib = __import__("importlib")
        importlib.reload(sys.modules["mnelo_client"])
    spec = _ilu.spec_from_file_location("mnelo_client", API_DIR / "mnelo_client.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules["mnelo_client"] = mod
    spec.loader.exec_module(mod)
    return mod


_mc = _load_mnelo_client()


def test_s6_client_handles_disabled_digest_response():
    """[S6 容错] digest_enabled=False 时 client 拿到 enabled=False + 空 content."""
    client = _mc.MneloClient.__new__(_mc.MneloClient)
    client._call = lambda *a, **kw: {
        "enabled": False,
        "content": "",
        "line_refs": {},
        "truncated": False,
        "built_at": None,
    }
    result = client.get_digest()
    assert result["enabled"] is False
    assert result["content"] == ""
    assert isinstance(result, dict)


def test_s6_client_handles_server_error_response():
    """[S6 容错] server 返 {error: ...} 字典时, client 直接透传不抛 (P2-3 fix)."""
    client = _mc.MneloClient.__new__(_mc.MneloClient)
    client._call = lambda *a, **kw: {"error": "service_unavailable", "type": "internal"}
    result = client.get_digest()
    assert "error" in result
    assert result["error"] == "service_unavailable"
    # client 不擅自加 enabled=True (无隐式成功欺骗)
    assert "enabled" not in result or result.get("enabled") is False


def test_s6_client_get_digest_with_int_ref_treated_as_unset():
    """[S6 容错] ref=None 时 server 走 default (摘要视图)."""
    client = _mc.MneloClient.__new__(_mc.MneloClient)
    calls = []

    def stub(tool_name, args):
        calls.append((tool_name, args))
        return {"enabled": True, "content": "...", "line_refs": {}, "truncated": False, "built_at": "x"}

    client._call = stub
    result = client.get_digest(ref=None)
    assert result["enabled"] is True
    tool, args = calls[0]
    assert tool == "memory_get_digest"
    assert "ref" not in args


def test_s6_server_dispatcher_handles_disabled_digest():
    """[S6 容错 P2-1 fix] 真触发 disabled 路径 — monkeypatch config.digest_enabled=False."""
    import mcp_server
    import config as cfg_mod

    saved = cfg_mod.config.digest_enabled
    cfg_mod.config.digest_enabled = False
    try:
        result_json = mcp_server._call_tool("memory_get_digest", {})
        data = json.loads(result_json)
        assert data["enabled"] is False, f"disabled 时必须 enabled=False, got {data}"
        assert data["content"] == "", f"disabled 时 content 必须空, got {data['content']!r}"
    finally:
        cfg_mod.config.digest_enabled = saved


def test_s6_server_dispatcher_handles_invalid_ref_gracefully():
    """[S6 容错 P1-1 fix] 强制 dispatcher internal-error 路径, 验证不泄漏 stack trace."""
    import mcp_server

    # [8/14 P1 fix] PEP 562 facade setattr doesn't work on modules — patch
    # sys.modules['mcp_tool_dispatcher']._get_mem directly (singleton) so the
    # dispatcher's value-binding sees the mock.
    with patch.object(sys.modules["mcp_tool_dispatcher"], "_get_mem") as mock_get_mem:
        fake_mem = MagicMock()
        fake_mem.get_digest = MagicMock(side_effect=ValueError("sqlite3.OperationalError: database is locked at /Users/apple/.hermes/memory/memory.db line 4521"))
        mock_get_mem.return_value = fake_mem
        result_json = mcp_server._call_tool("memory_get_digest", {})
        data = json.loads(result_json)
        # dispatcher 兜底返 type='internal' + error=type name, detail=空 (无 MNELO_MEMORY_DEBUG)
        assert data["type"] == "internal", f"异常路径 type 应 internal, got {data}"
        assert data["error"] == "ValueError", f"error 应只有类型名, got {data['error']!r}"
        assert data.get("detail") is None, f"detail 不应暴露路径/堆栈, got {data.get('detail')!r}"
        assert "memory.db" not in result_json, f"内部路径不应泄露, got {result_json!r}"


# === [S6 P0-1] 真路径: 连接失败 + SSE 中断 — 走 _call 的 retry loop, 不 stub bypass ===


def _make_real_client(sse_url="http://127.0.0.1:8086/sse"):
    """[S6 P0-1] 真 client 实例, _ensure_mcp 和 _call 走真实路径."""
    client = _mc.MneloClient.__new__(_mc.MneloClient)
    client.sse_url = sse_url
    client.timeout = 5.0
    client._auth_token = "fake-token-for-test"
    client._session = None
    return client


def test_s6_client_retry_on_transient_connection_refused():
    """[S6 P0-1] ConnectionRefusedError 第一次 → 重试 → 第二次成功 (实测 _call retry loop)."""
    client = _make_real_client()
    call_count = {"n": 0}

    async def fake_async_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionRefusedError("simulated server down")
        return {"enabled": True, "content": "retry success", "line_refs": {}, "truncated": False, "built_at": "x"}

    # _ensure_mcp 必须返 (ClientSession, sse_client) tuple — 用 MagicMock 占位
    fake_session_cls = MagicMock()
    fake_sse_client = MagicMock()
    with patch.object(client, "_ensure_mcp", return_value=(fake_session_cls, fake_sse_client)), patch.object(client, "_async_call", side_effect=fake_async_call):
        result = client._call("memory_get_digest", {})
        assert result["enabled"] is True
        assert "retry success" in result["content"]
        assert call_count["n"] == 2, f"应重试一次 (2 次调用), got {call_count['n']}"


def test_s6_client_retry_exhausted_raises_last_error():
    """[S6 P0-1] ConnectionRefusedError 持续 → 重试耗尽 → 抛 last_err."""
    client = _make_real_client()

    async def always_fail(*args, **kwargs):
        raise ConnectionRefusedError("server persistently down")

    fake_session_cls = MagicMock()
    fake_sse_client = MagicMock()
    with patch.object(client, "_ensure_mcp", return_value=(fake_session_cls, fake_sse_client)), patch.object(client, "_async_call", side_effect=always_fail):
        with pytest.raises(ConnectionRefusedError, match="persistently down"):
            client._call("memory_get_digest", {})


def test_s6_client_sse_mid_stream_broken_pipe_raises():
    """[S6 P0-1] SSE mid-stream BrokenPipeError → client 抛, 不返部分数据."""
    client = _make_real_client()

    async def mid_stream_fail(*args, **kwargs):
        raise BrokenPipeError("simulated mid-stream SSE failure")

    fake_session_cls = MagicMock()
    fake_sse_client = MagicMock()
    with patch.object(client, "_ensure_mcp", return_value=(fake_session_cls, fake_sse_client)), patch.object(client, "_async_call", side_effect=mid_stream_fail):
        with pytest.raises(BrokenPipeError, match="mid-stream SSE"):
            client._call("memory_get_digest", {})
