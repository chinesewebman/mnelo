# === task_states_stale.py — Stale task proposal pipeline ===
# [refactor 2026-08-12] 拆分自 task_states.py — 见 task_states_core.py 顶部注释.
# DESIGN §5.5 stale pipeline: propose → apply (with idempotent supercession).
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("mnelo.task_states")

# [refactor 2026-08-12] 跨模块依赖从 task_states_core 取 — 见 task_states_loop.py 同款注释.
from task_states_core import (  # noqa: E402
    TaskLoopError,
    _default_now,
)


def propose_stale_tasks(
    conn: Any,
    *,
    now: Optional[str] = None,
    stale_days_threshold: Optional[int] = None,  # [8/9 P1-yanru] 默认从 config.task_stale_days_threshold 读
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """[8/6 M5.2 + DESIGN TASK_LOOP 4.4 + M30/M32] 扫描 stale task, 写 audit_log Proposal.

    走 audit_log 现有表 (pass_name='stuck_task', status='proposed'). 跟 M5.1
    loop_tick_cron 提议 due loop 同模式, 但 pass_name 不同, 便于 L2 audit
    区分. mnelo 绝不自主转移任务 (D5) - Proposal 只做提议, 真正的 transition
    必须由 agent/用户经 memory_task_transition 显式 apply.

    收缩分桶 (DESIGN TASK_LOOP 4.4 D7):
      open > 7d       -> stale, 提议 review
      waiting > 14d   -> stale, 提议 review
      blocked > 3d    -> stale, 提议 review
      in_progress     -> stale_days_threshold (默认 7d) 单桶

    [M30 fix] 输入校验 - stale_days_threshold 必须正整数. 负数 / 0 / 非 int
    会让所有 task 触发 stale 或永不触发, 后果严重. 早抛错优于静默.

    Args:
        conn: open sqlite3.Connection.
        now: 时间参考 (默认 = 当前).
        stale_days_threshold: 通用 fallback (默认 7d). 上面分桶覆盖更具体.
        run_id: 自定义 run_id (默认 = 'stuck_task-<now>-<uuid8>').

    Returns:
        {
            "run_id": str,
            "scanned": int,
            "proposed": int,
            "skipped_existing": int,
            "proposals": [{"task_id", "state", "age_days", "threshold_days", "is_stale"}],
        }
    """
    # [8/9 P1-yanru] stale_days_threshold fallback — None 时从 config 读 (必须在 M30 校验前)
    if stale_days_threshold is None:
        from config import config as _cfg

        stale_days_threshold = _cfg.task_stale_days_threshold

    if not isinstance(stale_days_threshold, int) or stale_days_threshold < 1:
        raise TaskLoopError(
            f"stale_days_threshold 必须正整数, got {stale_days_threshold!r}",
            field="stale_days_threshold",
            code="InvalidThreshold",
        )

    import uuid as _uuid
    from datetime import datetime as _dt

    now_ts = now or _default_now()
    try:
        ref_now = _dt.fromisoformat(now_ts)
    except (ValueError, TypeError):
        ref_now = _dt.fromisoformat(_default_now())

    # 阈值桶 (DESIGN TASK_LOOP 4.4 D7)
    thresholds = {
        "open": 7,
        "waiting": 14,
        "blocked": 3,
        "in_progress": stale_days_threshold,
    }

    # 1. 扫 active task
    rows = conn.execute(
        """SELECT t.task_id, t.state, t.valid_from, t.reason, t.evidence_chunk_id
           FROM task_states t
           JOIN entities e ON e.id = t.task_id
           WHERE t.valid_until IS NULL
             AND t.state NOT IN ('done', 'cancelled')
             AND e.kind = 'task'"""
    ).fetchall()

    proposals = []
    skipped = 0
    for r in rows:
        state = r[1]
        threshold = thresholds.get(state, stale_days_threshold)
        try:
            vf = _dt.fromisoformat(r[2])
            age_days = (ref_now - vf).total_seconds() / 86400.0
        except (ValueError, TypeError):
            age_days = 0.0
        if age_days < threshold:
            continue
        # [8/6 M28 fix] 去重 - 排除已有未 resolved 的 Proposal.
        existing = conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task'
                 AND ref_type='task' AND ref_id=?
                 AND NOT EXISTS (
                     SELECT 1 FROM audit_log al2
                     WHERE al2.pass_name='stuck_task'
                       AND al2.action_type='stale_resolved'
                       AND al2.status='applied'
                       AND al2.ref_id=?
                 )""",
            (r[0], r[0]),
        ).fetchone()
        if existing:
            skipped += 1
            continue
        proposals.append(
            {
                "task_id": r[0],
                "state": state,
                "age_days": round(age_days, 2),
                "threshold_days": threshold,
                "is_stale": True,
                "last_reason": r[3],
            }
        )

    # 2. 写 audit_log Proposal
    rid = run_id or f"stuck_task-{now_ts}-{_uuid.uuid4().hex[:8]}"
    for p in proposals:
        conn.execute(
            """INSERT INTO audit_log (
                run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                "stuck_task",
                "stale_review",
                "task",
                p["task_id"],
                None,
                json.dumps(
                    {
                        "state": p["state"],
                        "age_days": p["age_days"],
                        "threshold_days": p["threshold_days"],
                        "proposed_at": now_ts,
                        "prompt": (
                            f"task {p['task_id']} 处于 {p['state']} 状态 {p['age_days']:.1f} 天, 超过阈值 {p['threshold_days']} 天. 建议: transition 到 done / cancelled / 下一步, 或更新 reason."
                        ),
                    },
                    ensure_ascii=False,
                ),
                0.7,
                "proposed",
                now_ts,
            ),
        )
    conn.commit()
    return {
        "run_id": rid,
        "scanned": len(rows),
        "proposed": len(proposals),
        "skipped_existing": skipped,
        "proposals": proposals,
    }


