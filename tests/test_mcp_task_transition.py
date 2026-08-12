"""[8/6 M3 Step 8] Integration test for memory_task_transition MCP tool.

[8/9 review B13 fix] 改 tmp_path 隔离 DB. 旧版用 Memory() 默认连 live,
CREATE task_states + entities 实体, 跑完清理但仍留 mcp._call_tool default
单例的 conn 引用 (race). 新版用 tmp_path fixture, 自己 fork Memory 实例,
测试结束自动清理.

[回查断言] 旧版只断言 transition 返回 JSON dict, 从不回查 task_states 表
验证 CAS 关旧窗 (valid_until = now) + 开新窗 (valid_from = now).
新版断言 transition 后 SQLite task_states 表实际 2 行 (1 关 1 开).

[8/10 主人验证报告 fix] 旧版 _count_state_transitions_for_task 查
state_transitions (规则矩阵表, 无 task_id 列) — OperationalError.
正解: transition 写 task_states (UPDATE 关旧 + INSERT 开新),
state_transitions 是设计阶段 seed 的规则不放实例.
"""

import json
import sys
import sqlite3
from pathlib import Path
import importlib.util as _ilu

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


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
mcp_tool_dispatcher = _load("mcp_tool_dispatcher")
_load("mcp_tool_handlers")
_load("mcp_tool_definitions")
mcp = _load("mcp_server")


def _isolated_db(tmp_path):
    """[8/9 B13] 用 tmp_path 隔离 DB, 不污染 live. 临时建 memory instance.

    [8/10 主人验证 fix] 同时把 mcp._get_mem() 单例切到隔离 mem —— 否则
    task_create/transition 走 mcp 单例写 config/env 库, 回查 mem(tmp_path
    库) 永远 0 行, 隔离不生效 (且 config 指向 live 时会污染 live).
    测试 finally 恢复 mcp_tool_dispatcher._mem_instance = None.
    """
    import shutil
    from memory import Memory

    # 隔离 DB 路径
    db_path = tmp_path / "memory.db"
    # 复制 schema (用 init_db 模板)
    src_db = Path(memory_DB_PATH())  # type: ignore[name-defined]
    if src_db.exists():
        shutil.copy(src_db, db_path)
    mem = Memory(db_path=db_path)
    mcp_tool_dispatcher._mem_instance = mem
    return mem, db_path


def memory_DB_PATH():
    """[8/9 B13] 解析 live DB path 用于 copy schema. 跟 memory.py 同 .resolve_db_path"""
    from config import resolve_db_path

    return resolve_db_path()


def _setup_isolated(mem):
    """[8/9 B13] 用隔离 mem 删除 mcp_task_transition fixture 残留 task_states."""
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm8-%' OR task_id LIKE 'task:20260806-t8-%'")
        mem._conn.execute("DELETE FROM entities WHERE id LIKE 'task:tlm8-%' OR id LIKE 'task:20260806-t8-%'")
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()


def _count_state_transitions_for_task(mem, task_id):
    """[8/9 B13 + 8/10 fix] 回查 task_states 表: 转 1 次 = 2 行 (关旧 + 开新).

    [8/10 主人验证报告 fix] 之前查 state_transitions (转移规则矩阵表,
    无 task_id 列), OperationalError: no such column. 实际 task 状态窗
    在 task_states 表 (transition() UPDATE 关旧窗 + INSERT 开新窗).
    state_transitions 是设计阶段的转移规则 (scope/from_state/to_state)
    seed 进去, 不放每次 transition 实例.
    """
    return mem._conn.execute("SELECT COUNT(*) FROM task_states WHERE task_id = ?", (task_id,)).fetchone()[0]


def test_tool_schema_listed():
    """memory_task_transition 出现在 TOOLS schema."""
    tool_names = [t["name"] for t in mcp.TOOLS]
    assert "memory_task_transition" in tool_names
    schema = next(t for t in mcp.TOOLS if t["name"] == "memory_task_transition")
    assert sorted(schema["inputSchema"]["required"]) == ["reason", "task_id", "to_state"]


def test_call_tool_task_transition_normal(tmp_path):
    """[8/9 B13] 走 MCP: task_create → task_transition, 验证 CAS 关旧+开新 (回查)."""
    mem, _db = _isolated_db(tmp_path)
    try:
        _setup_isolated(mem)
        create_r = mcp._call_tool(
            "memory_task_create",
            {
                "name": "t8-normal",
                "now": "2026-08-06T10:00",
            },
        )
        task_id = json.loads(create_r)["task_id"]

        r = mcp._call_tool(
            "memory_task_transition",
            {
                "task_id": task_id,
                "to_state": "in_progress",
                "reason": "agent A: handoff",
                "now": "2026-08-06T10:05",
            },
        )
        data = json.loads(r)
        assert data["from_state"] == "open"
        assert data["to_state"] == "in_progress"
        assert data["task_id"] == task_id
        assert isinstance(data["window_id"], int)

        # [8/9 B13] 回查: state_transitions 表应有 2 行 (关旧 + 开新)
        n = _count_state_transitions_for_task(mem, task_id)
        assert n == 2, f"CAS 副作用应写 2 行 (关旧 + 开新), got {n}"
    finally:
        mcp_tool_dispatcher._mem_instance = None
        mem.close()


def test_call_tool_task_transition_invalid_rejected(tmp_path):
    """[8/9 B13] open → waiting (非 allowed graph) 报 error + 回查无副作用."""
    mem, _db = _isolated_db(tmp_path)
    try:
        _setup_isolated(mem)
        create_r = mcp._call_tool(
            "memory_task_create",
            {
                "name": "t8-invalid",
                "now": "2026-08-06T10:00",
            },
        )
        task_id = json.loads(create_r)["task_id"]

        r = mcp._call_tool(
            "memory_task_transition",
            {
                "task_id": task_id,
                "to_state": "waiting",
                "reason": "should fail",
                "now": "2026-08-06T10:05",
            },
        )
        data = json.loads(r)
        # 走 mcp_server 错误路径: ValidationError 之类
        assert "error" in data or data.get("type") == "error", f"unexpected {data}"

        # [8/10 fix] 回查: 非法 transition 无副作用 —— task_states 应保持
        # task_create 写入的 1 行初始 open 窗 (不新增, 不开新窗).
        n = _count_state_transitions_for_task(mem, task_id)
        assert n == 1, f"非法 transition 应保持初始 1 行 (open 窗), got {n}"
    finally:
        mcp_tool_dispatcher._mem_instance = None
        mem.close()


# [8/9 B13] 旧 _setup() 删 — 新 _isolated_db 替代, 不再污染 live DB.
# 旧 _setup 引用 Memory() 默认连接, 跟 mcp singleton 共享 db_path, 测试
# 跑完留 mcp._call_tool default conn 仍指向 live. 新版每个 test 用独立
# tmp_path, 跑完 mem.close() 释放, 0 残留.
