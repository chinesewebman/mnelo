"""[8/15 E-A] memory_get_all 全量 dump 工具 (借鉴 Mem0 get_all).

主人 6/29 iron law 不抢决策, 但**调试 / 看库**需要全量 dump. Mem0 提供
get_all(user_id="alice") 返回所有 entity + relations + memories, mnelo
补此工具. 设计哲学: 不与 auto-extract 比 (主人 6/29), 只补"主人主动
看库"的便利.

[测试矩阵]
  1. 空库 → 返空字典 (不崩)
  2. 5 entities + 3 relations → 全部返
  3. chunks 也返 (memories 维度)
  4. user_id 过滤 (scoping_ids 兼容)
  5. limit + offset 分页 (避免一次拉 5000 entities 卡死)
  6. include_superseded=False 默认 (隐式排除 valid_until 非 NULL)
  7. soft-deleted 实体不返
  8. kind 过滤
  9. 关系按 kind 过滤
"""

import importlib.util as _ilu
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _load_from_repo(mod_name: str):
    target = str(_REPO / f"{mod_name}.py")
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "__file__", None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_validation_repo = _load_from_repo("validation")
_memory_repo = _load_from_repo("memory")
_memory_repo.ValidationError = _validation_repo.ValidationError  # type: ignore[attr-defined]


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory with tmp_path db + usearch backend."""
    import config as _cfg_mod

    monkeypatch.setattr(_cfg_mod.config, "search_backend", "usearch", raising=True)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(_cfg_mod.config, "db_path", db_path, raising=False)

    schema_path = _REPO / "schema.sql"
    import sqlite3 as _sqlite
    import re

    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    sql = re.sub(r"PRAGMA[^;]*;", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"INSTALL[^;]*;", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"LOAD[^;]*;", "", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)",
        "",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        # [bug fix D1 2026-08-16] Register iso_now() function before running schema.sql
        from datetime import datetime, timedelta as _td

        conn.create_function("iso_now", 0, lambda: datetime.now().isoformat(timespec="seconds"))
        conn.create_function("iso_now_offset", 1, lambda d: (datetime.now() + _td(days=d)).isoformat(timespec="seconds"))
        conn.executescript(sql)
    except Exception as e:
        if "already exists" not in str(e):
            raise
    conn.commit()
    conn.close()

    m = _memory_repo.Memory(db_path=db_path)
    yield m
    try:
        m.close()
    except Exception:
        pass


def _seed_basic_graph(mem):
    """塞 5 entities + 3 relations + 2 chunks for testing."""
    from memory import now as _now

    _t = _now()
    # 5 entities — 走 mnelo 完整 schema
    for eid, kind, name in [
        ("company:tb电工", "company", "特变电工"),
        ("country:cn", "country", "中国"),
        ("industry:变压器", "industry", "变压器"),
        ("stock:sh600089", "stock", "特变电工"),
        ("person:musk", "person", "Elon Musk"),
    ]:
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, importance, valid_from, "
            "valid_until, created_at, updated_at, properties_json, aliases_json, "
            "memory_type, recall_count, superseded_by, last_recalled, user_confirmed, processed_at) "
            "VALUES (?, ?, ?, 'manual', 0.5, ?, NULL, ?, ?, '{}', '[]', "
            "'fact', 0, NULL, NULL, 0, NULL)",
            (eid, kind, name, _t, _t, _t),
        )
    # 3 relations — 走 mnelo 完整 schema
    for rel, src, tgt in [  # SQL: source_id, target_id, relation
        ("located_in", "company:tb电工", "country:cn"),
        ("operates_in", "company:tb电工", "industry:变压器"),
        ("represents", "stock:sh600089", "company:tb电工"),
    ]:
        mem._conn.execute(
            "INSERT INTO relations (source_id, target_id, relation, weight, "
            "valid_from, valid_until, created_at, source, confidence, "
            "properties_json, evidence_chunk_id) "
            "VALUES (?, ?, ?, 1.0, ?, NULL, ?, 'manual', 1.0, '{}', NULL)",
            (src, tgt, rel, _t, _t),
        )
    # 2 chunks — 走 mnelo 完整 schema
    mem._conn.execute(
        "INSERT INTO chunks (id, content, source, timestamp, importance, "
        "memory_type, metadata_json, superseded_by, valid_until, recall_count, "
        "last_recalled, created_at, processed_at) "
        "VALUES ('chunk_a', '特变电工是中国变压器龙头企业', 'manual', ?, 0.7, "
        "'fact', '{}', NULL, NULL, 0, NULL, ?, NULL)",
        (_t, _t),
    )
    mem._conn.execute(
        "INSERT INTO chunks (id, content, source, timestamp, importance, "
        "memory_type, metadata_json, superseded_by, valid_until, recall_count, "
        "last_recalled, created_at, processed_at) "
        "VALUES ('chunk_b', 'Elon Musk 是 Tesla 创始人', 'manual', ?, 0.6, "
        "'fact', '{}', NULL, NULL, 0, NULL, ?, NULL)",
        (_t, _t),
    )
    mem._conn.commit()


class TestGetAll:
    """[8/15 E-A] memory_get_all 全量 dump 工具."""

    def test_empty_db_returns_empty(self, mem):
        """[E-A.1] 空库 → 返空字典 (不崩)."""
        result = mem.get_all()
        assert result == {
            "entities": [],
            "relations": [],
            "chunks": [],
            "totals": {"entities": 0, "relations": 0, "chunks": 0},
            "limit": 1000,
            "offset": 0,
        }

    def test_returns_all_active_entities(self, mem):
        """[E-A.2] 5 entities → 全部返."""
        _seed_basic_graph(mem)
        result = mem.get_all()
        assert result["totals"]["entities"] == 5
        assert len(result["entities"]) == 5
        kinds = {e["kind"] for e in result["entities"]}
        assert kinds == {"company", "country", "industry", "stock", "person"}

    def test_returns_all_relations(self, mem):
        """[E-A.3] 3 relations → 全部返."""
        _seed_basic_graph(mem)
        result = mem.get_all()
        assert result["totals"]["relations"] == 3
        assert len(result["relations"]) == 3
        rels = {(r["source_id"], r["relation"], r["target_id"]) for r in result["relations"]}
        assert rels == {
            ("company:tb电工", "located_in", "country:cn"),
            ("company:tb电工", "operates_in", "industry:变压器"),
            ("stock:sh600089", "represents", "company:tb电工"),
        }

    def test_returns_active_chunks(self, mem):
        """[E-A.4] 2 chunks → 全部返."""
        _seed_basic_graph(mem)
        result = mem.get_all()
        assert result["totals"]["chunks"] == 2
        assert len(result["chunks"]) == 2
        cids = {c["id"] for c in result["chunks"]}
        assert cids == {"chunk_a", "chunk_b"}

    def test_excludes_superseded_by_default(self, mem):
        """[E-A.5] include_superseded=False 默认 → 排除 valid_until 非 NULL."""
        _seed_basic_graph(mem)
        mem._conn.execute(
            "UPDATE entities SET valid_until = ? WHERE id = ?",
            (_seed_basic_graph.__defaults__, "person:musk") if False else (None, "person:musk"),
        )
        # 正确做法: 用 now 时间戳
        from memory import now as _now

        mem._conn.execute(
            "UPDATE entities SET valid_until = ? WHERE id = ?",
            (_now(), "person:musk"),
        )
        mem._conn.commit()
        result = mem.get_all()
        assert result["totals"]["entities"] == 4
        eids = {e["id"] for e in result["entities"]}
        assert "person:musk" not in eids

    def test_include_superseded_true_returns_all(self, mem):
        """[E-A.6] include_superseded=True → 包含软删."""
        _seed_basic_graph(mem)
        from memory import now as _now

        mem._conn.execute(
            "UPDATE entities SET valid_until = ? WHERE id = ?",
            (_now(), "person:musk"),
        )
        mem._conn.commit()
        result = mem.get_all(include_superseded=True)
        assert result["totals"]["entities"] == 5
        eids = {e["id"] for e in result["entities"]}
        assert "person:musk" in eids

    def test_kind_filter(self, mem):
        """[E-A.7] kind 过滤 → 只返指定 kind."""
        _seed_basic_graph(mem)
        result = mem.get_all(kind="company")
        assert result["totals"]["entities"] == 1
        assert result["entities"][0]["id"] == "company:tb电工"

    def test_relation_filter(self, mem):
        """[E-A.8] relation type 过滤 → 只返指定 relation."""
        _seed_basic_graph(mem)
        result = mem.get_all(relation="located_in")
        assert result["totals"]["relations"] == 1
        r = result["relations"][0]
        assert r["source_id"] == "company:tb电工"
        assert r["relation"] == "located_in"

    def test_limit_and_offset(self, mem):
        """[E-A.9] limit + offset 分页."""
        _seed_basic_graph(mem)
        page1 = mem.get_all(limit=2, offset=0)
        page2 = mem.get_all(limit=2, offset=2)
        page3 = mem.get_all(limit=2, offset=4)
        assert page1["totals"]["entities"] == 5
        assert len(page1["entities"]) == 2
        assert len(page2["entities"]) == 2
        assert len(page3["entities"]) == 1
        all_ids = {e["id"] for e in page1["entities"] + page2["entities"] + page3["entities"]}
        assert len(all_ids) == 5

    def test_user_id_filter_no_match(self, mem):
        """[E-A.10] scoping_ids 兼容 — 不存在的 user_id → 0 entity."""
        _seed_basic_graph(mem)
        result = mem.get_all(user_id="nonexistent")
        assert result["totals"]["entities"] == 0
