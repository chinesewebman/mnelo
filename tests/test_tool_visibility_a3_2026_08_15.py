"""[8/15 E-3] Tool visibility plan A3 test.

3 \u4e2a\u4e3b\u573a\u666f:
1. \u9ed8\u8ba4 (no flags): \u53ea\u66b4\u9732 13 tools (7 core + 4 audit + 2 advanced)
2. --audit-tools \u542f\u7528: 13 + 3 admin = 16
3. --all-tools: 24
4. \u9690\u5f0f call hidden tool \u2192 \u8fd4 informative error
5. Destructive tool (\u5982 memory_audit_undo) \u5728 admin tools \u542f\u7528\u540e\u989d\u5916 confirm (\u672a\u6765 \u5b9e\u65bd)
"""

import sys
from pathlib import Path

# \u52a0 repo root \u5230 sys.path \u4ee5 import mcp_tool_definitions
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import pytest
import json as _json


def test_default_exposes_13_tools():
    """[A3.1] \u9ed8\u8ba4\u65d7\u65b0\u542f\u52a8 \u2192 13 tools (7 core + 4 audit + 2 advanced)."""
    from mcp_tool_definitions import get_exposed_tools, TOOL_METADATA

    flags = {"audit_tools": False, "l2_tools": False, "all_tools": False}
    exposed = get_exposed_tools(flags)
    exposed_names = {t["name"] for t in exposed}

    expected = set()
    expected.update(["memory_remember", "memory_recall", "memory_relate", "memory_get_all", "memory_update", "memory_get_digest", "memory_forget"])
    expected.update(["memory_recall_stats", "memory_list_entities", "memory_search_relations", "memory_entity_resolve"])
    expected.update(["memory_graph_query", "memory_stats"])

    assert exposed_names == expected, f"\u9ed8\u8ba4\u66b4\u9732\u5e94\u4e3a 13 tools. \u5b9e\u9645 {len(exposed_names)}.\n\u5dee\u5f02: {exposed_names.symmetric_difference(expected)}"


def test_audit_tools_exposes_16():
    """[A3.2] --audit-tools \u542f\u7528 \u2192 13 + 3 admin = 16."""
    from mcp_tool_definitions import get_exposed_tools

    flags = {"audit_tools": True, "l2_tools": False, "all_tools": False}
    exposed = get_exposed_tools(flags)
    exposed_names = {t["name"] for t in exposed}

    assert "memory_audit_list" in exposed_names
    assert "memory_audit_undo" in exposed_names
    assert "memory_maintenance" in exposed_names
    assert "memory_task_create" not in exposed_names
    assert "memory_loop_create" not in exposed_names
    assert len(exposed_names) == 16


def test_l2_tools_exposes_20():
    """[A3.3] --l2-tools \u542f\u7528 \u2192 13 + 8 l2 = 21."""
    from mcp_tool_definitions import get_exposed_tools

    flags = {"audit_tools": False, "l2_tools": True, "all_tools": False}
    exposed = get_exposed_tools(flags)
    exposed_names = {t["name"] for t in exposed}

    assert "memory_task_create" in exposed_names
    assert "memory_task_transition" in exposed_names
    assert "memory_task_list" in exposed_names
    assert "memory_task_replay" in exposed_names
    assert "memory_loop_create" in exposed_names
    assert "memory_loop_tick" in exposed_names
    assert "memory_loop_update" in exposed_names
    assert "memory_loop_list" in exposed_names
    assert "memory_audit_undo" not in exposed_names
    assert "memory_maintenance" not in exposed_names
    # 7 core + 4 audit + 2 advanced + 8 l2 = 21 (NOT 20)
    assert len(exposed_names) == 21


def test_all_tools_exposes_all_24():
    """[A3.4] --all-tools \u2192 24 tools."""
    from mcp_tool_definitions import get_exposed_tools, TOOLS

    flags = {"audit_tools": True, "l2_tools": True, "all_tools": True}
    exposed = get_exposed_tools(flags)
    assert len(exposed) == 24
    assert {t["name"] for t in exposed} == {t["name"] for t in TOOLS}


def test_tool_tier_metadata():
    """[A3.5] TOOL_METADATA \u8986\u76d6\u6240\u6709 24 tools (\u9632\u4ef6\u8bef\u907d tool)."""
    from mcp_tool_definitions import TOOL_METADATA, TOOLS

    tool_names = {t["name"] for t in TOOLS}
    meta_names = set(TOOL_METADATA.keys())

    assert meta_names >= tool_names, f"\u6240\u6709 tool \u90fd\u9700 metadata. \u7f3a\u5931: {tool_names - meta_names}"

    tiers = {}
    for name, meta in TOOL_METADATA.items():
        t = meta.get("tier")
        tiers.setdefault(t, []).append(name)

    assert len(tiers.get("core", [])) == 7
    assert len(tiers.get("audit", [])) == 4
    assert len(tiers.get("advanced", [])) == 2
    assert len(tiers.get("l2", [])) == 8
    assert len(tiers.get("admin", [])) == 3


