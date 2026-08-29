"""
[P1a E4] 写路径集成测试 — memory.py remember() + mcp_server memory_remember.

主人 TASKS_L2_EXTRACT §E4 验收:
  - remember("我偏好简洁日报") → chunk.memory_type == "preference"
  - remember("记录今天建仓了 sh600089") → "episode"
  - 显式传 "fact" → 保持 fact (不被规则覆盖)
  - 显式传 None → 触发分类 (或不传 = None)
  - 第三人称偏好 ("用户喜欢") → fact (P1a 弱标记)
  - 普通记录 (无强标记) → fact (默认)

零误伤 (§5.1.4): 6 核心接口行为不变, 不破坏现有测试。
"""

import unittest

from memory import Memory, norm_memory_type
from validation import MEMORY_TYPES


class TestRememberClassification(unittest.TestCase):
    """[E4] Memory.remember() 自动分类 + 显式类型尊重"""

    @classmethod
    def setUpClass(cls):
        cls.mem = Memory()
        cls.src = "test_classify_write"

    @classmethod
    def tearDownClass(cls):
        # [8/6 plan §10] 后端感知清理 (helper 先 _index.remove 再 DELETE chunks)
        from helpers import cleanup_chunks

        cleanup_chunks(cls.mem, source=cls.src)
        cls.mem._conn.commit()
        cls.mem.close()

    def _read_chunk_type(self, chunk_id: str) -> str:
        """读回 chunk 的 memory_type"""
        row = self.mem._conn.execute("SELECT memory_type FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        # row 不会是 None (cid 是新生成的); Pyright 期望 str
        assert row is not None
        result: str = row[0]
        return result

    def test_01_default_none_triggers_preference(self):
        """[§5.4.1] remember("我偏好简洁日报") (默认 None) → preference"""
        cid = self.mem.remember(
            content="我偏好简洁日报",
            source=self.src,
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "preference", f"应自动分类 preference, 实际: {mtype}")

    def test_02_default_none_triggers_episode(self):
        """[§5.4.2 主人 spec] remember("我今天建仓了 sh600089") (默认 None) → episode (我+时间+动作复合)"""
        cid = self.mem.remember(
            content="我今天建仓了 sh600089",
            source=self.src,
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "episode", f"应自动分类 episode (我+时间+动作), 实际: {mtype}")

    def test_03_explicit_fact_respected(self):
        """[§5.4.3] 显式传 memory_type='fact' (即使是 preference 内容) → 仍 fact"""
        cid = self.mem.remember(
            content="我偏好简洁日报",  # 内容是 preference, 但显式传 fact
            source=self.src,
            memory_type="fact",
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "fact", f"显式传 fact 必须尊重, 实际: {mtype}")

    def test_04_explicit_preference_respected(self):
        """显式传 memory_type='preference' → preference (即使内容弱)"""
        cid = self.mem.remember(
            content="今天天气不错",  # 内容是 fact, 但显式传 preference
            source=self.src,
            memory_type="preference",
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "preference", f"显式传 preference 必须尊重, 实际: {mtype}")

    def test_05_no_strong_marker_falls_back_to_fact(self):
        """[§5.5] 无强标记内容 + 默认 None → fact (调用方默认)"""
        cid = self.mem.remember(
            content="这是一段普通记录",
            source=self.src,
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "fact", f"无强标记应默认 fact, 实际: {mtype}")

    def test_06_third_person_preference_falls_back_to_fact(self):
        """[§5.4] 第三人称偏好 (无'我'主语) + 默认 None → fact (P1a 弱标记)"""
        cid = self.mem.remember(
            content="用户喜欢这个方案",
            source=self.src,
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "fact", f"第三人称偏好应默认 fact, 实际: {mtype}")

    def test_07_traditional_chinese_classified_correctly(self):
        """[E4 双语] 繁体中文 "我偏好簡潔日報" (默认 None) → preference (繁→简后命中)"""
        cid = self.mem.remember(
            content="我偏好簡潔日報",
            source=self.src,
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "preference", f"繁体应分类 preference, 实际: {mtype}")

    def test_08_english_classified_correctly(self):
        """[E4 双语] 英文 "I prefer the concise report" (默认 None) → preference"""
        cid = self.mem.remember(
            content="I prefer the concise report",
            source=self.src,
        )
        mtype = self._read_chunk_type(cid)
        self.assertEqual(mtype, "preference", f"英文应分类 preference, 实际: {mtype}")

    def test_09_all_5_types_classifiable(self):
        """[E4 全面性] 5 类型 P1a 全可分类 (v0.2 严格化: episode/preference/decision 用第一人称)"""
        cases = [
            ("我偏好简洁日报", "preference"),
            ("我决定明天减仓", "decision"),
            ("我今天建仓了 sh600089", "episode"),
            ("步骤 1. 启动\n步骤 2. 配置\n步骤 3. 测试", "procedure"),
            ("临时草稿，稍后处理", "ephemeral"),
        ]
        for content, expected in cases:
            with self.subTest(content=content[:20]):
                cid = self.mem.remember(content=content, source=self.src)
                mtype = self._read_chunk_type(cid)
                self.assertEqual(mtype, expected, f"内容 '{content[:20]}' 应分类 {expected}, 实际: {mtype}")

    def test_10_explicit_invalid_type_raises(self):
        """[安全] 显式传非法 memory_type → ValidationError (不变行为)"""
        with self.assertRaises(Exception) as ctx:
            self.mem.remember(
                content="任何内容",
                source=self.src,
                memory_type="invalid_type_xyz",
            )
        # norm_memory_type 抛 ValidationError

    def test_11_existing_chunks_unchanged(self):
        """[零误伤] 显式传 fact 的原有 chunk 没被改动 (P1a 只影响新写入)"""
        # 先写一个事实型
        cid = self.mem.remember(
            content="明天是周五",
            source=self.src,
            memory_type="fact",
        )
        # 读回
        mtype1 = self._read_chunk_type(cid)

        # 跑多次其他分类写入 (插 5 个), 不应影响这个 chunk 的类型
        for content in ["我偏好简洁日报", "临时草稿", "今天建仓了"]:
            self.mem.remember(content=content, source=self.src)

        # 回到原 chunk, 类型应仍是 fact
        mtype2 = self._read_chunk_type(cid)
        self.assertEqual(mtype1, mtype2, "原 chunk 类型不应被改动")
        self.assertEqual(mtype2, "fact")


if __name__ == "__main__":
    unittest.main()
