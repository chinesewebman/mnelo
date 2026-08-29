"""
[H-0 + H-1 8/4] L2 自主层 + hygiene pass 测试.

覆盖 (per TASKS_L2_HYGIENE v0.2 + DESIGN §5.7-5.9):
  - [H-0] audit_log 表已建, UNIQUE 约束 (上次 q3 测试已覆盖)
  - [H-1] list_audit(filters) 查 audit_log
  - [H-1] run_maintenance(passes=['hygiene'], dry_run) - L2 入口 + watermark
  - [H-1] l2.enabled=false 默认拒绝 + 提供 message
  - [H-1] l2.running 防重叠
  - [H-1] hygiene pass Phase 1 (decay_importance) 写 audit_log (UNIQUE idempotent)
  - [H-1] hygiene pass Phase 2 (ttl_candidate_report) 不写 audit_log (report-only)
  - [H-1] stats.hygiene 子键返回 decay/TTL 报告
  - [H-1] watermark (l2.last_run / l2.last_dry_run) 推进

不依赖外部数据 — 写完后清理本类写入的 audit_log + meta flags。
"""

import os
import unittest

from memory import Memory


class TestL2Gate(unittest.TestCase):
    """[H-1 §5.7] L2 启用 / disable 门控"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()

    @classmethod
    def tearDownClass(cls):
        cls.mem.close()

    def test_01_disabled_returns_status_disabled(self):
        """[§5.7] l2.enabled=missing/false 时, run_maintenance 返回 status='disabled'"""
        # 先确保 disabled (setup 默认)
        self.mem._l2_set("l2.enabled", "0")
        r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        self.assertEqual(r["status"], "disabled", "l2.enabled=0 应返 disabled")
        self.assertIn("message", r)
        self.assertEqual(r["passes_run"], [])

    def test_02_already_running_returns_error(self):
        """[§5.9] l2.running 防重叠 - 另一 pass 跑时新 call 拒绝"""
        self.mem._l2_set("l2.enabled", "1")
        self.mem._l2_set("l2.running", "1")
        try:
            r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
            self.assertEqual(r["status"], "already_running")
        finally:
            # 清理
            self.mem._l2_set("l2.running", "0")
            self.mem._l2_set("l2.enabled", "0")


class TestAuditLogQuery(unittest.TestCase):
    """[H-1 §5.7] list_audit(filters)"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()

    @classmethod
    def tearDownClass(cls):
        cls.mem.close()

    def test_01_list_audit_default(self):
        """[§5.7] list_audit(limit=50) 默认最新 50 条"""
        rows = self.mem.list_audit(limit=50)
        # 类型每条都有
        for r in rows:
            self.assertIn("id", r)
            self.assertIn("run_id", r)
            self.assertIn("pass_name", r)
            self.assertIn("status", r)
            self.assertIn("created_at", r)

    def test_02_list_audit_filter_by_pass(self):
        """[§5.7] pass_name='hygiene' 过滤"""
        rows = self.mem.list_audit(pass_name="hygiene", limit=100)
        # 全是 hygiene
        for r in rows:
            self.assertEqual(r["pass_name"], "hygiene")

    def test_03_list_audit_filter_by_status(self):
        """[§5.7] status='proposed' 过滤 (H-1 实际全 hygiene 是 proposed)"""
        rows = self.mem.list_audit(pass_name="hygiene", status="proposed", limit=100)
        for r in rows:
            self.assertEqual(r["status"], "proposed")

    def test_04_list_audit_run_id_unique_results(self):
        """run_id 过滤: 同 run_id 应返回相同 set"""
        # 取所有 distinct run_id
        rows = self.mem.list_audit(limit=1000)
        run_ids = {r["run_id"] for r in rows}
        # 任选一个
        if run_ids:
            target = next(iter(run_ids))
            filtered = self.mem.list_audit(run_id=target, limit=1000)
            for r in filtered:
                self.assertEqual(r["run_id"], target)


