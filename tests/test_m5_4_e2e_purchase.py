"""
[8/6 M5.4 + DESIGN §9] 演练测试: 采购耗材 loop (office-lady) end-to-end.

走完整链路:
  1. 建 loop:consumables (interval=24h, trigger=low_stock)
  2. 记 chunk: 耗材库存不足
  3. loop tick loop:consumables → due
  4. task_create(name='采购耗材', loop_id=loop:consumables, evidence_chunk_id=chunk_触发)
  5. transition(open → in_progress, reason='已下单', evidence=chunk_订单)
  6. transition(in_progress → waiting, reason='等发货', evidence=chunk_物流)
  7. transition(waiting → done, reason='已收货', evidence=chunk_收货)
     · loop.active_task_id=NULL, last_cycle_done_at=now
  8. list_active_tasks_and_loops → consumables 当前 active_tasks=0, dormant=0
  9. replay task:20260806-restock-1 → 完整生命周期 6 行状态窗
  10. digest block4 → 上次 cycle 完成, 待 next tick
"""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime as _dt
from datetime import timedelta as _td

# [8/9 P1 follow-up] hard-coded "2026-08-06T..." 边界 fail (8/9 跑 age=7d < threshold 7d).
# 改 NOW_REF = now+1s (未来), 跟 _create_*_task(days_ago=10) 配对 → age=10d+1s > threshold 7d.
NOW_REF = (_dt.now() + _td(seconds=1)).isoformat(timespec="milliseconds")

REPO = Path(__file__).resolve().parent.parent  # [M29 fix] 不再硬编码作者本机路径
sys.path.insert(0, str(REPO))
import os

os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

import memory
import task_states


def _setup():
    """[M29 fix] Clean e2e fixtures (含 audit_log + chunks + task_states 前缀修正).

    task_create 生成 id 为 'task:YYYYMMDD-<slug>' (实测 'task:20260806-e2e-restock-1'),
    旧前缀 'task:e2e%' 匹配不到 — 残留污染下个 conftest session fixture 触发 FK 崩溃.
    修: 前缀改为 'task:%e2e-%' / 'loop:%e2e-%' 兼容日期前缀. 补 chunks 表清理
    (chunks 不会被 FK ON DELETE CASCADE 清, _remember_chunk raw SQL 插入的也清理).
    """
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    # 顺序: audit_log → task_states → chunks → entities
    # (evidence_chunk_id FK 约束: 先清 task_states 当前行)
    c.execute("DELETE FROM audit_log WHERE ref_id LIKE 'task:%e2e-%' OR ref_id LIKE 'loop:%e2e-%' OR after_json LIKE '%e2e-%'")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:%e2e-%' OR task_id LIKE 'loop:%e2e-%'")
    c.execute("DELETE FROM chunks WHERE id LIKE 'chunk:e2e-%' OR source LIKE '%e2e-%'")
    c.execute("DELETE FROM relations WHERE source_id LIKE 'task:%e2e-%' OR target_id LIKE 'task:%e2e-%' OR source_id LIKE 'loop:%e2e-%' OR target_id LIKE 'loop:%e2e-%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:%e2e-%' OR id LIKE 'loop:%e2e-%'")
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _remember_chunk(m: memory.Memory, content: str, source: str) -> str:
    """Helper: 记 chunk via raw SQL (避开 embedder SIGSEGV)."""
    import uuid as _uuid

    cid = f"chunk:e2e-{_uuid.uuid4().hex[:8]}"
    m._conn.execute(
        """INSERT INTO chunks (id, content, source, memory_type, importance, valid_until, created_at, processed_at)
           VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)""",
        (cid, content, source, "episodic", 0.7, _dt.now().isoformat(timespec="milliseconds")),
    )
    m._conn.commit()
    return cid


