"""
[8/6 M2 Step 5] Tests for task_create() + loop_create() + terminal bookkeeping.

DESIGN §5.1 memory_task_create / memory_loop_create.
DESIGN §4.2 step 5: 终端簿记 (done/cancelled 且是 loop active_task → 清 active).
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
        m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm5-%' OR task_id LIKE 'loop:tlm5-%'")
        m._conn.execute("DELETE FROM entities WHERE id LIKE 'task:tlm5-%' OR id LIKE 'loop:tlm5-%'")
    finally:
        m._conn.execute("PRAGMA foreign_keys = ON")
    m._conn.commit()


def test_task_create_basic_no_loop():
    """task_create 不带 loop_id: 建 entity + open 窗, 不动 loop."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        r = ts_mod.task_create(
            m._conn,
            name="独立一次性任务",
            now="2026-08-06T10:00",
        )
        assert r["current_state"] == "open"
        assert r["task_id"].startswith("task:20260806-")
        assert "loop_id" not in r

        # 校验 DB
        ent = tuple(
            m._conn.execute(
                "SELECT kind, name, memory_type FROM entities WHERE id=?",
                (r["task_id"],),
            ).fetchone()
        )
        assert ent == ("task", "独立一次性任务", "ephemeral")

        win = tuple(
            m._conn.execute(
                "SELECT state, valid_from, valid_until, reason FROM task_states WHERE task_id=? AND valid_until IS NULL",
                (r["task_id"],),
            ).fetchone()
        )
        assert win[0] == "open"
        assert win[1] == "2026-08-06T10:00"
        assert win[2] is None
        assert win[3] == "task_create"
    finally:
        m.close()


def test_task_create_with_loop_sets_active():
    """task_create 带 loop_id: 写 loop.properties_json.active_task_id + loop 状态窗 running."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        # 先建 loop
        loop_r = ts_mod.loop_create(
            m._conn,
            name="耗材库存",
            trigger="库存低于阈值",
            interval_hours=24,
            now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]
        assert loop_r["enabled"] is not False  # default True

        # 建 task
        task_r = ts_mod.task_create(
            m._conn,
            name="采购耗材",
            loop_id=lid,
            now="2026-08-06T10:00",
        )
        assert task_r["loop_id"] == lid

        # 校验 loop active_task_id 已设
        cfg = json.loads(
            m._conn.execute(
                "SELECT properties_json FROM entities WHERE id=?",
                (lid,),
            ).fetchone()[0]
        )
        assert cfg["active_task_id"] == task_r["task_id"]

        # 校验 loop 状态窗 'running'
        loop_state = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (lid,),
        ).fetchone()
        assert loop_state[0] == "running"
    finally:
        m.close()


def test_terminal_bookkeeping_clears_active_task():
    """terminal 簿记: task 推到 done → loop.active_task_id=NULL + last_cycle_done_at 设值."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        loop_r = ts_mod.loop_create(
            m._conn,
            name="耗材",
            trigger="库存低",
            interval_hours=24,
            now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        task_r = ts_mod.task_create(
            m._conn,
            name="采购",
            loop_id=lid,
            now="2026-08-06T10:00",
        )
        tid = task_r["task_id"]

        # 推到 done
        ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="in_progress",
            reason="x",
            now="2026-08-06T10:05",
        )
        result = ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="done",
            reason="收货",
            now="2026-08-06T10:30",
        )
        assert "terminal_bookkeeping" in result
        assert result["terminal_bookkeeping"]["loop_id"] == lid
        assert result["terminal_bookkeeping"]["action"] == "clear_active_task"

        # 校验 loop active_task_id 已清, last_cycle_done_at 已设
        cfg = json.loads(
            m._conn.execute(
                "SELECT properties_json FROM entities WHERE id=?",
                (lid,),
            ).fetchone()[0]
        )
        assert cfg["active_task_id"] is None
        assert cfg["last_cycle_done_at"] == "2026-08-06T10:30"

        # 校验 loop 状态窗 (running → 应该转移... 但本测试只测簿记)
        # 簿记只清 active_task_id; loop 状态窗本身不变 (因为非 done/cancelled)
        loop_state = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (lid,),
        ).fetchone()
        assert loop_state[0] == "running"
    finally:
        m.close()


def test_task_create_rejects_loop_active_task():
    """第二个 task_create 同一 loop → LOOP_HAS_ACTIVE_TASK 拒."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        loop_r = ts_mod.loop_create(
            m._conn,
            name="耗材",
            trigger="库存低",
            now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        ts_mod.task_create(m._conn, name="first", loop_id=lid, now="2026-08-06T10:00")

        try:
            ts_mod.task_create(m._conn, name="second", loop_id=lid, now="2026-08-06T10:05")
        except ts_mod.TaskLoopError as e:
            assert e.code == "LoopHasActiveTaskError"
        else:
            raise AssertionError("double-spawn should fail")
    finally:
        m.close()


def test_task_create_rejects_loop_disabled():
    """loop enabled=False → LOOP_DISABLED 拒."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        loop_r = ts_mod.loop_create(
            m._conn,
            name="耗材",
            trigger="库存低",
            enabled=False,
            now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        try:
            ts_mod.task_create(m._conn, name="abc", loop_id=lid, now="2026-08-06T10:00")
        except ts_mod.TaskLoopError as e:
            assert e.code == "LoopDisabledError"
        else:
            raise AssertionError("disabled loop should fail")
    finally:
        m.close()
