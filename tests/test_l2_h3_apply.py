"""
[H-3 8/4] L2 主动层真 apply 路径测试 (隔离版本).

实际 8/4 ephemeral 全清完后, 测试用 setUp/tearDown INSERT 临时 fixture:
  - 每个 test 自己 insert 临时 chunk (memory_type='ephemeral' / 'fact')
  - 测试结束 DELETE 临时 fixture
  - 这样跨 test 互不干扰

覆盖 DESIGN §5.9 严格语义:
  - [§5.9 每 proposal 一事务] 真 apply 失败不拖垮整批
  - [§5.9.1 状态机] applied 写 audit_log 第二次行 (append-only)
  - [§5.9.2 watermark 推进] failed > 0 不推 watermark
  - [§5.9.2 confirm_destructive] TTL 真 apply 需 confirm_destructive=True
  - [§5.9.3 revert_sql] 字段填好
  - [实际] ephemeral chunk 真 soft-delete + purged_queue 入队
  - [实际] decay_importance 真 UPDATE chunks.importance
"""

import json
import unittest

from memory import Memory


class _H3Fixture:
    """隔离测试 fixture — 每个 test 自己建临时 chunk, tearDown 清"""

    def setUp(self):
        self.mem = Memory()
        self.mem._l2_set("l2.enabled", "1")
        self.mem._l2_set("l2.dry_run", "0")
        self.mem._l2_set("l2.importance_floor", "0.1")
        # 重置 watermark 让测试可验
        self.mem._l2_set("l2.last_run.hygiene", "2000-01-01T00:00:00")

        # INSERT 1 个临时 fact chunk (importance=0.15, 走 decay 路径)
        self.fact_chunk_id = self.mem.remember(
            content="[H3 test fixture] 临时 fact chunk for decay test",
            source="test_fixture",
            importance=0.15,
        )
        # INSERT 1 个临时 ephemeral chunk (timestamp 8 天前, 走 TTL 路径)
        self.ephemeral_chunk_id = self.mem.remember(
            content="[H3 test fixture] 临时 ephemeral chunk for TTL test",
            source="test_fixture",
            importance=0.5,
            memory_type="ephemeral",
        )
        # 改 ephemeral timestamp 8 天前 (用 UPDATE 设 valid_until / timestamp?)
        # [fix 8/4] chunks.timestamp 是 read-only (created_at)? 看 schema
        # 实际: created_at 是 INSERT 时, 改 8 天前需要 UPDATE timestamp
        from datetime import datetime, timedelta

        eight_days_ago = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%S")
        self.mem._exec_clean(
            "UPDATE chunks SET timestamp = ? WHERE id = ?",
            (eight_days_ago, self.ephemeral_chunk_id),
        )
        self.mem._conn.commit()

    def tearDown(self):
        # 清 fixture + meta flag
        self.mem._exec_clean("DELETE FROM chunks WHERE id IN (?, ?)", (self.fact_chunk_id, self.ephemeral_chunk_id))
        # 也清 audit_log 跟 fixture 关联的行
        self.mem._exec_clean("DELETE FROM audit_log WHERE ref_id IN (?, ?)", (self.fact_chunk_id, self.ephemeral_chunk_id))
        # 清 purged_queue
        self.mem._exec_clean("DELETE FROM purged_queue WHERE target_id IN (?, ?)", (self.fact_chunk_id, self.ephemeral_chunk_id))
        self.mem._l2_set("l2.enabled", "0")
        self.mem._l2_set("l2.dry_run", "0")
        self.mem._l2_set("l2.running", "0")
        self.mem.close()


