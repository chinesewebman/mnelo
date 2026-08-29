"""[audit fix 6.1 + 6.2 2026-08-16] _get_mem db_path injection + reset helper.

Owner fix priority #5 (test isolation).
Original _get_mem hardcoded DB_PATH — tests couldn't isolate.
Fix: accept optional db_path, add _reset_mem_for_test() helper.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def reset_dispatcher_mem():
    """Reset _mem_instance before AND after each test.

    Note: use module attribute access (live) not `from ... import` (stale).
    """
    import mcp_tool_dispatcher

    mcp_tool_dispatcher._reset_mem_for_test()
    yield
    mcp_tool_dispatcher._reset_mem_for_test()


def test_get_mem_accepts_db_path_injection(tmp_path):
    """#6.1: _get_mem(db_path=X) creates Memory with X."""
    import mcp_tool_dispatcher

    db_path = tmp_path / "injected.db"
    mem = mcp_tool_dispatcher._get_mem(db_path=db_path)
    assert mem is not None
    cid = mem.remember("test injected db path")
    mem._conn.commit()

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT id FROM chunks WHERE id = ?", (cid,)).fetchone()
    conn.close()
    assert row is not None, "chunk should be in injected DB"


def test_reset_mem_for_test_releases_singleton(tmp_path):
    """#6.2: _reset_mem_for_test() releases _mem_instance so next _get_mem rebuilds.

    Note: use `mcp_tool_dispatcher._mem_instance` (live attribute access) — NOT
    `from ... import _mem_instance` (binds at import time = stale).
    """
    import mcp_tool_dispatcher

    db_path = tmp_path / "reset_test.db"
    m1 = mcp_tool_dispatcher._get_mem(db_path=db_path)
    assert mcp_tool_dispatcher._mem_instance is m1
    mcp_tool_dispatcher._reset_mem_for_test()
    assert mcp_tool_dispatcher._mem_instance is None
    m2 = mcp_tool_dispatcher._get_mem(db_path=tmp_path / "reset_test2.db")
    assert m2 is not m1, "after reset, _get_mem should rebuild instance"


def test_reset_mem_handles_unopened_instance():
    """#6.2: _reset_mem_for_test() is safe even if Memory never opened."""
    import mcp_tool_dispatcher

    assert mcp_tool_dispatcher._mem_instance is None
    mcp_tool_dispatcher._reset_mem_for_test()  # should not raise
    assert mcp_tool_dispatcher._mem_instance is None


def test_get_mem_default_uses_module_db_path():
    """#6.1: _get_mem() with no args uses memory.DB_PATH (backward compat).

    Note: 默认 db_path 走 module DB_PATH → 跟 live MCP server 争 zvec lock,
    单 writer 约束导致 RuntimeError. 此测试仅在 MCP 未跑时通过, 默认 skip.
    """
    import mcp_tool_dispatcher
    from memory import DB_PATH

    mcp_tool_dispatcher._reset_mem_for_test()
    try:
        mem = mcp_tool_dispatcher._get_mem()
    except RuntimeError as e:
        if "Can't lock" in str(e):
            pytest.skip(f"Live MCP server holds zvec lock — skip default-path test: {e}")
        raise
    try:
        assert mem is not None
        assert str(mem.db_path) == str(DB_PATH), "default _get_mem should use module DB_PATH"
        assert mcp_tool_dispatcher._mem_instance is mem
    finally:
        mcp_tool_dispatcher._reset_mem_for_test()
