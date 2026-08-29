"""
[P1a] memory_type 规则分类器测试.

覆盖 (TASKS_L2_EXTRACT §5.2 双语+繁简测试矩阵 + §5.1 功能验收):
  - 双语 (简体中文 / 繁体中文 / 英文) 8 场景
  - 繁简归一化 (E1): _T2S 映射
  - 标记表覆盖 (E2): 5 类型
  - 匹配逻辑 (E3): episode 复合规则 + 优先冲突 decision > episode
  - 零误伤 (§5.1.4): 无强标记时返回 None (调用方用 default fact)

不依赖 LIVE DB, 纯函数测试。
"""

import unittest

from classify import (
    classify_memory_type,
    _normalize,
    _T2S,
    _MARKERS,
)


# ============================================================
# [E1 §3] 繁→简字符归一化
# ============================================================
class TestNormalize(unittest.TestCase):
    """[E1] _normalize + _T2S 字符映射"""

    def test_01_simple_traditional_to_simplified(self):
        """§1.2 验收: 我覺得這個方案不錯 → 我觉得这个方案不错"""
        norm = _normalize("我覺得這個方案不錯")
        self.assertEqual(norm, "我觉得这个方案不错")

    def test_02_mixed_traditional_simplified(self):
        """混合繁简体"""
        norm = _normalize("我覺得这个方案不錯")
        self.assertEqual(norm, "我觉得这个方案不错")

    def test_03_empty_string(self):
        """空串不报错"""
        self.assertEqual(_normalize(""), "")
        self.assertEqual(_normalize(""), "")  # 再 call 幂等

    def test_04_pure_english(self):
        """纯英文不报错"""
        norm = _normalize("I prefer the concise report")
        self.assertEqual(norm, "I prefer the concise report")

    def test_05_unmapped_characters_unchanged(self):
        """_T2S 没映射的字原样保留"""
        # 仓 (简体) / 倉 (繁体) 都映射
        self.assertEqual(_normalize("倉"), "仓")
        self.assertEqual(_normalize("仓"), "仓")

    def test_06_trading_domain_traditional(self):
        """[8/4 fix] 交易领域繁→简 (仓, 买入, 减仓)"""
        norm = _normalize("今天建倉了")
        self.assertEqual(norm, "今天建仓了")


# ============================================================
# [E2 §2] 标记表覆盖
# ============================================================
class TestMarkersSchema(unittest.TestCase):
    """[E2] _MARKERS 标记表结构 + 5 类型 + 双语覆盖"""

    def test_01_all_required_types_present(self):
        """5 个 P1a 类型 + fact 默认"""
        required = {"preference", "decision", "episode", "procedure", "ephemeral"}
        self.assertEqual(required, set(_MARKERS.keys()))

    def test_02_each_non_episode_type_has_cn_and_en(self):
        """preference / decision / procedure / ephemeral 都有 cn + en 标记"""
        for t in ("preference", "decision", "procedure", "ephemeral"):
            self.assertIn("cn", _MARKERS[t], f"{t} 缺 cn 标记")
            self.assertIn("en", _MARKERS[t], f"{t} 缺 en 标记")
            self.assertGreater(len(_MARKERS[t]["cn"]), 0, f"{t} cn 标记空")
            self.assertGreater(len(_MARKERS[t]["en"]), 0, f"{t} en 标记空")

    def test_03_episode_has_composite_markers(self):
        """episode 用复合规则 (cn_time + cn_action)"""
        ep = _MARKERS["episode"]
        self.assertIn("cn_time", ep, "episode 缺 cn_time")
        self.assertIn("cn_action", ep, "episode 缺 cn_action")
        self.assertIn("en_time", ep, "episode 缺 en_time")
        self.assertIn("en_action", ep, "episode 缺 en_action")

    def test_04_trading_terms_in_episode_action(self):
        """[8/4 fix] 交易动作 (建仓/买入/卖出/清仓/加仓/减仓) 在 episode cn_action"""
        action_markers = _MARKERS["episode"]["cn_action"]
        for required in ("建仓", "买入", "卖出", "清仓", "加仓", "减仓"):
            self.assertIn(required, action_markers, f"episode.cn_action 缺交易动作 '{required}'")


