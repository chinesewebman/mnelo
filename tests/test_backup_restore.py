"""test_backup_restore.py — mnelo 备份/恢复链路测试 (TASKS_BACKUP_RESTORE A6).

覆盖:
- backup 创建快照 + gzip + sha256
- backup 日内去重
- backup retention 自动 prune
- backup dry-run 不写
- restore --list 列出全部 + sha256 状态
- restore dry-run 校验不落盘
- restore 实际恢复 → 当前 db 内容等效
- restore 损坏 sha256 失败提示降级
- restore 错 timestamp 报错清晰
"""

import gzip
import os
import shutil
import sqlite3
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config import config as _config  # noqa: E402
from scripts import backup_db, restore_db  # noqa: E402


class BackupRestoreBase(unittest.TestCase):
    """共用: 临时 snapshots 目录 + 内容填个小 db."""

    def setUp(self):
        # 临时目录
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="mnelo_br_")
        self.snap_dir = Path(self.tmp) / "snapshots"
        # 小 db: create + 2 chunks + 1 entity
        self.db_path = Path(self.tmp) / "test.db"
        con = sqlite3.connect(str(self.db_path))
        try:
            # [8/6 plan §14] schema 含 source/importance 让 rebuild_index 可跑
            con.executescript("""
                CREATE TABLE chunks (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    timestamp TEXT,
                    valid_until TEXT,
                    source TEXT,
                    importance REAL
                );
                CREATE TABLE entities (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    kind TEXT,
                    source TEXT,
                    timestamp TEXT
                );
                CREATE TABLE relations (
                    id TEXT PRIMARY KEY,
                    source_id TEXT,
                    target_id TEXT,
                    kind TEXT,
                    source TEXT,
                    timestamp TEXT
                );
                CREATE TABLE audit_log (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT,
                    pass_name TEXT,
                    action_type TEXT,
                    ref_id TEXT,
                    status TEXT,
                    timestamp TEXT,
                    created_at TEXT,
                    detail_json TEXT
                );
                CREATE TABLE vec0 (
                    id INTEGER PRIMARY KEY,
                    embedding BLOB
                );
            """)
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, datetime('now'), NULL, ?, ?)",
                ("c1", "hello world", "manual", 1.0),
            )
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, datetime('now'), NULL, ?, ?)",
                ("c2", "another chunk", "manual", 1.0),
            )
            con.execute(
                "INSERT INTO entities VALUES (?, ?, 'test', 'manual', datetime('now'))",
                ("e1", "Alice"),
            )
            con.commit()
        finally:
            con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBackup(BackupRestoreBase):
    def test_01_backup_creates_snapshot_file(self):
        result = backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        self.assertIn("path", result)
        self.assertTrue(Path(result["path"]).exists())
        self.assertGreater(result["size_mb"], 0)
        # gzip magic \x1f\x8b
        with open(result["path"], "rb") as f:
            self.assertEqual(f.read(2), b"\x1f\x8b")
        # sha256 sidebar
        sha = Path(result["path"]).parent / (Path(result["path"]).name + ".sha256")
        self.assertTrue(sha.exists(), "sha256 sidebar missing")
        self.assertIn(result["sha256"], sha.read_text())

    def test_02_backup_dry_run_creates_nothing(self):
        before = set(self.snap_dir.glob("*")) if self.snap_dir.exists() else set()
        result = backup_db.backup(self.snap_dir, retention=5, dry_run=True)
        self.assertTrue(result.get("dry_run"))
        after = set(self.snap_dir.glob("*")) if self.snap_dir.exists() else set()
        self.assertEqual(before, after, "dry-run 不应写任何文件")

    def test_03_backup_daily_dedup(self):
        # 第一次: 写
        r1 = backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        self.assertIn("path", r1)
        # 第二次同日: skip
        r2 = backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        self.assertTrue(r2.get("skipped"))
        self.assertIn("今已有", r2["reason"])
        # force: 允许
        r3 = backup_db.backup(self.snap_dir, retention=5, dry_run=False, force=True, db_path=self.db_path)
        self.assertIn("path", r3)
        # 总共 2 份
        gzs = list(self.snap_dir.glob("*.db.gz"))
        self.assertEqual(len(gzs), 2)

    def test_05_scheduled_disabled_skips(self):
        """[8/5 fix] --scheduled 且 [backup] enabled=false → 跳过, 不写快照."""
        with mock.patch.object(backup_db._config, "backup_enabled", False):
            old_argv = sys.argv
            sys.argv = ["backup_db.py", "--scheduled", "--dry-run", "--snapshot-dir", str(self.snap_dir)]
            try:
                rc = backup_db.main()
            finally:
                sys.argv = old_argv
        self.assertEqual(rc, 0)
        self.assertFalse(list(self.snap_dir.glob("*.db.gz")) if self.snap_dir.exists() else [])

    def test_04_backup_retention_prunes_old(self):
        # 造 5 份, 手动改 mtime 让 retention=3 删 2 份
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            ts = f"2026-08-0{i + 1}-030000"
            p = self.snap_dir / f"{ts}.db.gz"
            sha = p.parent / (p.name + ".sha256")
            p.write_bytes(b"x")
            sha.write_text("fake")
            os.utime(p, (1700000000 + i * 86400, 1700000000 + i * 86400))
        pruned = backup_db._prune_old(self.snap_dir, retention=3)
        self.assertEqual(pruned, 2)
        kept = list(self.snap_dir.glob("*.db.gz"))
        self.assertEqual(len(kept), 3)


