#!/usr/bin/env python3
"""l2_maintenance.py — L2MaintenanceMixin: L2 自主维护子系统 (DESIGN §5.2 + §5.7).

[refactor 2026-08-12] Memory 类 3853 行拆分 (PR #11 benchmarks 子包同样先例).
L2MaintenanceMixin 包含 11 个方法 + class-level constants:

  Constants:
    _L2_DEFAULTS / _MEMORY_TYPE_TTL_DAYS / _PROMOTE_*

  Methods:
    run_maintenance / _l2_get / _l2_set
    _run_hygiene_pass / _apply_decay_importance / _apply_ttl_soft_delete / _mark_skipped
    _run_promote_pass / _apply_promote_to_canonical / _apply_demote_canonical
    stats v2 (hygiene 版, 第 2 个 stats())

跨 mixin 依赖:
- self._conn (MemoryCore 提供)
- self._index (MemoryCore 提供)
- self._l2_get/_l2_set (本类内)
- self._run_audit_gc (AuditMixin 提供)
- self._exec_clean (AuditMixin 提供)
- self._apply_decay_importance / _apply_ttl_soft_delete / _run_hygiene_pass (本类内)
- self._run_promote_pass (本类内)
- self._apply_demote_canonical (本类内)
- self._run_audit_gc 被 run_maintenance 调用 (跨 mixin)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from memory import Memory  # noqa: F401  forward refs only — avoid circular at runtime

logger = logging.getLogger("mnelo")


class L2MaintenanceMixin:
    """[refactor 2026-08-12] L2 自主维护层 (DESIGN §5.7-5.9 + TASKS_L2_HYGIENE v0.2).

    L2 配置默认值 + TTL 规则按 memory_type. 落地 6 个 hygiene pass + 1 个 promote pass,
    共享核心: Proposal / Policy / Applier (DESIGN §5.2).

    入口:
        run_maintenance(passes=[...], dry_run=..., since=..., confirm_destructive=...)
    """

    # ============================================================
    # L2 配置默认值 (跟 DESIGN §5.7 config 模板一致; 实际读 meta 表)
    # ============================================================
    _L2_DEFAULTS: Dict[str, Any] = {
        "enabled": False,  # 全局默认 false, 显式开启
        "dry_run": True,  # 全局默认 dry-run
        "importance_floor": 0.1,  # hygiene pass 的 floor (§5.6)
        "caps": {"supersede": 20, "merge": 20, "purge": 50},
    }

    # TTL 规则按 memory_type (TASKS_L2_HYGIENE H3 §3 + DESIGN §3.0.5)
    _MEMORY_TYPE_TTL_DAYS: Dict[str, Optional[int]] = {
        "ephemeral": 7,
        "fact": 365,
        "preference": 180,
        "episode": 730,
        "decision": 730,
        "procedure": None,
    }

    # ============================================================
    # Promote / Demote 阈值 (DESIGN §5.2 P4 + TASKS_L2_HYGIENE H4)
    # ============================================================
    _PROMOTE_RECALL_THRESHOLD = 20
    _PROMOTE_REF_DEGREE_THRESHOLD = 10
    _PROMOTE_LONG_IMP_THRESHOLD = 0.8
    _PROMOTE_LONG_DAYS = 90
    _PROMOTE_DEMOTE_DAYS = 90
    _PROMOTE_DEMOTE_REF_THRESHOLD = 3
    _PROMOTE_MAX_CANONICAL = 50

    # ============================================================
    # [H-0 + H-1 8/4] L2 自主层基础设施 (DESIGN §5.7-5.9 + TASKS_L2_HYGIENE v0.2)
    # ============================================================

    # L2 配置项默认值 (跟 DESIGN §5.7 config 模板一致; 实际读 meta 表)
    _L2_DEFAULTS: Dict[str, Any] = {
        "enabled": False,  # 主人 §5.7: 全局默认 false, 显式开启
        "dry_run": True,  # 主人 §5.7: 全局默认 dry-run
        "importance_floor": 0.1,  # hygiene pass 的 floor (§5.6)
        "caps": {"supersede": 20, "merge": 20, "purge": 50},
    }

    # TTL 规则按 memory_type (TASKS_L2_HYGIENE H3 §3 + DESIGN §3.0.5)
    # 实际分布 (8/4 v0.2): fact 95.4% / procedure 3.4% / ephemeral 1.2%
    _MEMORY_TYPE_TTL_DAYS: Dict[str, Optional[int]] = {
        # ephemeral 7d: 实际 1.2% (草稿/临时, 主人 §3.0.5 + LLM 草稿衰减)
        "ephemeral": 7,
        # fact 365d: 实际 95.4% (事实/对话, 主人 §3.0.5 默认)
        "fact": 365,
        # preference 180d: 主人偏好 (实际 1 个, 但 schema 必备)
        "preference": 180,
        # episode 730d: 实际事件 (2年)
        "episode": 730,
        # decision 730d: 决策
        "decision": 730,
        # procedure 永久 (None = 不衰减)
        "procedure": None,
    }

    def _l2_get(self, key: str, default: Any = None) -> Any:
        """[H-1] 从 meta 表读 L2 配置项.

        Args:
            key: 'l2.enabled' / 'l2.dry_run' / 'l2.last_run.hygiene' 等
        """
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        v = row[0]
        # [fix 8/4] bool 解析优先 (否则 "0" / "1" 会被解析为 0.0 / 1.0)
        if v == "1":
            return True
        if v == "0":
            return False
        # [fix 8/4] 先 float (0.1 应解 float 不是 int)
        try:
            return float(v)
        except (ValueError, TypeError):
            pass
        try:
            return int(v)
        except (ValueError, TypeError):
            return v

    def _l2_set(self, key: str, value: Any) -> None:
        """[H-1] 写 L2 配置项到 meta 表."""
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        self._conn.commit()

    def run_maintenance(
        self,
        passes: Optional[List[str]] = None,
        dry_run: Optional[bool] = None,
        since: Optional[str] = None,
        confirm_destructive: bool = False,
    ) -> Dict:
        """[H-1 §5.7] L2 自主层入口. DESIGN §5.9 事务粒度 + watermark.

        Args:
            passes: ['hygiene', 'decay', 'ttl', 'purge', ...] (None = 全 enabled)
            dry_run: True/False/None (None = 用 meta.l2.dry_run 默认)
            since: ISO 时间戳, 仅处理 chunks WHERE created_at > since
            confirm_destructive: purge pass 需要 True 才真删 (§5.9.2)

        Returns:
            {passes_run, proposals: {pass_name: [proposal_dicts]},
             applied, skipped, failed, watermark_updated, gc_stats}
        """
        # 1. 校验 L2 是否启用
        enabled = self._l2_get("l2.enabled", self._L2_DEFAULTS["enabled"])
        if not enabled:
            return {
                "status": "disabled",
                "message": "L2 自主层未启用 ([l2].enabled=false). 设 l2.enabled=1 开启 (主人 §5.7).",
                "passes_run": [],
            }

        # 2. 防重叠 (meta.l2.running)
        # [fix 8/4] _l2_get 解析 boolean -> True/False, 直接判 bool 不是 str
        existing_running = self._l2_get("l2.running", False)
        if existing_running is True or existing_running == "1":
            return {
                "status": "already_running",
                "message": "另一 pass 正在跑 (l2.running=1). 等其完成.",
            }
        self._l2_set("l2.running", "1")

        try:
            # 3. dry_run 默认
            if dry_run is None:
                dry_run = self._l2_get("l2.dry_run", self._L2_DEFAULTS["dry_run"])

            # 4. 决定跑哪些 pass
            if passes is None:
                passes = ["hygiene"]  # 默认只跑 hygiene (§6.5 工具收敛原则)

            # 4.5 实际 GC audit_log (调 _run_audit_gc, 实际 v0.2 TASKS §3 L2 hygiene GC)
            # 实际 fix 8/4 audit #5 — 1yr 估算 150MB 不受控增长
            gc_enabled = self._l2_get("l2.gc.enabled", True)  # 默认 enabled
            gc_stats = {"applied_removed": 0, "skipped_removed": 0, "proposed_removed": 0}
            if gc_enabled and not dry_run:
                gc_stats = self._run_audit_gc()
            # dry_run 时也跑 (实际只 reports, 不真删)
            elif gc_enabled and dry_run:
                gc_stats = self._run_audit_gc(dry_run=True)

            # 5. run_id + timestamp
            import time as _time

            run_id = f"run_{int(_time.time() * 1000)}"

            # 6. 逐 pass 跑
            results: Dict[str, Any] = {
                "run_id": run_id,
                "dry_run": dry_run,
                "passes_run": [],
                "proposals": {},  # pass_name -> [proposal_dicts]
                "applied": 0,
                "skipped": 0,
                "failed": 0,
                "watermark_updated": [],
                "gc_stats": gc_stats,  # [H-3 audit #5] audit_log GC 实际
            }

            for pname in passes:
                if pname == "hygiene":
                    # [fix 8/4] ensure bool (l2.dry_run meta returns Optional)
                    actual_dry_run = bool(dry_run) if dry_run is not None else True
                    res = self._run_hygiene_pass(
                        run_id=run_id,
                        dry_run=actual_dry_run,
                        confirm_destructive=confirm_destructive,
                    )
                    results["passes_run"].append("hygiene")
                    results["proposals"]["hygiene"] = res["proposals"]
                    # [H4 §3.4] purge_candidates 聚合: 只挑 ttl_soft_delete 的 proposals
                    # (decay_importance 是降权不是真删, 不算 purge)
                    results["purge_candidates"] = [p for p in res["proposals"] if p.get("action") == "ttl_soft_delete"]
                    results["applied"] += res["applied"]
                    results["skipped"] += res["skipped"]
                    results["failed"] += res.get("failed", 0)
                    if res.get("watermark_updated"):
                        results["watermark_updated"].append("hygiene")
                elif pname == "promote":
                    # [P1-P4 8/5] TASKS_L2_SESSION_STATE Part 2: 事实晋升机制
                    actual_dry_run = bool(dry_run) if dry_run is not None else True
                    res = self._run_promote_pass(
                        run_id=run_id,
                        dry_run=actual_dry_run,
                        confirm_destructive=confirm_destructive,
                    )
                    results["passes_run"].append("promote")
                    results["proposals"]["promote"] = res["proposals"]
                    # promote pass 暴露 candidates 给上层 (admin UI / API 报告)
                    results.setdefault("promote_candidates", []).extend(res["candidates"])
                    results["applied"] += res["applied"]
                    results["skipped"] += res["skipped"]
                    results["failed"] += res.get("failed", 0)
                    if res.get("watermark_updated"):
                        results["watermark_updated"].append("promote")
                else:
                    results.setdefault("warnings", []).append(f"unknown pass '{pname}', skipped")

            return results
        finally:
            # 7. 清 l2.running flag
            self._l2_set("l2.running", "0")

    def _run_hygiene_pass(
        self,
        run_id: str,
        dry_run: bool,
        importance_floor: Optional[float] = None,
        confirm_destructive: bool = False,
    ) -> Dict:
        """[H-1 + H-3 8/4] hygiene pass — P1 §5.6 + DESIGN §5.9 + TASKS_L2_HYGIENE H3.

        严格 §5.9 语义 (8/4 实际):
          - Phase 1: importance decay 候选 (0.1-0.3 区间) — 真跑时 UPDATE chunks.importance
          - Phase 2: TTL 候选 (按 memory_type, 实际 ephemeral 7d 52 chunks) — 真跑 + confirm_destructive=True 才 soft-delete
          - dry_run=True: 全 proposed (不 apply, 不真改数据)
          - dry_run=False + confirm_destructive=True (Phase 2): 真 soft-delete
          - 每 proposal 一事务 (§5.9 "细粒度事务")
          - watermark 推进只在 pass 全 success (§5.9.2)
          - 失败 proposal 标 skipped + 错误记入 audit_log
          - applied 状态写 audit_log 第二次行 (append-only §5.9.1)
          - revert_sql 字段填 (§5.9.3 重放)
        """
        if importance_floor is None:
            importance_floor = self._l2_get(
                "l2.importance_floor",
                self._L2_DEFAULTS["importance_floor"],
            )
        # [fix Pyright] ensure float (None check; _l2_get may return None)
        if importance_floor is None:
            importance_floor = 0.1
        importance_floor = float(importance_floor)

        proposals = []
        applied = 0
        skipped = 0
        failed = 0
        cap_purge = 50  # §5.7 l2.caps.purge
        from memory import now  # lazy import — avoid circular at module load

        ts = now()

        # ============================================================
        # Phase 1: importance decay (实际 8/4 ~2259 候选, cap 50/批)
        # ============================================================
        decay_candidates = self._exec_clean(
            """SELECT id, memory_type, importance, content, timestamp
               FROM chunks
               WHERE valid_until IS NULL
                 AND importance > 0
                 AND importance < ?
                 AND memory_type != 'procedure'
               ORDER BY importance ASC, timestamp ASC
               LIMIT 50""",
            (importance_floor * 3,),
        ).fetchall()

        for i, row in enumerate(decay_candidates):
            if i >= cap_purge:
                skipped += 1
                continue

            chunk_id = row["id"]
            before = {"importance": row["importance"], "memory_type": row["memory_type"]}
            after = {"importance": max(0.0, row["importance"] - 0.05), "memory_type": row["memory_type"]}
            # revert_sql (§5.9.3): 重放回 before 状态
            revert_sql = f"UPDATE chunks SET importance = {before['importance']:.6f} WHERE id = '{chunk_id}' AND valid_until IS NULL"

            # === 写 audit_log proposed 状态 (§5.9.1) ===
            try:
                self._conn.execute(
                    """
                    INSERT INTO audit_log
                        (run_id, pass_name, action_type, ref_type, ref_id,
                         before_json, after_json, confidence, llm_used, status,
                         created_at, revert_sql)
                    VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                            ?, ?, 1.0, 0, 'proposed', ?, NULL)
                """,
                    (
                        run_id,
                        chunk_id,
                        json.dumps(before, ensure_ascii=False),
                        json.dumps(after, ensure_ascii=False),
                        ts,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # UNIQUE 撞 (同 run_id 同 ref_id 同 status) — idempotent skip
                skipped += 1
                continue

            proposals.append(
                {
                    "ref_type": "chunk",
                    "ref_id": chunk_id,
                    "before": before,
                    "after": after,
                    "action": "decay_importance",
                    "reason": f"importance {before['importance']:.2f} < {importance_floor * 3:.2f} (floor={importance_floor})",
                    "revert_sql": revert_sql,
                }
            )

            # === Apply 路径 (dry_run=False) — 每 proposal 一事务 (§5.9) ===
            if not dry_run:
                # [H5 P1] decay 也要包 try/except, 跟 ttl_soft_delete 路径对称.
                # 内层 _apply_decay_importance 自己 try/except 处理 rowcount=0 等场景
                # (返回 False); 外层处理 _apply 自身抛异常的边界.
                try:
                    apply_ok = self._apply_decay_importance(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        before=before,
                        after=after,
                        revert_sql=revert_sql,
                        ts=ts,
                    )
                    if apply_ok:
                        applied += 1
                    else:
                        failed += 1
                except Exception as e:  # noqa: BLE001 — 提案级隔离
                    logger.exception(f"[H5] decay_importance apply raised: {chunk_id}")
                    self._mark_skipped(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        ts=ts,
                        reason=f"decay_importance apply raised: {type(e).__name__}: {e}",
                        action_type="decay_importance",
                    )
                    failed += 1

        # ============================================================
        # Phase 2: TTL 候选 (按 memory_type)
        # [H-3 8/4 实际] ephemeral 7d 52 chunks / fact 365d 0 / ...
        # 真 apply 需 confirm_destructive=True (§5.9.2 "purge 是破坏性操作")
        # ============================================================
        for mtype, ttl_days in self._MEMORY_TYPE_TTL_DAYS.items():
            if ttl_days is None:
                continue  # procedure 永久

            cutoff_iso = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%S")

            # 取过期 chunks (报告 + 真 apply 候选)
            ttl_candidates = self._exec_clean(
                """SELECT id, memory_type, timestamp FROM chunks
                   WHERE valid_until IS NULL
                     AND memory_type = ?
                     AND timestamp < ?
                   ORDER BY timestamp ASC
                   LIMIT 50""",
                (mtype, cutoff_iso),
            ).fetchall()

            if not ttl_candidates:
                # 仍报 0 候选 (主人 §6 报告)
                proposals.append(
                    {
                        "ref_type": "report",
                        "ref_id": f"ttl_{mtype}",
                        "before": {"memory_type": mtype, "ttl_days": ttl_days},
                        "after": None,
                        "action": "ttl_candidate_report",
                        "reason": f"0 chunks older than {ttl_days} days (memory_type={mtype})",
                    }
                )
                continue

            # 每个 candidate 写 audit_log + (apply 路径) soft-delete
            for i, chunk_row in enumerate(ttl_candidates):
                if i >= cap_purge:
                    skipped += 1
                    continue

                chunk_id = chunk_row["id"]
                before = {"memory_type": mtype, "valid_until": None, "timestamp": chunk_row["timestamp"]}
                after = {"memory_type": mtype, "valid_until": ts, "timestamp": chunk_row["timestamp"]}
                # Undo must revive the chunk and cancel its delayed physical purge.
                revert_sql = f"UPDATE chunks SET valid_until = NULL WHERE id = '{chunk_id}'; DELETE FROM purged_queue WHERE target_id = '{chunk_id}' AND target_kind = 'chunk' AND done = 0"

                try:
                    self._conn.execute(
                        """
                        INSERT INTO audit_log
                            (run_id, pass_name, action_type, ref_type, ref_id,
                             before_json, after_json, confidence, llm_used, status,
                             created_at, revert_sql)
                        VALUES (?, 'hygiene', 'ttl_soft_delete', 'chunk', ?,
                                ?, ?, 1.0, 0, 'proposed', ?, NULL)
                    """,
                        (
                            run_id,
                            chunk_id,
                            json.dumps(before, ensure_ascii=False),
                            json.dumps(after, ensure_ascii=False),
                            ts,
                        ),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    skipped += 1
                    continue

                proposals.append(
                    {
                        "ref_type": "chunk",
                        "ref_id": chunk_id,
                        "before": before,
                        "after": after,
                        "action": "ttl_soft_delete",
                        "reason": f"memory_type={mtype} > {ttl_days} days",
                        "revert_sql": revert_sql,
                    }
                )

                # === Apply 路径 (dry_run=False + confirm_destructive=True) ===
                if not dry_run:
                    if not confirm_destructive:
                        # [§5.9.2] 没 confirm_destructive 标 skipped (破坏性操作需显式)
                        # [8/4 audit #6+8 fix] action_type 跟原 action 一致 ('ttl_soft_delete')
                        self._mark_skipped(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            ts=ts,
                            reason=f"ttl_soft_delete needs confirm_destructive=True (got {confirm_destructive})",
                            action_type="ttl_soft_delete",
                        )
                        # Status is skipped, but the run records a blocked destructive
                        # action as failed so the watermark cannot advance.
                        failed += 1
                    else:
                        # [H5 §5.9.1] 每 proposal 一事务 + 异常隔离
                        # 外层 try/except 包 _apply_ttl_soft_delete, 避免任一 proposal
                        # 异常打断整轮 — 失败 proposal 标 skipped + audit_log, 其它继续.
                        try:
                            apply_ok = self._apply_ttl_soft_delete(
                                run_id=run_id,
                                chunk_id=chunk_id,
                                mtype=mtype,
                                before=before,
                                after=after,
                                revert_sql=revert_sql,
                                ts=ts,
                            )
                            if apply_ok:
                                applied += 1
                            else:
                                failed += 1
                        except Exception as e:  # noqa: BLE001 — 提案级隔离, 详记日志
                            logger.exception(f"[H5] ttl_soft_delete apply raised: {chunk_id}")
                            self._mark_skipped(
                                run_id=run_id,
                                chunk_id=chunk_id,
                                ts=ts,
                                reason=f"ttl_soft_delete apply raised: {type(e).__name__}: {e}",
                                action_type="ttl_soft_delete",
                            )
                            failed += 1

        # ============================================================
        # Phase 3: watermark (§5.9.2)
        # [fix 8/4] applied==0 AND failed==0 = 没成功也没失败, 推 watermark (idempotent 软写)
        # [fix 8/4] failed > 0 = 有失败, 不推 (下次重跑失败项)
        # [fix 8/4] applied > 0 = 成功, 推 (不论 skipped, 因 skipped 是 cap 超限)
        # ============================================================
        watermark_updated = False
        if failed > 0:
            watermark_updated = False
        elif dry_run:
            if proposals:
                self._l2_set("l2.last_dry_run.hygiene", ts)
        else:
            # 真跑 + 无失败 (可能 applied=0 也推, 因为是 idempotent 软写)
            self._l2_set("l2.last_run.hygiene", ts)
            watermark_updated = True

        self._conn.commit()

        return {
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "proposals": proposals,
            "watermark_updated": watermark_updated,
        }

    def _apply_decay_importance(
        self,
        run_id: str,
        chunk_id: str,
        before: Dict,
        after: Dict,
        revert_sql: str,
        ts: str,
    ) -> bool:
        """[H-3 §5.9 实际] 真 UPDATE chunks.importance + 写 audit_log applied 行.

        Returns True on success, False on failure (skipped + 错误记入 audit_log).
        """
        try:
            # 1. 真改数据 (用 _exec_clean 保证 # 注释不报错)
            # [8/4 audit #3 fix] atomic UPDATE CAS (compare-and-swap) importance guard:
            #   SELECT-then-UPDATE race 窗口; §5.9.1 '每提案一事务'
            #   atomic CAS: WHERE importance = before.importance SELECT value
            #   Client 2 UPDATE 后 importance 已变 → CAS fail (rowcount=0) → _mark_skipped
            #   实际 (v0.2): 用 = (CAS) 而不是 < (range scan) 让 race fail-safe
            cur = self._exec_clean(
                """UPDATE chunks SET importance = ?
                   WHERE id = ? AND valid_until IS NULL
                     AND importance = ?""",
                (after["importance"], chunk_id, before["importance"]),
            )
            if cur.rowcount == 0:
                # chunk 已被别人改/删 (race condition) 或 importance 已变
                raise RuntimeError(f"chunk {chunk_id} not found, already soft-deleted, or importance changed (rowcount=0, expected={before['importance']:.4f})")

            # 2. 写 audit_log applied 行 (append-only §5.9.1)
            # [fix 8/4] UNIQUE(run_id, pass_name, action_type, ref_id, status)
            # 同 run_id 同 ref 已 proposed, 现在写 applied = 同 ref 不同 status
            # 但 UNIQUE 5 字段含 status, status 不同 = OK
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                        ?, ?, 1.0, 0, 'applied', ?, ?)
            """,
                (
                    run_id,
                    chunk_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    ts,
                    revert_sql,
                ),
            )
            self._conn.commit()
            return True
        except Exception as e:
            # [H5 P0 fix] rollback 必须在 _mark_skipped 前 — 否则 UPDATEs + purged_queue
            # INSERT 会被 _mark_skipped 末尾的 commit() 一并提交, 留半更新 (chunk
            # 软删除 + queue 入队 + audit_log skipped = 操作员被骗).
            self._conn.rollback()
            # [§5.9.1] 失败标 skipped + 错误记入
            # [8/4 audit #6+8 fix] action_type 跟 applied 行一致 (实际是 'decay_importance', 不是 'failed')
            self._mark_skipped(
                run_id=run_id,
                chunk_id=chunk_id,
                ts=ts,
                reason=f"decay_importance apply failed: {type(e).__name__}: {e}",
                action_type="decay_importance",
            )
            return False

    def _apply_ttl_soft_delete(
        self,
        run_id: str,
        chunk_id: str,
        mtype: str,
        before: Dict,
        after: Dict,
        revert_sql: str,
        ts: str,
    ) -> bool:
        """[H-3 §5.9 实际] TTL 过期 soft-delete (UPDATE valid_until + INSERT purged_queue) + 写 audit_log applied 行.

        [§5.9 设计意图] soft-delete = UPDATE valid_until (软写, 可回滚);
        物理删 = 30 天后 run_purge_worker 自动清 (commit 4bd654d).
        """
        try:
            # 1. UPDATE chunks.valid_until = now
            cur = self._exec_clean(
                """UPDATE chunks SET valid_until = ?
                   WHERE id = ? AND valid_until IS NULL""",
                (ts, chunk_id),
            )
            if cur.rowcount == 0:
                raise RuntimeError(f"chunk {chunk_id} not found or already soft-deleted")

            # 2. INSERT purged_queue (30 天延迟物理清, 跟 DESIGN §3.8 一致)
            # [fix 8/4 audit #4] 用 Python now() + timedelta 而不是 SQLite 'now', '+30 days',
            #    实际避免 T+ ISO vs 空格秒混用 (v0.3 报告 §0 nuance B)
            from datetime import timedelta as _td

            purged_at_iso = (datetime.now() + _td(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
            self._exec_clean(
                """INSERT INTO purged_queue
                       (target_id, target_kind, purged_at, done)
                   VALUES (?, 'chunk', ?, 0)""",
                (chunk_id, purged_at_iso),
            )

            # 3. 写 audit_log applied 行 (§5.9.1 append-only)
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'hygiene', 'ttl_soft_delete', 'chunk', ?,
                        ?, ?, 1.0, 0, 'applied', ?, ?)
            """,
                (
                    run_id,
                    chunk_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    ts,
                    revert_sql,
                ),
            )
            self._conn.commit()
            return True
        except Exception as e:
            # [H5 P0 fix] rollback 必须在 _mark_skipped 前 — 否则 UPDATE valid_until +
            # INSERT purged_queue 会被 _mark_skipped 末尾的 commit() 一并提交, 留半更新.
            self._conn.rollback()
            # [8/4 audit #6+8 fix] action_type 跟 applied 行一致
            self._mark_skipped(
                run_id=run_id,
                chunk_id=chunk_id,
                ts=ts,
                reason=f"ttl_soft_delete apply failed: {type(e).__name__}: {e}",
                action_type="ttl_soft_delete",
            )
            return False

    def _mark_skipped(
        self,
        run_id: str,
        chunk_id: str,
        ts: str,
        reason: str,
        action_type: str = "failed",  # [8/4 audit #6+8 fix] 默认 'failed' 兼容旧调用; 调用方传 action 跟 applied 一致
    ) -> None:
        """[§5.9.1] 失败 proposal 标 skipped + 错误记入 audit_log (append-only).

        Args:
            action_type: 实际 passed-in action ('decay_importance' / 'ttl_soft_delete' / 'failed')
                实际: §5.9.1 '失败 proposal 标 skipped' 不应改原 action_type
                实际: 默认 'failed' 兼容旧调用 (L0/L1 阶段)
        """
        try:
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'hygiene', ?, 'chunk', ?,
                        NULL, ?, 0, 0, 'skipped', ?, NULL)
            """,
                (
                    run_id,
                    action_type,
                    chunk_id,
                    json.dumps({"reason": reason}, ensure_ascii=False),
                    ts,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # UNIQUE 撞, 已写过一个 skipped 同 run_id + ref_id — OK
            pass

    # [P2/P3 P0-fix] 常量
    _PROMOTE_RECALL_THRESHOLD = 20
    _PROMOTE_REF_DEGREE_THRESHOLD = 10
    _PROMOTE_LONG_IMP_THRESHOLD = 0.8
    _PROMOTE_LONG_DAYS = 90
    _PROMOTE_DEMOTE_DAYS = 90
    _PROMOTE_DEMOTE_REF_THRESHOLD = 3
    _PROMOTE_MAX_CANONICAL = 50

    def _run_promote_pass(
        self,
        run_id: str,
        dry_run: bool = True,
        confirm_destructive: bool = False,
    ) -> Dict[str, Any]:
        """[P1-P3 8/5] TASKS_L2_SESSION_STATE §2.3.

        Part 2 事实晋升机制 — 把高频验证的 fact chunk 晋升为 canonical_fact 实体,
        久未召回的 canonical_fact 降级, 总量上限强制淘汰.

        Args:
            run_id: audit_log 关联 run id.
            dry_run: True 只生成 proposals 不写; False 真应用 (P2 promote / P3 demote).
            confirm_destructive: demote / 上限淘汰需要此显式确认 (跟 hygiene confirm_destructive 一致).

        Returns:
            {
              "candidates": [{chunk_id, signals, action: "promote"}],  # P1 扫描结果
              "demote_candidates": [{entity_id, reason}],  # P3 降级候选
              "proposals": [audit_log proposal dicts],
              "applied": int,
              "skipped": int,
              "failed": int,
              "promoted_entity_ids": [新晋升的 canonical_fact entity_id],
              "demoted_entity_ids": [降级的 entity_id],
            }
        """
        from datetime import datetime as _dt

        ts = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")

        # ===== P1: 扫描候选 =====
        # 三信号: recall_count ≥ 20, ref_degree ≥ 10, 长期 importance ≥ 0.8
        # 信号强度: recall + ref_degree*2 + long_imp_bonus (供排序)
        rows = self._conn.execute(
            """
            SELECT c.id, c.content, c.importance, c.recall_count,
                   (SELECT COUNT(*) FROM relations r
                    WHERE r.evidence_chunk_id = c.id AND r.valid_until IS NULL) AS ref_degree,
                   c.timestamp
            FROM chunks c
            WHERE c.valid_until IS NULL
              AND c.memory_type = 'fact'
            """
        ).fetchall()

        candidates: List[Dict[str, Any]] = []
        for row in rows:
            signals: Dict[str, Any] = {}
            score = 0
            if row["recall_count"] >= self._PROMOTE_RECALL_THRESHOLD:
                signals["recall_count"] = row["recall_count"]
                score += row["recall_count"]
            if row["ref_degree"] >= self._PROMOTE_REF_DEGREE_THRESHOLD:
                signals["ref_degree"] = row["ref_degree"]
                score += row["ref_degree"] * 2
            # 长期 importance ≥ 0.8 (timestamp < now - 90d)
            try:
                chunk_age_days = (_dt.now() - _dt.fromisoformat(row["timestamp"])).days
            except (ValueError, TypeError):
                chunk_age_days = 0
            if row["importance"] >= self._PROMOTE_LONG_IMP_THRESHOLD and chunk_age_days >= self._PROMOTE_LONG_DAYS:
                signals["long_high_imp"] = {
                    "importance": row["importance"],
                    "age_days": chunk_age_days,
                }
                score += 50  # 长期高重要给固定权重
            if signals:
                candidates.append(
                    {
                        "chunk_id": row["id"],
                        "signals": signals,
                        "score": score,
                        "action": "promote",
                    }
                )
        # 按 score 降序
        candidates.sort(key=lambda c: c["score"], reverse=True)

        # ===== P3 上限检查: canonical_fact 总数 =====
        canonical_count_row = self._conn.execute("SELECT COUNT(*) FROM entities WHERE kind='canonical_fact' AND valid_until IS NULL").fetchone()
        canonical_count = canonical_count_row[0]
        need_evict = max(0, canonical_count + len(candidates) - self._PROMOTE_MAX_CANONICAL)

        # ===== P3 降级候选: canonical_fact 90 天未召回 + ref_degree < 3 =====
        demote_candidates: List[Dict[str, Any]] = []
        demote_rows = self._conn.execute(
            """
            SELECT e.id, e.importance, e.last_recalled,
                   (SELECT COUNT(*) FROM relations r
                    WHERE (r.source_id = e.id OR r.target_id = e.id)
                      AND r.valid_until IS NULL) AS ref_degree
            FROM entities e
            WHERE e.kind = 'canonical_fact' AND e.valid_until IS NULL
            """
        ).fetchall()
        for ent in demote_rows:
            last_recalled = ent["last_recalled"]
            if last_recalled is None:
                # 从未召回 — 当作"老召回"算
                age_days = 9999
            else:
                try:
                    age_days = (_dt.now() - _dt.fromisoformat(last_recalled)).days
                except (ValueError, TypeError):
                    age_days = 0
            if age_days >= self._PROMOTE_DEMOTE_DAYS and ent["ref_degree"] < self._PROMOTE_DEMOTE_REF_THRESHOLD:
                demote_candidates.append(
                    {
                        "entity_id": ent["id"],
                        "reason": f"90d未召回(ref_degree={ent['ref_degree']})",
                        "importance": ent["importance"],
                    }
                )

        # ===== 上限腾位: 按 importance asc 补 demote 候选 =====
        if need_evict > 0:
            evict_rows = self._conn.execute(
                """
                SELECT e.id, e.importance
                FROM entities e
                WHERE e.kind = 'canonical_fact' AND e.valid_until IS NULL
                ORDER BY e.importance ASC
                LIMIT ?
                """,
                (need_evict,),
            ).fetchall()
            for ent in evict_rows:
                # 避免重复添加
                if any(d["entity_id"] == ent["id"] for d in demote_candidates):
                    continue
                demote_candidates.append(
                    {
                        "entity_id": ent["id"],
                        "reason": f"canonical_fact 上限{self._PROMOTE_MAX_CANONICAL}触发腾位",
                        "importance": ent["importance"],
                    }
                )

        # ===== P4 audit 接入: proposals =====
        proposals: List[Dict[str, Any]] = []
        # promote proposals
        for cand in candidates:
            proposals.append(
                {
                    "action_type": "promote_to_canonical",
                    "ref_type": "chunk",
                    "ref_id": cand["chunk_id"],
                    "signals": cand["signals"],
                    "score": cand["score"],
                }
            )
        # demote proposals
        for d in demote_candidates:
            proposals.append(
                {
                    "action_type": "demote_canonical",
                    "ref_type": "entity",
                    "ref_id": d["entity_id"],
                    "reason": d["reason"],
                }
            )

        result = {
            "candidates": candidates,
            "demote_candidates": demote_candidates,
            "proposals": proposals,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "promoted_entity_ids": [],
            "demoted_entity_ids": [],
            "watermark_updated": False,
        }

        if dry_run:
            return result

        # ===== 真应用 (P2 promote + P3 demote) =====
        # demote 是 destructive — 需 confirm_destructive
        apply_demotes = confirm_destructive

        # 1. demote (先降级腾位, 给 promote 留位)
        if apply_demotes:
            for d in demote_candidates:
                ok = self._apply_demote_canonical(
                    run_id=run_id,
                    entity_id=d["entity_id"],
                    reason=d["reason"],
                    ts=ts,
                )
                if ok:
                    result["applied"] += 1
                    result["demoted_entity_ids"].append(d["entity_id"])
                else:
                    result["failed"] += 1

        # 2. promote (限上限 — 已 demote 后计数)
        demoted_set = set(result["demoted_entity_ids"])
        effective_canonical = canonical_count - len(demoted_set)
        if effective_canonical + len(candidates) > self._PROMOTE_MAX_CANONICAL:
            slots = max(0, self._PROMOTE_MAX_CANONICAL - effective_canonical)
        else:
            slots = len(candidates)

        for cand in candidates[:slots]:
            ok = self._apply_promote_to_canonical(
                run_id=run_id,
                chunk_id=cand["chunk_id"],
                signals=cand["signals"],
                ts=ts,
            )
            if ok:
                result["applied"] += 1
                result["promoted_entity_ids"].append(cand["chunk_id"])
            else:
                result["failed"] += 1

        # [P4] watermark 推进
        if result["failed"] == 0:
            self._l2_set("l2.last_run.promote", ts)
            result["watermark_updated"] = True

        return result

    def _apply_promote_to_canonical(
        self,
        run_id: str,
        chunk_id: str,
        signals: Dict[str, Any],
        ts: str,
    ) -> bool:
        """[P2 8/5] chunk → canonical_fact entity + evidence 关系.

        §2.3 P2:
          - 抽核心事实 (内容截断 ≤200 字 + 首句)
          - id = "canonical:<slug(content 前 40 字)>"
          - 建 evidence_chunk_id → 源 chunk 关系 (existing relation.evidence_chunk_id 字段)
        """
        try:
            # 1. 取 chunk content
            row = self._conn.execute(
                "SELECT content, importance FROM chunks WHERE id = ? AND valid_until IS NULL",
                (chunk_id,),
            ).fetchone()
            if not row:
                raise RuntimeError(f"chunk {chunk_id} not found or soft-deleted")

            content = row["content"]
            # 核心事实: 内容截断 ≤200 字 + 首句 (第一句 80 字内)
            first_sentence = content.split("。")[0].split(".")[0].strip()
            if len(first_sentence) > 80:
                first_sentence = first_sentence[:80]
            core_fact = first_sentence[:200] if len(first_sentence) > 200 else first_sentence

            # 2. slug id (取核心前 40 字, alphanumeric + underscore)
            # [Part 2 review MEDIUM fix] 追加 chunk_id 短 hash 后缀, 防不同 chunk 共享
            # 首 40 字导致 slug 撞 → silent overwrite canonical_fact summary.
            import re as _re

            slug = _re.sub(r"[^a-zA-Z0-9_]", "_", core_fact[:40]).strip("_")
            if not slug:
                # fallback: 用 chunk_id[:16] (原行为)
                slug = chunk_id[:16]
                entity_id = f"canonical:{slug}"
            else:
                # 6-char hash suffix (16M 空间足够) — 防 slug collision
                import hashlib as _hl_slug

                _hash_suffix = _hl_slug.md5(chunk_id.encode(), usedforsecurity=False).hexdigest()[:6]
                entity_id = f"canonical:{slug}_{_hash_suffix}"

            # 3. upsert canonical_fact entity (重要性沿用 chunk)
            self._conn.execute(
                """
                INSERT INTO entities (id, kind, name, summary, importance, last_recalled)
                VALUES (?, 'canonical_fact', ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary = excluded.summary,
                    importance = MAX(importance, excluded.importance)
                """,
                (entity_id, core_fact, core_fact, row["importance"], ts),
            )

            # 4. 建 evidence 关系: entity ← chunk (relation.source_id=entity_id, target_id=entity_id, evidence_chunk_id=chunk_id)
            # 用一个特殊 relation kind 'canonical_evidence_of' 避免冲突
            # 注意: relations.id 是 INTEGER AUTOINCREMENT, 不能用 TEXT id — 用 chunk_id hash
            # [Part 2 review LOW fix] 防 silent drop: hash collision 时改用 max+1, 且事后 verify INSERT 生效
            import hashlib as _hl

            rel_id_hash = int.from_bytes(
                _hl.md5(f"{chunk_id}|{entity_id}".encode(), usedforsecurity=False).digest()[:4],
                "big",
                signed=False,
            )
            max_row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM relations").fetchone()
            base_id = rel_id_hash % (2**31) or (max_row[0] + 1)
            # 试探 INSERT; 如因 id collision 失败, 退到 MAX+1
            rel_id = base_id
            for attempt in range(3):
                try:
                    self._conn.execute(
                        """
                        INSERT INTO relations
                            (id, source_id, target_id, relation, weight, valid_from, evidence_chunk_id)
                        VALUES (?, ?, ?, 'canonical_evidence_of', 1.0, ?, ?)
                        """,
                        (rel_id, entity_id, entity_id, ts, chunk_id),
                    )
                    break
                except sqlite3.IntegrityError:
                    rel_id = max_row[0] + 1 + attempt + 1
            else:
                raise RuntimeError(f"evidence relation insert failed after 3 attempts for {chunk_id}|{entity_id}")

            # 5. audit_log applied
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'promote', 'promote_to_canonical', 'chunk', ?,
                        NULL, ?, 1.0, 0, 'applied', ?, ?)
                """,
                (
                    run_id,
                    chunk_id,
                    json.dumps(
                        {
                            "entity_id": entity_id,
                            "core_fact": core_fact,
                            "signals": signals,
                        },
                        ensure_ascii=False,
                    ),
                    ts,
                    f"DELETE FROM relations WHERE id='{rel_id}'; UPDATE entities SET valid_until='{ts}' WHERE id='{entity_id}' AND valid_until IS NULL;",
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            return False

    def _apply_demote_canonical(
        self,
        run_id: str,
        entity_id: str,
        reason: str,
        ts: str,
    ) -> bool:
        """[P3 8/5] canonical_fact 降级 → concept (kind 变更走版本链).

        §2.3 P3: supersede 为普通 concept; 历史保留.
        简化实现: kind 变更 (canonical_fact → concept) — valid_until IS NULL 保留可逆.
        """
        try:
            # 取旧 kind
            row = self._conn.execute(
                "SELECT kind FROM entities WHERE id = ? AND valid_until IS NULL",
                (entity_id,),
            ).fetchone()
            if not row:
                raise RuntimeError(f"canonical_fact entity {entity_id} not found")
            old_kind = row["kind"]

            # 更新 kind (P3 §2.3 简化实现: 不拆 entity, 只改 kind 标签)
            self._conn.execute(
                "UPDATE entities SET kind = 'concept' WHERE id = ?",
                (entity_id,),
            )

            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'promote', 'demote_canonical', 'entity', ?,
                        ?, ?, 1.0, 0, 'applied', ?, ?)
                """,
                (
                    run_id,
                    entity_id,
                    json.dumps({"kind": old_kind}, ensure_ascii=False),
                    json.dumps({"kind": "concept", "reason": reason}, ensure_ascii=False),
                    ts,
                    f"UPDATE entities SET kind='{old_kind}' WHERE id='{entity_id}';",
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            return False

    # === [8/15 E-3] Recall quality analytics ===================

    def recall_stats(self, days: int = 30, group_by: str = "method") -> Dict:
        """[8/15 E-3] 召回质量分析 — 让主人看清召回现状, 决定优化方向.

        主人 DESIGN §1.2 #6 短板修复: recall_log.recall_details_json 写满
        method/rank/distance/rrf_score/importance, 但无人消费. 本方法聚合
        recall_log, 输出:
          - totals: 总召回次数 / 唯一 query 数 / 总命中数 / 空结果数 + 率
          - latency_ms: p50 / p95 / p99 / avg
          - methods: 按 method 分组的 hit_count / avg_rank / avg_score
          - by_day: 按 created_at 聚合的日序列 (近 N 天)

        Args:
            days: 时间窗口 (近 N 天), 默认 30. None = 全部.
            group_by: 'method' / 'day' / 'kind' (kind = recall_kind 字段, 未来用)

        Returns:
            Dict 含 totals / latency_ms / methods / by_day 四个子键.

        Example:
            >>> m.recall_stats(days=7)
            {
              "window_days": 7,
              "totals": {"total_recalls": 116, "unique_queries": 87, ...},
              "latency_ms": {"p50": 13.7, "p95": 30.9, ...},
              "methods": {"vector": {"hit_count": 89, "avg_rank": 1.2, ...}, ...},
              "by_day": [{"day": "2026-08-09", "count": 12, "empty": 0}, ...],
            }
        """
        from datetime import datetime, timedelta

        # [8/15 E-3] 时间窗口过滤 — 老 recall 不计入. 主人 1.1 次/日低频,
        # 30 天窗口通常 30-40 条, 全量也无压力; days=None = 全部.
        params: list = []
        where = []
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
            where.append("created_at >= ?")
            params.append(cutoff)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        # === 1. Totals ===
        total_row = self._conn.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT query) AS uq FROM recall_log {where_sql}",
            params,
        ).fetchone()
        total_recalls = total_row["n"]
        unique_queries = total_row["uq"]

        # 空结果数 (results_json = '[]' 或 NULL)
        empty_row = self._conn.execute(
            f"""SELECT COUNT(*) AS n FROM recall_log {where_sql}
                {"AND" if where_sql else "WHERE"} results_json IN ('[]', '')""",
            params,
        ).fetchone()
        empty_results = empty_row["n"]
        empty_rate = (empty_results / total_recalls) if total_recalls else 0.0

        # 总命中数 (sum of results_json array length — SQLite JSON 数组)
        # 用 json_array_length 函数, 0 if null
        try:
            hits_row = self._conn.execute(
                f"""SELECT COALESCE(SUM(json_array_length(results_json)), 0) AS h
                    FROM recall_log {where_sql}""",
                params,
            ).fetchone()
            total_hits = int(hits_row["h"])
        except Exception:
            # 老 SQLite < 3.38 没 json_array_length → 退回 0
            total_hits = 0

        # === 2. Latency aggregation ===
        # 用 numpy 算 percentile (向量化); 也可不依赖 numpy 排序后取 idx.
        # mnelo 已用 numpy (embedder 依赖), 直接用.
        lat_rows = self._conn.execute(
            f"SELECT latency_ms FROM recall_log {where_sql} ORDER BY latency_ms",
            params,
        ).fetchall()
        if lat_rows:
            try:
                import numpy as _np

                arr = _np.array([r["latency_ms"] for r in lat_rows], dtype=float)
                latency = {
                    "p50": float(_np.percentile(arr, 50)),
                    "p95": float(_np.percentile(arr, 95)),
                    "p99": float(_np.percentile(arr, 99)),
                    "avg": float(arr.mean()),
                    "min": float(arr.min()),
                    "max": float(arr.max()),
                    "n": int(arr.size),
                }
            except ImportError:
                # numpy 不可用, 退化到 Python 排序 percentile
                vals = sorted(r["latency_ms"] for r in lat_rows)
                n = len(vals)

                def _pct(p):
                    idx = max(0, min(n - 1, int(p / 100.0 * n)))
                    return float(vals[idx])

                latency = {
                    "p50": _pct(50),
                    "p95": _pct(95),
                    "p99": _pct(99),
                    "avg": sum(vals) / n,
                    "min": vals[0],
                    "max": vals[-1],
                    "n": n,
                }
        else:
            latency = {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "min": 0.0, "max": 0.0, "n": 0}

        # === 3. Methods breakdown ===
        # recall_details_json 是 JSON 数组, 每项含 method / rank / rrf_score / distance.
        # SQLite 没原生 JSON iteration, 用 json_each() 展开.
        # 注意: json_each() 已经是 cross-join, where 条件要加在 recall_log r 上.
        methods: Dict[str, Dict] = {}
        try:
            # where_sql 可能是 '' 或 'WHERE X' 或 'WHERE X AND Y'
            # json_each 后不能再用 WHERE (因为 from 子句没 r 了), 要用 AND
            if where_sql:
                # 把 WHERE 替换成 AND, 然后这个 AND 加在 json_each 之后
                join_where = where_sql.replace("WHERE ", "AND ", 1)
            else:
                join_where = ""
            method_rows = self._conn.execute(
                f"""SELECT
                        json_extract(je.value, '$.method') AS method_str,
                        COUNT(*) AS hit_count,
                        AVG(CAST(json_extract(je.value, '$.rank') AS REAL)) AS avg_rank,
                        AVG(CAST(json_extract(je.value, '$.rrf_score') AS REAL)) AS avg_rrf,
                        AVG(CAST(json_extract(je.value, '$.distance') AS REAL)) AS avg_dist
                    FROM recall_log r, json_each(r.recall_details_json) je
                    WHERE 1=1 {join_where}
                    GROUP BY json_extract(je.value, '$.method')
                    ORDER BY hit_count DESC""",
                params,
            ).fetchall()
            for mr in method_rows:
                m_name = mr["method_str"] or "unknown"
                methods[m_name] = {
                    "hit_count": int(mr["hit_count"] or 0),
                    "avg_rank": float(mr["avg_rank"] or 0.0),
                    "avg_rrf_score": float(mr["avg_rrf"] or 0.0),
                    "avg_distance": float(mr["avg_dist"] or 0.0),
                }
        except Exception as e:  # noqa: BLE001 — 老 SQLite 没 json_each 兜底
            logger.warning(f"[recall_stats] methods breakdown failed: {e}")

        # === 4. By-day series (近 N 天) ===
        by_day: List[Dict] = []
        try:
            day_rows = self._conn.execute(
                f"""SELECT
                        substr(created_at, 1, 10) AS day,
                        COUNT(*) AS n,
                        SUM(CASE WHEN results_json IN ('[]', '') THEN 1 ELSE 0 END) AS empty
                    FROM recall_log {where_sql}
                    GROUP BY substr(created_at, 1, 10)
                    ORDER BY day""",
                params,
            ).fetchall()
            for dr in day_rows:
                by_day.append(
                    {
                        "day": dr["day"],
                        "count": int(dr["n"]),
                        "empty": int(dr["empty"] or 0),
                    }
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[recall_stats] by_day failed: {e}")

        return {
            "window_days": days,
            "totals": {
                "total_recalls": int(total_recalls),
                "unique_queries": int(unique_queries),
                "total_hits": total_hits,
                "empty_results": int(empty_results),
                "empty_rate": round(empty_rate, 4),
            },
            "latency_ms": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in latency.items()},
            "methods": methods,
            "by_day": by_day,
        }

    # Note: _AUDIT_GC_APPLIED_DAYS / _AUDIT_GC_SKIPPED_DAYS / _AUDIT_GC_PROPOSED_DAYS
    # 已在 audit_mixin.py AuditMixin 定义, 这里不重复 (避免 MRO 解析冲突).

    def stats(self) -> Dict:  # noqa: F811 — 2 个 stats() 是 design decision (8/4 396c432 修过时保留, 第 2 个是 hygiene 版)
        """统计 + [H-1 §6.5 v0.2 TASKS] hygiene 子键.

        [§6.5 工具收敛] 不新加 memory_hygiene_stats; 这里是 stats 的 hygiene 子键
        """
        stats = {}
        for t in self._ALLOWED_TABLES:  # 永远是 3 个白名单字符串
            total = self._conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            active = self._conn.execute(f"SELECT count(*) FROM {t} WHERE valid_until IS NULL").fetchone()[0]
            stats[t] = {"total": total, "active": active, "deleted": total - active}
        # [8/5] vectors 按实际 search 后端计数 (usearch/zvec 下 sqlite_vec 的 vectors 表恒 0)
        try:
            stats["vectors"] = self._index.size()
        except Exception as e:
            logger.warning(f"[stats] search index size failed: {e}")
            stats["vectors"] = 0
        stats["recall_log"] = self._conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]

        # [H-1 §6.5] hygiene 子键 (§1.1 v0.12 + TASK v0.2): 不新加 memory_hygiene_stats
        floor = self._l2_get("l2.importance_floor", self._L2_DEFAULTS["importance_floor"])
        decay_candidates = self._exec_clean(
            """SELECT COUNT(*) FROM chunks
               WHERE valid_until IS NULL
                 AND importance > 0 AND importance < ?
                 AND memory_type != 'procedure'""",
            (floor * 3,),
        ).fetchone()[0]  # type: ignore[arg-type]
        decay_floor_chunks = self._exec_clean(
            "SELECT COUNT(*) FROM chunks WHERE valid_until IS NULL AND importance <= ?",
            (floor,),
        ).fetchone()[0]  # type: ignore[arg-type]  # noqa
        purge_backlog = self._exec_clean("SELECT COUNT(*) FROM purged_queue WHERE done=0").fetchone()[0]  # type: ignore[arg-type]
        audit_log_total = self._exec_clean("SELECT COUNT(*) FROM audit_log").fetchone()[0]  # type: ignore[arg-type]
        freshness = self._exec_clean(
            """SELECT COALESCE(
                 CAST(SUM(CASE WHEN datetime(timestamp) >= datetime('now', '-30 days') THEN 1 ELSE 0 END) AS REAL)
                 / NULLIF(COUNT(*), 0), 0.0) AS freshness
               FROM chunks WHERE valid_until IS NULL"""
        ).fetchone()["freshness"]
        # [H4 §3.4] purge_candidates: 现在可被 purge 的 chunk 数 (TTL 过期 + 仍 active).
        # 跟 purge_backlog (已在 purged_queue, 等 30 天延迟) 区分 — 这是待入队的候选.
        # 每个 memory_type 用自己的 TTL 下界 (memory.py:1612 _MEMORY_TYPE_TTL_DAYS) —
        # 聚合求和, 跟 run_maintenance Phase 2 报告数严格对齐 (reviewer P1-1).
        per_type = []
        params = []
        for _mtype, _ttl in self._MEMORY_TYPE_TTL_DAYS.items():
            if _ttl is None:
                continue  # procedure 永久
            per_type.append(f"SELECT COUNT(*) AS n FROM chunks WHERE valid_until IS NULL AND memory_type = '{_mtype}' AND timestamp < datetime('now', ?)")
            params.append(f"-{_ttl} days")
        if per_type:
            union_sql = " UNION ALL ".join(per_type)
            row = self._conn.execute(f"SELECT COALESCE(SUM(n), 0) FROM ({union_sql})", params).fetchone()
            purge_candidates = row[0] if row else 0  # type: ignore[index]
        else:
            purge_candidates = 0
        stats["hygiene"] = {
            "importance_floor": floor,
            "decay_candidates": decay_candidates,
            "decay_floor_chunks": decay_floor_chunks,
            "purge_candidates": purge_candidates,
            "purge_backlog": purge_backlog,
            "audit_log_total": audit_log_total,
            "freshness": float(freshness or 0.0),
            "last_run_hygiene": self._l2_get("l2.last_run.hygiene"),
            "last_dry_run_hygiene": self._l2_get("l2.last_dry_run.hygiene"),
        }
        return stats


