#!/usr/bin/env python3
"""audit_mixin.py — AuditMixin: 4 audit methods split from memory.py.

[refactor 2026-08-12] Memory 类 3853 行拆分 (PR #11 benchmarks 子包同样先例).
AuditMixin: _exec_clean / list_audit / audit_undo / _run_audit_gc.

跨 mixin 依赖:
- self._conn (MemoryCore 提供)
- self._run_audit_gc 被 L2Maintenance.run_maintenance 调用
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    pass  # forward refs only — avoid circular import at runtime

logger = logging.getLogger("mnelo")


class AuditMixin:
    """[refactor 2026-08-12] Audit 子系统 mixin.

    Methods (4):
        _exec_clean:    SQLite execute 不支持 inline 注释 → strip 后 exec
        list_audit:     audit_log 查询
        audit_undo:     audit_log 回滚 (单条)
        _run_audit_gc:  audit_log GC (按 age 删除)

    Class constants:
        _AUDIT_GC_APPLIED_DAYS / _AUDIT_GC_SKIPPED_DAYS / _AUDIT_GC_PROPOSED_DAYS
    """

    # ============================================================
    # audit_log GC 阈值 (跟 _AUDIT_GC_* 常量同时存在于 L3393 附近 — 合并到此 mixin)
    # ============================================================
    _AUDIT_GC_APPLIED_DAYS = 90
    _AUDIT_GC_SKIPPED_DAYS = 30
    _AUDIT_GC_PROPOSED_DAYS = 7  # 仅当 ref_id 已 applied 才清 proposed

    def _exec_clean(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """[H-0 fix 8/4] SQLite execute() 不支持 SQL 注释 (#, --, /* */ 任意 unicode).
        Strip 所有 inline 注释后 execute. 这样 mnelo Python 源码可以保留
        §/¶ 等标记方便阅读, 不影响 SQL 语法.
        """
        # 简单 strip: 移除整行 # / -- 注释 + 移除 /* ... */ 块注释
        import re

        cleaned = re.sub(r"#[^\n]*", "", sql)  # 整行 # 注释 (含 §)
        cleaned = re.sub(r"--[^\n]*", "", cleaned)  # 整行 -- 注释
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)  # /* */ 块
        # 折叠多空行
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        return self._conn.execute(cleaned, params)

    def list_audit(
        self,
        run_id: Optional[str] = None,
        status: Optional[str] = None,
        pass_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """[H-1 §5.7] 查 audit_log (提案历史). DESIGN §5.9.1 状态机.

        Args:
            run_id: 过滤特定 run (可选)
            status: proposed / applied / reverted / skipped (可选)
            pass_name: 过滤特定 pass (可选)
            limit: max rows (默认 50, §5.7 memory_audit_list)
            offset: 跳过行

        Returns:
            List[{id, run_id, pass_name, action_type, ref_type, ref_id,
                  before_json, after_json, confidence, status, created_at}]
        """
        wheres, params = [], []
        if run_id:
            wheres.append("run_id=?")
            params.append(run_id)
        if status:
            wheres.append("status=?")
            params.append(status)
        if pass_name:
            wheres.append("pass_name=?")
            params.append(pass_name)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        sql = f"""SELECT id, run_id, pass_name, action_type, ref_type, ref_id,
                         before_json, after_json, confidence, llm_used, status,
                         created_at, revert_sql
                  FROM audit_log
                  {where_sql}
                  ORDER BY id DESC
                  LIMIT ? OFFSET ?"""
        params.extend([limit, offset])

        rows = self._conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            # 解析 json 字段
            before = json.loads(r["before_json"]) if r["before_json"] else None
            after = json.loads(r["after_json"]) if r["after_json"] else None
            result.append(
                {
                    "id": r["id"],
                    "run_id": r["run_id"],
                    "pass_name": r["pass_name"],
                    "action_type": r["action_type"],
                    "ref_type": r["ref_type"],
                    "ref_id": r["ref_id"],
                    "before": before,
                    "after": after,
                    "confidence": r["confidence"],
                    "llm_used": bool(r["llm_used"]),
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "revert_sql": r["revert_sql"],
                }
            )
        return result

    def audit_undo(self, audit_id: int) -> Dict[str, Any]:
        """Undo one applied audit record using its trusted, stored revert script."""
        row = self._conn.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()
        if not row:
            raise ValueError(f"audit record {audit_id} not found")
        if row["status"] != "applied":
            raise ValueError(f"audit record {audit_id} is not applied")
        revert_sql = row["revert_sql"]
        if not revert_sql:
            raise ValueError(f"audit record {audit_id} has no revert_sql")
        # executescript is intentional: TTL undo stores UPDATE + DELETE.
        self._conn.executescript(revert_sql)
        from memory import now  # lazy import — avoid circular at module load

        ts = now()
        self._conn.execute(
            """INSERT INTO audit_log
               (run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, llm_used, status,
                created_at, revert_sql)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reverted', ?, NULL)""",
            (row["run_id"], row["pass_name"], row["action_type"], row["ref_type"], row["ref_id"], row["after_json"], row["before_json"], row["confidence"], row["llm_used"], ts),
        )
        self._conn.commit()
        return {"audit_id": audit_id, "status": "reverted", "ref_id": row["ref_id"]}

    def _run_audit_gc(self, dry_run: bool = False) -> Dict[str, int]:
        """[H-3 audit #5 8/4] audit_log GC 实际 v0.2 TASKS §3 L2 hygiene.

        实际策略:
          - applied + created_at < now-90d → DELETE (实际 90 天审计 trace)
          - skipped + created_at < now-30d → DELETE (skipped 不持久)
          - proposed + created_at < now-7d AND 同一 ref_id 已 applied → DELETE
            (实际: applied 留下, proposed 占位清掉)
          - reverted 不动 (实际 v0.5 §5.9.1 "被 undo 实际保留")

        Returns:
            {applied_removed, skipped_removed, proposed_removed}

        实际每 runs (8/4 实测 ~13445 行累积, 实际若 GC 开, 删 ~30% = 实际 减少 ~4000 行).
        """
        from datetime import timedelta as _td

        now = datetime.now()
        applied_cutoff = (now - _td(days=self._AUDIT_GC_APPLIED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        skipped_cutoff = (now - _td(days=self._AUDIT_GC_SKIPPED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        proposed_cutoff = (now - _td(days=self._AUDIT_GC_PROPOSED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

        stats = {"applied_removed": 0, "skipped_removed": 0, "proposed_removed": 0}

        if dry_run:
            # 只统计, 不真删
            stats["applied_removed"] = self._exec_clean(
                """SELECT COUNT(*) FROM audit_log
                   WHERE status = 'applied' AND created_at < ?""",
                (applied_cutoff,),
            ).fetchone()[0]  # type: ignore[arg-type]
            stats["skipped_removed"] = self._exec_clean(
                """SELECT COUNT(*) FROM audit_log
                   WHERE status = 'skipped' AND created_at < ?""",
                (skipped_cutoff,),
            ).fetchone()[0]  # type: ignore[arg-type]
            stats["proposed_removed"] = self._exec_clean(
                """SELECT COUNT(*) FROM audit_log p
                   WHERE p.status = 'proposed' AND p.created_at < ?
                     AND EXISTS (
                       SELECT 1 FROM audit_log a
                       WHERE a.ref_id = p.ref_id AND a.status = 'applied'
                         AND a.run_id = p.run_id
                     )""",
                (proposed_cutoff,),
            ).fetchone()[0]  # type: ignore[arg-type]
            return stats

        # 真删 — 实际 §5.9 "每事务细粒度", GC 实际 one DELETE per status
        try:
            cur = self._exec_clean(
                """DELETE FROM audit_log
                   WHERE status = 'applied' AND created_at < ?""",
                (applied_cutoff,),
            )
            stats["applied_removed"] = cur.rowcount
        except Exception as e:
            logger.warning(f"[audit_gc] applied DELETE failed: {e}")

        try:
            cur = self._exec_clean(
                """DELETE FROM audit_log
                   WHERE status = 'skipped' AND created_at < ?""",
                (skipped_cutoff,),
            )
            stats["skipped_removed"] = cur.rowcount
        except Exception as e:
            logger.warning(f"[audit_gc] skipped DELETE failed: {e}")

        try:
            cur = self._exec_clean(
                """DELETE FROM audit_log
                   WHERE status = 'proposed' AND created_at < ?
                     AND EXISTS (
                       SELECT 1 FROM audit_log a
                       WHERE a.ref_id = audit_log.ref_id AND a.status = 'applied'
                         AND a.run_id = audit_log.run_id
                     )""",
                (proposed_cutoff,),
            )
            stats["proposed_removed"] = cur.rowcount
        except Exception as e:
            logger.warning(f"[audit_gc] proposed DELETE failed: {e}")

        self._conn.commit()
        return stats
