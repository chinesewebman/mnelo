"""
[8/6 M3 Step 12] Tests for loop_update + list_loops.

DESIGN §5.1:
  loop_update: 改 loop properties (enabled/trigger/interval/priority/owner_id),
                不会动 active_task_id / last_cycle_done_at.
                enabled 切换会落 dormant/running 状态窗.

  list_loops: 列 loop entities + current_state.
              enabled_only=True / state=过滤 / asof 时间切片.
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


def _setup():
    from memory import Memory
    mem = Memory()
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'task:tlm12-%' "
            "OR task_id LIKE 'task:20260806-t12-%' "
            "OR task_id LIKE 'loop:tlm12-%' "
            "OR task_id LIKE 'loop:20260806-t12-%'"
        )
        mem._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'task:tlm12-%' "
            "OR id LIKE 'task:20260806-t12-%' "
            "OR id LIKE 'loop:tlm12-%' "
            "OR id LIKE 'loop:20260806-t12-%'"
        )
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()
    mem.close()


def test_loop_update_enabled_to_disabled_writes_dormant():
    """loop_update(enabled=False) 关旧 running + 写 dormant 状态窗."""
    _setup()
    m = mem_mod.Memory()
    try:
        # 建 enabled=True loop
        loop_r = ts_mod.loop_create(
            m._conn, name="tlm12-a", trigger="x",
            enabled=True, now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        # 改 enabled=False
        result = ts_mod.loop_update(
            m._conn, loop_id=lid, enabled=False,
            now="2026-08-06T10:00",
        )
        assert result["enabled"] is False
        assert "enabled" in result["changed"]

        # 校验 current_state = dormant
        cur = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (lid,),
        ).fetchone()
        assert cur[0] == "dormant"
    finally:
        m.close()


def test_loop_update_dormant_to_running_resumes():
    """loop_update(enabled=True) 关旧 dormant + 写 running 窗."""
    _setup()
    m = mem_mod.Memory()
    try:
        loop_r = ts_mod.loop_create(
            m._conn, name="tlm12-b", trigger="x",
            enabled=False, now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        ts_mod.loop_update(m._conn, loop_id=lid, enabled=True, now="2026-08-06T10:00")

        cur = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (lid,),
        ).fetchone()
        assert cur[0] == "running"
    finally:
        m.close()


def test_loop_update_partial_fields_only():
    """loop_update 只改提供的字段 (None = 不改)."""
    _setup()
    m = mem_mod.Memory()
    try:
        loop_r = ts_mod.loop_create(
            m._conn, name="tlm12-c", trigger="old",
            interval_hours=24, priority=3, now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        # 只改 trigger
        result = ts_mod.loop_update(
            m._conn, loop_id=lid, trigger="new trigger",
            now="2026-08-06T10:00",
        )
        assert result["changed"] == {"trigger": "new trigger"}
        # interval_hours 仍是 24
        assert result["interval_hours"] == 24

        # 校验 DB
        cfg = json.loads(m._conn.execute(
            "SELECT properties_json FROM entities WHERE id=?", (lid,),
        ).fetchone()[0])
        assert cfg["trigger"] == "new trigger"
        assert cfg["interval_hours"] == 24
        assert cfg["priority"] == 3
    finally:
        m.close()


def test_loop_update_does_not_touch_active_task():
    """loop_update 不动 active_task_id (那是 task_create/transition 的领域)."""
    _setup()
    m = mem_mod.Memory()
    try:
        loop_r = ts_mod.loop_create(
            m._conn, name="tlm12-d", trigger="x",
            now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]
        task_r = ts_mod.task_create(
            m._conn, name="tlm12-d-task", loop_id=lid,
            now="2026-08-06T10:00",
        )
        active_before = task_r["task_id"]

        # 改 trigger, 不应动 active_task_id
        ts_mod.loop_update(m._conn, loop_id=lid, trigger="new", now="2026-08-06T10:05")

        cfg = json.loads(m._conn.execute(
            "SELECT properties_json FROM entities WHERE id=?", (lid,),
        ).fetchone()[0])
        assert cfg["active_task_id"] == active_before
    finally:
        m.close()


def test_loop_update_notfound():
    """loop_update 不存在 loop_id → LoopNotFoundError."""
    _setup()
    m = mem_mod.Memory()
    try:
        try:
            ts_mod.loop_update(m._conn, loop_id="loop:nonexistent", enabled=False)
        except ts_mod.LoopNotFoundError:
            pass
        else:
            raise AssertionError("expected LoopNotFoundError")
    finally:
        m.close()


def test_list_loops_basic():
    """list_loops 列出全部 loop + current_state."""
    _setup()
    m = mem_mod.Memory()
    try:
        ts_mod.loop_create(m._conn, name="tlm12-e1", trigger="x",
                          enabled=True, now="2026-08-06T09:00")
        ts_mod.loop_create(m._conn, name="tlm12-e2", trigger="y",
                          enabled=False, now="2026-08-06T09:01")

        r = ts_mod.list_loops(m._conn)
        names = [loop["name"] for loop in r["loops"]]
        assert "tlm12-e1" in names
        assert "tlm12-e2" in names
    finally:
        m.close()


def test_list_loops_enabled_only():
    """list_loops(enabled_only=True) 仅 enabled=True."""
    _setup()
    m = mem_mod.Memory()
    try:
        ts_mod.loop_create(m._conn, name="tlm12-on", trigger="x",
                          enabled=True, now="2026-08-06T09:00")
        ts_mod.loop_create(m._conn, name="tlm12-off", trigger="x",
                          enabled=False, now="2026-08-06T09:01")

        r = ts_mod.list_loops(m._conn, enabled_only=True)
        names = [loop["name"] for loop in r["loops"]]
        assert "tlm12-on" in names
        assert "tlm12-off" not in names
    finally:
        m.close()


def test_list_loops_state_filter():
    """list_loops(state='dormant') 仅 dormant loop."""
    _setup()
    m = mem_mod.Memory()
    try:
        ts_mod.loop_create(m._conn, name="tlm12-d1", trigger="x",
                          enabled=False, now="2026-08-06T09:00")
        ts_mod.loop_create(m._conn, name="tlm12-d2", trigger="x",
                          enabled=False, now="2026-08-06T09:01")

        r = ts_mod.list_loops(m._conn, state="dormant")
        states = [loop["current_state"] for loop in r["loops"]]
        assert all(s == "dormant" for s in states)
    finally:
        m.close()
