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
    inputSchema 嵌套 brace 用平衡匹配 (最多 5 层).
    """
    # 先找所有 {"name": "...", "description": "...", "inputSchema":
    head_re = re.compile(
        r'"name":\s*"(?P<name>[^"]+)",\s*'
        r'"description":\s*"(?P<description>(?:\\.|[^"\\])*)",\s*'
        r'"inputSchema":\s*',
        re.DOTALL,
    )
    for m in head_re.finditer(src):
        name = m.group("name")
        desc = m.group("description")
        # 从 m.end() 开始 brace-balance
        i = m.end()
        if i >= len(src) or src[i] != "{":
            continue
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
        yield name, desc, values


def _parse_lane_count(description: str) -> int | None:
    """从 description 提取 "N 路" 数字; 找不到返回 None."""
    # 匹配 "4 路召回" / "3-way" / "4-way" 等
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
        # rrf 是融合模式, _only 才是真 lane
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
        # 描述里 4 个 lane 名 (向量 / 图 / 元数据 / 实体) 必须对应 enum _only 名
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
        return  # 只查第一个 memory_recall
    raise AssertionError("mcp_server.py 里找不到 memory_recall tool 定义")


def test_no_tool_has_inconsistent_lane_count():
    """防御性扫描: 任何 tool description 里的 "N 路" 都应跟它的 enum lane 数一致.

    跳过不含 strategy enum 的 tool (跟 lane 概念无关).
    """
    src = _MCP_SERVER.read_text(encoding="utf-8")
    failures = []
    for name, desc, enum in _parse_tools_block(src):
        if "rrf" not in enum and not any(v.endswith("_only") for v in enum):
            continue  # 不是召回类 tool
        single_lanes = [v for v in enum if v != "rrf"]
        declared = _parse_lane_count(desc)
        if declared is None:
            continue  # description 没提 N 路, 不强求
        if declared != len(single_lanes):
            failures.append(
                f"{name}: description={declared}路, enum single_lanes={single_lanes}"
            )
    assert not failures, "tool description ↔ enum 不一致:\n  " + "\n  ".join(failures)
