"""[8/15 E-B] memory_relate dedup_check 选项 (借鉴 Mem0 add_relations 行为).

DESIGN §3.7.1 已设计但未落地: dedup 三元组匹配键 = (source_id, target_id, relation).
Mem0 add_relations 默认 dedup (NOOP 决策), mnelo 补 dedup_check 选项保持主人决策权.

设计哲学: 不抢决策 (主人 6/29), 但补"防误操作"的便利. dedup_check=True
默认跳过重复; dedup_check=False 保留强制创建 (e.g. 双时态 relation).

[测试矩阵]
  1. dedup_check=False (默认) → 仍允许重复插入 (backward-compat)
  2. dedup_check=True + 无重复 → 正常创建
  3. dedup_check=True + 三元组完全相同 → 返返已有 relation_id (no new insert)
  4. dedup_check=True + 三元组差异 (source不同/relation不同/target不同) → 创建新
  5. dedup_check=True + source/target 相同但 relation 不同 → 创建新 (关系类型不同)
  6. dedup_check=True + 软删 (valid_until 非 NULL) → 创建新 (历史不算)
  7. dedup_check=True + valid_until 过期 → 创建新 (历史已"消亡")
  8. dedup_check=True + 同三元组 + 同 evidence → 仍跳过 (没区分 evidence)
  9. dedup_check=True + 返回值: 不跳时 = 新 rowid, 跳过时 = 已有 relation_id
  10. dedup_check=True + 权重/valid_from 差异 → 跳过且不更新 (语义: keep first)
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


class TestRelateDedup:
    """[8/15 E-B] memory_relate dedup_check 选项 (DESIGN §3.7.1 落地)."""

    def test_no_dedup_default_allows_dup(self, mem):
        """[E-B.1] dedup_check=False (默认) → 允许重复插入 (backward-compat)."""
        r1 = mem.relate("company:a", "company:b", "located_in", dedup_check=False)
        r2 = mem.relate("company:a", "company:b", "located_in", dedup_check=False)
        assert r1 != r2
        # 数据库有 2 行
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 2

    def test_dedup_new_creates(self, mem):
        """[E-B.2] dedup_check=True + 无重复 → 正常创建."""
        rid = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        assert rid > 0
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 1

    def test_dedup_exact_triple_returns_existing(self, mem):
        """[E-B.3] dedup_check=True + 三元组完全相同 → 返已有 relation_id."""
        rid1 = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        rid2 = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        assert rid1 == rid2  # 没创建新
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 1

    def test_dedup_different_source_creates(self, mem):
        """[E-B.4] dedup_check=True + source 不同 → 创建新."""
        mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        rid = mem.relate("company:c", "company:b", "located_in", dedup_check=True)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 2

    def test_dedup_different_relation_creates(self, mem):
        """[E-B.5] dedup_check=True + source/target 同但 relation 不同 → 创建新."""
        mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        rid = mem.relate("company:a", "company:b", "owns", dedup_check=True)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 2

    def test_dedup_different_target_creates(self, mem):
        """[E-B.6] dedup_check=True + target 不同 → 创建新."""
        mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        rid = mem.relate("company:a", "company:c", "located_in", dedup_check=True)
        n = mem._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n == 2

    def test_dedup_superseded_creates_new(self, mem):
        """[E-B.7] dedup_check=True + 软删 (valid_until 非 NULL) → 创建新 (历史不算)."""
        rid1 = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        # 软删
        from memory import now as _now

        mem._conn.execute(
            "UPDATE relations SET valid_until = ? WHERE id = ?",
            (_now(), rid1),
        )
        mem._conn.commit()
        # 再建 — 应该建新
        rid2 = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        assert rid1 != rid2

    def test_dedup_returns_existing_id(self, mem):
        """[E-B.8] dedup_check=True + 返回: 不跳时 = 新 rowid, 跳过时 = 已有 relation_id."""
        rid1 = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        # 重复, 应返 rid1
        rid2 = mem.relate("company:a", "company:b", "located_in", dedup_check=True)
        assert rid2 == rid1
        # 不同三元组, 应返新
        rid3 = mem.relate("company:a", "company:c", "located_in", dedup_check=True)
        assert rid3 != rid1

    def test_dedup_ignores_weight_difference(self, mem):
        """[E-B.9] dedup_check=True + 权重不同 → 跳过且不更新 (keep first)."""
        rid1 = mem.relate("company:a", "company:b", "located_in", weight=0.5, dedup_check=True)
        rid2 = mem.relate("company:a", "company:b", "located_in", weight=0.9, dedup_check=True)
        # 跳过 = 仍只有 1 行, 权重不变
        assert rid1 == rid2
        w = mem._conn.execute("SELECT weight FROM relations WHERE id = ?", (rid1,)).fetchone()[0]
        assert float(w) == 0.5