# === 自测 ===
if __name__ == "__main__":
    import time

    from memory import Memory  # lazy import — avoid circular at module load

    with Memory() as m:
        # Use unique demo entities so this __main__ block doesn't collide
        # with real data in LIVE DB. The 'main_block_demo_<ts>:' suffix
        # ensures each run starts fresh.
        ts = int(time.time())
        # [8/9 P1 follow-up] demo_stock 用 host: namespace 防 validation reject.
        # 旧 non-namespaced id + kind='stock' 触发 ValidationError (require namespace
        # 或 kind IN whitelist). 改 host: namespace — 8/9 决定 host prefix 表外部 data.
        demo_stock = f"host:stock_demo_{ts}"
        demo_person = f"host:person_demo_{ts}"
        demo_source = f"main_block_demo_{ts}"

        # 1. remember
        cid = m.remember(
            content=f"测试: {demo_stock} 建仓 12000 @ 18.96",
            source=demo_source,
            importance=0.9,
            entities=[
                {
                    "id": demo_stock,
                    "kind": "stock",
                    "name": "demo stock",
                    "aliases": ["demo stock", "DS"],
                    "properties": {"ticker": demo_stock, "sector": "demo"},
                },
                {"id": demo_person, "kind": "person", "name": "demo person"},
            ],
            relations=[
                {
                    "source_id": demo_person,
                    "target_id": demo_stock,
                    "relation": "_建仓_于",
                    "weight": 1.0,
                    "properties": {"quantity": 12000, "price": 18.96, "amount": 227520},
                },
            ],
        )
        print(f"✅ remember → chunk_id: {cid}")

        # 2. relate
        rid = m.relate(demo_person, demo_stock, "_关注", weight=0.7, evidence_chunk_id=cid)
        print(f"✅ relate → relation_id: {rid}")

        # 3. recall
        results = m.recall(f"{demo_stock} demo stock", top_k=3)
        print(f"✅ recall → {len(results)} hits")
        for r in results:
            print(f"  - {r['method']} | score={r.get('rrf_score', r.get('distance', '?')):.3f} | {r['content'][:60]}")

        # 4. graph_query
        graph = m.graph_query(demo_stock, max_hops=2)
        print(f"✅ graph_query → {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        # 5. stats
        stats = m.stats()
        print(f"✅ stats: {stats}")

        # 6. update
        new_cid = m.update(cid, reason="修正", new_content=f"测试修正: {demo_stock} 实际 7,800")
        print(f"✅ update → new chunk_id: {new_cid}")

        # 7. forget
        f = m.forget(rid, target_kind="relation", reason="outdated")
        print(f"✅ forget → {f}")

        # 8. recall again
        results = m.recall("sh600089", top_k=3)
        print(f"✅ recall after updates → {len(results)} hits")
        for r in results:
            print(f"  - {r['method']} | {r['content'][:60]}")
