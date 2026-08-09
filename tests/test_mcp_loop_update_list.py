"""
[8/6 M3 Step 12] MCP integration tests for memory_loop_update + memory_loop_list.
"""
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import importlib.util as _ilu

def _load(name: str):
    spec = _ilu.spec_from_file_location(name, _REPO / f"{name}.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("config")
_load("embedder")
_load("search_index")
_load("validation")
_load("auth")
mcp = _load("mcp_server")


def _setup():
    from memory import Memory
    mem = Memory()
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'loop:tlm12m-%' "
            "OR task_id LIKE 'loop:20260806-t12m-%'"
        )
        mem._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'loop:tlm12m-%' "
            "OR id LIKE 'loop:20260806-t12m-%'"
        )
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()
    mem.close()


def test_loop_update_tool_schema():
    """memory_loop_update 出现在 TOOLS schema."""
    tool_names = [t["name"] for t in mcp.TOOLS]
    assert "memory_loop_update" in tool_names
    assert "memory_loop_list" in tool_names
    schema = next(t for t in mcp.TOOLS if t["name"] == "memory_loop_update")
    assert schema["inputSchema"]["required"] == ["loop_id"]


def test_loop_update_via_mcp_basic():
    """建 loop → MCP update 改 trigger."""
    _setup()
    r = mcp._call_tool("memory_loop_create", {
        "name": "tlm12m-trigger", "trigger": "old",
        "now": "2026-08-06T09:00",
    })
    lid = json.loads(r)["loop_id"]

    r2 = mcp._call_tool("memory_loop_update", {
        "loop_id": lid, "trigger": "new",
        "now": "2026-08-06T10:00",
    })
    data = json.loads(r2)
    assert data["loop_id"] == lid
    assert data["changed"] == {"trigger": "new"}


def test_loop_update_via_mcp_disabled_writes_dormant():
    """MCP enabled=False → current_state=dormant."""
    _setup()
    r = mcp._call_tool("memory_loop_create", {
        "name": "tlm12m-disable", "trigger": "x",
        "enabled": True, "now": "2026-08-06T09:00",
    })
    lid = json.loads(r)["loop_id"]

    mcp._call_tool("memory_loop_update", {
        "loop_id": lid, "enabled": False,
        "now": "2026-08-06T10:00",
    })

    # 校验 current_state = dormant
    r2 = mcp._call_tool("memory_loop_tick", {"loop_id": lid, "now": "2026-08-06T10:05"})
    # tick 现在 disabled → verdict=dormant
    data2 = json.loads(r2)
    assert data2["verdict"] == "dormant"


def test_loop_list_via_mcp():
    """memory_loop_list 走完整路径."""
    _setup()
    mcp._call_tool("memory_loop_create", {
        "name": "tlm12m-list1", "trigger": "x",
        "enabled": True, "now": "2026-08-06T09:00",
    })
    mcp._call_tool("memory_loop_create", {
        "name": "tlm12m-list2", "trigger": "y",
        "enabled": False, "now": "2026-08-06T09:01",
    })

    r = mcp._call_tool("memory_loop_list", {})
    data = json.loads(r)
    names = [loop["name"] for loop in data["loops"]]
    assert "tlm12m-list1" in names
    assert "tlm12m-list2" in names

    # enabled_only
    r2 = mcp._call_tool("memory_loop_list", {"enabled_only": True})
    data2 = json.loads(r2)
    names2 = [loop["name"] for loop in data2["loops"]]
    assert "tlm12m-list1" in names2
    assert "tlm12m-list2" not in names2
