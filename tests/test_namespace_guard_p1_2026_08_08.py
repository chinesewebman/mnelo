"""[8/8 P1] entity namespace guard 防御测试.

历史 importer 残留 (Honcho anno:* / 随机 TOKEN_C_* / 整句当 entity.name)
是 mnelo 4,226 条 concept 噪声 entity 的源头. namespace guard 在 _upsert_entity
入口处拦截, 防止新 importer / 脚本再次污染.

[测试矩阵]
  1. anno:* → ValidationError
  2. TOKEN_C_* → ValidationError
  3. concept + name > 50 chars → ValidationError
  4. 无 namespace + 非结构化 kind → ValidationError
  5. 白名单 namespace (identity:/stock:/holding:/loop:/task:) → 通过
  6. master_* prefix → 通过
  7. 短 name concept (≤ 50 chars) → 通过
  8. 无 namespace + 结构化 kind (person/provider/event/task/...) → 通过

[test id 约定]
  所有测试 entity 用 `test_nsguard_*` prefix, conftest `_clean_test_data_session`
  清 `id LIKE 'test_%'`, 自动兜底清理.
"""
import importlib.util as _ilu
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))  # [8/8 P1] 没 conftest 时手动加 sys.path


def _load_from_repo(mod_name: str):
    target = str(_REPO / f'{mod_name}.py')
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, '__file__', None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# 强制走 repo 版本 (避免 LIVE memory.py 覆盖)
_validation_repo = _load_from_repo('validation')
_memory_repo = _load_from_repo('memory')
# Rebind memory module 的 ValidationError 引用 (它 'from validation import')
_memory_repo.ValidationError = _validation_repo.ValidationError  # type: ignore[attr-defined]