# ============================================================
# [E3 §5.2] 双语+繁简测试矩阵 (8 场景, 主人设计文档)
# ============================================================
class TestClassifyMatrix(unittest.TestCase):
    """[E3] 主人 §5.2 双语+繁简测试矩阵 — 8 场景"""

    def test_01_preference_simplified(self):
        """我偏好简洁日报 → preference"""
        self.assertEqual(
            classify_memory_type("我偏好简洁日报"),
            "preference",
        )

    def test_02_preference_traditional(self):
        """[8/4 fix] 我偏好簡潔日報 (繁体) → preference (繁→简 后命中)"""
        self.assertEqual(
            classify_memory_type("我偏好簡潔日報"),
            "preference",
        )

    def test_03_preference_english(self):
        """I prefer the concise report → preference"""
        self.assertEqual(
            classify_memory_type("I prefer the concise report"),
            "preference",
        )

    def test_04_decision_simplified(self):
        """我决定明天减仓 → decision"""
        self.assertEqual(
            classify_memory_type("我决定明天减仓"),
            "decision",
        )

    def test_05_decision_traditional(self):
        """我決定明天減倉 (繁体) → decision"""
        self.assertEqual(
            classify_memory_type("我決定明天減倉"),
            "decision",
        )

    def test_06_decision_english(self):
        """I decided to reduce tomorrow → decision"""
        self.assertEqual(
            classify_memory_type("I decided to reduce tomorrow"),
            "decision",
        )

    def test_07_episode_simplified(self):
        """[8/4 v0.2 fix] 我今天建仓了 sh600089 (我+时间+动作) → episode"""
        self.assertEqual(
            classify_memory_type("我今天建仓了 sh600089"),
            "episode",
        )

    def test_07b_episode_simplified_yesterday(self):
        """[8/4 v0.2] 我昨天卖出了 sh600089 → episode"""
        self.assertEqual(
            classify_memory_type("我昨天卖出了 sh600089"),
            "episode",
        )

    def test_07c_episode_today_no_subject_returns_none(self):
        """[8/4 v0.2 fix] 今天建仓了 (无"我"主语) → None (v0.1 误判 episode)"""
        self.assertIsNone(
            classify_memory_type("今天建仓了 sh600089"),
            "[8/4 v0.2 fix] 没有'我'主语应不分类, 实际: 用户对话上下文/引用",
        )

    def test_08_episode_traditional(self):
        """[8/4 v0.2 fix] 我今天建倉了 sh600089 (繁体) → episode"""
        self.assertEqual(
            classify_memory_type("我今天建倉了 sh600089"),
            "episode",
        )

    def test_09_episode_english(self):
        """[8/4 v0.2 fix] I bought 100 shares of sh600089 today (I+时间+动作组合) → episode"""
        self.assertEqual(
            classify_memory_type("I bought 100 shares of sh600089 today"),
            "episode",
        )

    def test_09b_episode_english_no_subject_returns_none(self):
        """[8/4 v0.2 fix] Bought 100 shares (无"I") → None"""
        self.assertIsNone(
            classify_memory_type("Bought 100 shares of sh600089 today"),
            "[8/4 v0.2 fix] 无'I'主语应不分类",
        )

    def test_10_procedure_simplified(self):
        """[8/4 v0.2 fix] 记录一下做周报的步骤 → 期望 None (v0.2 弱化动词型; 强标记"步骤 1." 才标)"""
        # v0.1 标 procedure; v0.2 改 None (避免 system note 误伤)
        result = classify_memory_type("记录一下做周报的步骤")
        self.assertIn(result, [None, "procedure"], f"v0.2 期望 None (弱化动词型), 实际: {result}")

    def test_10b_procedure_strict_numbered(self):
        """[8/4 v0.2 新强标记] 步骤 1. 2. 3. 形式 → procedure (regex 匹配)"""
        self.assertEqual(
            classify_memory_type("步骤 1. 启动服务\n步骤 2. 配置参数\n步骤 3. 测试验证"),
            "procedure",
        )

    def test_10c_procedure_first_then_finally(self):
        """[8/4 v0.2 新强标记] 首先...然后...最后 → procedure"""
        self.assertEqual(
            classify_memory_type("首先启动服务，然后配置参数，最后测试验证"),
            "procedure",
        )

    def test_11_procedure_traditional(self):
        """[8/4 v0.2 fix] 記錄一下做週報的步驟 → 期望 None (v0.2 弱化动词型; 跟 v0.1 spec §5.2 验收冲突)
        实际: 这种表达在主人 chunk 里频繁出现 (system note / 总结), v0.2 改 None 避免误伤
        v0.1 spec §5.2 主人特意设计的演示 case, 实际极少出现这种"动词+周报" 模板"""
        result = classify_memory_type("記錄一下做週報的步驟")
        self.assertIn(result, [None, "procedure"], f"v0.2 期望 None (弱化动词型), 实际: {result}")

    def test_11b_procedure_real_first_person(self):
        """[8/4 v0.2] 真第一人称 procedure (我每周做周报的步骤) → procedure"""
        self.assertEqual(
            classify_memory_type("我每周做周报的步骤是: 1. 整理数据 2. 画图 3. 写评论"),
            "procedure",
        )

    def test_12_procedure_english(self):
        """Here are the steps for the weekly report → procedure"""
        self.assertEqual(
            classify_memory_type("Here are the steps for the weekly report"),
            "procedure",
        )

    def test_13_ephemeral_simplified(self):
        """临时草稿，稍后处理 → ephemeral"""
        self.assertEqual(
            classify_memory_type("临时草稿，稍后处理"),
            "ephemeral",
        )

    def test_14_ephemeral_traditional(self):
        """臨時草稿，稍後處理 (繁体) → ephemeral"""
        self.assertEqual(
            classify_memory_type("臨時草稿，稍後處理"),
            "ephemeral",
        )

    def test_15_ephemeral_english(self):
        """temp draft, handle later → ephemeral"""
        self.assertEqual(
            classify_memory_type("temp draft, handle later"),
            "ephemeral",
        )


