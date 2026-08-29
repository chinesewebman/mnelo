"""
[8/6 M3 Step 9-11] Integration tests for new task/loop MCP tools.

Step 9: memory_task_list / memory_task_replay
Step 10: memory_loop_create
Step 11: memory_loop_tick
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


def _setup():
    """Clean fixtures using a fresh Memory instance."""
    from memory import Memory

    mem = Memory()
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'task:tlm9-%' "
            "OR task_id LIKE 'task:tlm10-%' "
            "OR task_id LIKE 'task:tlm11-%' "
            "OR task_id LIKE 'task:20260806-t9-%' "
            "OR task_id LIKE 'task:20260806-t10-%' "
            "OR task_id LIKE 'task:20260806-t11-%' "
            "OR task_id LIKE 'loop:tlm9-%' "
            "OR task_id LIKE 'loop:tlm10-%' "
            "OR task_id LIKE 'loop:tlm11-%' "
            "OR task_id LIKE 'loop:20260806-t9-%' "
            "OR task_id LIKE 'loop:20260806-t10-%' "
            "OR task_id LIKE 'loop:20260806-t11-%'"
        )
        mem._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'task:tlm9-%' "
            "OR id LIKE 'task:tlm10-%' "
            "OR id LIKE 'task:tlm11-%' "
            "OR id LIKE 'task:20260806-t9-%' "
            "OR id LIKE 'task:20260806-t10-%' "
            "OR id LIKE 'task:20260806-t11-%' "
            "OR id LIKE 'loop:tlm9-%' "
            "OR id LIKE 'loop:tlm10-%' "
            "OR id LIKE 'loop:tlm11-%' "
            "OR id LIKE 'loop:20260806-t9-%' "
            "OR id LIKE 'loop:20260806-t10-%' "
            "OR id LIKE 'loop:20260806-t11-%'"
        )
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()
    mem.close()


def test_task_list_schema():
    """memory_task_list 出现在 TOOLS schema."""
    tool_names = [t["name"] for t in mcp.TOOLS]
    assert "memory_task_list" in tool_names
    assert "memory_task_replay" in tool_names


def test_task_list_active_only():
    """建 2 task, 默认 list 仅 active."""
    _setup()
    r1 = mcp._call_tool("memory_task_create", {"name": "first", "now": "2026-08-06T10:00"})
    tid1 = json.loads(r1)["task_id"]
    r2 = mcp._call_tool("memory_task_create", {"name": "second", "now": "2026-08-06T10:01"})
    tid2 = json.loads(r2)["task_id"]

    # 把 tid2 推到 done, 不应出现在默认 list
    mcp._call_tool(
        "memory_task_transition",
        {
            "task_id": tid2,
            "to_state": "in_progress",
            "reason": "A",
            "now": "2026-08-06T10:02",
        },
    )
    mcp._call_tool(
        "memory_task_transition",
        {
            "task_id": tid2,
            "to_state": "done",
            "reason": "B",
            "now": "2026-08-06T10:03",
        },
    )

    r = mcp._call_tool("memory_task_list", {})
    data = json.loads(r)
    task_ids = [t["task_id"] for t in data["tasks"]]
    assert tid1 in task_ids, f"tid1 (active) missing: {task_ids}"
    assert tid2 not in task_ids, f"tid2 (done) should be excluded: {task_ids}"


def test_task_replay_full_history():
    """replay 返回完整窗历史."""
    _setup()
    r = mcp._call_tool("memory_task_create", {"name": "replay", "now": "2026-08-06T10:00"})
    tid = json.loads(r)["task_id"]
    mcp._call_tool(
        "memory_task_transition",
        {
            "task_id": tid,
            "to_state": "in_progress",
            "reason": "start",
            "now": "2026-08-06T10:05",
        },
    )
    mcp._call_tool(
        "memory_task_transition",
        {
            "task_id": tid,
            "to_state": "waiting",
            "reason": "wait",
            "now": "2026-08-06T10:30",
        },
    )

    r = mcp._call_tool("memory_task_replay", {"task_id": tid})
    data = json.loads(r)
    assert data["current_state"] == "waiting"
    assert data["window_count"] == 3
    states = [w["state"] for w in data["windows"]]
    assert states == ["open", "in_progress", "waiting"]


def test_loop_create_schema():
    """memory_loop_create 出现在 TOOLS schema."""
    tool_names = [t["name"] for t in mcp.TOOLS]
    assert "memory_loop_create" in tool_names
    schema = next(t for t in mcp.TOOLS if t["name"] == "memory_loop_create")
    assert sorted(schema["inputSchema"]["required"]) == ["name", "trigger"]


def test_loop_create_and_tick():
    """建 loop → tick 走完整路径."""
    _setup()
    r = mcp._call_tool(
        "memory_loop_create",
        {
            "name": "消耗品",
            "trigger": "库存低",
            "interval_hours": 24,
            "now": "2026-08-06T09:00",
        },
    )
    data = json.loads(r)
    lid = data["loop_id"]
    assert "loop_id" in data
    assert data["enabled"] is True
    assert data["interval_hours"] == 24

    # tick
    r2 = mcp._call_tool("memory_loop_tick", {"loop_id": lid, "now": "2026-08-06T10:00"})
    data2 = json.loads(r2)
    assert data2["verdict"] == "due", f"first run should be due, got {data2['verdict']}"


def test_loop_tick_disabled_dormant():
    """loop enabled=False → tick verdict=dormant."""
    _setup()
    r = mcp._call_tool(
        "memory_loop_create",
        {
            "name": "暂挂",
            "trigger": "x",
            "enabled": False,
            "now": "2026-08-06T09:00",
        },
    )
    lid = json.loads(r)["loop_id"]
    r2 = mcp._call_tool("memory_loop_tick", {"loop_id": lid})
    data = json.loads(r2)
    assert data["verdict"] == "dormant"
