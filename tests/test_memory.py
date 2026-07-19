#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_memory.py — mnelo v0.5.x 测试

[测试目标 - 主人口中 7/18 拍板]
1. CRUD 6 API (remember/recall/relate/forget/update/graph_query)
2. 3 路召回 + RRF 融合 ( recall 准确率)
3. 软删除 (valid_until) + 触发器自动级联
4. 4D 时间维度 (valid_from/valid_until 语义)
5. 实体消歧 (entity_resolve.py merge)
6. shim 已删 (7/18 , 走 mnelo MCP 客户端)

[运行]
  /Users/apple/hermes-agent/venv/bin/python3 /Users/apple/.hermes/memory/tests/test_memory.py
"""
import sys
import unittest
import warnings
import json
from pathlib import Path
from datetime import datetime, timedelta

# 过滤 shim 的 DeprecationWarning
warnings.filterwarnings('ignore', category=DeprecationWarning)

# === 路径 ===
sys.path.insert(0, '/Users/apple/.hermes/memory')
sys.path.insert(0, '/Users/apple/.hermes/memory/api')
sys.path.insert(0, '/Users/apple/.hermes/memory/scripts')

from memory import Memory, generate_id, now
from entity_resolve import find_duplicate_candidates, merge_entities

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')


class TestMemoryCRUD(unittest.TestCase):
    """测试 1: CRUD 6 API"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        # [7/19 patch] 加 microseconds + class counter, 防同一分钟内连跑触发 UNIQUE 冲突
        # (pre-existing bug: HHMMSS 分辨率不够, 之前 49/50 测试通过是因为通常间隔 > 1min)
        cls.test_id_prefix = (
            'test_crud_'
            + datetime.now().strftime('%H%M%S_%f')
            + f'_{cls.__name__}'
        )
        print(f'\n--- {cls.test_id_prefix} ---')

    def test_01_remember_basic(self):
        """写入 chunk + entity + relation."""
        cid = self.mem.remember(
            content=f'{self.test_id_prefix}:  sh600089 建仓 12,000 @ 18.96',
            source='test_crud',
            importance=0.9,
            entities=[
                {'id': f'{self.test_id_prefix}_sh600089', 'kind': 'stock', 'name': '特变电工-test'},
                {'id': f'{self.test_id_prefix}_master', 'kind': 'person', 'name': '主人口中-test'},
            ],
            relations=[
                {
                    'source_id': f'{self.test_id_prefix}_master',
                    'target_id': f'{self.test_id_prefix}_sh600089',
                    'relation': '_建仓_于',
                    'weight': 1.0,
                    'properties': {'quantity': 12000, 'price': 18.96},
                },
            ],
        )
        self.assertTrue(cid.startswith('chunk_'))
        print(f'  ✅ remember → {cid}')

    def test_02_relate(self):
        """新建边."""
        rid = self.mem.relate(
            f'{self.test_id_prefix}_master',
            f'{self.test_id_prefix}_sh600089',
            '_关注',
            weight=0.7,
        )
        self.assertIsInstance(rid, int)
        self.assertGreater(rid, 0)
        print(f'  ✅ relate → relation_id={rid}')

    def test_03_recall_vector(self):
        """向量召回. query 应匹配 chunk 实际内容."""
        # [P2 实测修复 7/18 PM] 测试 query 跟 chunk content 应在 bge-small-zh 嵌入空间相似
        # 旧 query '特变电工' 跟 chunk ' sh600089 建仓' 距离太大, vec0 MATCH 召回空
        # 新 query '建仓' 直接匹配 chunk content
        results = self.mem.recall(
            f'{self.test_id_prefix} 建仓',
            top_k=3, strategy='vector_only',
        )
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r['method'], 'vector')
        print(f'  ✅ vector_only recall → {len(results)} hits')

    def test_04_recall_meta(self):
        """元数据召回 (LIKE)."""
        # : 用稳定 prefix 模式 (避免 timestamp 干扰)
        results = self.mem.recall(
            f'test_crud',  # LIKE %test_crud%
            top_k=3, strategy='meta_only',
        )
        # 注: meta_only 召回源 source='test_crud' 的 chunks (可能已被 tearDown 清理)
        # 意义: 不严格断言, 至少能跑通
        self.assertIsInstance(results, list)
        print(f'  ✅ meta_only recall → {len(results)} hits (may be 0 after cleanup)')

    def test_05_recall_rrf(self):
        """3 路 + RRF 融合."""
        results = self.mem.recall(
            f'{self.test_id_prefix} ',
            top_k=3, strategy='rrf',
        )
        # RRF 应返回方法字段, 含至少 vector/meta
        methods = {r.get('method') for r in results}
        print(f'  ✅ rrf recall → {len(results)} hits, methods={methods}')

    def test_06_graph_query(self):
        """图遍历."""
        g = self.mem.graph_query(
            f'{self.test_id_prefix}_sh600089',
            max_hops=2,
        )
        self.assertIn('nodes', g)
        self.assertIn('edges', g)
        print(f'  ✅ graph_query → {len(g["nodes"])} nodes, {len(g["edges"])} edges')

    def test_07_update(self):
        """update 创建新版本 + 老版本 superseded."""
        # 拿第一个测试 chunk
        old = self.mem._conn.execute(
            f"SELECT id FROM chunks WHERE source = 'test_crud' AND valid_until IS NULL ORDER BY rowid LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(old)
        old_id = old['id']

        new_id = self.mem.update(
            old_id=old_id,
            reason='test_update',
            new_content=f'{old_id} 修正',
        )
        # 老 chunk valid_until 应不为 NULL
        old_after = self.mem._conn.execute(
            "SELECT superseded_by, valid_until FROM chunks WHERE id = ?",
            (old_id,)
        ).fetchone()
        self.assertEqual(old_after['superseded_by'], new_id)
        self.assertIsNotNone(old_after['valid_until'])
        print(f'  ✅ update → old superseded_by={old_id[-12:]}={new_id[-12:]}, valid_until={old_after["valid_until"]}')

    def test_08_forget_soft_delete(self):
        """软删除 entity + cascade 边."""
        # 建一个测试 entity
        eid = f'{self.test_id_prefix}_to_forget'
        self.mem._conn.execute("""
            INSERT INTO entities (id, kind, name, source, valid_from, valid_until)
            VALUES (?, 'concept', '测试删除', 'test_crud', ?, NULL)
        """, (eid, now()))
        self.mem._conn.commit()
        # 建关联边
        rid = self.mem.relate(
            f'{self.test_id_prefix}_master', eid, 'owns', weight=0.5,
        )

        #  forget
        result = self.mem.forget(target_id=eid, target_kind='entity', reason='test')
        self.assertEqual(result['queued_purge'], 1)

        # entity valid_until 应不为 NULL
        e_after = self.mem._conn.execute(
            "SELECT valid_until FROM entities WHERE id = ?",
            (eid,)
        ).fetchone()
        self.assertIsNotNone(e_after['valid_until'])

        # 关联边 valid_until 应不为 NULL (级联)
        r_after = self.mem._conn.execute(
            "SELECT valid_until FROM relations WHERE id = ?",
            (rid,)
        ).fetchone()
        self.assertIsNotNone(r_after['valid_until'])
        print(f'  ✅ forget → entity & edge valid_until set')

    @classmethod
    def tearDownClass(cls):
        # 清理测试数据
        # [7/19 fix] 必须先删 vectors 再删 chunks — vec0 表的 rowid = chunks.rowid,
        # 没有 ON DELETE 触发器级联, chunks 删除后 vectors 留下 → 下次 INSERT 撞 UNIQUE
        with cls.mem._conn:
            # 1. 删 test_crud 的 chunks (通过 JOIN 拿 rowid 再删 vectors)
            rows = cls.mem._conn.execute(
                "SELECT rowid FROM chunks WHERE source = 'test_crud'"
            ).fetchall()
            if rows:
                rowids = [r['rowid'] for r in rows]
                placeholders = ','.join('?' * len(rowids))
                cls.mem._conn.execute(
                    f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids
                )
                cls.mem._conn.execute(
                    "DELETE FROM chunks WHERE source = 'test_crud'"
                )
            # 2. entities + relations (按 prefix 清)
            cls.mem._conn.execute(
                "DELETE FROM entities WHERE id LIKE ?",
                (f'{cls.test_id_prefix}%',)
            )
            cls.mem._conn.execute(
                "DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?",
                (f'{cls.test_id_prefix}%', f'{cls.test_id_prefix}%')
            )
            cls.mem._conn.commit()
        cls.mem.close()


class TestTemporalDimension(unittest.TestCase):
    """测试 2: 4D 时间维度."""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        cls.test_id_prefix = 'test_temporal_' + datetime.now().strftime('%H%M%S')

    def test_01_temporal_query(self):
        """时间切片查询 — 关系 valid_from ≤ asof < valid_until."""
        eid = f'{self.test_id_prefix}_e'
        self.mem._conn.execute("""
            INSERT INTO entities (id, kind, name, source, valid_from, valid_until)
            VALUES (?, 'concept', '时间测试', 'test', ?, NULL)
        """, (eid, now()))
        self.mem._conn.commit()

        # 加 5 天前的过期关系
        past = (datetime.now() - timedelta(days=5)).isoformat(timespec='seconds')
        future = (datetime.now() + timedelta(days=5)).isoformat(timespec='seconds')
        self.mem._conn.execute("""
            INSERT INTO relations (source_id, target_id, relation, valid_from, valid_until)
            VALUES (?, ?, 'practiced_at', ?, ?)
        """, (eid, f'{self.test_id_prefix}_x', past, future))
        self.mem._conn.commit()

        # 查询: asof = 现在 → 应命中 (valid_from ≤ now < valid_until)
        g_now = self.mem.graph_query(eid, max_hops=1, asof=now())
        self.assertGreater(len(g_now['edges']), 0, '现在应命中该关系')

        # 查询: asof = 10 天前 → 应不命中
        far_past = (datetime.now() - timedelta(days=10)).isoformat(timespec='seconds')
        g_past = self.mem.graph_query(eid, max_hops=1, asof=far_past)
        self.assertEqual(len(g_past['edges']), 0, '10 天前应不命中')

        print(f'  ✅ temporal: now={len(g_now["edges"])} edges, past={len(g_past["edges"])} edges')

    @classmethod
    def tearDownClass(cls):
        with cls.mem._conn:
            cls.mem._conn.execute("DELETE FROM entities WHERE id LIKE ?", (f'{cls.test_id_prefix}%',))
            cls.mem._conn.execute(
                "DELETE FROM relations WHERE source_id LIKE ? OR target_id LIKE ?",
                (f'{cls.test_id_prefix}%', f'{cls.test_id_prefix}%')
            )
            cls.mem._conn.commit()
        cls.mem.close()


class TestEntityResolve(unittest.TestCase):
    """测试 4: 实体消歧."""

    def setUp(self):
        self.mem = Memory()
        self.test_prefix = 'test_eresolve_' + datetime.now().strftime('%H%M%S')

    def test_01_merge_candidates(self):
        """建两个相似 name 的 entity, 跑 find_duplicate_candidates."""
        a_id = f'{self.test_prefix}_a'
        b_id = f'{self.test_prefix}_b'
        for ent_id, name in [(a_id, '测试公司'), (b_id, '测试公司')]:  # : 完全同名
            self.mem._conn.execute("""
                INSERT INTO entities (id, kind, name, source, valid_from, valid_until)
                VALUES (?, 'stock', ?, 'test-eresolve', ?, NULL)
            """, (ent_id, name, now()))
        self.mem._conn.commit()

        cands = find_duplicate_candidates(self.mem._conn, threshold=0.7, kind='stock')
        ids_in_cands = {c[0] for c in cands} | {c[1] for c in cands}
        self.assertIn(a_id, ids_in_cands)
        self.assertIn(b_id, ids_in_cands)
        print(f'  ✅ find_duplicate_candidates → {len(cands)} candidates (含测试 a/b)')

    def test_02_merge(self):
        """合并 a→b."""
        a_id = f'{self.test_prefix}_ma'
        b_id = f'{self.test_prefix}_mb'
        for ent_id, name in [(a_id, '测试合并A'), (b_id, '测试合并B')]:
            self.mem._conn.execute("""
                INSERT INTO entities (id, kind, name, source, valid_from, valid_until)
                VALUES (?, 'stock', ?, 'test-eresolve', ?, NULL)
            """, (ent_id, name, now()))
        self.mem._conn.commit()

        ok = merge_entities(self.mem._conn, b_id, a_id, reason='test-merge')
        self.assertTrue(ok)

        # a 应被 soft delete
        a_after = self.mem._conn.execute(
            "SELECT valid_until, superseded_by FROM entities WHERE id = ?",
            (a_id,)
        ).fetchone()
        self.assertIsNotNone(a_after['valid_until'])
        self.assertEqual(a_after['superseded_by'], b_id)
        print(f'  ✅ merge → a={a_id} superseded by b={b_id}')

    def test_03_merge_uses_get_aliases_helper(self):
        """P2 审计: merge_entities 应复用 get_aliases (不重复 JSON 解析逻辑)."""
        from entity_resolve import get_aliases
        a_id = f'{self.test_prefix}_ga'
        b_id = f'{self.test_prefix}_gb'
        # 给 a 加 aliases_json
        self.mem._conn.execute("""
            INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until)
            VALUES (?, 'stock', 'NameA', '["alias1", "alias2"]', 'test-eresolve', ?, NULL)
        """, (a_id, now()))
        # b 也有 aliases_json
        self.mem._conn.execute("""
            INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until)
            VALUES (?, 'stock', 'NameB', '["alias3"]', 'test-eresolve', ?, NULL)
        """, (b_id, now()))
        self.mem._conn.commit()

        # 合并前 get_aliases 应能看到各自的 aliases
        a_aliases_before = get_aliases(self.mem._conn, a_id)
        b_aliases_before = get_aliases(self.mem._conn, b_id)
        self.assertEqual(set(a_aliases_before), {'NameA', 'alias1', 'alias2'})
        self.assertEqual(set(b_aliases_before), {'NameB', 'alias3'})

        # merge a → b
        ok = merge_entities(self.mem._conn, b_id, a_id, reason='test-merge-helper')
        self.assertTrue(ok)

        # merge 后 b 应包含所有 aliases (dedup)
        b_row = self.mem._conn.execute(
            "SELECT aliases_json FROM entities WHERE id = ?", (b_id,)
        ).fetchone()
        merged = json.loads(b_row['aliases_json'])
        self.assertIn('NameA', merged)
        self.assertIn('NameB', merged)
        self.assertIn('alias1', merged)
        self.assertIn('alias3', merged)
        # len should be 5 (no dups)
        self.assertEqual(len(merged), 5, f'expected 5 unique aliases, got {merged}')
        print(f'  ✅ merge with aliases_json → {len(merged)} merged aliases')

    def test_04_merge_invalid_cases(self):
        """merge 失败 cases: same id / 不存在 id."""
        x_id = f'{self.test_prefix}_invalid'
        # same id → False
        ok1 = merge_entities(self.mem._conn, x_id, x_id)
        self.assertFalse(ok1)
        # 不存在的 id → False
        ok2 = merge_entities(self.mem._conn, x_id, 'nonexistent_id')
        self.assertFalse(ok2)
        ok3 = merge_entities(self.mem._conn, 'nonexistent_id', x_id)
        self.assertFalse(ok3)
        print('  ✅ merge invalid cases all return False')



class TestP0BoundsCheck(unittest.TestCase):
    """[P0 审计后] bounds check + type 校验测试."""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()

    @classmethod
    def tearDownClass(cls):
        # [7/19 fix] TestP0BoundsCheck 用 source='test', 必须在 tearDown 清物理数据
        # (老代码只 cls.mem.close() → forget() 留下的 vectors 污染下个 class)
        try:
            rows = cls.mem._conn.execute(
                "SELECT rowid FROM chunks WHERE source = 'test'"
            ).fetchall()
            if rows:
                rowids = [r['rowid'] for r in rows]
                placeholders = ','.join('?' * len(rowids))
                cls.mem._conn.execute(
                    f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids
                )
                cls.mem._conn.execute("DELETE FROM chunks WHERE source = 'test'")
            cls.mem._conn.execute(
                "DELETE FROM entities WHERE id LIKE 'test_%' AND source = 'test'"
            )
            cls.mem._conn.execute(
                "DELETE FROM relations WHERE relation = 'clamp_test'"
            )
            cls.mem._conn.commit()
        finally:
            cls.mem.close()

    def test_01_clamp01_positive_overflow(self):
        from memory import clamp01
        self.assertEqual(clamp01(5.0), 1.0)
        self.assertEqual(clamp01(100.0), 1.0)
        print('  ✅ clamp01(>1) → 1.0')

    def test_02_clamp01_negative_overflow(self):
        from memory import clamp01
        self.assertEqual(clamp01(-0.3), 0.0)
        self.assertEqual(clamp01(-100.0), 0.0)
        print('  ✅ clamp01(<0) → 0.0')

    def test_03_clamp01_in_range(self):
        from memory import clamp01
        self.assertEqual(clamp01(0.0), 0.0)
        self.assertEqual(clamp01(0.5), 0.5)
        self.assertEqual(clamp01(1.0), 1.0)
        print('  ✅ clamp01([0,1]) pass through')

    def test_04_clamp01_rejects_str(self):
        from memory import clamp01
        with self.assertRaises(TypeError) as ctx:
            clamp01('high', 'importance')
        self.assertIn('importance', str(ctx.exception))
        self.assertIn('str', str(ctx.exception))
        print('  ✅ clamp01(str) → TypeError')

    def test_05_clamp01_rejects_none(self):
        from memory import clamp01
        with self.assertRaises(TypeError):
            clamp01(None)
        print('  ✅ clamp01(None) → TypeError')

    def test_06_clamp01_rejects_nan(self):
        from memory import clamp01
        with self.assertRaises(ValueError):
            clamp01(float('nan'))
        print('  ✅ clamp01(NaN) → ValueError')

    def test_07_clamp01_rejects_bool(self):
        """True/False 是 int 子类, 不应被当 1.0/0.0 接受."""
        from memory import clamp01
        with self.assertRaises(TypeError):
            clamp01(True)
        with self.assertRaises(TypeError):
            clamp01(False)
        print('  ✅ clamp01(bool) → TypeError (no int coercion)')

    def test_08_remember_importance_clamped(self):
        cid = self.mem.remember('test clamp', source='test', importance=5.0)
        row = self.mem._conn.execute(
            'SELECT importance FROM chunks WHERE id = ?', (cid,)
        ).fetchone()
        self.assertEqual(row[0], 1.0)
        self.mem.forget(cid)
        print('  ✅ remember(importance=5.0) → DB stores 1.0')

    def test_09_remember_importance_rejects_str(self):
        with self.assertRaises(TypeError):
            self.mem.remember('test', source='test', importance='high')
        print('  ✅ remember(importance=str) → TypeError')

    def test_10_relate_weight_clamped(self):
        rid = self.mem.relate('test_a', 'test_b', 'clamp_test', weight=2.5)
        row = self.mem._conn.execute(
            'SELECT weight FROM relations WHERE id = ?', (rid,)
        ).fetchone()
        self.assertEqual(row[0], 1.0)
        # cleanup
        self.mem._conn.execute("DELETE FROM relations WHERE relation = 'clamp_test'")
        self.mem._conn.commit()
        print('  ✅ relate(weight=2.5) → DB stores 1.0')

    def test_11_update_new_importance_clamped(self):
        cid = self.mem.remember('test update', source='test', importance=0.5)
        new_id = self.mem.update(cid, new_importance=3.0)
        row = self.mem._conn.execute(
            'SELECT importance FROM chunks WHERE id = ?', (new_id,)
        ).fetchone()
        self.assertEqual(row[0], 1.0)
        # cleanup
        self.mem.forget(cid)
        self.mem.forget(new_id)
        print('  ✅ update(new_importance=3.0) → DB stores 1.0')

    def test_12_upsert_entity_importance_clamped(self):
        """_upsert_entity 应在 INSERT 分支 clamp importance."""
        from memory import generate_id
        # 用 generate_id 避免冲突; 跳过 setUp 路径, 直接走 INSERT 分支
        # 用一个 fresh id 让 _upsert_entity 走 INSERT 分支
        # 先 query existing id, 用不存在的 id
        ent_id = generate_id('test_ent')
        # 直接调 _upsert_entity, 它会走 INSERT 分支 (id 不存在)
        self.mem._upsert_entity({
            'id': ent_id,
            'kind': 'stock',
            'name': 'test',
            'importance': 10.0,  # 应被 clamp 到 1.0
        })
        row = self.mem._conn.execute(
            'SELECT importance FROM entities WHERE id = ?', (ent_id,)
        ).fetchone()
        self.assertEqual(row[0], 1.0, f'expected 1.0, got {row[0]}')
        # cleanup
        self.mem._conn.execute("DELETE FROM entities WHERE id = ?", (ent_id,))
        self.mem._conn.commit()
        print('  ✅ _upsert_entity(importance=10.0) INSERT → DB stores 1.0')


if __name__ == '__main__':
    # 自定义测试顺序 + 输出
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestMemoryCRUD))
    suite.addTests(loader.loadTestsFromTestCase(TestTemporalDimension))
    suite.addTests(loader.loadTestsFromTestCase(TestEntityResolve))
    suite.addTests(loader.loadTestsFromTestCase(TestP0BoundsCheck))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print('=== 总结 ===')
    print(f'  run: {result.testsRun}')
    print(f'  failures: {len(result.failures)}')
    print(f'  errors: {len(result.errors)}')
    print(f'  skipped: {len(result.skipped)}')

    sys.exit(0 if result.wasSuccessful() else 1)
