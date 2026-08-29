"""
[8/6 M2 Step 4] Tests for loop_tick() — 4 verdict states.

DESIGN §4.3 loop_tick(loop_id):
  1. not enabled → dormant
  2. active 在飞 (active_state ∉ done/cancelled) → waiting
  3. last_cycle_done_at is None → due (first run)
  4. elapsed(last, now) < interval_hours → not_due
  5. else → due
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
        m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm4-%' OR task_id LIKE 'loop:tlm4-%'")
        m._conn.execute("DELETE FROM entities WHERE id LIKE 'task:tlm4-%' OR id LIKE 'loop:tlm4-%'")
    finally:
        m._conn.execute("PRAGMA foreign_keys = ON")
    m._conn.commit()


def _mk_loop(m, suffix: str, props: dict) -> str:
    lid = f"loop:tlm4-{suffix}"
    m._conn.execute(
        "INSERT INTO entities (id, kind, name, properties_json) VALUES (?, ?, ?, ?)",
        (lid, "loop", f"tlm4-{suffix}", json.dumps(props)),
    )
    m._conn.commit()
    return lid


def _mk_task_with_window(m, suffix: str, state: str = "open") -> str:
    tid = f"task:tlm4-{suffix}"
    m._conn.execute(
        "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
        (tid, "task", f"tlm4-{suffix}"),
    )
    m._conn.execute(
        "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
        (tid, state, "2026-08-06T10:00", "2026-08-06T10:00"),
    )
    m._conn.commit()
    return tid


def test_loop_tick_dormant_when_disabled():
    """loop enabled=false → verdict=dormant."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        lid = _mk_loop(
            m,
            "dormant",
            {
                "trigger": "x",
                "interval_hours": 24,
                "enabled": False,
            },
        )
        r = ts_mod.loop_tick(m._conn, loop_id=lid, now="2026-08-06T11:00")
        assert r["verdict"] == "dormant"
        assert r["enabled"] is False
    finally:
        m.close()


def test_loop_tick_waiting_when_active_task_open():
    """loop.active_task_id 指向 in_progress task → waiting."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task_with_window(m, "active", "in_progress")
        lid = _mk_loop(
            m,
            "waiting",
            {
                "trigger": "x",
                "interval_hours": 24,
                "enabled": True,
                "active_task_id": tid,
            },
        )
        r = ts_mod.loop_tick(m._conn, loop_id=lid, now="2026-08-06T11:00")
        assert r["verdict"] == "waiting"
        assert r["active_state"] == "in_progress"
    finally:
        m.close()


def test_loop_tick_due_first_run():
    """last_cycle_done_at is None → due."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        lid = _mk_loop(
            m,
            "first",
            {
                "trigger": "x",
                "interval_hours": 24,
                "enabled": True,
                "active_task_id": None,
            },
        )
        r = ts_mod.loop_tick(m._conn, loop_id=lid, now="2026-08-06T11:00")
        assert r["verdict"] == "due"
    finally:
        m.close()


def test_loop_tick_not_due_within_interval():
    """elapsed < interval → not_due."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        lid = _mk_loop(
            m,
            "recent",
            {
                "trigger": "x",
                "interval_hours": 24,
                "enabled": True,
                "active_task_id": None,
                "last_cycle_done_at": "2026-08-06T10:00",
            },
        )
        r = ts_mod.loop_tick(m._conn, loop_id=lid, now="2026-08-06T11:00")
        assert r["verdict"] == "not_due"
        assert r["elapsed_hours"] < 24
    finally:
        m.close()


def test_loop_tick_due_after_interval():
    """elapsed >= interval → due."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        lid = _mk_loop(
            m,
            "overdue",
            {
                "trigger": "x",
                "interval_hours": 24,
                "enabled": True,
                "active_task_id": None,
                "last_cycle_done_at": "2026-08-05T10:00",
            },
        )
        r = ts_mod.loop_tick(m._conn, loop_id=lid, now="2026-08-06T11:00")
        assert r["verdict"] == "due"
        assert r["elapsed_hours"] >= 24
    finally:
        m.close()


def test_loop_tick_not_found_raises():
    """loop_id 不存在 → LoopNotFoundError."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        try:
            ts_mod.loop_tick(m._conn, loop_id="loop:nope", now="2026-08-06T11:00")
        except ts_mod.LoopNotFoundError as e:
            assert e.field == "loop_id"
        else:
            raise AssertionError("missing loop should fail")
    finally:
        m.close()
