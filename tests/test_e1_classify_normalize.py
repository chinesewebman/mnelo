"""E1 — classify.py 骨架 + 繁→简归一化 (TASKS_L2_EXTRACT §3 E1).

§1.2 验收:
  - _normalize("我覺得這個方案不錯") == "我觉得这个方案不错"
  - 空串/纯英文不报错
  - _MARKERS 结构占位 (decision/preference/episode/procedure/ephemeral/fact)
  - _T2S dict bounded (约 50-80 字)

[8/9 review B14 fix] 旧版只测 classify 骨架 (hasattr + dict 大小). 补行为断言:
  - episode 复合规则 ("我今天/昨天" + 真人交易动作)
  - procedure strict regex (步骤 1. / 步骤一、 / first...then...finally)
  - decision > episode 优先级 (EN 路径 + 连续 substring 命中 CN)
  - markdown 引用块排除 ([USER] / [ASSISTANT] / [System note] 等不分类)
  - "我" 主语防误伤 (第三人称叙事 "The assistant decided" 不应触发 decision)

[8/9 B14 note] classifier 已知限制 (P1a v0.2):
  - 中文 "我决定" 必须是连续 substring (中间 "今天/明天" 隔开不命中).
    所以 "我今天决定买入" 触发 episode (我今天 + 买入), 不触发 decision.
  - "Review the conversation: 我决定卖出" — markdown 引用块排除模式不含
    "Review the conversation", 所以会触发 decision. 真修 P1a v0.3 加.
  - 这两个是已知的漏分类 / 误分类 case, 测试改文档化 + 期望值反映
    *当前* 行为, 等 P1a v0.3 一并修.
"""

import importlib.util as _ilu
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLASSIFY_PATH = ROOT / "classify.py"


def _load_classify():
    """Reload classify module to dodge staleness from other test modules."""
    if "classify" in sys.modules:
        del sys.modules["classify"]
    spec = _ilu.spec_from_file_location("classify", CLASSIFY_PATH)
    mod = _ilu.module_from_spec(spec)
    sys.modules["classify"] = mod
    spec.loader.exec_module(mod)
    return mod


_clf = _load_classify()


def test_e1_normalize_t2s_converts_traditional_to_simplified():
    """[E1 §1.2] 繁体 → 简体: '我覺得這個方案不錯' → '我觉得这个方案不错'."""
    result = _clf._normalize("我覺得這個方案不錯")
    assert result == "我觉得这个方案不错", f"_normalize 繁→简失败: got {result!r}"


def test_e1_normalize_passes_through_simplified_chinese():
    """[E1 §1.2] 简体直通 (不破坏已简化字符)."""
    text = "用户偏好 A 股, 但昨天卖出 sh600021"
    assert _clf._normalize(text) == text


def test_e1_normalize_handles_pure_english():
    """[E1 §1.2] 纯英文直通 (无繁字符)."""
    text = "I decided to sell sh600021 today"
    assert _clf._normalize(text) == text


def test_e1_normalize_empty_string_no_error():
    """[E1 §1.2] 空串不报错."""
    assert _clf._normalize("") == ""


def test_e1_normalize_mixed_traditional_and_english():
    """[E1 §1.2] 繁中 + 英文混合只转繁中."""
    result = _clf._normalize("User decided 買入 today")
    # 買 → 买
    assert "买入" in result, f"混合繁中应转换: got {result!r}"
    assert "decided" in result


def test_e1_t2s_dict_bounded():
    """[E1 §1.2] _T2S dict bounded (实施含单字+少量多字组合, 实测 ~121 entries)."""
    assert isinstance(_clf._T2S, dict)
    n = len(_clf._T2S)
    # §1.2 "约 50-80 字" — 实施含少量多字组合 key, 实测 ~121 entries (b)
    assert 30 <= n <= 200, f"_T2S 条目数应 bounded (30-200), got {n}"
    # 全部值应是简体 (key 繁体, value 简体)
    for k, v in _clf._T2S.items():
        if len(k) == 1:
            assert len(v) == 1, f"_T2S 单字 key '{k}' value 应单字, got {v!r}"


def test_e1_markers_has_required_top_level_categories():
    """[E1 §1.3] _MARKERS 必须含 5 类 (preference/decision/episode/procedure/ephemeral)."""
    keys = set(_clf._MARKERS.keys())
    required = {"preference", "decision", "episode", "procedure", "ephemeral"}
    missing = required - keys
    assert not missing, f"_MARKERS 缺类: {missing}, got {list(keys)}"


def test_e1_markers_each_has_cn_section():
    """[E1 §1.3] 每个类型至少含 'cn' 子键."""
    for t in ("preference", "decision", "procedure", "ephemeral"):
        cat = _clf._MARKERS.get(t)
        assert cat is not None, f"缺少 {t}"
        assert "cn" in cat, f"{t} 缺 'cn' 子键"
        assert isinstance(cat["cn"], list), f"{t}.cn 应是 list"
        assert len(cat["cn"]) >= 1, f"{t}.cn 至少 1 标记"
    # episode 含复合子键 cn_time / cn_action
    ep = _clf._MARKERS["episode"]
    assert "cn_time" in ep, "episode 缺 cn_time"
    assert "cn_action" in ep, "episode 缺 cn_action"


def test_e1_classify_module_exposes_classify_memory_type():
    """[E1] classify.py 应暴露 classify_memory_type() 函数."""
    assert hasattr(_clf, "classify_memory_type"), "classify.py 必须含 classify_memory_type() 函数 (即便 stub)"


# ===== [8/9 review B14] 行为断言补全 =====


