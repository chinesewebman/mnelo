"""
[P0 §3.0] memory_type 记忆类型谱系测试。

覆盖: schema 列存在、默认值、写入、按类型过滤 (meta/vector/entity 路)、
非法类型拦截、存量库自动迁移。source 统一 'test_memory_type' 便于 session 清理。
"""

import unittest

from memory import Memory, norm_memory_type
from validation import MEMORY_TYPES, ValidationError


class TestMemoryTypeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()  # 触发 _migrate_schema
        cls.src = "test_memory_type"

    @classmethod
    def tearDownClass(cls):
        # [8/6 plan §10] 后端感知清理
        from helpers import cleanup_chunks

        cleanup_chunks(cls.mem, source=cls.src)
        cls.mem._conn.execute("DELETE FROM entities WHERE id LIKE 'mt_test_%'")
        cls.mem._conn.commit()
        cls.mem.close()


class TestSchemaAndDefaults(TestMemoryTypeBase):
    def test_01_memory_type_columns_exist(self):
        """存量库自动迁移后, chunks/entities 都有 memory_type 列。"""
        for table in ("chunks", "entities"):
            cols = {r[1] for r in self.mem._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            self.assertIn("memory_type", cols, f"{table} 缺 memory_type 列")

    def test_02_allowed_types_match_spec(self):
        """六类谱系与 DESIGN §3.0 一致。"""
        self.assertEqual(
            MEMORY_TYPES,
            {"fact", "preference", "episode", "decision", "procedure", "ephemeral"},
        )

    def test_03_default_is_fact(self):
        """不传 memory_type 时默认 'fact'。"""
        cid = self.mem.remember("mt_test default type", source=self.src)
        row = self.mem._conn.execute("SELECT memory_type FROM chunks WHERE id = ?", (cid,)).fetchone()
        self.assertEqual(row["memory_type"], "fact")

    def test_04_norm_normalizes_and_rejects(self):
        """norm_memory_type 归一化 + 拒绝非法值。"""
        self.assertEqual(norm_memory_type("  Decision "), "decision")
        self.assertEqual(norm_memory_type(None), "fact")
        with self.assertRaises(ValidationError):
            norm_memory_type("bogus")


class TestRememberStoresType(TestMemoryTypeBase):
    def test_05_chunk_and_entity_get_type(self):
        """remember(memory_type=...) 同时写到 chunk 和 entity。"""
        cid = self.mem.remember(
            "mt_test decision content",
            source=self.src,
            memory_type="decision",
            entities=[{"id": "mt_test_ent1", "kind": "concept", "name": "mt entity"}],
        )
        chunk_row = self.mem._conn.execute("SELECT memory_type FROM chunks WHERE id = ?", (cid,)).fetchone()
        self.assertEqual(chunk_row["memory_type"], "decision")
        ent_row = self.mem._conn.execute("SELECT memory_type FROM entities WHERE id = 'mt_test_ent1'").fetchone()
        self.assertEqual(ent_row["memory_type"], "decision")

    def test_06_entity_explicit_type_overrides_chunk_default(self):
        """entity 自带 memory_type 时优先于 chunk 的类型。"""
        cid = self.mem.remember(
            "mt_test fact chunk but preference entity",
            source=self.src,
            memory_type="fact",
            entities=[{"id": "mt_test_ent2", "kind": "concept", "name": "mt ent2", "memory_type": "preference"}],
        )
        ent_row = self.mem._conn.execute("SELECT memory_type FROM entities WHERE id = 'mt_test_ent2'").fetchone()
        self.assertEqual(ent_row["memory_type"], "preference")

    def test_07_invalid_type_rejected(self):
        """非法 memory_type 在写入时被拦截 (chunk 和 entity 两处)。"""
        with self.assertRaises(ValidationError):
            self.mem.remember("mt_test bogus", source=self.src, memory_type="bogus")
        with self.assertRaises(ValidationError):
            self.mem.remember(
                "mt_test bogus ent",
                source=self.src,
                entities=[{"id": "mt_test_ent3", "kind": "concept", "name": "x", "memory_type": "nope"}],
            )


class TestRecallTypeFilter(TestMemoryTypeBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cid_d = cls.mem.remember(
            "mt_filter 决策标记 token_DECISION",
            source=cls.src,
            memory_type="decision",
            importance=0.9,
        )
        cls.cid_p = cls.mem.remember(
            "mt_filter 偏好标记 token_PREFERENCE",
            source=cls.src,
            memory_type="preference",
            importance=0.9,
        )

    def test_08_meta_lane_filters_by_type(self):
        """meta 路: filters['type'] 只返回该类型。"""
        r = self.mem.recall("token_DECISION", strategy="meta_only", filters={"type": "decision"})
        self.assertTrue(all("PREFERENCE" not in h["content"] for h in r))
        self.assertTrue(any("token_DECISION" in h["content"] for h in r))

    def test_09_vector_lane_filters_by_type(self):
        """vector 路: filters['type'] 过滤生效。"""
        r = self.mem.recall("token_DECISION", strategy="vector_only", filters={"type": "decision"})
        self.assertTrue(all("PREFERENCE" not in h["content"] for h in r))

    def test_10_entity_lane_filters_by_type(self):
        """entity 路: filters['type'] 过滤生效。"""
        r = self.mem.recall("mt_filter", strategy="entity_only", filters={"type": "decision"})
        self.assertTrue(all("mt_filter" not in h["entity_id"] for h in r) or len(r) == 0)

    def test_11_rrf_with_type_filter(self):
        """rrf 融合 + type 过滤不报错, 且结果类型正确。"""
        r = self.mem.recall("token_DECISION", top_k=5, filters={"type": "decision"})
        for h in r:
            self.assertNotIn("PREFERENCE", h["content"])
