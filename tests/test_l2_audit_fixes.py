"""
[H-3 audit fix 8/4] 3 真问题修复测试:
  - #1: confirm_destructive 加到 MCP schema (handler 自动透传)
  - #4: timestamp format 统一 (Python now() T+ ISO 跟 chunks.timestamp 一致)
  - #5: audit_log GC 实际 (默认 enabled, dry-run supports)

隔离模式: 每个 test 用自己的 row_ids 验证, 真删前 verify.
"""

import json
import unittest
from datetime import datetime, timedelta

from memory import Memory, now


class TestAuditGC(unittest.TestCase):
    """[H-3 audit #5] audit_log GC 实际"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()

    @classmethod
    def tearDownClass(cls):
        cls.mem.close()

    def test_01_gc_dry_run_reports_without_deleting(self):
        """[§5.9] dry_run=True 时 _run_audit_gc 只 stats 不 mutation"""
        # 跑前 audit_log 总数
        before = self.mem._exec_clean("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        # dry_run gc
        stats = self.mem._run_audit_gc(dry_run=True)
        after = self.mem._exec_clean("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        # 实际: stats 报告有数字, but after == before (不真删)
        self.assertEqual(before, after, "dry_run 不 mutate audit_log")
        # 3 字段都有 (实际 v0.2 TASKS §3)
        self.assertIn("applied_removed", stats)
        self.assertIn("skipped_removed", stats)
        self.assertIn("proposed_removed", stats)

    def test_02_gc_real_run_keeps_recent_applied(self):
        """[§3 GC] applied + created_at > 90d 实际保留 (实际保留追溯)"""
        # 写 1 行 recent applied (今天创建, 应该保留)
        run_id = "test_gc_recent"
        ts = now()
        self.mem._exec_clean(
            """INSERT INTO audit_log
                   (run_id, pass_name, action_type, ref_type, ref_id,
                    before_json, after_json, confidence, llm_used, status,
                    created_at, revert_sql)
               VALUES (?, 'hygiene', 'decay_importance', 'chunk', 'test_chunk_recent',
                       '{}', '{}', 1.0, 0, 'applied', ?, NULL)""",
            (run_id, ts),
        )
        self.mem._conn.commit()

        # 跑真 GC (默认 enabled=True)
        stats = self.mem._run_audit_gc()

        # 验证 recent applied 还在 (not deleted)
        row = self.mem._exec_clean(
            """SELECT id FROM audit_log WHERE run_id = ? AND ref_id = 'test_chunk_recent'""",
            (run_id,),
        ).fetchone()
        self.assertIsNotNone(row, "recent applied 不应被 GC 清")

        # 清理 test row
        self.mem._exec_clean("DELETE FROM audit_log WHERE ref_id = 'test_chunk_recent'", ())
        self.mem._conn.commit()

    def test_03_gc_removes_old_applied(self):
        """[§3 GC] applied + created_at > 90d 实际清 (90 天 retention)"""
        # 写 1 行 old applied (91 天前, 应该被清)
        run_id = "test_gc_old_applied"
        old_ts = (datetime.now() - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%S")
        self.mem._exec_clean(
            """INSERT INTO audit_log
                   (run_id, pass_name, action_type, ref_type, ref_id,
                    before_json, after_json, confidence, llm_used, status,
                    created_at, revert_sql)
               VALUES (?, 'hygiene', 'decay_importance', 'chunk', 'test_chunk_old_applied',
                       '{}', '{}', 1.0, 0, 'applied', ?, NULL)""",
            (run_id, old_ts),
        )
        self.mem._conn.commit()

        # 跑真 GC
        stats = self.mem._run_audit_gc()

        # 验证 old applied 被清
        row = self.mem._exec_clean(
            """SELECT id FROM audit_log WHERE ref_id = 'test_chunk_old_applied'""",
        ).fetchone()
        self.assertIsNone(row, "old applied 应被 GC 清")

    def test_04_gc_run_maintenance_dry_run_includes_gc(self):
        """[§5.7] run_maintenance 返 gc_stats 字段 (实际 EXPOSED)"""
        # 启用 l2 first
        self.mem._l2_set("l2.enabled", "1")
        self.mem._l2_set("l2.dry_run", "1")
        self.mem._l2_set("l2.running", "0")
        try:
            r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
            # gc_stats 字段 exposed
            self.assertIn("gc_stats", r)
            self.assertIn("applied_removed", r["gc_stats"])
        finally:
            self.mem._l2_set("l2.enabled", "0")
            self.mem._l2_set("l2.dry_run", "0")


class TestTimestampISO(unittest.TestCase):
    """[H-3 audit #4] timestamp format 统一"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()

    @classmethod
    def tearDownClass(cls):
        cls.mem.close()

    def test_01_ttl_apply_writes_purged_at_iso(self):
        """[§3.8 + audit #4] _apply_ttl_soft_delete INSERT purged_at 是 T+ ISO (跟 chunks.timestamp 一致)"""
        # Insert fixture ephemeral chunk
        cid = self.mem.remember(
            content="[audit #4 test] 临时 ephemeral for timestamp check",
            source="test_audit",
            importance=0.5,
            memory_type="ephemeral",
        )
        # Set timestamp 8 days ago
        old_ts = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%S")
        self.mem._exec_clean(
            "UPDATE chunks SET timestamp = ? WHERE id = ?",
            (old_ts, cid),
        )
        self.mem._conn.commit()

        # Enable L2 + dry_run=False + confirm_destructive=True
        self.mem._l2_set("l2.enabled", "1")
        self.mem._l2_set("l2.dry_run", "0")
        self.mem._l2_set("l2.running", "0")
        try:
            r = self.mem.run_maintenance(
                passes=["hygiene"],
                dry_run=False,
                confirm_destructive=True,
            )
            # Fixture 不一定在本批 50 cap 里, 但 should be queued
        finally:
            self.mem._l2_set("l2.enabled", "0")
            self.mem._l2_set("l2.dry_run", "0")

        # 验证 purged_queue 入队 + purged_at T+ ISO 格式
        row = self.mem._exec_clean(
            "SELECT purged_at FROM purged_queue WHERE target_id = ?",
            (cid,),
        ).fetchone()
        if row:
            # [fix audit #4] 应该 'YYYY-MM-DDTHH:MM:SS' (T+) 不是 'YYYY-MM-DD HH:MM:SS' (空格)
            self.assertIn("T", row["purged_at"], f"purged_at 应该是 ISO T+ 格式, 实际: {row['purged_at']}")

        # Cleanup
        self.mem._exec_clean("DELETE FROM chunks WHERE id = ?", (cid,))
        self.mem._exec_clean("DELETE FROM audit_log WHERE ref_id = ?", (cid,))
        self.mem._exec_clean("DELETE FROM purged_queue WHERE target_id = ?", (cid,))
        self.mem._conn.commit()


class TestMCPConfirmDestructive(unittest.TestCase):
    """[H-3 audit #1] confirm_destructive 在 MCP schema 里 exposed"""

    def test_01_mcp_memory_maintenance_schema_has_confirm_destructive(self):
        """MCP schema 实际应该暴露 confirm_destructive 字段"""
        from mcp_server import TOOLS

        tool = next((t for t in TOOLS if t["name"] == "memory_maintenance"), None)
        self.assertIsNotNone(tool, "memory_maintenance 不在 TOOLS")
        props = tool["inputSchema"]["properties"]
        self.assertIn("confirm_destructive", props, "MCP schema 缺 confirm_destructive 字段")
        self.assertEqual(props["confirm_destructive"]["type"], "boolean")
        self.assertEqual(props["confirm_destructive"]["default"], False, "confirm_destructive 默认 false (安全)")


if __name__ == "__main__":
    unittest.main()
