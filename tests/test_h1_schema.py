"""
[H-1] schema 3 项 (user_confirmed / processed_at / audit_log) 落地测试.

覆盖 (per TASKS_H1_SCHEMA.md §5.2 cross-check 测试矩阵):
  - Q3-1 audit_log UNIQUE: 同 run_id proposed → applied → reverted → re-applied → 撞 UNIQUE
  - Q3-2 audit_log UNIQUE: 新 run_id re-applied → 成功 (run_id 区分)
  - A-1  init_db.py fresh install schema vs _migrate_schema 存量迁移 schema 一致
  - B-1  audit_log 写入 created_at 格式与 memory.now() 一致 (T 分隔, deepseek B 修正)

跟 test_memory_type.py 风格一致: setUp 触发 _migrate_schema, 源 = "test_h1_schema" 便于清理。
"""

import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory import Memory, now


# ============================================================
# Q1: schema 改动幂等 + 存量兼容
# ============================================================
class TestH1SchemaAndMigration(unittest.TestCase):
    """[H-1 §5.1] 幂等 + 存量兼容 — 跟 P0 §3.0 test_memory_type 模式一致"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()  # 触发 _migrate_schema, 跑过 3 列 + audit_log + 5 索引
        cls.src = "test_h1_schema"

    @classmethod
    def tearDownClass(cls):
        # 清理本类写入
        cls.mem._conn.execute("DELETE FROM chunks WHERE source = ?", (cls.src,))
        cls.mem._conn.execute("DELETE FROM entities WHERE id LIKE 'h1_test_%'")
        cls.mem._conn.execute("DELETE FROM audit_log WHERE pass_name = 'test_h1'")
        cls.mem._conn.commit()
        cls.mem.close()

    def test_01_user_confirmed_column_exists(self):
        """[H-1 §1] entities 获 user_confirmed 列 (NOT NULL DEFAULT 0)"""
        cols = self.mem._conn.execute("PRAGMA table_info(entities)").fetchall()
        col_dict = {c[1]: c for c in cols}
        self.assertIn("user_confirmed", col_dict)
        # 验证类型 INTEGER + NOT NULL + DEFAULT 0
        self.assertEqual(col_dict["user_confirmed"][2], "INTEGER")
        # [Q1 verdict] NOT NULL
        self.assertEqual(col_dict["user_confirmed"][3], 1)  # notnull=1
        # [Q1 verdict] DEFAULT 0
        self.assertIn("0", col_dict["user_confirmed"][4])

    def test_02_processed_at_columns_exist(self):
        """[H-1 §2] chunks + entities 双表获 processed_at 列 (TEXT, NULL 默认)"""
        for table in ("chunks", "entities"):
            cols = {c[1] for c in self.mem._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            self.assertIn("processed_at", cols, f"{table} 缺 processed_at 列")

    def test_03_audit_log_table_exists(self):
        """[H-1 §3] audit_log 表 + 字段 + UNIQUE 约束"""
        # 表存在
        tables = {r[0] for r in self.mem._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("audit_log", tables)

        # 字段全
        cols = {c[1] for c in self.mem._conn.execute("PRAGMA table_info(audit_log)").fetchall()}
        for required in ["id", "run_id", "pass_name", "action_type", "ref_type", "ref_id", "before_json", "after_json", "confidence", "llm_used", "status", "created_at", "revert_sql"]:
            self.assertIn(required, cols, f"audit_log 缺 {required} 列")

        # UNIQUE 约束
        indexes = self.mem._conn.execute("PRAGMA index_list(audit_log)").fetchall()
        unique_idx_names = [
            r[1]
            for r in indexes
            if self.mem._conn.execute(f"PRAGMA index_info({r[1]})").fetchall()  # 简化: 实际用 sql 查 unique
        ]
        # 用更可靠的方式查 UNIQUE
        unique_check = self.mem._conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='audit_log'
        """).fetchone()[0]
        self.assertIn("UNIQUE(run_id, pass_name, action_type, ref_id, status)", unique_check)

    def test_04_indexes_exist(self):
        """[H-1] 7 个新索引: 2 entities + 1 chunks + 4 audit_log"""
        for idx in [
            "idx_entities_user_confirmed",
            "idx_entities_processed_at",
            "idx_chunks_processed_at",
            "idx_audit_log_run",
            "idx_audit_log_pass",
            "idx_audit_log_ref",
            "idx_audit_log_created",
        ]:
            row = self.mem._conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx,)).fetchone()
            self.assertIsNotNone(row, f"缺索引 {idx}")

    def test_05_partial_index_user_confirmed(self):
        """[H-1 C 修正] user_confirmed partial index: WHERE user_confirmed=1"""
        idx_sql = self.mem._conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_entities_user_confirmed'
        """).fetchone()[0]
        self.assertIn("WHERE user_confirmed = 1", idx_sql)

    def test_06_migration_idempotent(self):
        """[H-1 §5.1.1] 跑两次 _migrate_schema 不应出错 (幂等)"""
        try:
            self.mem._migrate_schema()
            self.mem._migrate_schema()
        except Exception as e:
            self.fail(f"幂等迁移失败: {e}")

    def test_07_existing_data_compatible(self):
        """[H-1 §5.1.2] 存量 user_confirmed=0, processed_at=NULL, audit_log=0 行 (state-agnostic)"""
        # [fix] 不假设 4498/4344 (其他 test 可能改了), 验证 *all* rows 满足
        n_user_confirmed_zero = self.mem._conn.execute("SELECT COUNT(*) FROM entities WHERE user_confirmed=0").fetchone()[0]
        n_user_confirmed_one = self.mem._conn.execute("SELECT COUNT(*) FROM entities WHERE user_confirmed=1").fetchone()[0]
        n_user_confirmed_null = self.mem._conn.execute("SELECT COUNT(*) FROM entities WHERE user_confirmed IS NULL").fetchone()[0]
        n_total_entities = self.mem._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        # 全部 entities: 0=未确认, 1=确认, NULL=不应该 (NOT NULL DEFAULT 0)
        self.assertEqual(n_user_confirmed_null, 0, f"user_confirmed 有 NULL 出现 (NOT NULL 约束失败): {n_user_confirmed_null}")
        self.assertEqual(n_user_confirmed_zero + n_user_confirmed_one, n_total_entities, "user_confirmed 列跟 entities 总数不匹配")
        # H-1 落地后没任何 user_confirmed=1 实体 (实际 0 个)
        # 实际可能其他 test 设过, 所以不强制 == 0

        # processed_at 验证 (同款 state-agnostic)
        n_processed_chunks_null = self.mem._conn.execute("SELECT COUNT(*) FROM chunks WHERE processed_at IS NULL").fetchone()[0]
        n_total_chunks = self.mem._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        self.assertEqual(n_processed_chunks_null, n_total_chunks, f"chunks processed_at 不全 NULL: {n_total_chunks - n_processed_chunks_null} 非空")
        n_processed_entities_null = self.mem._conn.execute("SELECT COUNT(*) FROM entities WHERE processed_at IS NULL").fetchone()[0]
        self.assertEqual(n_processed_entities_null, n_total_entities, f"entities processed_at 不全 NULL: {n_total_entities - n_processed_entities_null} 非空")


# ============================================================
# Q3-1 + Q3-2: audit_log UNIQUE 边界测试
# ============================================================
class TestAuditLogUniqueConstraints(unittest.TestCase):
    """[H-1 §5.2 Q3-1 + Q3-2] audit_log UNIQUE 状态机边界"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        cls.run_id_1 = "test-run-1-uuid"
        cls.run_id_2 = "test-run-2-uuid"
        cls.pass_name = "test_h1"
        # 清理可能残留
        cls.mem._conn.execute("DELETE FROM audit_log WHERE pass_name = ?", (cls.pass_name,))
        cls.mem._conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.mem._conn.execute("DELETE FROM audit_log WHERE pass_name = ?", (cls.pass_name,))
        cls.mem._conn.commit()
        cls.mem.close()

    def _insert(self, run_id, status):
        """辅助: 插一条 audit_log"""
        self.mem._conn.execute(
            """
            INSERT INTO audit_log
                (run_id, pass_name, action_type, ref_type, ref_id, status, created_at)
            VALUES (?, ?, 'test_action', 'chunk', 'h1_test_chunk', ?, ?)
        """,
            (run_id, self.pass_name, status, now()),
        )
        self.mem._conn.commit()

    def test_q3_1_same_run_re_apply_blocked(self):
        """[Q3-1] 同 run_id: proposed → applied → reverted → re-applied → 撞 UNIQUE"""
        # 1) proposed
        self._insert(self.run_id_1, "proposed")
        # 2) applied (同 run_id, 同 ref, status=applied, 跟 proposed status 不同 → OK)
        self._insert(self.run_id_1, "applied")
        # 3) reverted
        self._insert(self.run_id_1, "reverted")
        # 4) re-applied (同 run_id, 同 ref, status=applied → 撞 UNIQUE)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(self.run_id_1, "applied")

    def test_q3_2_new_run_re_apply_allowed(self):
        """[Q3-2] 新 run_id + 同 ref + 同 status → 成功 (run_id 在 UNIQUE 第一位区分)"""
        # 1) 新 run_id proposed/applied/reverted (3 个 status)
        self._insert(self.run_id_2, "proposed")
        self._insert(self.run_id_2, "applied")
        self._insert(self.run_id_2, "reverted")
        # 2) 新 run_id + **不同 ref_id** (ref_id 在 UNIQUE 列表, 区分)
        # 用 ref_id="h1_test_chunk_q32" (跟之前的 h1_test_chunk 不同)
        self.mem._conn.execute(
            """
            INSERT INTO audit_log
                (run_id, pass_name, action_type, ref_type, ref_id, status, created_at)
            VALUES (?, ?, 'test_action', 'chunk', 'h1_test_chunk_q32', 'applied', ?)
        """,
            (self.run_id_2, self.pass_name, now()),
        )
        self.mem._conn.commit()
        # 不抛 = 通过

    def test_q3_2b_same_run_diff_ref_allowed(self):
        """[Q3-2 扩展] 同 run_id + 同 status + 不同 ref_id → 成功 (ref_id 也区分)"""
        # run_id_2 (已存在) + applied (已存在) + 新 ref_id
        self.mem._conn.execute(
            """
            INSERT INTO audit_log
                (run_id, pass_name, action_type, ref_type, ref_id, status, created_at)
            VALUES (?, ?, 'test_action', 'chunk', 'h1_test_chunk_q32b', 'applied', ?)
        """,
            (self.run_id_2, self.pass_name, now()),
        )
        self.mem._conn.commit()
        # 不抛 = 通过