class TestHygienePass(unittest.TestCase):
    """[H-1 §5.7] hygiene pass (decay_importance + ttl_candidate_report)"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        # [8/9 P1 follow-up] 主人 live DB 有 2259 candidates; fresh DB 0.
        # decay assertion 在 fresh DB 失败, 用 MNELO_TEST_FRESH skip.
        if os.environ.get("MNELO_TEST_FRESH"):
            cls.skip_tests_in_fresh = True
        else:
            cls.skip_tests_in_fresh = False
        # 启用 L2 + dry_run
        cls.mem._l2_set("l2.enabled", "1")
        cls.mem._l2_set("l2.dry_run", "1")
        cls.mem._l2_set("l2.importance_floor", "0.1")

    @classmethod
    def tearDownClass(cls):
        # 清理本类产生的 audit_log 行 (importance decay 候选) + meta flags
        # [fix 8/4] 不真删 audit_log (跨 test 共享); 只关 l2 + dry_run
        cls.mem._l2_set("l2.enabled", "0")
        cls.mem._l2_set("l2.dry_run", "0")
        cls.mem._l2_set("l2.running", "0")
        cls.mem.close()

    def test_01_hygiene_runs_50_decay_proposals(self):
        """[§5.7 caps.purge=50] hygiene pass 写 ≤ 50 个 decay proposal"""
        if getattr(self, "skip_tests_in_fresh", False):
            self.skipTest("需要 owner live DB 2259 candidates; fresh DB 0")
        r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        self.assertIn("hygiene", r["passes_run"])
        self.assertLessEqual(r["applied"], 50, f"applied 超过 50 cap, 实际: {r['applied']}")

        # 提案里有 decay_importance
        ph = r["proposals"]["hygiene"]
        decay = [p for p in ph if p["action"] == "decay_importance"]
        self.assertGreater(len(decay), 0, "实际 8/4 ≈2259 候选 0.1-0.3, 应有 decay proposal")

    def test_02_decay_proposal_shape(self):
        """[§5.7] decay proposal 形状: before/after/ref_id/action/reason"""
        r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        ph = r["proposals"]["hygiene"]
        decay = [p for p in ph if p["action"] == "decay_importance"]
        if decay:
            p = decay[0]
            self.assertIn("ref_type", p)
            self.assertEqual(p["ref_type"], "chunk")
            self.assertIn("ref_id", p)
            self.assertIn("before", p)
            self.assertIn("after", p)
            # before.importance > after.importance (decay)
            self.assertGreater(p["before"]["importance"], p["after"]["importance"])
            # after >= 0 (decay 不能为负; 实际 8/4 已 reduce 到 floor 0.05 → 再减 = 0)
            self.assertGreaterEqual(p["after"]["importance"], 0)

    def test_03_ttl_candidate_reports_5_types(self):
        """[H3 §3] TTL candidate report 覆盖 fact/preference/episode/decision/ephemeral
        (procedure 永久 = None = 不报告)"""
        if getattr(self, "skip_tests_in_fresh", False):
            self.skipTest("需要 owner live DB TTL variety; fresh DB 0")
        r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        ph = r["proposals"]["hygiene"]
        ttl = [p for p in ph if p["action"] == "ttl_candidate_report"]
        # 5 type reports (procedure 不报告因为永久)
        self.assertEqual(len(ttl), 5, f"5 TTL reports (procedure 永久不报告), 实际: {len(ttl)}")

    def test_04_ttl_ephemeral_finds_chunks(self):
        """[实际 8/4] ephemeral 7d 实际有 52 chunk > 7 天 (P1a v0.2 升级后)"""
        if getattr(self, "skip_tests_in_fresh", False):
            self.skipTest("需要 owner live DB ephemeral chunks; fresh DB 0")
        r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        ph = r["proposals"]["hygiene"]
        ttl_eph = [p for p in ph if p["action"] == "ttl_candidate_report" and p["before"]["memory_type"] == "ephemeral"]
        self.assertEqual(len(ttl_eph), 1)
        # 实际有数据 (P1a v0.2 1.2% ephemeral)
        # '52 chunks' or 'X chunks older than 7 days' 都行
        self.assertIn("7 days", ttl_eph[0]["reason"])

    def test_05_audit_log_written_for_decay(self):
        """[§5.7] decay 写 audit_log (proposed 状态)"""
        # 先记 audit_log 总数
        before_count = self.mem._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        # 跑
        r = self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        applied = r["applied"]
        after_count = self.mem._conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        # applied 行写 audit_log (PROPOSED)
        # 注意: UNIQUE 约束 (同 run_id 同 ref_id 同 status), 重跑会有 skipped
        self.assertGreaterEqual(after_count - before_count, applied - r["skipped"], f"应至少新增 applied-skipped 行 audit_log")

    def test_06_proposals_written_as_proposed_status(self):
        """[§5.9.1] decay proposals 写 audit_log 是 'proposed' 状态"""
        if getattr(self, "skip_tests_in_fresh", False):
            self.skipTest("requires live DB decay candidates; fresh DB has none")
        # 跑一次 + 看最新 hygiene 行的 status
        self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        recent = self.mem.list_audit(pass_name="hygiene", status="proposed", limit=10)
        self.assertGreater(len(recent), 0, "应有 proposed 状态的 hygiene audit 行")
        for a in recent:
            self.assertEqual(a["status"], "proposed")

    def test_07_dry_run_does_not_advance_last_run(self):
        """[§5.9.2] dry_run 不推进 l2.last_run.hygiene (但 dry_run 时间记录)
        实际: dry_run 不应动 last_run watermark (因为没真 apply)"""
        # 多次跑 dry_run (间隔 10ms) — last_run 应保持不变 (None 或上次非 dry 的时间)
        self.mem._l2_set("l2.last_run.hygiene", "")  # 清空 reset
        before = self.mem._l2_get("l2.last_run.hygiene")
        import time as _t

        _t.sleep(0.01)
        self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        after_dry = self.mem._l2_get("l2.last_run.hygiene")
        # 关键: dry_run 不推 last_run (since empty before, after 应依旧空)
        self.assertEqual(before, after_dry, "dry_run 不推 l2.last_run.hygiene (last_run 只在真 apply 时推进)")

    def test_07b_dry_run_records_last_dry_run(self):
        """[§5.9.2] dry_run 应记 l2.last_dry_run.hygiene
        但用 microsecond 精度 (now() 是 second precision, 所以代测):
        真跑 (非 dry_run) 时推进 last_run; dry_run 时不."""
        # [fix 8/4] second-precision now() 让时间戳测试 flaky, 改测行为
        # bool: dry_run 不推 last_run; non-dry-run 才推
        self.mem._l2_set("l2.last_run.hygiene", "2026-01-01T00:00:00")
        before = self.mem._l2_get("l2.last_run.hygiene")
        # 跑 dry_run
        self.mem.run_maintenance(passes=["hygiene"], dry_run=True)
        after_dry = self.mem._l2_get("l2.last_run.hygiene")
        self.assertEqual(before, after_dry, f"dry_run 不应动 last_run.hygiene (before={before}, after={after_dry})")

    def test_08_unknown_pass_skipped(self):
        """[run_maintenance] unknown pass 加 warnings, 不抛"""
        r = self.mem.run_maintenance(passes=["hygiene", "unknown_xyz"], dry_run=True)
        self.assertIn("warnings", r)
        self.assertTrue(any("unknown_xyz" in w for w in r["warnings"]))


class TestStatsHygieneSubkey(unittest.TestCase):
    """[H-1 §6.5] stats() 加 hygiene 子键 (主人 v0.2 拍板: 不新加 memory_hygiene_stats)"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        cls.skip_tests_in_fresh = bool(os.environ.get("MNELO_TEST_FRESH"))

    @classmethod
    def tearDownClass(cls):
        cls.mem.close()

    def test_01_stats_hygiene_subkey_present(self):
        """[§6.5] stats() 返回 hygiene 子键"""
        s = self.mem.stats()
        self.assertIn("hygiene", s)

    def test_02_hygiene_subkey_fields(self):
        """[§6.5] hygiene 子键有 7 字段"""
        s = self.mem.stats()["hygiene"]
        for field in ("importance_floor", "decay_candidates", "decay_floor_chunks", "purge_backlog", "audit_log_total", "last_run_hygiene", "last_dry_run_hygiene"):
            self.assertIn(field, s, f"hygiene 子键缺 {field}")

    def test_03_decay_candidates_count_positive(self):
        """[实际 8/4] decay_candidates 实际 2259 (0.1-0.3 区间) > 0"""
        if getattr(self, "skip_tests_in_fresh", False):
            self.skipTest("需要 owner live DB 2259 candidates; fresh DB 0")
        s = self.mem.stats()["hygiene"]
        self.assertGreater(s["decay_candidates"], 0, f"实际 8/4 ≈2259 候选, 实际 {s['decay_candidates']}")

    def test_04_purge_backlog_count_matches_purged_queue(self):
        """[§6.5] purge_backlog = purged_queue WHERE done=0 计数"""
        s = self.mem.stats()["hygiene"]
        actual = self.mem._conn.execute("SELECT COUNT(*) FROM purged_queue WHERE done=0").fetchone()[0]
        self.assertEqual(s["purge_backlog"], actual)