from validation import ValidationError  # noqa: E402


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory instance.

    [8/8 P1] 用 tmp_path + usearch backend, 避免撞 LIVE mcp_server 持的 zvec LOCK.
    测 namespace guard 是 entity 写路径 — sqlite-only, 不需要 zvec 索引.

    步骤: 1) monkey-patch config 走 usearch + tmp_path db
          2) 跑 scripts/init_db.py 建表 (跟 LIVE schema.sql 一致, 自带 sqlite-vec 注释剥离)
          3) Memory() init — 触发 embedder warm-up + usearch.index 初始化
    """
    import config as _cfg_mod
    monkeypatch.setattr(_cfg_mod.config, 'search_backend', 'usearch', raising=True)
    db_path = tmp_path / 'test.db'
    monkeypatch.setattr(_cfg_mod.config, 'db_path', db_path, raising=False)

    # 跑 schema.sql (内置 executescript 处理 -- 注释剥离)
    schema_path = _REPO / 'schema.sql'
    import sqlite3 as _sqlite
    import re
    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    # 跳过 PRAGMA / INSTALL / LOAD (sqlite-vec 加载) — usearch 模式不需要
    sql = re.sub(r'PRAGMA[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'INSTALL[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'LOAD[^;]*;', '', sql, flags=re.IGNORECASE)
    # 移除 vec0 段 (usearch 模式不写 vec0 表)
    sql = re.sub(
        r'CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)',
        '', sql, flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        conn.executescript(sql)
    except Exception as e:
        # 表已存在 — idempotent re-init, 跳过
        if 'already exists' not in str(e):
            raise
    conn.commit()
    conn.close()

    m = _memory_repo.Memory(db_path=db_path)
    yield m
    # 兜底清理本测试产生的 entity (conftest session-level 也清, 双保险)
    try:
        m._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'test_nsguard_%' "
            "AND valid_until IS NULL"
        )
        m._conn.execute(
            "DELETE FROM relations WHERE source_id LIKE 'test_nsguard_%' "
            "OR target_id LIKE 'test_nsguard_%'"
        )
        m._conn.commit()
    finally:
        m.close()


# ============================================================
# 黑名单 — 必须拒
# ============================================================

class TestNamespaceBlacklist:
    def test_anno_namespace_rejected(self, mem):
        """anno:* 是 HonchoImporter NER 历史残留. 必须拒."""
        ent = {
            "id": "anno:mentions:Python",
            "kind": "concept",
            "name": "Python",
        }
        with pytest.raises(ValidationError) as exc_info:
            mem._upsert_entity(ent)
        assert "anno:*" in str(exc_info.value)

    def test_anno_namespace_any_kind_rejected(self, mem):
        """即使改 kind, anno: 也拒 (不是 kind 问题, 是 namespace 问题)."""
        for kind in ("concept", "person", "provider", "event", "task"):
            ent = {"id": f"anno:foo_{kind}", "kind": kind, "name": "foo"}
            with pytest.raises(ValidationError) as exc_info:
                mem._upsert_entity(ent)
            assert "anno:*" in str(exc_info.value), f"kind={kind} 应该被拒"

    def test_token_c_namespace_rejected(self, mem):
        """TOKEN_C_* 随机 session token. 必须拒."""
        ent = {
            "id": "TOKEN_C_1785851842346446",
            "kind": "concept",
            "name": "TOKEN_C_1785851842346446",
        }
        with pytest.raises(ValidationError) as exc_info:
            mem._upsert_entity(ent)
        assert "TOKEN_*" in str(exc_info.value)

    def test_token_any_suffix_rejected(self, mem):
        """TOKEN_ 前缀都拒 (不只是 _C_)."""
        for suffix in ("C_123", "foo_bar", "abc"):
            ent = {"id": f"TOKEN_{suffix}", "kind": "concept", "name": "x"}
            with pytest.raises(ValidationError) as exc_info:
                mem._upsert_entity(ent)
            assert "TOKEN_*" in str(exc_info.value)


class TestConceptNameLength:
    def test_long_concept_name_rejected(self, mem):
        """concept kind 的 name > 50 chars 拒 (防整句当 entity)."""
        ent = {
            "id": "test_nsguard_long_concept",
            "kind": "concept",
            "name": "imported sleep runs at Beijing time 02:00 to avoid daytime CPU contention",
        }
        with pytest.raises(ValidationError) as exc_info:
            mem._upsert_entity(ent)
        assert "concept" in str(exc_info.value)
        assert "50 chars" in str(exc_info.value)

    def test_short_concept_name_allowed(self, mem):
        """concept + ≤ 50 chars → 通过."""
        ent = {
            "id": "test_nsguard_short_concept",
            "kind": "concept",
            "name": "翁氏 (Weng-fit)",
        }
        mem._upsert_entity(ent)  # 不抛 = pass
        # 验证写入成功
        row = mem._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
            ("test_nsguard_short_concept",),
        ).fetchone()
        assert row is not None

    def test_exactly_50_chars_allowed(self, mem):
        """边界: 正好 50 chars 通过."""
        ent = {
            "id": "test_nsguard_50char",
            "kind": "concept",
            "name": "a" * 50,
        }
        mem._upsert_entity(ent)


class TestNamelessKindCheck:
    def test_nameless_id_with_structured_kind_allowed(self, mem):
        """无 ':' 的 id + person kind → 通过."""
        ent = {
            "id": "test_nsguard_person_alice",
            "kind": "person",
            "name": "Alice",
        }
        mem._upsert_entity(ent)

    def test_nameless_id_with_unknown_kind_rejected(self, mem):
        """无 ':' 的 id + 未知 kind (e.g. 'foo') → 拒."""
        ent = {
            "id": "test_nsguard_unknown_kind",
            "kind": "foo",  # 不在 _NAMELESS_KINDS 白名单
            "name": "bar",
        }
        with pytest.raises(ValidationError) as exc_info:
            mem._upsert_entity(ent)
        assert "non-namespaced" in str(exc_info.value)

    def test_nameless_id_with_concept_short_name_allowed(self, mem):
        """无 namespace + concept + 短 name → 通过 (concept 已在 _NAMELESS_KINDS)."""
        ent = {
            "id": "test_nsguard_concept_x",
            "kind": "concept",
            "name": "翁氏",
        }
        mem._upsert_entity(ent)


# ============================================================
# 白名单 — 必须放行
# ============================================================

class TestNamespaceWhitelist:
    @pytest.mark.parametrize("ns", [
        "identity:github_handle:test_nsguard_user",
        "stock:test_nsguard_600021",
        "holding:2026-08-08:test_nsguard_600021",
        "loop:test_nsguard_daily",
        "task:test_nsguard_build_x",
    ])
    def test_allowed_namespace_passes(self, mem, ns):
        """5 个官方 namespace 都通过 (不管 name 多长)."""
        ent = {
            "id": ns,
            "kind": "concept" if ns.startswith("loop:") else (
                "identity_fact" if ns.startswith("identity:") else (
                    "stock" if ns.startswith("stock:") else (
                        "position_snapshot" if ns.startswith("holding:") else "task"
                    )
                )
            ),
            "name": "x" * 100,  # 长 name 也允许 (结构化 kind 不限)
        }
        mem._upsert_entity(ent)  # 不抛 = pass

    def test_master_prefix_passes(self, mem):
        """master_* 是 SOUL §mnelo ops #4 拍板的主语前缀."""
        ent = {
            "id": "master_test_nsguard_claude_code_bigbox",
            "kind": "concept",
            "name": "Claude Code BigBox Master",
        }
        mem._upsert_entity(ent)
        row = mem._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
            ("master_test_nsguard_claude_code_bigbox",),
        ).fetchone()
        assert row is not None


# ============================================================
# 集成 — memory_remember 路径也会触发 guard
# ============================================================

class TestRememberIntegration:
    def test_remember_with_anno_entity_rejected(self, mem):
        """memory_remember 传 anno: entity → ValidationError 上抛."""
        from memory import Memory as _M
        with pytest.raises(ValidationError):
            mem.remember(
                content="测试 chunk",
                source="test_nsguard_remember",
                importance=0.7,
                entities=[{
                    "id": "anno:test_nsguard_should_fail",
                    "kind": "concept",
                    "name": "test",
                }],
            )

    def test_remember_with_clean_entity_succeeds(self, mem):
        """memory_remember 传白名单 entity → 成功."""
        cid = mem.remember(
            content="测试 chunk with clean entity",
            source="test_nsguard_remember_clean",
            importance=0.7,
            entities=[{
                "id": "test_nsguard_clean_concept",
                "kind": "concept",
                "name": "干净 concept",
            }],
        )
        assert cid is not None
        # 验证 entity 写入
        row = mem._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
            ("test_nsguard_clean_concept",),
        ).fetchone()
        assert row is not None
