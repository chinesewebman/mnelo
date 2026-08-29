"""
[8/6 M28 review-pass] 3 个边界修复测试.

覆盖 review-pass aaca03a 的 3 个发现:
  M28.1 [中] forget_task/loop 重复 forget 抛 TaskAlreadyForgotten / LoopAlreadyForgotten
  M28.2 [中] propose_stale_tasks apply 后再提议 — 不应被旧 pending 永久跳过
  M28.3 [低] memory.forget(task/loop) 死代码 + docstring 实际行为对齐 — 静态契约
"""

import inspect as _inspect
import sqlite3
import sys
from pathlib import Path

REPO = Path("/Users/apple/.hermes/memory")
sys.path.insert(0, str(REPO))
import os

os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

import memory
import task_states

from datetime import datetime as _dt
from datetime import timedelta as _td

# [8/9 P1 follow-up] hard-coded "2026-08-06T..." 边界 fail (8/9 跑 age=7d < threshold 7d).
# 改 NOW_REF = now+1s (未来), 跟 _create_*_task(days_ago=10) 配对 → age=10d+1s > threshold 7d.
NOW_REF = (_dt.now() + _td(seconds=1)).isoformat(timespec="milliseconds")


def _setup():
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:m28-%' OR task_id LIKE 'loop:m28-%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:m28-%' OR id LIKE 'loop:m28-%'")
    c.execute("DELETE FROM audit_log WHERE (pass_name='forced_forget' OR pass_name='stuck_task') AND (ref_id LIKE 'task:%m28-%' OR ref_id LIKE 'loop:%m28-%')")
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _create_task(name: str, now: str = NOW_REF) -> str:
    m = memory.Memory()
    try:
        r = task_states.task_create(m._conn, name=name, now=now)
        tid = r["task_id"]
        m._conn.commit()
        return tid
    finally:
        m.close()


def _create_loop(name: str, now: str = NOW_REF) -> str:
    m = memory.Memory()
    try:
        r = task_states.loop_create(m._conn, name=name, trigger="x", interval_hours=24, now=now)
        lid = r["loop_id"]
        m._conn.commit()
        return lid
    finally:
        m.close()


# ===== M28.1 forget_task 重复 forget =====


def test_m28_1_forget_task_repeat_rejected():
    """[M28.1] 第二次 forget_task 抛 TaskAlreadyForgotten (不写 audit_log 假绿)."""
    _setup()
    tid = _create_task("m28-forget-dup")
    m = memory.Memory()
    try:
        # 第一次成功
        r1 = task_states.forget_task(m._conn, tid, reason="first")
        assert r1["task_id"] == tid
        # 第二次应抛错 (不是静默返回 rows_invalidated=0)
        try:
            task_states.forget_task(m._conn, tid, reason="second")
            raise AssertionError("second forget 应抛错")
        except task_states.TaskLoopError as e:
            assert e.code == "TaskAlreadyForgotten"
        # 校验 audit_log 只写了 1 条 forced_forget (不是 2 条)
        rows = m._conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='forced_forget' AND ref_id=?""",
            (tid,),
        ).fetchall()
        assert len(rows) == 1, f"应有 1 条 audit_log, got {len(rows)}"
    finally:
        m.close()


def test_m28_1b_forget_loop_repeat_rejected():
    _setup()
    lid = _create_loop("m28-forget-loop-dup")
    m = memory.Memory()
    try:
        task_states.forget_loop(m._conn, lid, reason="first")
        try:
            task_states.forget_loop(m._conn, lid, reason="second")
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "LoopAlreadyForgotten"
    finally:
        m.close()


# ===== M28.2 propose apply 后再提议 =====


def test_m28_2_propose_after_apply_re_proposes():
    """[M28.2] 同一 task 被 propose → apply → 仍 stale 时, 第二次 propose 应提议.

    旧 bug: append-only 让原 proposal 行永远 status='proposed',
    第二次 propose 走 skipped_existing 永远跳过, stuck_task 检测静默失效.
    修后: NOT EXISTS (stale_resolved/applied) 子查询, apply 后允许重新提议.
    """
    _setup()
    # 建 11 天前 open task (用 NOW_REF 跟 _create_task 对齐)
    back = (_dt.now() - _td(days=11)).isoformat(timespec="milliseconds")
    m = memory.Memory()
    try:
        r = task_states.task_create(m._conn, name="m28-repropose", now=back)
        tid = r["task_id"]
        m._conn.commit()

        # 第一次 propose
        s1 = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        assert any(p["task_id"] == tid for p in s1["proposals"])

        # 找 proposal_id + apply (用 'ignored_will_revisit' 模拟用户忽略但没转移)
        row = m._conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND status='proposed' AND ref_id=?""",
            (tid,),
        ).fetchone()
        pid = row[0]
        task_states.apply_stale_proposal(
            m._conn,
            pid,
            applied_action="ignored_will_revisit_later",
        )

        # 第二次 propose — 旧逻辑会跳, 新逻辑应再提议 (因为 apply 后 task 仍 stale)
        s2 = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        # 关键断言: 本 task 应在 s2['proposals'] 中 (不被旧 pending 永久跳过)
        proposed_ids = [p["task_id"] for p in s2["proposals"]]
        err_msg = f"M28.2 fix: apply 后第二次 propose 应再提议本 task, got {proposed_ids}"
        assert tid in proposed_ids, err_msg
    finally:
        m.close()


# ===== M28.3 memory.forget 死代码 + docstring 静态契约 =====


def test_m28_3_memory_forget_no_confirm_placeholder():
    """[M28.3] memory.forget() 不应有 confirm_forget 占位变量, docstring 应跟实现对齐."""
    src = _inspect.getsource(memory.Memory.forget)
    # 修后: 无 confirm_forget 占位
    assert "confirm_forget" not in src, "死代码 confirm_forget 应已删除"
    # docstring 应明确: 一律拦截, 无 escape hatch
    assert "D11 TTL 豁免" in src
    assert "task_states.forget_task" in src
    assert "task_states.forget_loop" in src
    # 不应再有「显式 confirm」之类的描述
    msg = "docstring 仍误导 — 应改为 '一律拦截, 显式删除走 task_states.forget_task/loop'"
    assert "显式 confirm_forget=True 才接受" not in src, msg
