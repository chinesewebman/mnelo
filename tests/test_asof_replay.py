"""
[7/21] asof 时间切片回归测试。

Bug: ARCHITECTURE.md 声称所有 4 路召回都接受 asof, 但实现里只有 graph 路
真正用了 asof — vector/meta/entity 三路 SQL 只过滤 `valid_until IS NULL`,
asof 参数被忽略。导致 "问 2026-06-01 时点 X" 的历史回放对 meta/entity 路不生效。

修复后: 每条 chunk 在 asof 时点有效 = `valid_until IS NULL OR valid_until > asof`
(meta/vector 路); entity 额外要求 `valid_from <= asof`。
默认 asof=now() 时行为与旧实现完全一致 (valid_until 不会在未来)。

本测试用 meta_only 路验证历史回放 (vector 路在 update()/forget() 时会物理删除
旧版本向量, 无法回放 — 这是已知限制, 见 ARCHITECTURE.md)。
"""

import time
import unittest

from memory import Memory


def _uniq(prefix="asof"):
    return f"{prefix}_{int(time.time() * 1_000_000)}"


class TestAsofHistoricalReplay(unittest.TestCase):
    """meta 路历史回放: update 后的旧版本在 asof < valid_until 时可见。"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        cls.token_old = _uniq("asof_old")
        cls.token_new = _uniq("asof_new")
        cls.old_content = f"asof_old {cls.token_old} 原始版本"
        cls.new_content = f"asof_new {cls.token_new} 修正版本"
        cls.cid1 = cls.mem.remember(cls.old_content, source="test_asof_replay", importance=0.5)
        # update → 旧版本 valid_until=now, 新版本 valid_until=NULL
        cls.cid2 = cls.mem.update(cls.cid1, reason="fix", new_content=cls.new_content)

    @classmethod
    def tearDownClass(cls):
        for cid in (cls.cid1, cls.cid2):
            try:
                cls.mem.forget(cid, target_kind="chunk", reason="test_cleanup")
            except Exception:
                pass
        cls.mem.close()

    def test_old_version_visible_before_valid_until(self):
        """asof 在过去 (update 之前) → 旧版本仍可见。"""
        past = "2000-01-01T00:00:00"
        results = self.mem.recall(self.token_old, top_k=5, strategy="meta_only", asof=past)
        self.assertTrue(
            any(self.token_old in h["content"] for h in results),
            f"asof={past} 应能看到旧版本 {self.token_old}, 实际 {[h['content'][:40] for h in results]}",
        )

    def test_old_version_hidden_after_valid_until(self):
        """asof 在遥远未来 (update 之后) → 旧版本不可见, 新版本可见。"""
        future = "2999-01-01T00:00:00"
        results = self.mem.recall(self.token_old, top_k=5, strategy="meta_only", asof=future)
        self.assertFalse(
            any(self.token_old in h["content"] for h in results),
            f"asof={future} 旧版本应已失效, 实际 {[h['content'][:40] for h in results]}",
        )
        new_results = self.mem.recall(self.token_new, top_k=5, strategy="meta_only", asof=future)
        self.assertTrue(any(self.token_new in h["content"] for h in new_results))

    def test_default_asof_equals_now(self):
        """不带 asof (默认 now) → 只有活跃版本, 行为与修复前一致。"""
        results = self.mem.recall(self.token_old, top_k=5, strategy="meta_only")
        self.assertFalse(any(self.token_old in h["content"] for h in results))
        new_results = self.mem.recall(self.token_new, top_k=5, strategy="meta_only")
        self.assertTrue(any(self.token_new in h["content"] for h in new_results))


class TestAsofVectorAndEntity(unittest.TestCase):
    """vector / entity 路也接受 asof 且不报错 (回归: 参数被忽略时代码仍可跑)。"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        cls.token = _uniq("asof_entity")
        cls.cid = cls.mem.remember(
            f"asof_entity {cls.token} 关联测试",
            source="test_asof_replay",
            importance=0.5,
            entities=[{"id": cls.token, "kind": "concept", "name": cls.token}],
        )

    @classmethod
    def tearDownClass(cls):
        try:
            cls.mem.forget(cls.cid, target_kind="chunk", reason="test_cleanup")
        except Exception:
            pass
        cls.mem.close()

    def test_vector_lane_with_asof(self):
        r = self.mem.recall(self.token, top_k=3, strategy="vector_only", asof="2000-01-01T00:00:00")
        self.assertIsInstance(r, list)

    def test_entity_lane_with_asof(self):
        # asof 未来 → entity 有效 (valid_from <= asof, valid_until NULL)
        r = self.mem.recall(self.token, top_k=3, strategy="entity_only", asof="2999-01-01T00:00:00")
        self.assertTrue(any(self.token in (h.get("entity_id") or "") for h in r))

    def test_entity_lane_excludes_future_entity(self):
        # asof 在 entity 创建之前 → 应被 valid_from 过滤掉 (回归: 旧实现忽略 asof 会误召回)
        r = self.mem.recall(self.token, top_k=3, strategy="entity_only", asof="2000-01-01T00:00:00")
        self.assertFalse(any(self.token in (h.get("entity_id") or "") for h in r))