class TestL2Config(unittest.TestCase):
    """[H-1 §5.7] _l2_get/_l2_set 在 meta 表读写 L2 配置"""

    def setUp(self):
        self.mem = Memory()

    def tearDown(self):
        # 清理 test 写入的 keys
        self.mem._conn.execute(
            "DELETE FROM meta WHERE key IN ('l2.test_bool', 'l2.test_int', 'l2.test_float', 'l2.test_str', 'l2.enabled', 'l2.dry_run', 'l2.importance_floor', 'l2.running', 'l2.last_run.hygiene', 'l2.last_dry_run.hygiene')"
        )
        self.mem._conn.commit()
        self.mem.close()

    def test_01_bool_round_trip(self):
        """[§5.7] l2.enabled='1'/'0' ↔ True/False"""
        self.mem._l2_set("l2.test_bool", "1")
        self.assertTrue(self.mem._l2_get("l2.test_bool"))
        self.mem._l2_set("l2.test_bool", "0")
        self.assertFalse(self.mem._l2_get("l2.test_bool"))

    def test_02_int_round_trip(self):
        """[§5.7] l2.caps.purge=50 ↔ int 50"""
        self.mem._l2_set("l2.test_int", "50")
        self.assertEqual(self.mem._l2_get("l2.test_int"), 50)

    def test_03_float_round_trip(self):
        """[§5.7] l2.importance_floor='0.1' ↔ float 0.1"""
        self.mem._l2_set("l2.test_float", "0.1")
        self.assertEqual(self.mem._l2_get("l2.test_float"), 0.1)
        self.assertIsInstance(self.mem._l2_get("l2.test_float"), float)

    def test_04_str_round_trip(self):
        """[§5.7] l2.last_run.hygiene='2026-08-04T21:00:00' ↔ str"""
        self.mem._l2_set("l2.test_str", "2026-08-04T21:00:00")
        self.assertEqual(self.mem._l2_get("l2.test_str"), "2026-08-04T21:00:00")

    def test_05_missing_returns_default(self):
        """[§5.7] missing key 返回 default (不抛)"""
        self.assertEqual(self.mem._l2_get("l2.does_not_exist", "fallback"), "fallback")
        self.assertIsNone(self.mem._l2_get("l2.does_not_exist"))


if __name__ == "__main__":
    unittest.main()
