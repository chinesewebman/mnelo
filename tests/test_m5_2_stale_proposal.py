"""
[8/6 M5.2 + DESIGN §4.4] L2 stuck_task Proposal 测试.

覆盖:
  M5.2.1 propose_stale_tasks 写 audit_log (status='proposed', pass_name='stuck_task')
  M5.2.2 阈值桶 (open >7d, waiting >14d, blocked >3d, in_progress 7d)
  M5.2.3 幂等: 已有 pending Proposal 跳过 (skipped_existing 计数)
  M5.2.4 apply_stale_proposal 标记 status='applied' (新 audit_log row, append-only)
  M5.2.5 apply 错误 proposal_id 抛 ProposalNotFound / ProposalMismatch
  M5.2.6 list_stale_proposals 按 status 过滤
  M5.2.7 提议文本含 prompt (DESIGN §4.4 "⚠ 需决策")
  M5.2.8 mnelo 不自主 transfer — propose 后 task state 不变
"""

import sqlite3
import sys
from pathlib import Path

os_path = Path("/Users/apple/.hermes/memory")
import os

sys.path.insert(0, str(os_path))
os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

import memory
import task_states

# [8/9 P1 follow-up] 测试 hard-coded "2026-08-06T15:00" 在 8/9 跑会边界 fail (now-7d == threshold,
# propose_stale_tasks 用 < 严格小于). 改用 NOW_REF = now + 1s (未来 1 秒), 跟 _create_task(days_ago=10)
# 配对 → valid_from = now - 10d, NOW_REF = now + 1s, age = 10d + 1s > threshold 7d (state=open), 必 stale.
# 任何时区/时间漂移都不会边界 fail, 跟 8/9 业务日期解耦.
import datetime as _dt

NOW_REF = (_dt.datetime.now() + _dt.timedelta(seconds=1)).isoformat(timespec="milliseconds")


def _setup():
    """[M5.2 fix] Clean m5-stale test fixtures (含 audit_log + test-other)."""
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:m5-stale-%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:m5-stale-%'")
    # [M5.2 fix] audit_log 含 ref_id LIKE 'task:%m5-stale-%' — 不清掉 propose 跳过
    c.execute("DELETE FROM audit_log WHERE pass_name='stuck_task' AND ref_id LIKE 'task:%m5-stale-%'")
    c.execute("DELETE FROM audit_log WHERE run_id='test-other'")
    c.execute("DELETE FROM audit_log WHERE pass_name='loop_tick_cron' AND ref_id='loop:fake'")
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _create_task(name: str, state: str = "open", days_ago: int = 10) -> str:
    """[M5.2 fix] 建 task, 走合法转移路径推到指定 state.

    状态机 (DESIGN §3.2 default 矩阵):
      open → in_progress / done / cancelled
      in_progress → waiting / blocked / done / cancelled
      waiting → in_progress / done / cancelled
      blocked → in_progress / waiting / done / cancelled
    open 不能直跳 waiting/blocked, 必须经 in_progress.
    """
    m = memory.Memory()
    try:
        back = _dt.datetime.now() - _dt.timedelta(days=days_ago)
        vf = back.isoformat(timespec="milliseconds")
        r = task_states.task_create(m._conn, name=name, owner_id="person:yanru", now=vf)
        tid = r["task_id"]
        # 走合法路径
        if state in ("waiting", "blocked", "done", "cancelled"):
            # 先 open → in_progress
            task_states.transition(
                m._conn,
                task_id=tid,
                to_state="in_progress",
                reason=f"to in_progress (then {state})",
                now=vf,
            )
        if state in ("waiting", "blocked"):
            # in_progress → waiting / blocked
            task_states.transition(
                m._conn,
                task_id=tid,
                to_state=state,
                reason=f"to {state}",
                now=vf,
            )
        elif state == "done":
            task_states.transition(
                m._conn,
                task_id=tid,
                to_state="done",
                reason=f"to done",
                now=vf,
            )
        m._conn.commit()
        return tid
    finally:
        m.close()


# ===== M5.2.1 basic propose =====


def test_m5_2_1_propose_writes_audit_log_proposed():
    _setup()
    tid = _create_task("m5-stale-propose", days_ago=10)
    m = memory.Memory()
    try:
        result = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        assert result["scanned"] >= 1
        assert result["proposed"] >= 1
        # 校验 audit_log 行
        row = m._conn.execute(
            """SELECT pass_name, action_type, ref_type, ref_id, status, after_json
               FROM audit_log
               WHERE pass_name='stuck_task' AND ref_id=?""",
            (tid,),
        ).fetchone()
        assert row is not None, f"audit_log row missing for {tid}"
        assert row[0] == "stuck_task"
        assert row[1] == "stale_review"
        assert row[2] == "task"
        assert row[3] == tid
        assert row[4] == "proposed"
        after = __import__("json").loads(row[5])
        assert "prompt" in after
        assert after["state"] == "open"
        assert after["threshold_days"] == 7
    finally:
        m.close()


