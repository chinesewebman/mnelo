"""S1 — 注册 memory_get_digest MCP 工具 (TASKS_L2_SESSION_STATE §1.3A).

设计文档 §1.0: 任何 MCP 客户端 → memory_get_digest 无 ref → 摘要;
                 ref=<行号> → 展开源 chunk; 非法 ref → 明确错误.

测试覆盖两层:
- tool schema 注册 (走 mcp_server.TOOLS 静态表, 不依赖运行时 reload)
- _call_tool 内部 dispatcher (dispatch 路径, 测 ref/None/非法 ref 三种调用)

[8/5 P2] test_mcp_coverage_round4 会在某些条件下 reload mcp_server, 因此测试
只用静态 schema + dispatcher, 不碰 server instance state.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server  # noqa: E402


def _setup_minimal_digest(mem):
    # [8/9 P1 follow-up] 加 identity: namespace 防 _enforce_entity_namespace_guard
    # 拒 non-namespaced id + kind='identity_fact' (不在 _NAMELESS_KINDS 白名单).
    cid = mem.remember(
        content="identity: S1 test subject",
        source="s1_test",
        importance=1.0,
        entities=[
            {
                "id": "identity:s1_test_id",
                "kind": "identity_fact",
                "name": "S1 Tester",
                "summary": "S1 Tester",
            }
        ],
    )
    return cid


def _cleanup_s1(mem):
    # [8/6 plan §10] 顺序 bug 修复: 原先 DELETE chunks 再 DELETE vectors,
    # 子查询空, vectors 残留. helper 先 _index.remove 再 DELETE chunks.
    from helpers import cleanup_chunks

    cleanup_chunks(mem, source="s1_test")
    mem._conn.execute("DELETE FROM entities WHERE id='identity:s1_test_id'")
    mem._conn.execute("DELETE FROM meta WHERE key IN ('digest_chunk_id', 'digest_dirty')")
    mem._conn.commit()


def test_s1_tool_registered_in_schema():
    """[S1 验收] TOOLS 表必须含 memory_get_digest schema (含 ref 参数)."""
    entries = [t for t in mcp_server.TOOLS if t.get("name") == "memory_get_digest"]
    assert entries, "TOOLS 表缺 memory_get_digest"
    tool = entries[0]
    props = tool["inputSchema"]["properties"]
    assert "ref" in props, f"inputSchema 必须含 ref 参数, got {list(props)}"


def test_s1_dispatch_in_registry():
    """[S1 验收] _TOOL_REGISTRY 必须含 memory_get_digest → (get_digest, None)."""
    entry = mcp_server._TOOL_REGISTRY.get("memory_get_digest")
    assert entry is not None, "_TOOL_REGISTRY 缺 memory_get_digest"
    assert entry[0] == "get_digest", f"应委托给 get_digest 方法, got {entry}"


def test_s1_no_ref_returns_digest_compressed_view():
    """[S1 验收] 无 ref → 摘要压缩视图 (走 _call_tool 内部 dispatcher)."""
    from memory import Memory

    mem = Memory()
    try:
        _setup_minimal_digest(mem)
        mem._conn.execute("UPDATE meta SET value='1' WHERE key='digest_dirty'")
        mem._conn.commit()

        # 直接调 _call_tool — 走 dispatcher, 不依赖 server instance
        result_json = mcp_server._call_tool("memory_get_digest", {})
        data = json.loads(result_json)
        assert data.get("enabled") is True, f"应 enabled=True, got {data}"
        assert "content" in data
        assert "chunk_id" in data
        assert "line_refs" in data
        assert "truncated" in data
        assert "built_at" in data
        assert "S1 Tester" in data["content"], f"摘要应含 identity_fact, got {data['content']}"
    finally:
        _cleanup_s1(mem)
        mem.close()


def test_s1_ref_returns_expanded_source_chunks():
    """[S1 验收] ref=<行号> → 展开源 chunk (source_chunks)."""
    from memory import Memory

    mem = Memory()
    try:
        _setup_minimal_digest(mem)
        mem._conn.execute("UPDATE meta SET value='1' WHERE key='digest_dirty'")
        mem._conn.commit()
        digest = mem.get_digest()
        line_refs = digest["line_refs"]
        assert line_refs, "digest 必须有 line_refs"
        first_line = next(iter(line_refs.keys()))

        result_json = mcp_server._call_tool("memory_get_digest", {"ref": first_line})
        data = json.loads(result_json)
        assert "source_chunks" in data, f"ref 应返 source_chunks, got {list(data.keys())}"
    finally:
        _cleanup_s1(mem)
        mem.close()


def test_s1_invalid_ref_returns_explicit_error():
    """[S1 验收] 非法 ref → 明确错误 (走 dispatcher, 不报 internal exception)."""
    result_json = mcp_server._call_tool("memory_get_digest", {"ref": "99999_does_not_exist"})
    data = json.loads(result_json)
    assert "error" in data, f"非法 ref 应返 error 字段, got {data}"
