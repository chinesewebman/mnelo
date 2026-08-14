"""CI check: tool description lane 数必须跟 enum 长度一致.

Issue #3 报告: `memory_recall` description 写 "3 路召回 (向量 + 图遍历 + 元数据)"
但 strategy enum 有 5 个值 (rrf, vector_only, graph_only, meta_only, entity_only),
跟 DESIGN.md "4-way RRF" + 真实实现 (entity 路 7/18 加的) 都对不上.

本测试把这种 description ↔ enum 不一致挡在 CI.

[8/14 P1 fix] 8/12 refactor 后 22 Tool() 定义搬到 mcp_tool_definitions.py,
mcp_server.py 只剩 facade re-export. 本 test 从 grep 源码改为走 facade
(`from mcp_server import TOOLS`), 跟 module 拆分位置解耦, 后续再拆
module 也不会破.

设计:
- 走 facade `from mcp_server import TOOLS` 拿 22 个 tool schema (纯数据, 不触发
  mcp/Starlette/uvicorn).
- 每个 tool 的 description 必须包含跟 enum lane 数一致的数字 (「N 路」+ N == len(enum)-1,
  因为 'rrf' 是融合模式不单算).
- 若 description 跟 enum 矛盾, test 失败并打印两者方便定位.
"""

import re

from mcp_server import TOOLS as _TOOLS


def _iter_tools():
    """Yield (name, description, enum_values) for every mcp_server TOOLS entry.

    通过 `mcp_server.TOOLS` 拿 (re-export mcp_tool_definitions.TOOLS, list[dict]),
    保证不依赖文件路径或源码位置, refactor 拆 module 不再破本 test.
    """
    for entry in _TOOLS:
        if not isinstance(entry, dict):
            # 兼容未来 mcp SDK 升级到 Tool 对象的情况
            name = getattr(entry, "name", "?")
            desc = getattr(entry, "description", "") or ""
            schema = getattr(entry, "inputSchema", None) or {}
        else:
            name = entry.get("name", "?")
            desc = entry.get("description", "") or ""
            schema = entry.get("inputSchema") or {}
        # 在 inputSchema.properties 扫 enum (memory_recall 是 strategy, 其他 tool 多半单 enum field)
        enum_values = []
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for prop_schema in props.values():
            if isinstance(prop_schema, dict) and "enum" in prop_schema:
                enum_values = list(prop_schema["enum"])
                break  # 取第一个 enum field
        yield name, desc, enum_values


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
    for name, desc, enum in _iter_tools():
        if name != "memory_recall":
            continue
        single_lanes = [v for v in enum if v != "rrf"]
        declared = _parse_lane_count(desc)
        assert declared is not None, f"memory_recall description 找不到 lane 数: {desc!r}"
        assert declared == len(single_lanes), f"memory_recall description 写 {declared} 路, 但 strategy enum 有 {len(single_lanes)} 个单 lane: {single_lanes}. description = {desc!r}"
        lane_keywords = {
            "vector_only": "向量",
            "graph_only": "图",
            "meta_only": "元数据",
            "entity_only": "实体",
        }
        for v, kw in lane_keywords.items():
            if v in enum:
                assert kw in desc, f"memory_recall description 缺 '{kw}' (对应 enum {v!r}): {desc!r}"
        return
    raise AssertionError("mcp_server.TOOLS 找不到 memory_recall tool 定义 (facade re-export 失败?)")


def test_no_tool_has_inconsistent_lane_count():
    """防御性扫描: 任何 tool description 里的 "N 路" 都应跟它的 enum lane 数一致.

    跳过不含 strategy enum 的 tool (跟 lane 概念无关). enum 含 None 的 (默认未指定)
    也跳过 — 那是 input contract, 不是 lane count.
    """
    failures = []
    for name, desc, enum in _iter_tools():
        # 过滤 None (input contract default=未指定, 非 lane)
        enum_clean = [v for v in enum if isinstance(v, str)]
        if "rrf" not in enum_clean and not any(v.endswith("_only") for v in enum_clean):
            continue
        single_lanes = [v for v in enum_clean if v != "rrf"]
        declared = _parse_lane_count(desc)
        if declared is None:
            continue
        if declared != len(single_lanes):
            failures.append(f"{name}: description={declared}路, enum single_lanes={single_lanes}")
    assert not failures, "tool description ↔ enum 不一致:\n  " + "\n  ".join(failures)
