"""
[8/6 M30 review-pass] 3 个多角度优化测试.

覆盖:
  M30.1 [race fix] apply_stale_proposal 并发安全 — BEGIN IMMEDIATE 事务, 双
       并发 apply 第二个应抛 ProposalAlreadyResolved (而不是双写 audit_log)
  M30.2 [validation] propose_stale_tasks stale_days_threshold 必须正整数,
       负数 / 0 / 非 int 抛 InvalidThreshold
  M30.3 [digest contract] render_digest_block4 输出截断到 ≤2000 字符 (digest
       injection 契约, 防止 Agent context overflow)
"""

import sqlite3
import sys
from pathlib import Path
from datetime import datetime as _dt
from datetime import timedelta as _td

# [8/9 P1 follow-up] hard-coded "2026-08-06T15:00" 边界 fail (8/9 跑 age=7d < threshold 7d).
# 改 NOW_REF = now+1s (未来), 跟 _create_stale_task(days_ago=10) 配对 → age=10d+1s > threshold 7d.
NOW_REF = (_dt.now() + _td(seconds=1)).isoformat(timespec="milliseconds")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import os

os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

import memory
import task_states


def _setup():
    """[M32.1 fix] task_states/entities 清理前缀加日期通配符 '%m30-%' / '%m32-%'.

    task_create 生成的 id 形如 'task:YYYYMMDD-<slug>', 例如
    'task:20260727-m30-race-dup'. 旧 'task:m30-%' 永远不命中 — 日期前缀
    夹在 'task:' 与 'm30' 之间. 跨测试泄漏 ghost open task + 破坏
    test_m4_digest_block4 (counts.active_tasks 断言失败).

    [M32.4 fix] 同步清 m32-* — 并发测试需要全新 task, 不能有 resolved
    audit_log 行残留 (会触发 ProposalAlreadyResolved 阻断两个 apply).
    """
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:%m30-%' OR task_id LIKE 'loop:%m30-%' OR task_id LIKE 'task:%m32-%' OR task_id LIKE 'loop:%m32-%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:%m30-%' OR id LIKE 'loop:%m30-%' OR id LIKE 'task:%m32-%' OR id LIKE 'loop:%m32-%'")
    c.execute(
        "DELETE FROM audit_log WHERE (pass_name='stuck_task' OR pass_name='forced_forget') AND (ref_id LIKE 'task:%m30-%' OR ref_id LIKE 'loop:%m30-%' OR ref_id LIKE 'task:%m32-%' OR ref_id LIKE 'loop:%m32-%')"
    )
    c.execute("DELETE FROM chunks WHERE id LIKE 'chunk:m30-%' OR id LIKE 'chunk:m32-%' OR source LIKE '%m30-%' OR source LIKE '%m32-%'")
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _create_stale_task(name: str, days_ago: int = 10) -> str:
    """建 open task, valid_from backdated."""
    m = memory.Memory()
    try:
        back = (_dt.now() - _td(days=days_ago)).isoformat(timespec="milliseconds")
        r = task_states.task_create(m._conn, name=name, now=back)
        tid = r["task_id"]
        m._conn.commit()
        return tid
    finally:
        m.close()


# ===== M30.1 race condition =====