def test_is_tool_hidden_default():
    """[A3.6] \u9ed8\u8ba4\u4e0b core \u4e0d\u9690\u85cf, l2/admin \u9690\u85cf."""
    from mcp_tool_definitions import is_tool_hidden

    flags = {"audit_tools": False, "l2_tools": False, "all_tools": False}
    assert not is_tool_hidden("memory_remember", flags)
    assert not is_tool_hidden("memory_recall_stats", flags)
    assert not is_tool_hidden("memory_graph_query", flags)
    assert is_tool_hidden("memory_task_create", flags)
    assert is_tool_hidden("memory_loop_tick", flags)
    assert is_tool_hidden("memory_audit_undo", flags)
    assert is_tool_hidden("memory_maintenance", flags)


def test_is_tool_hidden_with_flags():
    """[A3.7] --audit-tools/--l2-tools \u89e3\u9501\u5bf9\u5e94 tier."""
    from mcp_tool_definitions import is_tool_hidden

    flags_audit = {"audit_tools": True, "l2_tools": False, "all_tools": False}
    assert not is_tool_hidden("memory_audit_undo", flags_audit)
    assert not is_tool_hidden("memory_maintenance", flags_audit)
    assert is_tool_hidden("memory_task_create", flags_audit)

    flags_l2 = {"audit_tools": False, "l2_tools": True, "all_tools": False}
    assert not is_tool_hidden("memory_task_create", flags_l2)
    assert not is_tool_hidden("memory_loop_tick", flags_l2)
    assert is_tool_hidden("memory_audit_undo", flags_l2)

    flags_all = {"audit_tools": True, "l2_tools": True, "all_tools": True}
    assert not is_tool_hidden("memory_audit_undo", flags_all)
    assert not is_tool_hidden("memory_task_create", flags_all)


def test_destructive_metadata():
    """[A3.8] destructive \u6807\u8bb0 \u2014 \u672a\u6765 confirm UX \u4f7f\u7528\u3002"""
    from mcp_tool_definitions import TOOL_METADATA

    assert TOOL_METADATA["memory_audit_undo"].get("destructive") is True
    assert TOOL_METADATA["memory_maintenance"].get("long_running") is True

    for name in ["memory_remember", "memory_recall", "memory_relate", "memory_update", "memory_forget"]:
        assert not TOOL_METADATA[name].get("destructive", False), f"{name} \u4e0d\u5e94\u6807 destructive"


def test_default_dispatcher_flags():
    """[A3.9] _TOOL_VIS_FLAGS \u9ed8\u8ba4 3 flag = False."""
    from mcp_tool_dispatcher import _TOOL_VIS_FLAGS

    assert _TOOL_VIS_FLAGS == {
        "audit_tools": False,
        "l2_tools": False,
        "all_tools": False,
    }


def test_call_tool_hidden_returns_informative_error():
    """[A3.10] Hidden tool \u9690\u5f0f call \u2192 informative error."""
    import mcp_tool_dispatcher as disp

    disp._TOOL_VIS_FLAGS = {
        "audit_tools": False,
        "l2_tools": False,
        "all_tools": False,
    }
    result = disp._call_tool("memory_audit_undo", {})
    parsed = _json.loads(result)
    assert parsed["type"] == "hidden_tool"
    assert parsed["tier"] == "admin"
    assert "--audit-tools" in parsed["hint"]
    assert parsed["tool"] == "memory_audit_undo"

    result = disp._call_tool("memory_task_create", {})
    parsed = _json.loads(result)
    assert parsed["type"] == "hidden_tool"
    assert parsed["tier"] == "l2"
    assert "--l2-tools" in parsed["hint"]


def test_call_tool_visible_executes():
    """[A3.11] Visible tool (\u5982 memory_recall) \u9690\u5f0f call \u4ecd OK."""
    import mcp_tool_dispatcher as disp

    disp._TOOL_VIS_FLAGS = {
        "audit_tools": False,
        "l2_tools": False,
        "all_tools": False,
    }
    try:
        result = disp._call_tool("memory_recall", {"query": "test", "top_k": 1})
    except Exception as e:
        result = str(e)
    if isinstance(result, str) and "hidden_tool" in result:
        pytest.fail("memory_recall \u4e0d\u5e94\u8fd4 hidden_tool error")


def test_tier_for_known_tool():
    """[A3.12] get_tool_tier \u8fd4\u56de\u6b63\u786e tier."""
    from mcp_tool_definitions import get_tool_tier

    assert get_tool_tier("memory_remember") == "core"
    assert get_tool_tier("memory_recall_stats") == "audit"
    assert get_tool_tier("memory_graph_query") == "advanced"
    assert get_tool_tier("memory_task_create") == "l2"
    assert get_tool_tier("memory_audit_undo") == "admin"

    assert get_tool_tier("memory_unknown_xyz") == "core"