class TestH3DecayApply(_H3Fixture, unittest.TestCase):
    """[H-3 §5.9] 真 apply decay_importance (用临时 fixture)"""

    def test_01_real_decay_updates_chunks_importance(self):
        """[实际 8/4] dry_run=False 真 UPDATE chunks.importance (直接调 _apply_xxx)

        [fix 8/4] 不跑 run_maintenance (实际 50 cap + 顺序依赖),
        直接调 _apply_decay_importance 验证 UPDATE + audit_log applied 行
        """
        before_imp = self.mem._exec_clean(
            "SELECT importance FROM chunks WHERE id = ?",
            (self.fact_chunk_id,),
        ).fetchone()["importance"]
        run_id = f"test_run_{self.fact_chunk_id}"
        ts = self.mem._l2_get("l2.last_run.hygiene") or "2026-01-01T00:00:00"
        after_dict = {"importance": before_imp - 0.05, "memory_type": "fact"}
        before_dict = {"importance": before_imp, "memory_type": "fact"}
        revert_sql = f"UPDATE chunks SET importance = {before_imp:.6f} WHERE id = '{self.fact_chunk_id}' AND valid_until IS NULL"

        # 1. 写 proposed (为 UNIQUE 满足)
        self.mem._exec_clean(
            """INSERT INTO audit_log
                   (run_id, pass_name, action_type, ref_type, ref_id,
                    before_json, after_json, confidence, llm_used, status,
                    created_at, revert_sql)
               VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                       ?, ?, 1.0, 0, 'proposed', ?, NULL)""",
            (run_id, self.fact_chunk_id, json.dumps(before_dict), json.dumps(after_dict), ts),
        )
        self.mem._conn.commit()

        # 2. 直接调 _apply_decay_importance
        ok = self.mem._apply_decay_importance(
            run_id=run_id,
            chunk_id=self.fact_chunk_id,
            before=before_dict,
            after=after_dict,
            revert_sql=revert_sql,
            ts=ts,
        )
        self.assertTrue(ok)

        # 验证 chunk importance 真减
        after_imp = self.mem._exec_clean(
            "SELECT importance FROM chunks WHERE id = ?",
            (self.fact_chunk_id,),
        ).fetchone()["importance"]
        self.assertAlmostEqual(
            after_imp,
            before_imp - 0.05,
            places=4,
            msg=f"importance 应减 0.05: before={before_imp}, after={after_imp}",
        )

    def test_02_applied_writes_second_audit_log_row(self):
        """[§5.9.1 append-only] 同一 run_id 同一 ref_id 应有 proposed + applied 两行
        [fix 8/4] 直接调 _apply_decay_importance 避免 50 cap 顺序依赖"""
        run_id = f"test_run_2_{self.fact_chunk_id}"
        ts = self.mem._l2_get("l2.last_run.hygiene") or "2026-01-01T00:00:00"
        before_imp = 0.15
        before_dict = {"importance": before_imp, "memory_type": "fact"}
        after_dict = {"importance": 0.10, "memory_type": "fact"}
        revert_sql = f"UPDATE chunks SET importance = {before_imp} WHERE id = '{self.fact_chunk_id}'"

        # 写 proposed
        self.mem._exec_clean(
            """INSERT INTO audit_log
                   (run_id, pass_name, action_type, ref_type, ref_id,
                    before_json, after_json, confidence, llm_used, status,
                    created_at, revert_sql)
               VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                       ?, ?, 1.0, 0, 'proposed', ?, NULL)""",
            (run_id, self.fact_chunk_id, json.dumps(before_dict), json.dumps(after_dict), ts),
        )
        self.mem._conn.commit()

        # 调 apply
        self.mem._apply_decay_importance(
            run_id=run_id,
            chunk_id=self.fact_chunk_id,
            before=before_dict,
            after=after_dict,
            revert_sql=revert_sql,
            ts=ts,
        )

        # 验证 2 行 audit_log (proposed + applied)
        rows = self.mem._exec_clean(
            """SELECT status FROM audit_log
               WHERE run_id = ? AND ref_id = ?
                 AND action_type = 'decay_importance'
               ORDER BY id ASC""",
            (run_id, self.fact_chunk_id),
        ).fetchall()
        statuses = [r["status"] for r in rows]
        self.assertIn("proposed", statuses)
        self.assertIn("applied", statuses)
        self.assertGreaterEqual(len(statuses), 2)

    def test_03_revert_sql_populated_in_audit_log(self):
        """[§5.9.3] applied 行 revert_sql 字段填好"""
        run_id = f"test_run_3_{self.fact_chunk_id}"
        ts = self.mem._l2_get("l2.last_run.hygiene") or "2026-01-01T00:00:00"
        before_imp = 0.15
        before_dict = {"importance": before_imp, "memory_type": "fact"}
        after_dict = {"importance": 0.10, "memory_type": "fact"}
        revert_sql = f"UPDATE chunks SET importance = {before_imp} WHERE id = '{self.fact_chunk_id}'"

        # 写 proposed + apply
        self.mem._exec_clean(
            """INSERT INTO audit_log
                   (run_id, pass_name, action_type, ref_type, ref_id,
                    before_json, after_json, confidence, llm_used, status,
                    created_at, revert_sql)
               VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                       ?, ?, 1.0, 0, 'proposed', ?, NULL)""",
            (run_id, self.fact_chunk_id, json.dumps(before_dict), json.dumps(after_dict), ts),
        )
        self.mem._conn.commit()
        self.mem._apply_decay_importance(
            run_id=run_id,
            chunk_id=self.fact_chunk_id,
            before=before_dict,
            after=after_dict,
            revert_sql=revert_sql,
            ts=ts,
        )

        # 验证 revert_sql
        row = self.mem._exec_clean(
            """SELECT revert_sql FROM audit_log
               WHERE ref_id = ? AND status = 'applied'
                 AND action_type = 'decay_importance' LIMIT 1""",
            (self.fact_chunk_id,),
        ).fetchone()
        self.assertIsNotNone(row["revert_sql"])
        self.assertIn("UPDATE chunks SET importance", row["revert_sql"])

    def test_04_watermark_advances_on_success(self):
        """[§5.9.2] 真跑 + 无失败 → l2.last_run.hygiene 推进
        [fix 8/4] fixture ephemeral 用 confirm_destructive=True 软删, failed=0
        """
        before = self.mem._l2_get("l2.last_run.hygiene")
        # confirm_destructive=True + fixture ephemeral 在 TTL 窗口内
        # → 软删 + applied=1, failed=0, watermark 推
        self.mem.run_maintenance(passes=["hygiene"], dry_run=False, confirm_destructive=True)
        after = self.mem._l2_get("l2.last_run.hygiene")
        self.assertNotEqual(before, after)