def test_m30_1_apply_double_resolved_check_atomic():
    """[M30.1] apply_stale_proposal 重复 apply 应抛 ProposalAlreadyResolved.

    旧 bug: check + insert 非原子, 两并发 apply 第二个 INSERT UNIQUE 不冲突
    (run_id 含 proposal_id 不同). M30 修后: BEGIN IMMEDIATE 事务, SQLite
    序列化, 第二个 check 命中 stale_resolved/applied 抛错.
    """
    _setup()
    tid = _create_stale_task("m30-race-dup")

    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        # propose
        result = task_states.propose_stale_tasks(c, now=NOW_REF)
        pid = None
        for p in result["proposals"]:
            if p["task_id"] == tid:
                # find audit_log id
                row = c.execute(
                    """SELECT id FROM audit_log
                       WHERE pass_name='stuck_task' AND ref_id=? AND status='proposed'""",
                    (tid,),
                ).fetchone()
                pid = row[0]
                break
        assert pid is not None

        # 第一次 apply 成功
        applied1 = task_states.apply_stale_proposal(
            c,
            pid,
            applied_action="first_apply",
        )
        assert applied1["status"] == "applied"

        # 第二次 apply 应抛 ProposalAlreadyResolved
        try:
            task_states.apply_stale_proposal(
                c,
                pid,
                applied_action="second_apply_attempt",
            )
            raise AssertionError("second apply should raise")
        except task_states.TaskLoopError as e:
            assert e.code == "ProposalAlreadyResolved", f"expected ProposalAlreadyResolved, got {e.code}"

        # 校验 audit_log 只 1 行 stale_resolved/applied (不双写)
        n = c.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE pass_name='stuck_task' AND action_type='stale_resolved'
                 AND ref_id=? AND status='applied'""",
            (tid,),
        ).fetchone()[0]
        assert n == 1, f"应有 1 行 stale_resolved/applied, got {n}"
    finally:
        c.close()


def test_m30_1b_apply_second_proposal_blocked_by_resolved():
    """[M30.1b] 同一 task 第二次 apply 应被 ProposalAlreadyResolved 拒绝.

    M28.1 设计: 一旦 ref_id 走 applied, 后续所有 proposal_id 的 apply 都拒绝.
    不允许多次 apply (stale 状态可重新 propose, 但已 resolved 锁定).
    测试 M30 + M28.1 协作.
    """
    _setup()
    tid = _create_stale_task("m30-race-distinct")

    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        # 第一次 propose + apply
        r1 = task_states.propose_stale_tasks(c, now=NOW_REF)
        row1 = c.execute(
            "SELECT id FROM audit_log WHERE pass_name='stuck_task' AND ref_id=? AND status='proposed'",
            (tid,),
        ).fetchone()
        assert row1 is not None
        pid1 = row1[0]
        task_states.apply_stale_proposal(c, pid1, applied_action="ignored_first_time")

        # 第二次 propose (M28 fix: apply 后允许再提议)
        r2 = task_states.propose_stale_tasks(c, now=NOW_REF)
        proposed_ids = [p["task_id"] for p in r2["proposals"]]
        assert tid in proposed_ids, f"M28: apply 后应再提议, got {proposed_ids}"

        # 找第二次 proposal_id (新 proposed 行, max id)
        rows_all = c.execute(
            "SELECT id FROM audit_log WHERE pass_name='stuck_task' AND ref_id=? AND status='proposed'",
            (tid,),
        ).fetchall()
        pid2 = max(r[0] for r in rows_all)
        assert pid2 != pid1, f"second proposal id 应不同, got {pid2}"

        # 第二次 apply 应被 M28.1 + M30 双重拒绝: ref_id 已 applied, 任何
        # proposal_id 都不能再 apply (即便 proposal_id 本身没被 resolved).
        try:
            task_states.apply_stale_proposal(c, pid2, applied_action="second_apply")
            raise AssertionError("second proposal apply 应抛 ProposalAlreadyResolved")
        except task_states.TaskLoopError as e:
            assert e.code == "ProposalAlreadyResolved"
    finally:
        c.close()


# ===== M30.2 input validation =====


def test_m30_2_propose_rejects_invalid_threshold():
    """[M30.2] propose_stale_tasks 拒绝负数 / 0 / 非 int threshold."""
    _setup()
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        # 负数
        try:
            task_states.propose_stale_tasks(c, stale_days_threshold=-1)
            raise AssertionError("negative threshold should raise")
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidThreshold"

        # 0
        try:
            task_states.propose_stale_tasks(c, stale_days_threshold=0)
            raise AssertionError("zero threshold should raise")
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidThreshold"

        # 字符串
        try:
            task_states.propose_stale_tasks(c, stale_days_threshold="7")  # type: ignore[arg-type]
            raise AssertionError("string threshold should raise")
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidThreshold"

        # float 仍走 int 校验
        try:
            task_states.propose_stale_tasks(c, stale_days_threshold=7.5)  # type: ignore[arg-type]
            raise AssertionError("float threshold should raise")
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidThreshold"

        # 正整数 OK
        result = task_states.propose_stale_tasks(c, stale_days_threshold=14)
        assert "proposed" in result
    finally:
        c.close()


# ===== M30.3 digest truncation =====


def test_m32_4_concurrent_apply_only_one_succeeds():
    """[M32.4] 真实并发 (双连接) apply 同 proposal_id, 应只有 1 成功.

    旧 M30.1 是同连接顺序双 apply, 只能验幂等. 本测开双 sqlite3 连接 +
    threading 双线程并发, 验 BEGIN IMMEDIATE 事务在跨连接 write-lock 下的
    序列化. 期望:
      - 1 个 apply 成功 (status=applied)
      - 1 个 apply 抛 TaskLoopError ProposalAlreadyResolved
      - audit_log 只 1 行 stale_resolved/applied (不双写)
    """
    import threading
    import time as _time

    _setup()
    tid = _create_stale_task("m32-concurrent")

    c1 = sqlite3.connect(str(memory.DB_PATH), timeout=30)
    try:
        # propose (用 c1, 让 audit_log id 一致)
        result = task_states.propose_stale_tasks(c1, now=NOW_REF)
        # 找本 task 的 proposal_id
        row = c1.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND ref_id=? AND status='proposed'""",
            (tid,),
        ).fetchone()
        assert row is not None
        pid = row[0]

        # 双连接并发 apply (c1 vs c2)
        # [M32.4 fix] sqlite3.Connection 默认 thread-local, 子线程内必须
        # 自己 connect (check_same_thread=False 也行, 但每线程独立连接更
        # 接近真实并发场景). BEGIN IMMEDIATE 拿写锁的序列化行为在这模式下成立.
        results = {"a": None, "b": None}
        errors = {"a": None, "b": None}

        def worker(name: str):
            conn = sqlite3.connect(str(memory.DB_PATH), timeout=30)
            try:
                # sleep 0 让两线程竞争锁
                _time.sleep(0.01)
                r = task_states.apply_stale_proposal(
                    conn,
                    pid,
                    applied_action=f"from_{name}",
                )
                results[name] = r
            except task_states.TaskLoopError as e:
                errors[name] = e.code
            finally:
                conn.close()

        ta = threading.Thread(target=worker, args=("a",))
        tb = threading.Thread(target=worker, args=("b",))
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)

        # 期望: 一个成功 + 一个 ProposalAlreadyResolved
        ok_count = sum(1 for k in ("a", "b") if results[k] is not None)
        err_count = sum(1 for k in ("a", "b") if errors[k] is not None)
        assert ok_count == 1, f"应 1 个 apply 成功, got {ok_count} ({results}, {errors})"
        assert err_count == 1, f"应 1 个 apply 抛错, got {err_count} ({results}, {errors})"
        # 错误码应为 ProposalAlreadyResolved
        assert errors["a"] == "ProposalAlreadyResolved" or errors["b"] == "ProposalAlreadyResolved"

        # 校验 audit_log 只 1 行 stale_resolved/applied (race fix 核心)
        n = c1.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE pass_name='stuck_task' AND action_type='stale_resolved'
                 AND ref_id=? AND status='applied'""",
            (tid,),
        ).fetchone()[0]
        assert n == 1, f"并发 apply 应只有 1 行 stale_resolved/applied, got {n}"
    finally:
        c1.close()


def test_m30_3_render_digest_block4_truncates_to_2000_chars():
    """[M30.3] render_digest_block4 输出截断到 ≤2000 字符 (digest 契约).

    DESIGN §4.4 + README: digest 500-2000 字符. Block 4 (未闭环) 是 digest 一
    部分, 单独应 ≤2000 chars (整体 digest 可能 block 1+2+3+4 一起 ≤2000).
    旧实现不截断 — 大量 stale task 会让 block 4 撑爆 digest, 注入 agent 上下文
    overflow. 修: 截断 + "..." 后缀.
    """
    _setup()  # [M32.1 fix] 清理 m30 跨测试残留 (fixture prefix 兼容日期)
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        c.execute("PRAGMA foreign_keys = OFF")
        # 清理 m30-digest 残留
        c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:m30-digest-%'")
        c.execute("DELETE FROM entities WHERE id LIKE 'task:m30-digest-%'")
        # 建 100 个超长 name 的 active task
        for i in range(100):
            long_name = "m30-digest-truncate-" + str(i) + "-" + "X" * 50
            tid = "task:m30-digest-" + str(i).zfill(4)
            c.execute(
                "INSERT OR IGNORE INTO entities (id, kind, name, properties_json, valid_until, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (tid, "task", long_name, "{}", NOW_REF, NOW_REF),
            )
            # [M32 fix] task_states.created_at NOT NULL 必填 (schema.sql H-1).
            c.execute(
                "INSERT OR IGNORE INTO task_states (task_id, state, valid_from, valid_until, reason, evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, NULL, ?)",
                (tid, "open", NOW_REF, "task_create", NOW_REF),
            )
        c.commit()

        # 跑 list_active_tasks_and_loops + render_digest_block4
        active = task_states.list_active_tasks_and_loops(c, now=NOW_REF)
        text_lines, refs = task_states.render_digest_block4(active)
        # render_digest_block4 返回 list[str] — 拼成 string 测长度
        text_block = "\n".join(text_lines)

        # 断言 1: block 4 输出 ≤2000 chars
        n_active = len(active["active_tasks"])
        # [M32.3 fix] 校验 — live DB 可能含其他 stale task (e2e/m28/m5 残留),
        # 我们 100 个 m30-digest task 自身已足够触发 2000 chars 截断.
        # 不依赖 n_active 阈值, 直接断言:
        #   1. block 4 总长 ≤ 2000 chars (含末尾 "..." 截断)
        #   2. 截断后应以 "..." 结尾 (M30.3 显式 truncated flag 后置)
        assert len(text_block) <= 2000, "block 4 应 ≤2000 chars, got " + str(len(text_block))
        assert text_block.endswith("..."), "block 4 应被截断以 ... 结尾 (M30.3 truncation)"
    finally:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:m30-digest-%'")
        c.execute("DELETE FROM entities WHERE id LIKE 'task:m30-digest-%'")
        c.execute("PRAGMA foreign_keys = ON")
        c.commit()
        c.close()