# ===== M5.2.2 threshold buckets =====


def test_m5_2_2_threshold_buckets():
    """[M5.2.2] open >7d / waiting >14d / blocked >3d 分桶."""
    _setup()
    # 建 4 个 task, 每个都 stale (刚过阈值)
    t_open = _create_task("m5-stale-open", "open", days_ago=8)
    t_waiting = _create_task("m5-stale-waiting", "waiting", days_ago=15)
    t_blocked = _create_task("m5-stale-blocked", "blocked", days_ago=4)
    t_in_progress = _create_task("m5-stale-inprogress", "in_progress", days_ago=8)

    m = memory.Memory()
    try:
        result = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        proposed_ids = {p["task_id"] for p in result["proposals"]}
        assert t_open in proposed_ids, f"open >7d 应 stale"
        assert t_waiting in proposed_ids, f"waiting >14d 应 stale"
        assert t_blocked in proposed_ids, f"blocked >3d 应 stale"
        assert t_in_progress in proposed_ids, f"in_progress >7d 应 stale"

        # 校验不同 state 的 threshold_days
        for p in result["proposals"]:
            if p["task_id"] == t_open:
                assert p["threshold_days"] == 7
            elif p["task_id"] == t_waiting:
                assert p["threshold_days"] == 14
            elif p["task_id"] == t_blocked:
                assert p["threshold_days"] == 3
            elif p["task_id"] == t_in_progress:
                assert p["threshold_days"] == 7
    finally:
        m.close()


# ===== M5.2.3 idempotent =====


def test_m5_2_3_idempotent_skip_existing():
    """[M5.2.3] 已有 pending Proposal 跳过.

    [M28 fix] 校验本 task 不被重复提议 (其他 task 隔离).
    旧测试 assert r2["proposed"] == 0 假设全局无新提议, 但 live DB 有其他
    stale task (e2e / m28 残留) 会让全局 count > 0. 改校验本 task 在 r2 proposals
    不出现.
    """
    _setup()
    tid = _create_task("m5-stale-idemp", days_ago=10)

    m = memory.Memory()
    try:
        r1 = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        # 本 task 第一次必被提议
        r1_ids = [p["task_id"] for p in r1["proposals"]]
        assert tid in r1_ids

        # 第二次扫, 本 task 已有 pending 应跳过 (其他 task 不管)
        r2 = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        r2_ids = [p["task_id"] for p in r2["proposals"]]
        assert tid not in r2_ids, f"本 task 二次扫应跳过, got {tid} in {r2_ids}"
        # skipped_existing 至少 1 (本 task 自身)
        assert r2["skipped_existing"] >= 1
    finally:
        m.close()


# ===== M5.2.4 apply =====


def test_m5_2_4_apply_marks_applied():
    """[M5.2.4] apply_stale_proposal 标记 status='applied' (新 audit_log row)."""
    _setup()
    tid = _create_task("m5-stale-apply", days_ago=10)

    m = memory.Memory()
    try:
        scan = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        # 找 proposal_id
        rows = m._conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND status='proposed' AND ref_id=?""",
            (tid,),
        ).fetchall()
        assert len(rows) == 1
        pid = rows[0][0]

        # apply
        applied = task_states.apply_stale_proposal(
            m._conn,
            pid,
            applied_action="transitioned to done",
        )
        assert applied["status"] == "applied"
        assert applied["ref_id"] == tid

        # 校验 audit_log: 1 行 proposed + 1 行 applied (append-only)
        rows2 = m._conn.execute(
            """SELECT status, action_type FROM audit_log
               WHERE pass_name='stuck_task' AND ref_id=?
               ORDER BY id ASC""",
            (tid,),
        ).fetchall()
        assert len(rows2) == 2, f"expected 2 rows (proposed+applied), got {rows2}"
        assert rows2[0][0] == "proposed"
        assert rows2[0][1] == "stale_review"
        assert rows2[1][0] == "applied"
        assert rows2[1][1] == "stale_resolved"

        # 重复 apply 抛错
        try:
            task_states.apply_stale_proposal(m._conn, pid, applied_action="dup")
            raise AssertionError("repeat apply should raise")
        except task_states.TaskLoopError as e:
            assert e.code == "ProposalAlreadyResolved"
    finally:
        m.close()


# ===== M5.2.5 apply error paths =====


def test_m5_2_5_apply_invalid_proposal_id():
    """[M5.2.5] 错误 proposal_id 抛 ProposalNotFound."""
    _setup()
    m = memory.Memory()
    try:
        try:
            task_states.apply_stale_proposal(m._conn, 999999, applied_action="x")
            raise AssertionError("expected raise")
        except task_states.TaskLoopError as e:
            assert e.code == "ProposalNotFound"
    finally:
        m.close()


def test_m5_2_5b_apply_wrong_pass_name():
    """[M5.2.5b] 拿现有 audit_log (非 stuck_task) apply — ProposalMismatch."""
    _setup()
    m = memory.Memory()
    try:
        # 插一行非 stuck_task 的 audit_log
        m._conn.execute(
            """INSERT INTO audit_log (
                run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("test-other", "loop_tick_cron", "tick_due", "loop", "loop:fake", None, "{}", 1.0, "proposed", NOW_REF),
        )
        m._conn.commit()
        fake_id = m._conn.execute("SELECT id FROM audit_log WHERE pass_name='loop_tick_cron' ORDER BY id DESC LIMIT 1").fetchone()[0]
        try:
            task_states.apply_stale_proposal(m._conn, fake_id, applied_action="x")
            raise AssertionError("expected raise")
        except task_states.TaskLoopError as e:
            assert e.code == "ProposalMismatch"
    finally:
        m.close()


