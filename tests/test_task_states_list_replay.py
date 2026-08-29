"""
[8/6 M2 Step 3] Tests for list_tasks() + replay_task().

DESIGN §5.1 memory_task_list / memory_task_replay.
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
mem_mod = _load("memory")
ts_mod = _load("task_states")


def _setup(m):
    m._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        m._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'task:tlm2-%' "
            "OR task_id LIKE 'task:tlm3-%' "
            "OR task_id LIKE 'task:tlm7-%' "
            "OR task_id LIKE 'task:tlm8-%' "
            "OR task_id LIKE 'task:20260806-m3-%' "
            "OR task_id LIKE 'task:20260806-t8-%' "
            "OR task_id LIKE 'loop:tlm7-%' "
            "OR task_id LIKE 'loop:tlm8-%' OR task_id LIKE 'task:rf%' OR task_id LIKE 'loop:rf%' OR task_id LIKE 'task:20260806-rf%' OR task_id LIKE 'loop:20260806-rf%' OR task_id LIKE 'task:20260806-t1%' OR task_id LIKE 'loop:20260806-t1%' OR task_id LIKE 'task:tlm9-%' OR task_id LIKE 'task:tlm10-%' OR task_id LIKE 'task:tlm11-%' OR task_id LIKE 'task:20260806-t9-%' OR task_id LIKE 'task:20260806-t10-%' OR task_id LIKE 'task:20260806-t11-%' OR task_id LIKE 'loop:tlm12m-%' OR task_id LIKE 'loop:20260806-t12m-%' OR task_id LIKE 'loop:tlm12-%' OR task_id LIKE 'loop:cli-%' OR task_id LIKE 'task:20260806-cli-%' OR task_id LIKE 'task:m4-%' OR task_id LIKE 'loop:m4-%' OR task_id LIKE 'task:tlm4-%' OR task_id LIKE 'loop:tlm4-%' OR task_id LIKE 'task:20260806-m4-%' OR task_id LIKE 'task:rf15-%' OR task_id LIKE 'loop:rf15-%' OR task_id LIKE 'task:20260806-rf15-%' OR task_id LIKE 'task:step14-%' OR task_id LIKE 'loop:step14-%' OR task_id LIKE 'task:20260806-step14-%' OR task_id LIKE 'loop:20260806-step14-%' OR task_id LIKE 'loop:20260806-rf15-%' OR task_id = 'task:nonexistent-rf15' OR task_id LIKE 'loop:tlm9-%' OR task_id LIKE 'loop:tlm10-%' OR task_id LIKE 'loop:tlm11-%' OR task_id LIKE 'loop:20260806-t9-%' OR task_id LIKE 'loop:20260806-t10-%' OR task_id LIKE 'loop:20260806-t11-%' OR task_id LIKE 'task:20260806-first%' OR task_id LIKE 'task:20260806-replay%' OR task_id LIKE 'task:20260806-second%' OR task_id LIKE 'task:20260806-active%'"
        )
        m._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'task:tlm2-%' "
            "OR id LIKE 'task:tlm3-%' "
            "OR id LIKE 'task:tlm7-%' "
            "OR id LIKE 'task:tlm8-%' "
            "OR id LIKE 'task:20260806-m3-%' "
            "OR id LIKE 'task:20260806-t8-%' "
            "OR id LIKE 'loop:tlm3-%' "
            "OR id LIKE 'loop:tlm7-%' "
            "OR id LIKE 'loop:tlm8-%' OR id LIKE 'task:rf%' OR id LIKE 'loop:rf%' OR id LIKE 'task:20260806-rf%' OR id LIKE 'loop:20260806-rf%' OR id LIKE 'task:20260806-t1%' OR id LIKE 'loop:20260806-t1%' OR id LIKE 'task:tlm9-%' OR id LIKE 'task:tlm10-%' OR id LIKE 'task:tlm11-%' OR id LIKE 'task:20260806-t9-%' OR id LIKE 'task:20260806-t10-%' OR id LIKE 'task:20260806-t11-%' OR id LIKE 'loop:tlm12m-%' OR id LIKE 'loop:20260806-t12m-%' OR id LIKE 'loop:tlm12-%' OR id LIKE 'loop:cli-%' OR id LIKE 'task:20260806-cli-%' OR id LIKE 'task:m4-%' OR id LIKE 'loop:m4-%' OR id LIKE 'task:tlm4-%' OR id LIKE 'loop:tlm4-%' OR id LIKE 'task:20260806-m4-%' OR id LIKE 'task:rf15-%' OR id LIKE 'loop:rf15-%' OR id LIKE 'task:20260806-rf15-%' OR id LIKE 'task:step14-%' OR id LIKE 'loop:step14-%' OR id LIKE 'task:20260806-step14-%' OR id LIKE 'loop:20260806-step14-%' OR id LIKE 'loop:20260806-rf15-%' OR id = 'task:nonexistent-rf15' OR id LIKE 'loop:tlm9-%' OR id LIKE 'loop:tlm10-%' OR id LIKE 'loop:tlm11-%' OR id LIKE 'loop:20260806-t9-%' OR id LIKE 'loop:20260806-t10-%' OR id LIKE 'loop:20260806-t11-%' OR id LIKE 'task:20260806-first%' OR id LIKE 'task:20260806-replay%' OR id LIKE 'task:20260806-second%' OR id LIKE 'task:20260806-active%'"
        )
    finally:
        m._conn.execute("PRAGMA foreign_keys = ON")
    m._conn.commit()


def _mk_task(m, suffix: str, props: dict = None) -> str:
    tid = f"task:tlm3-{suffix}"
    m._conn.execute(
        "INSERT INTO entities (id, kind, name, properties_json) VALUES (?, ?, ?, ?)",
        (tid, "task", f"tlm3-{suffix}", json.dumps(props or {})),
    )
    m._conn.execute(
        "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
        (tid, "open", "2026-08-06T10:00", "2026-08-06T10:00"),
    )
    m._conn.commit()
    return tid


def test_list_tasks_default_active_only():
    """默认 state filter 排除 done/cancelled/dormant/paused."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        a = _mk_task(m, "active-a")  # 不动
        b = _mk_task(m, "active-b")
        c = _mk_task(m, "done-c")
        # c 推到 done
        ts_mod.transition(m._conn, task_id=c, to_state="in_progress", reason="x", now="2026-08-06T10:05")
        ts_mod.transition(m._conn, task_id=c, to_state="done", reason="x", now="2026-08-06T10:30")

        result = ts_mod.list_tasks(m._conn)
        ids = sorted([t["task_id"] for t in result["tasks"]])
        assert ids == sorted([a, b]), f"only active tasks, got {ids}"
        assert result["count"] == 2
    finally:
        m.close()


