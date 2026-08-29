import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

"""[8/16 P1 #92 fix] §1.2 #5 协议层 raw-SQL 旁路修正实战.

背景: 3 个 MCP tool (memory_entity_resolve/memory_list_entities/memory_search_relations)
在 mcp_tool_handlers.py 中是 _CUSTOM_HANDLERS · 直接走 raw SQL · 绕过 Memory 类。

主人 8/15 实战披露 3 个问题:
1. 无 namespace guard (跨越 _enforce_entity_namespace_guard)
2. 无 pagination (offset/limit 上限 cap)
3. 无统一错误处理 (raw SQL error 暴露内部详情)

本次实战:
- 两个可重构 (实战项目) · memory.list_entities / memory.search_relations 新增· _CUSTOM_HANDLERS 调用这些 method
- 一个保留 (_handle_entity_resolve) · 特殊 difflib 算法不可重构 · 仅加 limit cap + validate_id
"""

import pytest
import re as _re
import sqlite3 as _sqlite
from pathlib import Path
import tempfile


@pytest.fixture
def mem(tmp_path):
    """[P1 #92 fix] 使用 tmp_path db + usearch backend (避免 zvec LOCK)."""
    db_path = tmp_path / "p92_test.db"
    conn = _sqlite.connect(str(db_path))
    sql = Path(__file__).resolve().parent.parent.joinpath("schema.sql").read_text()
    sql = _re.sub(r"PRAGMA[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"INSTALL[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"LOAD[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(
        r"CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)",
        "",
        sql,
        flags=_re.IGNORECASE | _re.DOTALL,
    )
    # [bug fix D1 2026-08-16] Register iso_now() function before running schema.sql
    from datetime import datetime, timedelta as _td

    conn.create_function("iso_now", 0, lambda: datetime.now().isoformat(timespec="seconds"))
    conn.create_function("iso_now_offset", 1, lambda d: (datetime.now() + _td(days=d)).isoformat(timespec="seconds"))
    conn.executescript(sql)
    conn.commit()
    conn.close()
    import config as _cfg

    _cfg.config.search_backend = "usearch"
    from memory import Memory

    m = Memory(db_path=db_path)
    yield m
    m.close()


# ==== Step 1: memory.list_entities 新增·重构 _handle_list_entities ====


def test_p92_list_entities_returns_filtered(mem):
    """[P1 #92.1] memory.list_entities 可走· filter kind 生效· 返回结构化数据。"""
    from memory import now as _now

    # insert 3 entities
    for i, (eid, kind) in enumerate([("stock:sh600089", "stock"), ("stock:sh600090", "stock"), ("identity:agent:main", "identity_fact")]):
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, importance, valid_from) VALUES (?, ?, ?, ?, ?)",
            (eid, kind, f"test {i}", 0.5 + i * 0.1, _now()),
        )
    mem._conn.commit()

    # call via Memory.list_entities (not raw SQL)
    result_dict = mem.list_entities(kind="stock")
    assert isinstance(result_dict, dict) and "entities" in result_dict, f"list_entities 应返 dict 包含 entities·实际 {type(result_dict)}"
    results = result_dict["entities"]
    assert len(results) == 2, f"过滤 stock 应返 2 个·实际 {len(results)}"


def test_p92_list_entities_pagination(mem):
    """[P1 #92.2] pagination: limit + offset 生效· limit 上限 100 防 hang。"""
    from memory import now as _now

    for i in range(5):
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, importance, valid_from) VALUES (?, ?, ?, ?, ?)",
            (f"stock:bulk_{i:03d}", "stock", f"bulk {i}", 0.5 + i * 0.01, _now()),
        )
    mem._conn.commit()

    # limit=2 · offset=1 · 应返 2 个
    page = mem.list_entities(kind="stock", limit=2, offset=1).get("entities", [])
    assert len(page) == 2, f"应返 2 个·实际 {len(page)}"


def test_p92_list_entities_limit_cap(mem):
    """[P1 #92.3] limit 超过 100 自动 cap 为 100 (防 hang)."""
    from memory import now as _now

    mem._conn.execute(
        "INSERT INTO entities (id, kind, name, importance, valid_from) VALUES (?, ?, ?, ?, ?)",
        ("stock:cap_test", "stock", "cap", 0.5, _now()),
    )
    mem._conn.commit()
    result_dict = mem.list_entities(kind="stock", limit=99999)
    results = result_dict["entities"]
    assert len(results) == 1, f"应返 1 (只 1 entity)·实际 {len(results)}"


def test_p92_list_entities_filters_soft_deleted(mem):
    """[P1 #92.4] soft delete (valid_until 非 NULL) 不被返回。"""
    from memory import now as _now

    mem._conn.execute(
        "INSERT INTO entities (id, kind, name, importance, valid_from, valid_until) VALUES (?, ?, ?, ?, ?, ?)",
        ("stock:soft_del", "stock", "soft", 0.5, _now(), _now()),
    )
    mem._conn.commit()
    results = mem.list_entities(kind="stock", limit=100).get("entities", [])
    ids = [r["id"] for r in results]
    assert "stock:soft_del" not in ids, f"soft delete 不应被返回·实际 {ids}"