class TestRestore(BackupRestoreBase):
    def test_01_list_snapshots(self):
        backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        backup_db.backup(self.snap_dir, retention=5, dry_run=False, force=True, db_path=self.db_path)
        snaps = restore_db._list_snapshots(self.snap_dir)
        self.assertEqual(len(snaps), 2)
        for s in snaps:
            self.assertTrue(s["sha256_ok"])

    def test_02_dry_run_validates_only(self):
        backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        # 假冒 target (live db) — dry-run 不应动它
        target = self.db_path
        target_sha_before = target.read_bytes()[:8]
        report = restore_db.restore(self.snap_dir, ts=None, target=target, dry_run=True)
        self.assertTrue(report["sha256_ok"])
        self.assertEqual(report["integrity_check"]["integrity_check"], "ok")
        self.assertEqual(report["integrity_check"]["foreign_key_check"], "ok")
        self.assertNotIn("restored", report)
        self.assertTrue(report["dry_run"])
        # 目标文件未动
        self.assertEqual(target.read_bytes()[:8], target_sha_before)

    def test_03_atomic_replace_restore(self):
        # [8/6 plan §14] rebuild=False: 手搓 schema 无 source/importance 列,
        # rebuild_index 会失败. 这里只验证 DB 恢复, 不验证索引重建.
        # 先做个快照
        backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        # 改 live db 内容
        con = sqlite3.connect(str(self.db_path))
        con.execute("INSERT INTO chunks VALUES (?, ?, datetime('now'), NULL, ?, ?)", ("c3", "new", "manual", 1.0))
        con.commit()
        # 改前 checksum
        pre = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        con.close()
        self.assertEqual(pre, 3)
        # 恢复 (rebuild=False, 只换 DB)
        report = restore_db.restore(self.snap_dir, ts=None, target=self.db_path, rebuild=False)
        self.assertTrue(report["sha256_ok"])
        self.assertEqual(report["integrity_check"]["integrity_check"], "ok")
        self.assertIn("restored", report)
        # c3 没了
        con = sqlite3.connect(str(self.db_path))
        post = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        con.close()
        self.assertEqual(post, 2, "恢复后应回到 2 chunks")
        # isolated 应该存在
        self.assertIsNotNone(report.get("isolated_to"))

    def test_04_corrupt_sha256_fails_safely(self):
        backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        # 改 sha256
        sha = next(self.snap_dir.glob("*.db.gz.sha256"))
        sha.write_text("0" * 64 + "  " + sha.read_text().split()[-1])
        report = restore_db.restore(self.snap_dir, ts=None, target=self.db_path)
        self.assertFalse(report["sha256_ok"])
        self.assertIn("error", report)
        self.assertIn("sha256", report["error"])

    def test_05_missing_snapshot_errors_cleanly(self):
        # snap_dir 需存在才能进 "无快照" 分支
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(FileNotFoundError) as ctx:
            restore_db._select_snapshot(self.snap_dir, "2099-01-01-000000")
        self.assertIn("无快照", str(ctx.exception))

    def test_06_restore_refuses_when_server_running(self):
        """[8/5 fix] MCP server 运行时恢复 live db → 拒绝, 且不碰现有 db."""
        result = backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        self.assertIn("path", result)
        orig = self.db_path.read_bytes()
        with mock.patch.object(restore_db, "_server_running", return_value=True), mock.patch.object(restore_db._config, "db_path", self.db_path):
            report = restore_db.restore(self.snap_dir, ts=None, target=self.db_path)
        self.assertIn("error", report)
        self.assertIn("server", report["error"].lower())
        self.assertEqual(self.db_path.read_bytes(), orig)  # 未被替换

    def test_07_restore_force_overrides_server_running(self):
        """[8/5 fix] --force 时放行, 带 warning."""
        result = backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)
        self.assertIn("path", result)
        with mock.patch.object(restore_db, "_server_running", return_value=True), mock.patch.object(restore_db._config, "db_path", self.db_path):
            report = restore_db.restore(self.snap_dir, ts=None, target=self.db_path, force=True)
        self.assertIn("warning", report)
        self.assertIn("restored", report)