def test_list_tasks_with_state_filter():
    """state filter 走 SQL 路径."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        a = _mk_task(m, "ip-a")
        b = _mk_task(m, "ip-b")
        # a → in_progress
        ts_mod.transition(m._conn, task_id=a, to_state="in_progress", reason="x", now="2026-08-06T10:05")

        result = ts_mod.list_tasks(m._conn, state="in_progress")
        assert result["count"] == 1
        assert result["tasks"][0]["task_id"] == a
        assert result["tasks"][0]["state"] == "in_progress"
    finally:
        m.close()


def test_replay_task_returns_full_history():
    """replay_task 返回全部 windows + current_state."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task(m, "replay")
        ts_mod.transition(m._conn, task_id=tid, to_state="in_progress", reason="开始", now="2026-08-06T10:05")
        ts_mod.transition(m._conn, task_id=tid, to_state="waiting", reason="等", now="2026-08-06T10:30")
        ts_mod.transition(m._conn, task_id=tid, to_state="in_progress", reason="继续", now="2026-08-06T11:00")

        result = ts_mod.replay_task(m._conn, task_id=tid)
        assert result["current_state"] == "in_progress"
        assert result["window_count"] == 4
        assert [w["state"] for w in result["windows"]] == [
            "open",
            "in_progress",
            "waiting",
            "in_progress",
        ]
        # 第一窗 valid_until=10:05, 第二窗 10:30, 第三窗 11:00, 第四窗 None
        assert result["windows"][0]["valid_until"] == "2026-08-06T10:05"
        assert result["windows"][-1]["valid_until"] is None
    finally:
        m.close()


def test_replay_task_asof_slice():
    """asof 切片: 只返回 valid_from <= asof < valid_until 的窗."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task(m, "asof")
        ts_mod.transition(m._conn, task_id=tid, to_state="in_progress", reason="x", now="2026-08-06T10:05")
        ts_mod.transition(m._conn, task_id=tid, to_state="waiting", reason="x", now="2026-08-06T10:30")
        ts_mod.transition(m._conn, task_id=tid, to_state="in_progress", reason="x", now="2026-08-06T11:00")

        # asof 10:00: 期望 open 窗起点 (valid_from=10:00, valid_until=10:05; valid_from <= 10:00 ✅)
        # half-open [valid_from, valid_until) 区间, valid_until=10:05 > 10:00 ✅
        result = ts_mod.replay_task(m._conn, task_id=tid, asof="2026-08-06T10:00")
        states = [w["state"] for w in result["windows"]]
        assert states == ["open"], f"asof 10:00 (起点), got {states}"

        # asof 10:20: open 窗已关 (10:05 < 10:20), 期望 in_progress (10:05-10:30)
        result = ts_mod.replay_task(m._conn, task_id=tid, asof="2026-08-06T10:20")
        states = [w["state"] for w in result["windows"]]
        assert states == ["in_progress"], f"asof 10:20, got {states}"

        # asof 10:45: 期望 waiting (10:30-11:00)
        result = ts_mod.replay_task(m._conn, task_id=tid, asof="2026-08-06T10:45")
        states = [w["state"] for w in result["windows"]]
        assert states == ["waiting"], f"asof 10:45, got {states}"
    finally:
        m.close()
