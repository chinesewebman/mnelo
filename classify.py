"""
classify.py — P1a 记忆类型规则分类器 (DESIGN §5.2).

[8/4 实际驱动] v0.3 报告 §2: 4344/4344 chunks 100% fact (6 类系统空架子).
本模块**零 LLM** — P1a 规则分类, 给写路径/回填用; P1b (LLM 语义分类) 是后续阶段.

接口契约 (TASKS_L2_EXTRACT §1.1):
    classify_memory_type(text: str) -> Optional[str]
    - 命中强标记 → 返回 memory_type ('preference' / 'decision' / 'episode' / 'procedure' / 'ephemeral')
    - 无命中 / 弱标记 → 返回 None (调用方保持默认 fact)

设计原则 (DESIGN §5.5 宁缺毋滥):
    - 强标记才分类; 模糊 / 无标记 → 保持 fact (留给 P1b LLM)
    - 高精度优先, 召回其次 — 错误的类型比没类型更有害
    - episode 用复合规则 (时间 AND 动作都命中)
    - 优先级 decision > episode > 其余 (决策优先, 因为决策常伴随事件)

技术决策 (TASKS_L2_EXTRACT §1.2):
    - 繁→简归一化用 bounded dict _T2S (零依赖, 50-80 字), 不依赖 opencc
    - 标记集单一事实源 (繁→简后统一简体匹配), 不维护双份表
    - 字符集膨胀 (几百字) 时再评估 opencc
"""

from __future__ import annotations

from typing import Dict, List, Optional

__all__ = ["classify_memory_type", "_normalize", "_T2S", "_MARKERS"]


# ========================================
# [TASKS_L2_EXTRACT §1.2] 繁→简字符归一化
# ========================================
_T2S: Dict[str, str] = {
    # 常見高頻繁體字 (按 §2.2 標記集實際出現的字補全)
    "覺": "觉",
    "歡": "欢",
    "買": "买",
    "賣": "卖",
    "時": "时",
    "點": "点",
    "倉": "仓",
    "減": "减",
    "暫": "暂",
    "訂": "订",
    "佔": "占",
    "後": "后",
    "於": "于",
    "較": "较",
    "擇": "择",
    "標": "标",
    "計": "计",
    "劃": "划",
    "務": "务",
    "應": "应",
    "會": "会",
    "個": "个",
    "這": "这",
    "麼": "么",
    "說": "说",
    "讓": "让",
    "種": "种",
    "樣": "样",
    "對": "对",
    "現": "现",
    "們": "们",
    "為": "为",
    "與": "与",
    "從": "从",
    "來": "来",
    "價": "价",
    "報": "报",
    "記": "记",
    "錄": "录",
    "細": "细",
    "潔": "洁",
    "簡": "简",
    "單": "单",
    "處": "处",
    "預": "预",
    "響": "响",
    "達": "达",
    "測": "测",
    "準": "准",
    "確": "确",
    "實": "实",
    "際": "际",
    "盤": "盘",
    "滿": "满",
    "觀": "观",
    "眾": "众",
    "腦": "脑",
    "體": "体",
    "貨": "货",
    "幣": "币",
    "變": "变",
    "動": "动",
    "態": "态",
    "麵": "面",
    "條": "条",
    "齊": "齐",
    "備": "备",
    "無": "无",
    "設": "设",
    "識": "识",
    "監": "监",
    "聽": "听",
    "聲": "声",
    "東": "东",
    "兒": "儿",
    "員": "员",
    # === [8/4 fix] 测试矩阵失败补全 ===
    "錯": "错",
    "案": "案",
    "不": "不",  # 案 不 不 都不需要 (本来就是简体)
    "決": "决",
    "定": "定",
    "明天": "明天",  # 明天 原简体
    "減倉": "减仓",
    "建倉": "建仓",
    "加倉": "加仓",
    "賣出": "卖出",
    "買入": "买入",
    "清倉": "清仓",
    # 测试 §5.2 场景需要全部繁→简
    "簡潔": "简洁",
    "日報": "日报",
    "記錄": "记录",
    "週報": "周报",
    "步驟": "步骤",
    "方法": "方法",
    "怎": "怎",  # 怎么 简
    "如何": "如何",
    "通常": "通常",
    "這樣": "这样",
    "每次": "每次",
    "模板": "模板",
    "規範": "规范",
    "臨時": "临时",
    "草稿": "草稿",
    "待定": "待定",
    "處理": "处理",
    "稍後": "稍后",
    "佔位": "占位",
    "暫定": "暂定",
    "計畫": "计划",
    "目標": "目标",
    "判斷": "判断",
    "選擇": "选择",
    "決定": "决定",
    "打算": "打算",
    "計劃": "计划",
    "目標是": "目标是",
    # 测试需要的"我決定..." / "我計劃..." / "我選擇..."
    "偏好": "偏好",
    "喜歡": "喜欢",
    "希望": "希望",
    "想要": "想要",
    # 普通名词 (繁简都是简体的, 强调)
    "對象": "对象",
}


