"""
[8/6 M2 Step 1] Tests for transition() function.

DESIGN §11 M2 — 行为 (transition CAS):
  covered in this file:
  - test_transition_ok_closes_old_opens_new
  - test_transition_invalid_graph_rejected
  - test_transition_force_requires_reason
  - test_transition_reopen_done_to_open

  small + idempotent: 4 tests, run under --noconftest with backend=usearch
  (zvec lock conflict).  fixture prefix 'task:tlm2-' avoided collision with M1.
"""

import json
import os
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
    """Clean fixtures: prefix 'task:tlm2-'.  We do not depend on entities.rowid."""
    m._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm2-%'")
        m._conn.execute("DELETE FROM entities WHERE id LIKE 'task:tlm2-%'")
    finally:
        m._conn.execute("PRAGMA foreign_keys = ON")
    m._conn.commit()


def _mk_task(m, suffix: str, props: dict = None) -> str:
    """Create a task entity + initial open window.  Returns task_id."""
    tid = f"task:tlm2-{suffix}"
    m._conn.execute(
        "INSERT INTO entities (id, kind, name, properties_json) VALUES (?, ?, ?, ?)",
        (tid, "task", f"tlm2-{suffix}", json.dumps(props or {})),
    )
    m._conn.execute(
        "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
        (tid, "open", "2026-08-06T10:00", "2026-08-06T10:00"),
    )
    m._conn.commit()
    return tid


def test_transition_ok_closes_old_opens_new():
    """正常 transfer: 关旧窗 + 开新窗 + 返回值带 from_state/to_state/window_id."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task(m, "ok")

        result = ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="in_progress",
            reason="agent A: handoff to B",
            now="2026-08-06T10:30",
        )

        assert result["from_state"] == "open"
        assert result["to_state"] == "in_progress"
        assert result["task_id"] == tid
        assert result["valid_from"] == "2026-08-06T10:30"
        assert isinstance(result["window_id"], int) and result["window_id"] > 0

        # 数据库层验证: 旧窗 valid_until 设置, 新窗 valid_until=NULL
        windows = [
            tuple(r)
            for r in m._conn.execute(
                "SELECT state, valid_from, valid_until FROM task_states WHERE task_id=? ORDER BY id",
                (tid,),
            )
        ]
        assert windows == [
            ("open", "2026-08-06T10:00", "2026-08-06T10:30"),
            ("in_progress", "2026-08-06T10:30", None),
        ]
    finally:
        m.close()


def test_transition_invalid_graph_rejected():
    """INVALID_TRANSITION: open → waiting 拒 (允许图无此边)."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task(m, "invalid")

        try:
            ts_mod.transition(
                m._conn,
                task_id=tid,
                to_state="waiting",
                reason="should fail",
                now="2026-08-06T10:30",
            )
        except ts_mod.InvalidTransitionError as e:
            assert "open" in str(e) and "waiting" in str(e), f"message: {e}"
            assert e.field == "to_state"
        else:
            raise AssertionError("open→waiting should fail")

        # 数据库: 状态未变
        cur = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchone()
        assert cur[0] == "open"
    finally:
        m.close()


def test_transition_force_requires_reason():
    """D8: force=True 但 reason 为空 → ReasonRequiredError."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task(m, "force")

        # 1. force=True + reason 为空 → 拒
        try:
            ts_mod.transition(
                m._conn,
                task_id=tid,
                to_state="waiting",
                reason="",
                force=True,
                now="2026-08-06T10:30",
            )
        except ts_mod.ReasonRequiredError as e:
            assert e.field == "reason"
        else:
            raise AssertionError("force=True without reason should fail")

        # 2. force=True + reason 有值 → 绕过允许图 (允许: open→waiting 是非标准)
        result = ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="waiting",
            reason="纠正: 跳级转移, 上游确认",
            force=True,
            now="2026-08-06T10:30",
        )
        assert result["to_state"] == "waiting"
    finally:
        m.close()


def test_transition_reopen_done_to_open():
    """D8: done → open 是 reopen 逃生门 (允许图里只存这一条)."""
    m = mem_mod.Memory()
    try:
        _setup(m)
        tid = _mk_task(m, "reopen")
        # 推到 done: open → in_progress → done
        ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="in_progress",
            reason="开始",
            now="2026-08-06T10:05",
        )
        ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="done",
            reason="完成",
            now="2026-08-06T10:30",
        )

        # 此时 from_state=done. done→in_progress 应被 step 1.2 拦截 (terminal).
        try:
            ts_mod.transition(
                m._conn,
                task_id=tid,
                to_state="in_progress",
                reason="bad",
                now="2026-08-06T10:45",
            )
        except ts_mod.InvalidTransitionError:
            pass
        else:
            raise AssertionError("done→in_progress should fail (terminal)")

        # done→open reopen 允许 (D8 逃生门)
        result = ts_mod.transition(
            m._conn,
            task_id=tid,
            to_state="open",
            reason="reopen 逃生门: 客户调整",
            now="2026-08-06T11:00",
        )
        assert result["from_state"] == "done"
        assert result["to_state"] == "open"
    finally:
        m.close()