def apply_stale_proposal(
    conn: Any,
    proposal_id: int,
    *,
    applied_action: str,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    # [M5.2 fix] UNIQUE(run_id, pass_name, action_type, ref_id, status) 约束
    # proposal_id 全局唯一, 自带 part of run_id 防并发 apply 碰撞.
    """[8/6 M5.2 + DESIGN §4.4] 标记 stuck_task Proposal 为 applied (用户/agent 评估后).

    走 audit_log 现有 status 字段 ('proposed' → 'applied'). 不自主转移任务,
    只记录人类决策. 真正的 transition 已经由 agent 经 memory_task_transition
    显式发起 (跟 status 解耦).

    Args:
        conn: open sqlite3.Connection.
        proposal_id: audit_log row id (propose_stale_tasks 写入行).
        applied_action: 文字描述用户做了啥 (e.g. 'transitioned to done',
                        'ignored', 'will_revisit_at_2026-08-10').
        now: 时间参考 (默认 = 当前).

    Returns:
        {"proposal_id": int, "status": "applied", "ref_id": str, "applied_action": str}
    """
    now_ts = now or _default_now()
    # [M30 fix] race condition — 旧实现 check + insert 分两步, 两 agent 并发
    # apply 同 proposal_id 都过 check + 双写 audit_log (UNIQUE run_id 不同不冲突).
    # 修: SQLite WAL + 显式 BEGIN IMMEDIATE 拿写锁, 整个 check+insert 原子化.
    # 应用层幂等性: 仍保留 stale_resolved/applied 行检测 (重复 apply 显式报错).
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, pass_name, status, ref_id FROM audit_log WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"proposal_id={proposal_id} 不存在",
                field="proposal_id",
                code="ProposalNotFound",
            )
        if row[1] != "stuck_task":
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"proposal_id={proposal_id} 不是 stuck_task Proposal (pass_name={row[1]})",
                field="proposal_id",
                code="ProposalMismatch",
            )
        # [M5.2 fix] 校验是否已 applied — 同 ref_id 已有 stale_resolved 行
        resolved = conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND action_type='stale_resolved'
                 AND ref_id=? AND status='applied'""",
            (row[3],),
        ).fetchone()
        if resolved:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"ref_id={row[3]} 已被 applied (resolved_id={resolved[0]}), 不能重复",
                field="proposal_id",
                code="ProposalAlreadyResolved",
            )
        # [M5.2 fix] UNIQUE(run_id, pass_name, action_type, ref_id, status) 约束
        # run_id 跟原 Proposal 不同 (含 proposal_id 区分), status='applied' 跟原
        # 'proposed' 自然错开. 整个 check+insert 在 BEGIN IMMEDIATE 事务里, 并发
        # apply 会被 SQLite 序列化, 第二个拿到锁时 resolved check 命中失败抛错.
        conn.execute(
            """INSERT INTO audit_log (
                run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"resolved-{now_ts}-{proposal_id}",
                "stuck_task",
                "stale_resolved",
                "task",
                row[3],
                json.dumps({"original_proposal_id": proposal_id, "status": "proposed"}, ensure_ascii=False),
                json.dumps({"applied_action": applied_action, "resolved_at": now_ts}, ensure_ascii=False),
                1.0,
                "applied",
                now_ts,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        # 兜底: 任何异常回滚 (BEGIN IMMEDIATE 没 COMMIT 必须 ROLLBACK, 否则 SQLite 锁死)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {
        "proposal_id": proposal_id,
        "status": "applied",
        "ref_id": row[3],
        "applied_action": applied_action,
    }