# ===== M5.2.6 list_stale_proposals =====


def test_m5_2_6_list_stale_proposals_filtered():
    """[M5.2.6] list_stale_proposals 按 status 过滤.

    [M5.2 fix] 只校验本 task 的 row, 不依赖全局 count (前次测试 run 残留 16 行).
    """
    _setup()
    tid = _create_task("m5-stale-list", days_ago=10)

    m = memory.Memory()
    try:
        task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        rs = task_states.list_stale_proposals(m._conn, status="proposed")
        assert rs["count"] >= 1
        # 校验本 task 在 pending 列表中
        assert any(p["ref_id"] == tid for p in rs["proposals"])
        # 不 apply, 应当没有任何 applied row 关联本 task
        rs2 = task_states.list_stale_proposals(m._conn, status="applied")
        for p in rs2["proposals"]:
            assert p["ref_id"] != tid, f"不应有 {tid} 的 applied row, got {p}"
    finally:
        m.close()


# ===== M5.2.7 prompt text =====


def test_m5_2_7_proposal_prompt_text():
    """[M5.2.7] Proposal after_json.prompt 包含 ⚠ 需决策 标记 (DESIGN §4.4)."""
    _setup()
    tid = _create_task("m5-stale-prompt", days_ago=10)
    m = memory.Memory()
    try:
        task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        after = m._conn.execute(
            """SELECT after_json FROM audit_log
               WHERE pass_name='stuck_task' AND ref_id=?""",
            (tid,),
        ).fetchone()[0]
        d = __import__("json").loads(after)
        assert "prompt" in d
        # 包含中文 + 状态名
        assert "open" in d["prompt"]
        assert "超过阈值" in d["prompt"]
        assert "天" in d["prompt"]
    finally:
        m.close()


# ===== M5.2.8 mnelo 不自主转移 =====


def test_m5_2_8_propose_does_not_mutate_task_state():
    """[M5.2.8 + D5] mnelo 绝不自主转移 — propose 后 task state 仍 open, 没新状态窗."""
    _setup()
    tid = _create_task("m5-stale-no-mutate", days_ago=10)
    m = memory.Memory()
    try:
        # 校验起点: 1 行 open state
        rows_before = m._conn.execute(
            "SELECT id FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchall()
        assert len(rows_before) == 1

        # 跑 propose
        task_states.propose_stale_tasks(m._conn, now=NOW_REF)

        # 校验: 仍然 1 行 open state (没插新状态窗)
        rows_after = m._conn.execute(
            "SELECT id FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchall()
        assert len(rows_after) == 1, f"propose 不应写 task_states, got {len(rows_after)} rows"

        # state 仍 open
        state = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchone()[0]
        assert state == "open"
    finally:
        m.close()


# ===== M5.2.9 fresh task < 7d 不被提议 =====


def test_m5_2_9_fresh_task_not_proposed():
    """[M5.2.9] 昨日建的 task 不应被提议 (age < 7d)."""
    _setup()
    tid = _create_task("m5-stale-fresh", days_ago=1)
    m = memory.Memory()
    try:
        result = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        proposed_ids = {p["task_id"] for p in result["proposals"]}
        assert tid not in proposed_ids, f"fresh task (1d) 不应被提议"
    finally:
        m.close()