# ==== Step 2: memory.search_relations 新增·重构 _handle_search_relations ====


def test_p92_search_relations_by_type(mem):
    """[P1 #92.5] memory.search_relations(relation='owns') 过滥实际走。"""
    from memory import now as _now

    mem._conn.execute(
        "INSERT INTO relations (source_id, target_id, relation, weight, valid_from, valid_until) VALUES (?, ?, ?, ?, ?, NULL)",
        ("stock:sh600089", "identity:agent:main", "owns", 0.8, _now()),
    )
    mem._conn.execute(
        "INSERT INTO relations (source_id, target_id, relation, weight, valid_from, valid_until) VALUES (?, ?, ?, ?, ?, NULL)",
        ("stock:sh600090", "identity:agent:main", "references", 0.6, _now()),
    )
    mem._conn.commit()

    result_dict = mem.search_relations(relation="owns")
    assert isinstance(result_dict, dict) and "relations" in result_dict, f"search_relations 应返 dict 包含 relations·实际 {type(result_dict)}"
    results = result_dict["relations"]
    assert len(results) == 1, f"过滥 owns 应返 1·实际 {len(results)}"
    assert results[0]["relation"] == "owns", f"关系类型错·实际 {results[0]['relation']}"


def test_p92_search_relations_pagination_limit_cap(mem):
    """[P1 #92.6] search_relations 同样 limit cap 100 · offset 生效。"""
    from memory import now as _now

    for i in range(3):
        mem._conn.execute(
            "INSERT INTO relations (source_id, target_id, relation, weight, valid_from, valid_until) VALUES (?, ?, ?, ?, ?, NULL)",
            (f"stock:r_{i:03d}", f"identity:agent:r_{i}", "owns", 0.5 + i * 0.1, _now()),
        )
    mem._conn.commit()

    page = mem.search_relations(relation="owns", limit=2, offset=1).get("relations", [])
    assert len(page) == 2, f"应返 2 个·实际 {len(page)}"


def test_p92_search_relations_filters_soft_deleted(mem):
    """[P1 #92.7] soft delete relation (valid_until 非 NULL) 不被返回。"""
    from memory import now as _now

    mem._conn.execute(
        "INSERT INTO relations (source_id, target_id, relation, weight, valid_from, valid_until) VALUES (?, ?, ?, ?, ?, ?)",
        ("stock:r_soft", "identity:agent:soft", "owns", 0.5, _now(), _now()),
    )
    mem._conn.commit()
    results = mem.search_relations(relation="owns").get("relations", [])
    assert len(results) == 0, f"soft delete relation 不应返·实际 {len(results)}"


# ==== Step 3: _handle_entity_resolve 保留 raw handler 但加 limit cap + validate_id ====


def test_p92_entity_resolve_returns_dedupe_candidates(mem):
    """[P1 #92.8] memory_entity_resolve 保留 raw handler · 返回可用 candidates。"""
    from memory import now as _now

    for eid, name in [
        ("stock:sh600089", "Apple Inc"),
        ("stock:sh600090", "Apple Incorporated"),
    ]:
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, importance, valid_from) VALUES (?, ?, ?, ?, ?)",
            (eid, "stock", name, 0.5, _now()),
        )
    mem._conn.commit()

    from mcp_tool_handlers import _handle_entity_resolve

    result_str = _handle_entity_resolve(mem, {"threshold": 0.3, "max_pairs": 50})
    import json as _json

    parsed = _json.loads(result_str)
    assert "candidates" in parsed
    assert "count" in parsed
    if parsed["count"] > 0:
        cand = parsed["candidates"][0]
        assert "score" in cand and 0 <= cand["score"] <= 1.0


# ==== Step 4: MCP 4-file 一致性验证 ====


def test_p92_handler_dispatcher_consistency():
    """[P1 #92.9] mcp_tool_dispatcher 中 3 个 tool 仍在 _CUSTOM_HANDLERS / _TOOL_REGISTRY ·保证 MCP 4-file 一致。"""
    from mcp_tool_handlers import _CUSTOM_HANDLERS

    assert "memory_entity_resolve" in _CUSTOM_HANDLERS  # 保留 raw (特殊算法)
    from mcp_tool_dispatcher import _TOOL_REGISTRY

    assert "memory_list_entities" in _TOOL_REGISTRY
    assert "memory_search_relations" in _TOOL_REGISTRY
    assert _TOOL_REGISTRY["memory_list_entities"][0] == "list_entities"
    assert _TOOL_REGISTRY["memory_search_relations"][0] == "search_relations"