def list_stale_proposals(
    conn: Any,
    *,
    status: str = "proposed",
    limit: int = 50,
) -> Dict[str, Any]:
    """[8/6 M5.2 + 8/6 fix + M34 fix] 列 stuck_task Proposal (DESIGN §4.4 上浮).

    [8/6 fix] status='proposed' 过滤排除已 resolved 的 Proposal (audit_log 中同
    ref_id 有 stale_resolved 行) — 让 propose/apply 闭环后 list_stale_proposals
    不再返回本 Proposal.

    [M34 fix] status 白名单 - 拒绝 invalid 状态. 旧实现任意字符串都过 else 分
    支当 SQL 参数, 静默返 'unfiltered' 行迷惑 caller. 显式抛错.

    Args:
        conn: open sqlite3.Connection.
        status: 'proposed' / 'applied' / 'reverted' / 'all'.
        limit: 上限.

    Returns:
        {"proposals": [{"id", "run_id", "ref_id", "action_type",
                        "after_json", "status", "created_at"}], "count": int}

    Raises:
        TaskLoopError: status 不在白名单.
    """
    if status not in ("proposed", "applied", "reverted", "all"):
        raise TaskLoopError(
            f"status {status!r} 不在白名单 (proposed/applied/reverted/all)",
            field="status",
            code="InvalidStatusError",
        )
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise TaskLoopError(
            f"limit 必须 1-1000 整数, got {limit!r}",
            field="limit",
            code="InvalidLimitError",
        )
    if status == "proposed":
        # 排除已 resolved (有 stale_resolved 行的 ref_id)
        rows = conn.execute(
            """SELECT id, run_id, action_type, ref_id, after_json, status, created_at
               FROM audit_log
               WHERE pass_name='stuck_task' AND action_type='stale_review'
                 AND status='proposed'
                 AND ref_id NOT IN (
                     SELECT ref_id FROM audit_log
                     WHERE pass_name='stuck_task' AND action_type='stale_resolved'
                       AND status='applied'
                 )
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    elif status == "all":
        # [M34 fix] 'all' 白名单 - 审计追溯需要看所有阶段 (无 not-resolved 过滤)
        rows = conn.execute(
            """SELECT id, run_id, action_type, ref_id, after_json, status, created_at
               FROM audit_log
               WHERE pass_name='stuck_task'
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, run_id, action_type, ref_id, after_json, status, created_at
               FROM audit_log
               WHERE pass_name='stuck_task' AND status=?
               ORDER BY id DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    proposals = []
    for r in rows:
        try:
            after = json.loads(r[4]) if r[4] else None
        except (ValueError, TypeError):
            after = None
        proposals.append(
            {
                "id": r[0],
                "run_id": r[1],
                "action_type": r[2],
                "ref_id": r[3],
                "after_json": after,
                "status": r[5],
                "created_at": r[6],
            }
        )
    return {"proposals": proposals, "count": len(proposals)}


# [M30.3 fix] digest block 4 输出上限 — DESIGN §4.4 + README 契约: digest
# 500-2000 字符. block 4 是 digest 一部分, 单独应 ≤2000 chars. 大量 stale
# task 会让 block 4 撑爆 digest 注入 agent 上下文, 必须截断.
