"""CI check: tool description lane 数必须跟 enum 长度一致.

Issue #3 报告: `memory_recall` description 写 "3 路召回 (向量 + 图遍历 + 元数据)"
但 strategy enum 有 5 个值 (rrf, vector_only, graph_only, meta_only, entity_only),
跟 DESIGN.md "4-way RRF" + 真实实现 (entity 路 7/18 加的) 都对不上.

本测试把这种 description ↔ enum 不一致挡在 CI.

设计:
- 纯文本扫描 + 正则解析, 避免 import 整个 mcp_server (它依赖 sqlite_vec / mcp SDK).
- 每个 tool 的 description 必须包含跟 enum lane 数一致的数字 (「N 路」+ N == len(enum)-1,
  因为 'rrf' 是融合模式不单算).
- 若 description 跟 enum 矛盾, test 失败并打印两者方便定位.
"""
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_MCP_SERVER = _REPO / "mcp_server.py"


def _parse_tools_block(src: str):
    """Yield (name, description, enum_values) for every tool entry.

    走 mcp_server.py 里 TOOLS list 的字面结构 — 不 exec, 纯文本.
    支持 multi-line description (Python 隐式拼接).
    """
    out = []
    # 用 `("name": "...", )` 来定位每个 tool entry start
    for m in re.finditer(r'"name":\s*"(?P<name>[^"]+)",\s*"description":\s*', src):
        name = m.group("name")
        i = m.end()
        has_paren = False
        if i < len(src) and src[i] == "(":
            has_paren = True
            i += 1
        # skip 空白 + 注释到 quote
        while i < len(src) and src[i] in " \t\n":
            i += 1
        if i >= len(src) or src[i] not in "'\"":
            continue
        quote = src[i]
        i += 1
        desc_chars = []
        # 吃字符串直到匹配 quote 次数平衡 (open = 1, close = 1) — 即 description 完整结束
        while i < len(src):
            c = src[i]
            if c == "\\":
                if i + 1 < len(src):
                    desc_chars.append(src[i+1])
                    i += 2
                    continue
            if c == quote:
                i += 1
                # peek next 非空白
                while i < len(src) and src[i] in " \t\n":
                    i += 1
                if i < len(src) and src[i] == quote:
                    # multi-line concat 续段
                    desc_chars.append(" ")  # separator placeholder
                    i += 1
                    continue
                # 真正结束 — 不 append placeholder
                break
            desc_chars.append(c)
            i += 1
        # 如果是 ( 开头, 跳过  )  and ,
        if has_paren:
            # description 收尾 ) + ,
            while i < len(src) and src[i] in " \t\n),":
                i += 1
        else:
            while i < len(src) and src[i] in " \t\n,":
                i += 1
        # 找 "inputSchema": { (跳过 "inputSchema" key + 空白)
        m_schema = re.search(r'"inputSchema"\s*:\s*\{', src[i:])
        if not m_schema:
            continue
        i = i + m_schema.end() - 1  # 停在最后一个 {
        # brace balance 从这个 { 开始
        depth = 0
        j = i
        while j < len(src):
            c = src[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        block = src[i:j]
        enum_match = re.search(r'"enum":\s*\[(?P<values>[^\]]*)\]', block)
        if not enum_match:
            continue
        values = [
            v.strip().strip('"')
            for v in enum_match.group("values").split(",")
            if v.strip()
        ]
        # 清理 description
        description = "".join(desc_chars).strip()
        out.append((name, description, values))
    return out


def _parse_lane_count(description: str) -> int | None:
    """从 description 提取 "N 路" 数字; 找不到返回 None."""
    m = re.search(r"(\d)\s*路", description)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d)-way", description, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def test_memory_recall_description_matches_enum():
    """memory_recall: description 写的 N 路 必须跟 strategy enum (除去 'rrf') 数量一致.

    约定: 'rrf' 是融合 mode, 不算单 lane; _only 后缀是单 lane shortcut.
    """
    src = _MCP_SERVER.read_text(encoding="utf-8")
    for name, desc, enum in _parse_tools_block(src):
        if name != "memory_recall":
            continue
        single_lanes = [v for v in enum if v != "rrf"]
        declared = _parse_lane_count(desc)
        assert declared is not None, (
            f"memory_recall description 找不到 lane 数: {desc!r}"
        )
        assert declared == len(single_lanes), (
            f"memory_recall description 写 {declared} 路, "
            f"但 strategy enum 有 {len(single_lanes)} 个单 lane: {single_lanes}. "
            f"description = {desc!r}"
        )
        lane_keywords = {
            "vector_only": "向量",
            "graph_only": "图",
            "meta_only": "元数据",
            "entity_only": "实体",
        }
        for v, kw in lane_keywords.items():
            if v in enum:
                assert kw in desc, (
                    f"memory_recall description 缺 '{kw}' (对应 enum {v!r}): {desc!r}"
                )
        return
    raise AssertionError("mcp_server.py 里找不到 memory_recall tool 定义")


def test_no_tool_has_inconsistent_lane_count():
    """防御性扫描: 任何 tool description 里的 "N 路" 都应跟它的 enum lane 数一致.

    跳过不含 strategy enum 的 tool (跟 lane 概念无关).
    """
    src = _MCP_SERVER.read_text(encoding="utf-8")
    failures = []
    for name, desc, enum in _parse_tools_block(src):
        if "rrf" not in enum and not any(v.endswith("_only") for v in enum):
            continue
        single_lanes = [v for v in enum if v != "rrf"]
        declared = _parse_lane_count(desc)
        if declared is None:
            continue
        if declared != len(single_lanes):
            failures.append(
                f"{name}: description={declared}路, enum single_lanes={single_lanes}"
            )
    assert not failures, "tool description ↔ enum 不一致:\n  " + "\n  ".join(failures)