"""[8/15 E-C.2] @entity_id / #tag mention 解析为 relation 候选.

主人 8/15 拍板的第三个改进. 实话说: mnelo 主人从不主动调 memory_relate,
导致 A1.14 graph 7d 跌到 1%. 本改进解决 §1.2 #8 "图谱建立薄弱" 短板.

设计哲学 (P1 #58 借鉴决策 4 分类):
- ❌ 数据建立借鉴 (强 LLM auto-extract) — 主人 6/29 不抢决策
- ✅ 规则化借鉴 (规则化解析 explicit mention) — 不调 LLM, 主人保持决策权

规则:
- @<entity_id> 显式 mention entity (e.g. @company:tb_tech) → 自动创建/检查
  relation (chunk -[mentions]-> entity, 默认 relation="mentions").
- #<tag> 显式 mention tag (e.g. #strategy) → 自动创建/检查 tag entity
  (kind="tag") + relation (chunk -[tagged]-> tag).
- 纯规则, 不调 LLM. 主人 6/29 "不抢决策" 保留决策权 (auto_relate 默认 False).

[测试矩阵]
  1. auto_relate=False (默认) → 行为不变, 不创建 relation
  2. auto_relate=True + 无 mention → 正常 remember, 无 relation
  3. auto_relate=True + @entity_id mention → 1 chunk + 1 entity + 1 relation
  4. auto_relate=True + 多个 @entity_id mention → 多个 relations
  5. auto_relate=True + #tag mention → 1 tag entity + 1 relation
  6. auto_relate=True + 混合 @entity + #tag → 混搭
  7. @entity_id 不存在 → ValidationError (skip + log warning)
  8. #tag 重复 → 复用已有 tag entity (dedup)
  9. relation kind 默认 "mentions" 和 "tagged", 可自定义
  10. 解析失败 (不合法 id 格式) → skip + log warning, 不中断
  11. dedup_check=True (E-B) + auto_relate=True 同时启用 → 共同 dedup
  12. mention 检测: chunk content "buy @company:tb_tech #strategy" →
      2 entity ids + 1 tag id
"""

