"""[8/6 M34] list_stale_proposals status + limit 校验测试.

覆盖:
  M34.1 status 白名单 - rejected invalid status
  M34.2 limit 范围 - 拒绝 <1, >1000, 非 int
  M34.3 status='all' - 包含 resolved/applied entries
"""

import sys
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import task_states
import memory
from datetime import datetime, timedelta

# [8/9 P1 follow-up] hard-coded "2026-08-06T..." 边界 fail.
NOW_REF = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="milliseconds")


def _setup():
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:%m34-%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:%m34-%'")
    c.execute("DELETE FROM audit_log WHERE ref_id LIKE 'task:%m34-%'")
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _create_active_task(name: str, age_days: int = 10) -> str:
    c = sqlite3.connect(str(memory.DB_PATH))
    back = (datetime.now() - timedelta(days=age_days)).isoformat(timespec="milliseconds")
    r = task_states.task_create(c, name=name, now=back)
    tid = r["task_id"]
    c.commit()
    c.close()
    return tid


def test_m34_status_invalid_rejected():
    """[M34.1] status='garbage' 抛 InvalidStatusError."""
    _setup()
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.list_stale_proposals(c, status="garbage")
            raise AssertionError("应抛 TaskLoopError")
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidStatusError", f"got {e.code}"
    finally:
        c.close()


def test_m34_status_proposed_valid():
    """[M34.1 正向] status='proposed' 通过."""
    _setup()
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        result = task_states.list_stale_proposals(c, status="proposed")
        assert "proposals" in result
        assert "count" in result
    finally:
        c.close()


def test_m34_status_all_includes_resolved():
    """[M34.3] status='all' 包含 resolved 行."""
    _setup()
    tid = _create_active_task("m34-status-all", age_days=10)
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        task_states.propose_stale_tasks(c, now=NOW_REF)
        # Apply one proposal
        row = c.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND ref_id=? AND status='proposed'""",
            (tid,),
        ).fetchone()
        assert row
        pid = row[0]
        task_states.apply_stale_proposal(c, pid, applied_action="test_resolved")

        # status='proposed' 应返回 [] (tid 已 resolved)
        proposed = task_states.list_stale_proposals(c, status="proposed")
        all_results = task_states.list_stale_proposals(c, status="all")

        applied_ids = {p["ref_id"] for p in all_results["proposals"] if p["status"] == "applied"}
        proposed_ids = {p["ref_id"] for p in proposed["proposals"]}
        assert tid in applied_ids, f"status='all' 应含 applied 条目, got {applied_ids}"
        assert tid not in proposed_ids, "status='proposed' 应排除 resolved"
    finally:
        c.close()


def test_m34_limit_invalid_rejected():
    """[M34.2] limit=0, limit=-1, limit=10000, limit='x' 都拒绝."""
    _setup()
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        for bad in (0, -1, 10000, "x"):
            try:
                task_states.list_stale_proposals(c, limit=bad)
                raise AssertionError(f"limit={bad} 应抛错")
            except task_states.TaskLoopError as e:
                assert e.code == "InvalidLimitError", f"got {e.code} for limit={bad}"
    finally:
        c.close()


def test_m34_limit_boundary_valid():
    """[M34.2 正向] limit=1, limit=1000 边界合法."""
    _setup()
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        for ok in (1, 1000, 50):
            result = task_states.list_stale_proposals(c, limit=ok)
            assert "proposals" in result
    finally:
        c.close()