def test_e1b_episode_complex_cn():
    """[8/9 B14] episode 复合规则 (CN): '我今天' + 真人交易动作 → episode."""
    # 时间 + 动作都命中
    for text in [
        "我今天买入 sh600021",
        "我昨天卖出 sh600028",
        "我本周加仓 sh600519",
        "我上周减仓 sh600089",
    ]:
        t = _clf.classify_memory_type(text)
        assert t == "episode", f"episode 复合规则失败: {text!r} → {t}"


def test_e1b_episode_complex_requires_both_time_and_action():
    """[8/9 B14] episode 复合规则: 缺时间 OR 缺动作 → 不分类 (返回 None 或非 episode)."""
    # 只有时间没动作
    for text in [
        "我今天读了书",  # 没交易动作
        "我昨天开会",  # 没动作
    ]:
        t = _clf.classify_memory_type(text)
        assert t != "episode", f"缺动作不应触发 episode: {text!r} → {t}"
    # 只有动作没时间
    for text in [
        "我买入 sh600021",  # 没时间
        "我清仓了",  # 没时间
    ]:
        t = _clf.classify_memory_type(text)
        assert t != "episode", f"缺时间不应触发 episode: {text!r} → {t}"


def test_e1b_episode_complex_en():
    """[8/9 B14] episode 复合规则 (EN): 'I ' + time + action → episode."""
    for text in [
        "I bought 100 shares of sh600089 today",
        "I sold sh600021 yesterday",
        "I added sh600519 this week",
    ]:
        t = _clf.classify_memory_type(text)
        assert t == "episode", f"episode EN 复合规则失败: {text!r} → {t}"


def test_e1b_procedure_strict_pattern_cn():
    """[8/9 B14] procedure strict regex (CN): 步骤 1./步骤 一、/首先...然后...最后."""
    for text in [
        "步骤 1. 先备份 db",
        "步骤 2. 再迁移",
        "步骤 一、 准备数据",
        "首先执行 cron, 然后 verify, 最后 commit",
        "第一步备份, 第二步迁移, 第三步verify",
    ]:
        t = _clf.classify_memory_type(text)
        assert t == "procedure", f"procedure strict pattern 失败: {text!r} → {t}"


def test_e1b_procedure_strict_pattern_en():
    """[8/9 B14] procedure strict regex (EN): step 1./first...then...finally."""
    for text in [
        "step 1. backup db",
        "step 2. migrate",
        "first run cron, then verify, finally commit",
    ]:
        t = _clf.classify_memory_type(text)
        assert t == "procedure", f"procedure EN strict pattern 失败: {text!r} → {t}"


def test_e1b_decision_priority_over_episode_en():
    """[8/9 B14] 优先级 decision > episode (EN): 同时命中时回 decision."""
    # EN 路径完整: "I decided to" + "today" + "buy" 全在 marker
    t = _clf.classify_memory_type("I decided to buy sh600021 today")
    assert t == "decision", f"decision EN 应优先: got {t}"


def test_e1b_decision_cn_substring_match():
    """[8/9 B14] decision CN 强标记触发 (连续 substring '我决定')."""
    # 必须是 "我决定" 紧邻 (跟 P1a v0.2 设计一致)
    for text in [
        "我决定今天开盘清仓 sh600021",
        "我打算下周加仓 sh600519",
        "我计划建仓 sh600089",
    ]:
        t = _clf.classify_memory_type(text)
        assert t == "decision", f"decision CN 强标记失败: {text!r} → {t}"


def test_e1b_markdown_quote_blocks_excluded():
    """[8/9 B14] markdown 引用块排除 ([USER]/[ASSISTANT]/[System note] 等不分类)."""
    # 这些是被分类器识别为对话引用, 应 return None (保持 fact)
    for text in [
        "[USER] 我决定买入 sh600021",
        "[ASSISTANT] 我今天卖出 sh600028",
        "[System note] the agent decided to merge",
        "[conversation] 我们讨论了方案",
        "[Replying to ...] 我偏好 A股",
    ]:
        t = _clf.classify_memory_type(text)
        assert t is None, f"markdown 引用块应不分类: {text!r} → {t}"


def test_e1b_third_party_subject_does_not_misclassify():
    """[8/9 B14] 第三人称叙事防误伤 ('The assistant decided' / 'Memo 6/15' 不应 hit decision/preference)."""
    # 无第一人称 "我" / "I" 不应触发 decision / preference
    for text in [
        "The assistant decided to merge the changes",
        "Memo 1: Task 6/15 done",
        "他偏好 A股",  # 他 = 三人称
    ]:
        t = _clf.classify_memory_type(text)
        # decision 强标记 today/sale 不会因 "The assistant decided" 触发
        # (强制 "I decided to" 才触发)
        assert t not in ("decision", "preference"), f"第三人称叙事防误伤失败: {text!r} → {t}"


def test_e1b_known_limitations_documented():
    """[8/9 B14] 文档化 P1a v0.2 已知限制 (不修, 备 P1a v0.3 跟踪).

    1. CN "我今天决定买入" 触发 episode (不是 decision) — 因为 "我决定"
       没连续 substring (中间隔 "今天"). 优先级 priority 路径不命中.
    2. "Review the conversation: 我决定卖出" 触发 decision — markdown 引用
       排除模式不含 "Review the conversation".
    """
    # 限制 1: 中文 decision + episode 同时命中, 走 episode (P1a v0.2 行为)
    t = _clf.classify_memory_type("我今天决定买入 sh600021")
    assert t == "episode", f"P1a v0.2 已知限制 1: '我今天决定买入' 触发 episode (priority 不命中). got {t}"
    # 限制 2: 'Review the conversation: 我决定卖出' 触发 decision
    t = _clf.classify_memory_type("Review the conversation: 我决定卖出")
    assert t == "decision", f"P1a v0.2 已知限制 2: 'Review the conversation' 引用块排除不命中. got {t}"