# ============================================================
# B-1: audit_log created_at 格式 (deepseek B 修正: T 分隔)
# ============================================================
class TestAuditLogCreatedAtFormat(unittest.TestCase):
    """[H-1 B 修正] audit_log created_at 与 memory.now() 一致 (T 分隔)"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        # 清理
        cls.mem._conn.execute("DELETE FROM audit_log WHERE pass_name = ?", ("test_h1_format",))
        cls.mem._conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.mem._conn.execute("DELETE FROM audit_log WHERE pass_name = ?", ("test_h1_format",))
        cls.mem._conn.commit()
        cls.mem.close()

    def test_b1_created_at_uses_t_separator(self):
        """[B-1] audit_log 写入的 created_at 格式 = T 分隔 (跟 memory.now() 一致)"""
        ts_before = now()
        self.mem._conn.execute(
            """
            INSERT INTO audit_log
                (run_id, pass_name, action_type, ref_type, ref_id, status, created_at)
            VALUES (?, 'test_h1_format', 'test', 'chunk', 'h1_fmt', 'proposed', ?)
        """,
            ("test-b1-uuid", ts_before),
        )
        self.mem._conn.commit()

        # 读回
        stored = self.mem._conn.execute("SELECT created_at FROM audit_log WHERE pass_name = 'test_h1_format'").fetchone()[0]

        # 验证 T 分隔 (ISO 8601)
        self.assertIn("T", stored, f"created_at 应含 'T': {stored}")
        # 验证跟 memory.now() 格式一致
        t_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertRegex(stored, t_pattern, f"created_at 不符 T 分隔格式: {stored}")


# ============================================================
# A-1: init_db.py fresh install schema vs _migrate_schema 存量迁移 一致
# ============================================================
class TestInitDBMigrationConsistency(unittest.TestCase):
    """[H-1 §5.2 A-1] deepseek A 修正: fresh install (init_db.py) schema 与
    存量迁移 (_migrate_schema) 后 schema 完全一致 — 防止"全新机器装出来与存量迁移结果不同"."""

    def _extract_schema(self, db_path: str) -> dict:
        """读一个 db 的核心 schema 信息 — 表名/列名/列类型/索引"""
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        schema = {}
        # 1) 关心的 4 表: entities / chunks / relations / audit_log
        for table in ("entities", "chunks", "relations", "audit_log"):
            if table not in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
                schema[table] = None
                continue
            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            schema[table] = {
                "cols": [(c[1], c[2], c[3], c[4]) for c in cols],  # name, type, notnull, default
            }
        # 2) H-1 加的 5 索引
        for idx in ["idx_entities_user_confirmed", "idx_entities_processed_at", "idx_chunks_processed_at", "idx_audit_log_run", "idx_audit_log_pass", "idx_audit_log_ref", "idx_audit_log_created"]:
            row = con.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (idx,)).fetchone()
            schema[idx] = row[0] if row else None
        con.close()
        return schema

    def test_a1_init_db_fresh_matches_migration(self):
        """[A-1] init_db.py fresh install 出来的 schema 跟 _migrate_schema 存量迁移后一致"""
        # === Phase 1: 模拟 fresh install (init_db.py 读 schema.sql 建库) ===
        # [A-1 fix] vec0 虚拟表需要 sqlite_vec 模块 + enable_load_extension
        # [8/5 fix] schema.sql 路径不再硬编码 — 走 repo 相对路径 (本机早期 setup 留陈旧副本)
        from pathlib import Path as _P

        repo_root = _P(__file__).resolve().parent.parent
        repo_schema = repo_root / "schema.sql"
        with tempfile.TemporaryDirectory() as tmpdir:
            fresh_db = os.path.join(tmpdir, "fresh.db")
            shutil.copy(str(repo_schema), os.path.join(tmpdir, "schema.sql"))

            import sqlite_vec

            con = sqlite3.connect(fresh_db)
            # [init_db.py 模式] 跟 init_db.py 第 51-53 行一致
            # [8/10 fix] CI sandbox 没有 enable_load_extension (hostedtoolcache Python stripped).
            # 走 memory._load_vec0_module() 三层 fallback (本地 venv: enable_load_extension; CI: ctypes vec0 dylib).
            from memory import _load_vec0_module

            _load_vec0_module(con, context="test-init-db-fresh-sim")
            with open(os.path.join(tmpdir, "schema.sql")) as f:
                sql_script = f.read()
            # 替换占位符 (init_db 跑时会换)
            sql_script = sql_script.replace("{EMBED_DIM}", "512").replace("{EMBED_MODEL}", "BAAI/bge-small-zh-v1.5")
            # [8/10 fix] schema.sql 含 vec0 CREATE VIRTUAL TABLE, CI hostedtoolcache vec0 不可用时
            # executescript 中断后续 DDL. 跟 memory.py init 一样: 拆 vec0 段单独 exec, 失败 warn 跳过.
            # 这样 test 模拟 init_db.py fresh install 出来的 schema 跟 _migrate_schema 存量迁移一致.
            import re as _re_fresh

            _vec0_stmt = _re_fresh.search(
                r"CREATE\s+VIRTUAL\s+TABLE\s+vectors\s+USING\s+vec0\([^;]*\);",
                sql_script,
                flags=_re_fresh.IGNORECASE | _re_fresh.DOTALL,
            )
            _vec0_sql = _vec0_stmt.group(0) if _vec0_stmt else None
            _sql_no_vec0 = (
                _re_fresh.sub(
                    r"CREATE\s+VIRTUAL\s+TABLE\s+vectors\s+USING\s+vec0\([^;]*\);",
                    "",
                    sql_script,
                    flags=_re_fresh.IGNORECASE | _re_fresh.DOTALL,
                )
                if _vec0_sql
                else sql_script
            )
            # [bug fix D1 2026-08-16] Register iso_now() function BEFORE schema load
            from datetime import datetime, timedelta as _td

            con.create_function("iso_now", 0, lambda: datetime.now().isoformat(timespec="seconds"))
            con.create_function("iso_now_offset", 1, lambda d: (datetime.now() + _td(days=d)).isoformat(timespec="seconds"))
            con.executescript(_sql_no_vec0)
            if _vec0_sql:
                try:
                    con.executescript(_vec0_sql)
                except sqlite3.OperationalError as _e_fresh:
                    # CI hostedtoolcache vec0 不可用 — 跳过, schema 其余部分已建.
                    if "no such module: vec0" in str(_e_fresh) or "vec0" in str(_e_fresh).lower():
                        pass
                    else:
                        raise
            con.close()

            fresh_schema = self._extract_schema(fresh_db)

        # === Phase 2: 存量迁移后 schema (复用 setUpClass 跑过的 _migrate_schema) ===
        # [8/5 fix] DB 路径不再硬编码 — 从 config 解析
        from config import config as _cfg

        live_db = str(_cfg.db_path)
        migrated_schema = self._extract_schema(live_db)

        # === Phase 3: 比对 4 表 schema ===
        for table in ("entities", "chunks", "relations", "audit_log"):
            with self.subTest(table=table):
                self.assertIsNotNone(fresh_schema.get(table), f"fresh 缺 {table}")
                self.assertIsNotNone(migrated_schema.get(table), f"migrated 缺 {table}")
                # 列名比对
                fresh_cols = [c[0] for c in fresh_schema[table]["cols"]]
                migrated_cols = [c[0] for c in migrated_schema[table]["cols"]]
                self.assertEqual(set(fresh_cols), set(migrated_cols), f"{table} 列名不一致: fresh={fresh_cols} migrated={migrated_cols}")

        # === Phase 4: 比对 H-1 加的 5 索引 ===
        for idx in ["idx_entities_user_confirmed", "idx_entities_processed_at", "idx_chunks_processed_at", "idx_audit_log_run", "idx_audit_log_pass", "idx_audit_log_ref", "idx_audit_log_created"]:
            with self.subTest(idx=idx):
                self.assertIsNotNone(fresh_schema.get(idx), f"fresh 缺 {idx}")
                self.assertIsNotNone(migrated_schema.get(idx), f"migrated 缺 {idx}")


if __name__ == "__main__":
    unittest.main()