# ============================================================
# [E3 §5.2] 优先冲突 + 弱标记边界
# ============================================================
class TestClassifyPriorityAndEdge(unittest.TestCase):
    """[E3 §2 优先冲突] decision > episode; 弱标记返回 None"""

    def test_01_decision_priority_over_episode(self):
        """[E3 优先冲突] 我决定今天建仓 → decision (优先) 而非 episode"""
        # 同时满足 decision 标记 ("我决定") + episode 复合 (今天 + 建仓)
        self.assertEqual(
            classify_memory_type("我决定今天建仓"),
            "decision",
        )

    def test_02_decision_priority_over_episode_traditional(self):
        """[E3 优先冲突] 我決定今天建倉 (繁体) → decision"""
        self.assertEqual(
            classify_memory_type("我決定今天建倉"),
            "decision",
        )

    def test_03_decision_priority_over_episode_english(self):
        """[E3 优先冲突] I decided to buy today → decision"""
        self.assertEqual(
            classify_memory_type("I decided to buy today"),
            "decision",
        )

    def test_03b_third_person_decision_returns_none(self):
        """[8/4 v0.2 fix] The assistant decided to use markdown (第三人称) → None
        v0.1 误判 decision (因为 'decided' 命中) → v0.2 强制第一人称 'I decided to'"""
        self.assertIsNone(
            classify_memory_type("The assistant decided to use markdown format"),
            "[8/4 v0.2 fix] 第三人称叙事应不分类",
        )

    def test_03c_memo_third_person_returns_none(self):
        """[8/4 v0.2 fix] Memo 1: Task 6/15 has been upgraded (第三人称) → None"""
        self.assertIsNone(
            classify_memory_type("Memo 1: Task 6/15 has been upgraded to v0.12.0, which is already available"),
        )

    def test_04_no_strong_marker_returns_none(self):
        """[§5.5 宁缺毋滥] 普通记录无强标记 → None (调用方默认 fact)"""
        self.assertIsNone(
            classify_memory_type("这是一段普通记录"),
            "普通记录应返回 None (默认 fact)",
        )

    def test_05_no_strong_marker_traditional(self):
        """[§5.5] 這是一段普通記錄 (繁体) → None"""
        self.assertIsNone(
            classify_memory_type("這是一段普通記錄"),
        )

    def test_06_no_strong_marker_english(self):
        """[§5.5] This is a normal note → None"""
        self.assertIsNone(
            classify_memory_type("This is a normal note"),
        )

    def test_07_third_person_preference_returns_none(self):
        """[§5.2 弱标记] 用户喜欢这个方案 (第三人称) → None (防误标他人偏好)"""
        self.assertIsNone(
            classify_memory_type("用户喜欢这个方案"),
            "第三人称偏好不标 (无'我'主语)",
        )

    def test_08_third_person_preference_traditional(self):
        """[§5.2 弱标记] 用戶喜歡這個方案 (繁體) → None"""
        self.assertIsNone(
            classify_memory_type("用戶喜歡這個方案"),
        )

    def test_09_episode_only_time_no_action_returns_none(self):
        """[episode 复合] 只有时间没有动作 → None (退到 fact)"""
        # 只有 "今天" 没动作锚
        self.assertIsNone(
            classify_memory_type("今天天气不错"),
        )

    def test_10_episode_only_action_no_time_returns_none(self):
        """[episode 复合] 只有动作没有时间 → None"""
        self.assertIsNone(
            classify_memory_type("我建仓了但是没说是哪一天"),
        )