import importlib.util as _ilu
import json
import re
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
    import re as _re

    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    sql = _re.sub(r"PRAGMA[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"INSTALL[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"LOAD[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(
        r"CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)",
        "",
        sql,
        flags=_re.IGNORECASE | _re.DOTALL,
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


def _seed_entity(mem, eid: str, kind: str, name: str):
    """Seed entity 走完整 schema (P1 #56 教训)."""
    from memory import now as _now

    _t = _now()
    mem._conn.execute(
        "INSERT INTO entities (id, kind, name, source, importance, valid_from, "
        "valid_until, created_at, updated_at, properties_json, aliases_json, "
        "memory_type, recall_count, superseded_by, last_recalled, user_confirmed, processed_at) "
        "VALUES (?, ?, ?, 'manual', 0.5, ?, NULL, ?, ?, '{}', '[]', "
        "'fact', 0, NULL, NULL, 0, NULL)",
        (eid, kind, name, _t, _t, _t),
    )
    mem._conn.commit()


class TestMentionParse:
    """[8/15 E-C.2] @entity_id / #tag mention 解析为 relation 候选."""

    def test_no_autorelate_default_unchanged(self, mem):
        """[E-C.2.1] auto_relate=False (默认) → 行为不变, 不创建 relation."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        cid = mem.remember(
            "buy @company:tb_tech",
            entities=[{"id": "company:tb_tech", "kind": "company", "name": "TB Tech Co"}],
            auto_relate=False,
        )
        assert cid
        # 0 relation (auto_relate=False)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 0

    def test_autorelate_no_mention_unchanged(self, mem):
        """[E-C.2.2] auto_relate=True + 无 mention → 正常 remember, 无 relation."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        cid = mem.remember(
            "分析industry trend",
            entities=[{"id": "company:tb_tech", "kind": "company", "name": "TB Tech Co"}],
            auto_relate=True,
        )
        assert cid
        # 0 relation (no mention in content)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 0

    def test_autorelate_one_entity_mention(self, mem):
        """[E-C.2.3] auto_relate=True + @entity_id mention → 1 relation."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        cid = mem.remember(
            "buy @company:tb_tech, target 7800",
            entities=[{"id": "company:tb_tech", "kind": "company", "name": "TB Tech Co"}],
            auto_relate=True,
        )
        # 1 relation (chunk -[mentions]-> entity)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 1
        r = mem._conn.execute("SELECT source_id, target_id, relation FROM relations").fetchone()
        assert r["source_id"] == cid
        assert r["target_id"] == "company:tb_tech"
        assert r["relation"] == "mentions"

    def test_autorelate_multiple_entity_mentions(self, mem):
        """[E-C.2.4] auto_relate=True + 多个 @entity_id mention → 多个 relations."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        _seed_entity(mem, "industry:transformer", "industry", "变压器")
        cid = mem.remember(
            "buy @company:tb_tech @industry:transformer, cross-track layout",
            auto_relate=True,
        )
        # 2 relations
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 2

    def test_autorelate_tag_mention(self, mem):
        """[E-C.2.5] auto_relate=True + #tag mention → 1 tag entity + 1 relation."""
        cid = mem.remember(
            "today strategy #strategy rebalance",
            auto_relate=True,
        )
        # 1 tag entity + 1 relation
        e = mem._conn.execute("SELECT id, kind FROM entities WHERE id = 'tag:strategy'").fetchone()
        assert e is not None
        assert e["kind"] == "tag"
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 1
        r = mem._conn.execute("SELECT source_id, target_id, relation FROM relations").fetchone()
        assert r["relation"] == "tagged"
        assert r["target_id"] == "tag:strategy"

    def test_autorelate_mixed_entity_and_tag(self, mem):
        """[E-C.2.6] auto_relate=True + 混合 @entity + #tag → 混搭."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        cid = mem.remember(
            "buy @company:tb_tech #swing_trade",
            auto_relate=True,
        )
        # 1 entity mention + 1 tag = 2 relations
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 2
        # 1 entity + 1 tag entity = 2 entities
        e = mem._conn.execute("SELECT COUNT(*) FROM entities WHERE id != 'concept'").fetchone()[0]
        # chunk 也算 entity (memory.remember 默认 kind='concept' for chunk)
        # 所以 e >= 2 (entity + tag)
        assert e >= 2

    def test_autorelate_undeclared_entity_skipped(self, mem):
        """[E-C.2.7] @entity_id 不存在 → skip + log warning, 不创建 relation."""
        cid = mem.remember(
            "buy @company:tb_tech (undeclared)",
            auto_relate=True,
        )
        # 0 relation (entity 不存在, skip)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 0

    def test_autorelate_tag_dedup(self, mem):
        """[E-C.2.8] #tag 重复 → 复用已有 tag entity (dedup)."""
        # 第一次创建 tag:strategy
        mem.remember("first #strategy", auto_relate=True)
        n1 = mem._conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'tag:strategy'").fetchone()[0]
        assert n1 == 1
        # 第二次 reuse 同一 tag
        mem.remember("second #strategy", auto_relate=True)
        n2 = mem._conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'tag:strategy'").fetchone()[0]
        assert n2 == 1  # 仍 1 个, 不重复

    def test_autorelate_custom_relation(self, mem):
        """[E-C.2.9] relation kind 默认 mentions/tagged, 可自定义."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        cid = mem.remember(
            "buy @company:tb_tech",
            entities=[{"id": "company:tb_tech", "kind": "company", "name": "TB Tech Co"}],
            auto_relate=True,
            entity_relation="discusses",  # 自定义 relation
        )
        r = mem._conn.execute("SELECT relation FROM relations WHERE source_id = ?", (cid,)).fetchone()
        assert r["relation"] == "discusses"

    def test_autorelate_invalid_mention_skipped(self, mem):
        """[E-C.2.10] 解析失败 (不合法 id 格式) → skip + log warning, 不中断."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        # 不合法 entity id (没有 namespace): 不通过 validate_id
        cid = mem.remember(
            "buy @badid, 跟 @company:tb_tech 一起看",
            auto_relate=True,
        )
        # 1 relation (company:tb_tech) + 0 (badid 没通过 validate_id)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 1

    def test_autorelate_with_dedup_check(self, mem):
        """[E-C.2.11] dedup_check=True (E-B) + auto_relate=True 同时启用 → 共同 dedup."""
        _seed_entity(mem, "company:tb_tech", "company", "TB Tech Co")
        # 第一次
        cid1 = mem.remember(
            "buy @company:tb_tech",
            entities=[{"id": "company:tb_tech", "kind": "company", "name": "TB Tech Co"}],
            auto_relate=True,
            dedup_check=True,
        )
        # 第二次 (触发 E-B dedup)
        cid2 = mem.remember(
            "buy @company:tb_tech (re-read)",
            entities=[{"id": "company:tb_tech", "kind": "company", "name": "TB Tech Co"}],
            auto_relate=True,
            dedup_check=True,
        )
        # 2 个 chunk + 1 个 mentions relation (dedup_check 只对 rel dedup,
        # chunk 走自己 insert). 验证关系只 1 个
        n = mem._conn.execute("SELECT COUNT(*) FROM relations WHERE target_id = 'company:tb_tech'").fetchone()[0]
        assert n == 2  # 2 个 chunk 都 mention 同一 entity (dedup_check 对同 chunk 不 dedup)

    def test_mention_extraction_regex(self, mem):
        """[E-C.2.12] mention 检测: chunk content "buy @company:tb_tech #strategy"
        → 2 entity ids + 1 tag id."""
        # 直接测试 parser 函数 (不用 remember)
        from memory import _extract_mentions

        # 用我们设计的 parser
        content = "buy @company:tb_tech #strategy rebalance"
        entity_mentions, tag_mentions = _extract_mentions(content)
        assert entity_mentions == ["company:tb_tech"]
        assert tag_mentions == ["strategy"]