def _normalize(text: str) -> str:
    """繁→簡字符归一化 + 简单处理空串/纯英文.

    Args:
        text: 任意文本

    Returns:
        归一化后的文本 (繁→簡字符已替换)
    """
    if not text:
        return text
    return "".join(_T2S.get(ch, ch) for ch in text)


# ========================================
# [TASKS_L2_EXTRACT §2 + P1a v0.2 audit fix 8/4] 标记集
# ========================================
# [8/4 v0.2 实际审计 fix] 主人实际 4348 chunks 跑一遍发现 4 大误伤:
#   - procedure 16.3% 大部分是 system note / 报告格式 / 上下文 (主人对话 system prompt)
#   - preference 5+ 误伤 (第三/二人称引用 + 标题含"我")
#   - decision 6+ 误伤 (第三人称叙事: "The assistant decided" / "Memo 1: Task 6/15")
#   - episode 18 误伤 ("Review the conversation" 指令 + 上下文引用)
#
# 修法 (P1a v0.2):
#   - 强标记必须带"我"/"I" 主语 (偏好/决策统一规则, 防第三人称叙事误伤)
#   - episode 强标记要求"我今天/我昨天" + 真人交易动作, 排除"对话引用"
#   - procedure 弱化动词型 ("记录一下"/"记一下"), 加"步骤 1./2./3." 强标记过滤
#   - markdown 引用块 ([USER]/[ASSISTANT]/[System note]) 不参与匹配 (新加)
_MARKERS: Dict[str, Dict[str, List[str]]] = {
    # preference (偏好): 必须"我"+第一人称, 防第三人称误伤
    "preference": {
        "cn": [
            "我偏好",
            "我喜欢",
            "更喜欢",
            "我更喜欢",
            "我更喜欢",
            "我更喜欢",
            "我比较喜欢",
            "我希望",
            "我想要",
            "我希望",
            # [8/4 v0.2 fix] "倾向于" 必须是 "我倾向于" 才标
            "我倾向于",
        ],
        "en": [
            "i prefer",
            "i prefer to",
            "i like",
            "i'd like",
            "my favorite",
            "i would rather",
            "i like to",
            # [8/4 v0.2 fix] "i'd rather" / "i'd prefer" 等第一人称变体
            "i'd rather",
            "i'd prefer",
        ],
        # 弱标记 (不触发, 仅文档化)
        "_weak_cn": ["倾向于", "喜欢", "爱好", "偏爱"],
        "_weak_en": ["prefer", "like", "favorite"],
    },
    # decision (决定/计划): 必须 "I decided to" / "I plan to", 去掉 "decided" 裸
    "decision": {
        "cn": [
            "我决定",
            "我打算",
            "我计划",
            "我的目标是",
            "我的判断是",
            "我选择",
            "我决定不",
            "我决定做",
        ],
        "en": [
            # [8/4 v0.2 fix] 强制第一人称 + "to" 后缀, 防 "The assistant decided to use" 误伤
            "i decided",
            "i decided to",
            "i decide",
            "i plan to",
            "i plan",
            "my decision is",
            "my call is",
            "i am going to",  # "I am going to" 是强决策意图
        ],
    },
    # episode (事件): 复合规则 - "我"+时间+动作, 防上下文/引用误伤
    "episode": {
        # [8/4 v0.2 fix] 时间锚必须含 "我" 主语前缀
        "cn_time": [
            "我今天",
            "我昨天",
            "我前天",
            "我本周",
            "我上周",
            "我今早",
            "我昨晚",
            "我今晨",
            "我今午",
        ],
        "cn_action": [
            "建仓",
            "买入",
            "卖出",
            "清仓",
            "加仓",
            "减仓",
            "开了",
            "平了",
        ],
        # [8/4 v0.2 fix] EN: 拆 "I" + 时间 + 动作 各部分, substring 组合匹配
        # (实际 "I bought 100 shares of sh600089 today" 中间有 token 间隔)
        "en_subject": ["i "],  # "i " 开头 (i 后空格)
        "en_time": [
            "today",
            "yesterday",
            "this morning",
            "last week",
            "this week",
        ],
        "en_action": [
            "bought",
            "sold",
            "added",
            "reduced",
            "closed",
            "buy",
            "sell",
            "add",
            "reduce",
        ],
    },
    # procedure (步骤/流程): 弱化动词型, 加 "步骤 1./2./3." 强过滤
    "procedure": {
        "cn": [
            # [8/4 v0.2 fix] 去掉 "记录一下"/"记一下" (动词型, 误伤 system note)
            # 保留祈使/步骤型
            "步骤",
            "流程",
            "通常这样",
            "每次都是",
            "模板",
            "规范",
            # [8/4 v0.2 fix] 新加强标记: "步骤 1. 2. 3." 形式 (regex 模式)
            # 也在 cn 列表保留前缀, 但主要靠 _STRICT_PROCEDURE_PATTERNS 匹配
        ],
        "en": [
            "steps",
            "how to",
            "process",
            "workflow",
            "procedure",
            "template",
            "convention",
        ],
        # [8/4 v0.2 fix] 强标记模式 (regex): 必须有步骤连接词/序号
        "_strict_patterns_cn": [
            r"步骤\s*[1-9一二三四五六七八九十][\.、\s]",  # "步骤 1." / "步骤 一、"
            r"首先.*然后.*最后",  # "首先...然后...最后"
            r"首先.*接着.*最后",
            r"第一步.*第二步.*第三步",
        ],
        "_strict_patterns_en": [
            r"step\s+[1-9][\.\)\s]",
            r"first.*then.*finally",
        ],
        # 弱标记 (文档化, 不触发)
        "_weak_cn": ["记录一下", "记一下", "方法", "怎么", "如何"],
        "_weak_en": ["here are the steps"],
    },
    # ephemeral (临时/草稿) — 不变
    "ephemeral": {
        "cn": ["临时", "草稿", "待定", "暂定", "占位", "稍后", "佔位", "暫定"],
        "en": ["draft", "temp", "temporary", "placeholder", "wip", "tbd", "todo"],
    },
}