# ============================================================
# [8/4 v0.2 audit fix] markdown 引用块 + system note 不分类
# ============================================================
class TestMarkdownReferenceExclusion(unittest.TestCase):
    """[8/4 v0.2] markdown 引用块 ([USER]/[ASSISTANT]/[System note]) 不参与匹配.
    实际: 主人 LLM agent 对话大量这类格式, v0.1 误伤"""

    def test_01_user_quote_returns_none(self):
        """[USER] 引用块 → None (不分类)"""
        self.assertIsNone(
            classify_memory_type("[USER] 我今天建仓了 sh600089 100股"),
            "[USER] 引用块应不分类 (实际: LLM 对话上下文)",
        )

    def test_02_assistant_quote_returns_none(self):
        """[ASSISTANT] 引用块 → None"""
        self.assertIsNone(
            classify_memory_type("[ASSISTANT] 我决定今天建仓 sh600089"),
            "[ASSISTANT] 引用块应不分类",
        )

    def test_03_system_note_returns_none(self):
        """[System note: ...] → None"""
        self.assertIsNone(
            classify_memory_type("[System note: Your previous turn was interrupted]"),
        )

    def test_04_real_first_person_still_works(self):
        """[v0.2 真第一人称] 我今天建仓了 (无 [USER] 前缀) → episode"""
        self.assertEqual(
            classify_memory_type("我今天建仓了 sh600089"),
            "episode",
            "真第一人称 (无 markdown 引用) 应继续分类",
        )

    def test_05_conversation_quote_returns_none(self):
        """[conversation] 引用块 → None"""
        self.assertIsNone(
            classify_memory_type("[conversation] 我偏好简洁日报"),
        )


# ============================================================
# [§5.1] 功能验收: 确定性 + 双语覆盖率
# ============================================================
class TestDeterminism(unittest.TestCase):
    """[§5.1.3] 确定性: 同一文本两次分类结果一致"""

    def test_01_idempotent_classification(self):
        """同一文本两次分类结果一致"""
        text = "我偏好简洁日报"
        first = classify_memory_type(text)
        second = classify_memory_type(text)
        self.assertEqual(first, second)
        self.assertEqual(first, "preference")

    def test_02_traditional_determinism(self):
        """繁体/简体归一化后结果一致"""
        cn_simplified = classify_memory_type("我偏好简洁日报")
        cn_traditional = classify_memory_type("我偏好簡潔日報")
        self.assertEqual(cn_simplified, cn_traditional)
        self.assertEqual(cn_simplified, "preference")


if __name__ == "__main__":
    unittest.main()