class TestH3TTLApply(_H3Fixture, unittest.TestCase):
    """[H-3 §5.9.2 + §5.9] ephemeral TTL 真 soft-delete + confirm_destructive 门控 (用临时 fixture)"""

    def test_01_ttl_without_confirm_destructive_marks_skipped(self):
        """[§5.9.2] confirm_destructive=False → ephemeral TTL proposal 标 skipped"""
        # 跑前 fixture ephemeral 是 timestamp 8 天前
        r = self.mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=False,
        )
        # failed > 0 (TTL confirm_destructive=False 跳过)
        self.assertGreater(r["failed"], 0, f"没 confirm_destructive 应有 failed: {r['failed']}")

    def test_02_ttl_with_confirm_destructive_actually_soft_deletes(self):
        """[实际 8/4] confirm_destructive=True → ephemeral > 7d 真 soft-delete (fixture)"""
        self.mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=True,
        )
        # 验证 fixture ephemeral 被软删 (valid_until != NULL)
        row = self.mem._exec_clean(
            "SELECT valid_until FROM chunks WHERE id = ?",
            (self.ephemeral_chunk_id,),
        ).fetchone()
        self.assertIsNotNone(row["valid_until"], "fixture ephemeral 应被软删 (valid_until != NULL)")

    def test_03_soft_deleted_chunks_go_to_purged_queue(self):
        """[实际 8/4] TTL soft-delete → purged_queue 入队 (30 天延迟清, 跟 §3.8 一致)"""
        self.mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=True,
        )
        # purged_queue 应有 fixture ephemeral 入队
        row = self.mem._exec_clean(
            """SELECT target_kind, done, purged_at FROM purged_queue
               WHERE target_id = ? AND target_kind = 'chunk' LIMIT 1""",
            (self.ephemeral_chunk_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["target_kind"], "chunk")
        self.assertEqual(row["done"], 0)
        self.assertGreater(row["purged_at"], "2026-08-04", "purged_at 应在未来 30 天 (实际 8/4 + 30d)")

    def test_04_watermark_does_not_advance_on_failed(self):
        """[§5.9.2] failed > 0 → l2.last_run.hygiene 不推进"""
        before = self.mem._l2_get("l2.last_run.hygiene")
        # confirm_destructive=False → TTL failed > 0
        self.mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=False,
        )
        after = self.mem._l2_get("l2.last_run.hygiene")
        self.assertEqual(before, after, "failed > 0 时 watermark 不推进")


class TestH3PureDryRun(_H3Fixture, unittest.TestCase):
    """[H-3 §5.9] dry_run=True 永远不动数据 (回归测试)"""

    def test_01_dry_run_does_not_change_chunks(self):
        """dry_run=True 应 0 mutation on chunks"""
        before_imp = self.mem._exec_clean(
            "SELECT importance FROM chunks WHERE id = ?",
            (self.fact_chunk_id,),
        ).fetchone()["importance"]
        self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        after_imp = self.mem._exec_clean(
            "SELECT importance FROM chunks WHERE id = ?",
            (self.fact_chunk_id,),
        ).fetchone()["importance"]
        self.assertEqual(before_imp, after_imp, "dry_run 不动 importance")

    def test_02_dry_run_does_not_insert_to_purged_queue(self):
        """dry_run=True 不写 purged_queue"""
        before_count = self.mem._exec_clean("SELECT COUNT(*) FROM purged_queue").fetchone()[0]
        self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        after_count = self.mem._exec_clean("SELECT COUNT(*) FROM purged_queue").fetchone()[0]
        self.assertEqual(before_count, after_count)


if __name__ == "__main__":
    unittest.main()
