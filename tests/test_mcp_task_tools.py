"""
[8/6 M3 Step 7] Integration test for memory_task_create MCP tool.

走 mcp_server._call_tool() 路径, 验证:
  - TOOLS schema 包含 memory_task_create
  - _call_tool('memory_task_create') 走 _handle_task_simple → task_states.task_create
  - 返回 {task_id, current_state, status, open_window_id}
  - DB 真实写入 entity + open 窗
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
# [8/15 E-3 fix P1 #89] mcp_tool_dispatcher._TOOL_VIS_FLAGS 要在真实的 mcp_server 引用的那个 module 上。
# _load 走 _ilu.spec_from_file_location 重新加载 — mcp_server 的 top-level import 是原来的。
# 两者是不同 module object. 直接设 sys.modules 上的那个:
mcp_disp = sys.modules["mcp_tool_dispatcher"]
mcp_disp._TOOL_VIS_FLAGS = {"audit_tools": True, "l2_tools": True, "all_tools": True}


ts_mod = _load("task_states")


def _setup():
    """Clean fixtures using a fresh Memory instance (don't disturb mcp singleton)."""
    from memory import Memory

    mem = Memory()
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm7-%' OR task_id LIKE 'loop:tlm7-%'")
        mem._conn.execute("DELETE FROM entities WHERE id LIKE 'task:tlm7-%' OR id LIKE 'loop:tlm7-%'")
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()
    mem.close()


def test_tool_schema_listed():
    """memory_task_create 出现在 TOOLS schema."""
    tool_names = [t["name"] for t in mcp.TOOLS]
    assert "memory_task_create" in tool_names, f"missing schema; got {tool_names}"
    schema = next(t for t in mcp.TOOLS if t["name"] == "memory_task_create")
    assert "name" in schema["inputSchema"]["properties"]
    assert "loop_id" in schema["inputSchema"]["properties"]
    assert schema["inputSchema"]["required"] == ["name"]


def test_call_tool_task_create_basic():
    """_call_tool('memory_task_create') 走 dispatcher, 写库 + 返回 JSON."""
    _setup()
    result_json = mcp._call_tool(
        "memory_task_create",
        {
            "name": "M3-integrate-test",
            "now": "2026-08-06T10:00",
        },
    )
    data = json.loads(result_json)
    assert data["task_id"].startswith("task:20260806-m3-integrate-test")
    assert data["current_state"] == "open"
    assert "open_window_id" in data


def test_call_tool_task_create_with_loop():
    """完整流程: loop_create (走 task_states) + task_create (走 MCP)."""
    _setup()
    mem = mcp._get_mem()
    loop_r = ts_mod.loop_create(
        mem._conn,
        name="耗材-m3-probe",
        trigger="库存低",
        now="2026-08-06T09:00",
    )
    lid = loop_r["loop_id"]

    task_r = mcp._call_tool(
        "memory_task_create",
        {
            "name": "维护任务-m3-probe",
            "loop_id": lid,
            "now": "2026-08-06T10:00",
        },
    )
    data = json.loads(task_r)
    assert data["loop_id"] == lid

    cfg = json.loads(
        mem._conn.execute(
            "SELECT properties_json FROM entities WHERE id=?",
            (lid,),
        ).fetchone()[0]
    )
    assert cfg["active_task_id"] == data["task_id"]


def test_call_tool_task_create_rejects_empty_name():
    """call_task 走 task_create → 空 name 拒收."""
    _setup()
    result_json = mcp._call_tool("memory_task_create", {"name": ""})
    data = json.loads(result_json)
    assert "error" in data or "task_id" not in data