class TestEndToEnd(BackupRestoreBase):
    def test_round_trip_real_db(self):
        """端到端: 拷 live db → backup → restore → 校验完整性 (不依赖 snapshot->source 一致).

        Note: 不能断言 source == snapshot (live db 在 MCP 跑时会变).
        只验证 snapshot 内部 integrity + restore 后可读 + 行数合理.
        """
        real_db = _config.db_path
        if not Path(real_db).exists():
            self.skipTest(f"live db 不存在: {real_db}")
        if os.environ.get("MNELO_TEST_FRESH"):
            self.skipTest("requires a populated live DB; fresh CI DB is covered by fixture round-trip tests")
        live_copy = Path(self.tmp) / "live_copy.db"
        shutil.copy2(str(real_db), str(live_copy))
        tmp_snap = Path(self.tmp) / "snaps"
        # backup
        result = backup_db.backup(tmp_snap, retention=5, dry_run=False, db_path=live_copy)
        self.assertIn("path", result)
        # restore → 另一个空 db
        restore_target = Path(self.tmp) / "restored.db"
        # [8/6 plan §14] rebuild=False — live db 是 LIVE, 重建索引会改 live 状态,
        # 这里只验证恢复, 不验证索引重建 (TestRestoreAfterIndex 单独覆盖).
        report = restore_db.restore(tmp_snap, ts=None, target=restore_target, dry_run=False, rebuild=False)
        self.assertTrue(report["sha256_ok"])
        self.assertEqual(report["integrity_check"]["integrity_check"], "ok")
        self.assertEqual(report["integrity_check"]["foreign_key_check"], "ok")
        # 行数 > 0 (live db 至少有几条)
        self.assertGreater(report["integrity_check"]["row_counts"]["chunks"], 0)


class TestRestoreAfterIndex(BackupRestoreBase):
    """[8/6 plan §14] 恢复后索引可 recall — 验证 backup/restore 链路后
    usearch.index 重建正确, knn 能命中."""

    def test_restore_rebuilds_index_and_knn_returns_hits(self):
        # 1. backup (schema 已含 source/importance, 上面 setUp 加了)
        backup_db.backup(self.snap_dir, retention=5, dry_run=False, db_path=self.db_path)

        # 2. 改坏 db (加新 chunk, 让 snapshot 比 live 旧)
        con = sqlite3.connect(str(self.db_path))
        con.execute("INSERT INTO chunks VALUES (?, ?, datetime('now'), NULL, ?, ?)", ("c_garbage", "garbage", "manual", 1.0))
        con.commit()
        con.close()

        # 3. restore with rebuild=True (验证索引重建后内容跟 snapshot 一致)
        restore_target = Path(self.tmp) / "restored.db"
        report = restore_db.restore(self.snap_dir, ts=None, target=restore_target, dry_run=False, rebuild=True)

        # 4. 校验 DB 恢复了
        self.assertTrue(report["sha256_ok"])
        self.assertIn("restored", report)
        self.assertNotIn("error", report)

        # 5. 校验索引被重建了
        self.assertIn("index_rebuilt", report)
        ir = report["index_rebuilt"]
        self.assertNotIn("error", ir, f"index rebuild failed: {ir.get('error')}")
        self.assertEqual(ir["added"], 2, "应重建 2 条 (setUp 写的 c1+c2)")
        self.assertIn("fresh", ir)
        self.assertTrue(ir["fresh"], "fresh=True 应 unlink 旧索引文件")

        # 6. 校验恢复后的 db 能 recall
        from search_index import build_search_index

        idx = build_search_index("auto", restore_target, dim=512)
        try:
            # size 应该跟 chunks 活跃数一致
            con = sqlite3.connect(str(restore_target))
            alive = con.execute("SELECT COUNT(*) FROM chunks WHERE valid_until IS NULL").fetchone()[0]
            con.close()
            self.assertEqual(idx.size(), alive, f"idx.size()={idx.size()} vs alive={alive}")
        finally:
            idx.close()


if __name__ == "__main__":
    unittest.main()