# ========================================
# [TASKS_L2_EXTRACT §3] 匹配逻辑
# ========================================
def classify_memory_type(text: str) -> Optional[str]:
    """P1a 规则分类主入口.

    Args:
        text: 原文 (调用方已 sanitize, 这里只读不写)

    Returns:
        memory_type 字符串 / None
        - 命中强标记 → 返回对应类型
        - 无命中 / 弱标记 → 返回 None (调用方保持默认 fact)
    """
    norm = _normalize(text)
    lower = norm.lower()

    # [8/4 v0.2 audit fix] markdown 引用块不参与匹配 (防 system note / 对话上下文误伤)
    # 模式: [USER] / [ASSISTANT] / [System note] / [conversation] 等
    import re as _re

    if _re.match(r"^\s*\[(USER|ASSISTANT|System note|conversation|Replying to)\b", norm):
        # 这是引用/对话内容, 不分类 (return None = fact)
        return None

    # === episode 复合规则 [8/4 v0.2 fix]: "我"+时间 AND 动作都命中 ===
    # [8/4 v0.2 fix] CN: 时间锚必须含 "我" 主语 (我今天/我昨天/...)
    cn_time_hit = any(t in norm for t in _MARKERS["episode"]["cn_time"])
    cn_action_hit = any(a in norm for a in _MARKERS["episode"]["cn_action"])
    if cn_time_hit and cn_action_hit:
        # 优先级 decision > episode (TASKS §2 优先冲突表)
        if _check_marker(norm, lower, "decision"):
            return "decision"
        return "episode"

    # [8/4 v0.2 fix] EN: "I " 主语 + 时间 + 动作 substring 组合
    en_subject_hit = any(lower.startswith(s) or f". {s}" in lower or f"\n{s}" in lower for s in _MARKERS["episode"]["en_subject"])
    en_time_hit = any(t in lower for t in _MARKERS["episode"]["en_time"])
    en_action_hit = any(a in lower for a in _MARKERS["episode"]["en_action"])
    if en_subject_hit and en_time_hit and en_action_hit:
        if _check_marker(norm, lower, "decision"):
            return "decision"
        return "episode"

    # === procedure 强标记 [8/4 v0.2 fix]: regex 步骤序号 + 连接词 ===
    cn_strict = any(_re.search(p, norm) for p in _MARKERS["procedure"].get("_strict_patterns_cn", []))
    en_strict = any(_re.search(p, lower) for p in _MARKERS["procedure"].get("_strict_patterns_en", []))
    if cn_strict or en_strict:
        return "procedure"

    # === 单标记类: 优先级 decision > preference > procedure > ephemeral ===
    for t in ("decision", "preference", "procedure", "ephemeral"):
        if _check_marker(norm, lower, t):
            return t

    return None


def _check_marker(norm: str, lower: str, memory_type: str) -> bool:
    """检查 cn/en 强标记 (TASKS §2 列表)."""
    markers = _MARKERS[memory_type]
    # CN 标记
    if any(m in norm for m in markers.get("cn", [])):
        return True
    # EN 标记
    if any(m in lower for m in markers.get("en", [])):
        return True
    # [8/4 v0.2 fix] procedure strict pattern 也算强标记
    if memory_type == "procedure":
        import re as _re

        if any(_re.search(p, norm) for p in markers.get("_strict_patterns_cn", [])):
            return True
        if any(_re.search(p, lower) for p in markers.get("_strict_patterns_en", [])):
            return True
    return False