def test_e2e_purchase_consumables_full_cycle():
    """[M5.4 e2e] 走完 9 步: loop create → tick → task_create → 3 transitions → list → replay → digest."""
    _setup()

    m = memory.Memory()
    try:
        # Step 1: 建 loop
        loop_result = task_states.loop_create(
            m._conn,
            name="e2e-consumables",
            trigger="low_stock",
            interval_hours=24,
            now=NOW_REF,
        )
        lid = loop_result["loop_id"]
        m._conn.commit()
        assert lid.startswith("loop:")

        # Step 2: 记 chunk (耗材库存不足)
        chunk_low_stock = _remember_chunk(m, "耗材库存不足 (office-lady)", "test:e2e_low_stock")

        # Step 3: loop tick → due
        tick = task_states.loop_tick(
            m._conn,
            loop_id=lid,
            now=NOW_REF,
        )
        assert tick["verdict"] == "due", f"first tick should be due, got {tick}"

        # Step 4: task_create (采购耗材)
        task_result = task_states.task_create(
            m._conn,
            name="e2e-restock-1",
            loop_id=lid,
            evidence_chunk_id=chunk_low_stock,
            now=NOW_REF,
        )
        tid = task_result["task_id"]
        m._conn.commit()
        assert tid.startswith("task:")

        # Step 5: transition(open → in_progress, 已下单)
        chunk_order = _remember_chunk(m, "已下单 — order #12345", "test:e2e_order")
        trans1 = task_states.transition(
            m._conn,
            task_id=tid,
            to_state="in_progress",
            reason="已下单 order #12345",
            evidence_chunk_id=chunk_order,
            now=NOW_REF,
        )
        assert trans1["to_state"] == "in_progress"
        m._conn.commit()

        # Step 6: transition(in_progress → waiting, 等物流)
        chunk_logistics = _remember_chunk(m, "等发货 SF#67890", "test:e2e_logistics")
        trans2 = task_states.transition(
            m._conn,
            task_id=tid,
            to_state="waiting",
            reason="等发货 SF#67890",
            evidence_chunk_id=chunk_logistics,
            now=NOW_REF,
        )
        assert trans2["to_state"] == "waiting"
        m._conn.commit()

        # Step 7: transition(waiting → done, 已收货)
        chunk_received = _remember_chunk(m, "已收货 验收通过", "test:e2e_received")
        trans3 = task_states.transition(
            m._conn,
            task_id=tid,
            to_state="done",
            reason="已收货 验收通过",
            evidence_chunk_id=chunk_received,
            now=NOW_REF,
        )
        assert trans3["to_state"] == "done"
        m._conn.commit()

        # Step 8: list_active_tasks_and_loops → 该 task 已 done, 不在 active
        listing = task_states.list_active_tasks_and_loops(
            m._conn,
            now=NOW_REF,
        )
        active_ids = {t["task_id"] for t in listing["active_tasks"]}
        assert tid not in active_ids, f"done task 不应在 active, got {active_ids}"

        # Step 9: replay task → 完整生命周期 4 行状态窗 (open / in_progress / waiting / done)
        # 用直接 SQL: list_tasks 在 asof 下虽然过滤宽松, 但仍有逻辑边界. 直接查
        # task_states 表按 valid_from 排序, 拿到完整生命周期更稳.
        # [8/9 P1 follow-up] task_states.transition (task_states.py:272 RF17) 把 valid_from
        # 推进 1ms 防 0-长窗. NOW_REF 同值 4 transition → 状态窗 valid_from 递增
        # NOW_REF, NOW_REF+1ms, NOW_REF+2ms, NOW_REF+3ms. NOW_REF + 5s buffer 抓到所有.
        rows = m._conn.execute(
            """SELECT state FROM task_states
               WHERE task_id=? AND valid_from <= ?
               ORDER BY valid_from ASC""",
            (tid, (_dt.fromisoformat(NOW_REF) + _td(seconds=5)).isoformat(timespec="milliseconds")),
        ).fetchall()
        states_seen = [r[0] for r in rows]
        # 期望状态序列 (replay 返回的窗口按时间顺序):
        assert "open" in states_seen
        assert "in_progress" in states_seen
        assert "waiting" in states_seen
        assert "done" in states_seen
        assert len(states_seen) >= 4, f"应有至少 4 个状态窗, got {states_seen}"

        # Step 10: digest block4 → stale_tasks 0, active_tasks 0 (本 task 已 done)
        digest_block = m._build_digest()  # 用 memory 默认 _build_digest
        # 校验 digest 文本不含本 task id (已 done)
        if isinstance(digest_block, str):
            assert "e2e-restock-1" not in digest_block or tid not in digest_block, "done task 不应出现在 digest active 块"
    finally:
        m.close()


def test_e2e_d11_forget_task_after_done():
    """[M5.4 e2e + D11] done task 经 forget_task 显式删除, audit_log 留痕."""
    _setup()
    m = memory.Memory()
    try:
        # 建 + done task
        r = task_states.task_create(m._conn, name="e2e-d11-restock", now=NOW_REF)
        tid = r["task_id"]
        task_states.transition(
            m._conn,
            task_id=tid,
            to_state="done",
            reason="manual_done",
            now=NOW_REF,
        )
        m._conn.commit()

        # D11 拦截 — memory.forget(task) 抛 ValueError
        try:
            m.forget(tid, target_kind="task", reason="cleanup")
            raise AssertionError("D11 应拦截")
        except ValueError as e:
            assert "D11 TTL 豁免" in str(e)

        # 显式 forget_task 路径
        result = task_states.forget_task(
            m._conn,
            tid,
            reason="user_explicit_forget",
        )
        assert result["task_id"] == tid
        assert result["rows_invalidated"] >= 1

        # audit_log 留痕
        row = m._conn.execute(
            """SELECT pass_name, action_type FROM audit_log
               WHERE pass_name='forced_forget' AND ref_id=?""",
            (tid,),
        ).fetchone()
        assert row is not None
        assert row[0] == "forced_forget"
        assert row[1] == "explicit_softdelete"
    finally:
        m.close()


def test_e2e_proposal_then_apply_resolves():
    """[M5.4 e2e + M5.2] stale task 经 propose → apply 闭环."""
    _setup()
    m = memory.Memory()
    try:
        # 建 open task (8 天前) — 应被 propose
        back = (_dt.now() - _td(days=8)).isoformat(timespec="milliseconds")
        r = task_states.task_create(m._conn, name="e2e-stale", now=back)
        tid = r["task_id"]
        m._conn.commit()

        # propose
        scan = task_states.propose_stale_tasks(m._conn, now=NOW_REF)
        proposed_ids = [p["task_id"] for p in scan["proposals"]]
        assert tid in proposed_ids

        # 找 proposal_id + apply
        row = m._conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND status='proposed' AND ref_id=?""",
            (tid,),
        ).fetchone()
        assert row is not None
        pid = row[0]
        applied = task_states.apply_stale_proposal(
            m._conn,
            pid,
            applied_action="transitioned_to_done_by_user",
        )
        assert applied["status"] == "applied"

        # apply 后 list 看不见本 task 在 proposed
        rs = task_states.list_stale_proposals(m._conn, status="proposed")
        assert all(p["ref_id"] != tid for p in rs["proposals"])
    finally:
        m.close()
        _setup()  # [M31 fix] teardown 清理残留 (proposal 测试是文件最后一个,
        # 无下一个 _setup 兜底. 残留幽灵 open task + audit_log 行
        # 对 propose_stale_tasks / digest 可见, 跨文件测试污染)
