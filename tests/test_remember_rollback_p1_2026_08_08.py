"""[8/8 P1 fix] Memory.remember() 必须保证 entity validation 失败时 chunk 不写入.

之前 bug: remember() 先 INSERT chunk (line 555) 再 for-loop _upsert_entity.
如果 namespace guard / validate_entity_payload 抛 ValidationError, chunk
INSERT 已经进 SQLite WAL, mcp_server 单例 Memory conn 复用下次 commit 可能
连同提交, 留下孤儿 chunk (entity 缺席但 chunk 占位).

修复: remember() 在 INSERT chunk 之前先 dry-run validate 全部 entities.
ValidationError → 抛异常 → SQLite 事务自动 rollback (因为 commit 在 line 627
之后), chunk 不入库.

[测试矩阵]
  1. anno: entity + remember → chunk 不入库
  2. 长 concept name + remember → chunk 不入库
  3. 干净 entity + remember → chunk 入库
  4. 多个 entity, 第 2 个坏 → chunk 不入库
  5. entity validation 失败不污染 audit_log
"""
import importlib.util as _ilu
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


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


_validation_repo = _load_from_repo('validation')
_memory_repo = _load_from_repo('memory')
_memory_repo.ValidationError = _validation_repo.ValidationError  # type: ignore[attr-defined]

from validation import ValidationError  # noqa: E402


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory with tmp_path db + usearch backend (no zvec LOCK)."""
    import config as _cfg_mod
    monkeypatch.setattr(_cfg_mod.config, 'search_backend', 'usearch', raising=True)
    db_path = tmp_path / 'test.db'
    monkeypatch.setattr(_cfg_mod.config, 'db_path', db_path, raising=False)

    schema_path = _REPO / 'schema.sql'
    import sqlite3 as _sqlite
    import re
    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    sql = re.sub(r'PRAGMA[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'INSTALL[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(r'LOAD[^;]*;', '', sql, flags=re.IGNORECASE)
    sql = re.sub(
        r'CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)',
        '', sql, flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        conn.executescript(sql)
    except Exception as e:
        if 'already exists' not in str(e):
            raise
    conn.commit()
    conn.close()

    m = _memory_repo.Memory(db_path=db_path)
    yield m
    try:
        m._conn.execute(
            "DELETE FROM chunks WHERE source LIKE 'test_rollback_%'"
        )
        m._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'test_rollback_%' "
            "AND valid_until IS NULL"
        )
        m._conn.commit()
    finally:
        m.close()


def _count_chunks(mem, source: str) -> int:
    return mem._conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE source = ?", (source,)
    ).fetchone()[0]


def _count_audit(mem, source_marker: str) -> int:
    return mem._conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE ref_id LIKE ?", (f"%{source_marker}%",)
    ).fetchone()[0]


class TestRememberRollback:
    def test_anno_entity_does_not_create_chunk(self, mem):
        """anno: entity → ValidationError → chunk 不入库 (事务回滚)."""
        source = "test_rollback_anno"
        with pytest.raises(ValidationError) as exc_info:
            mem.remember(
                content="anno guard test content",
                source=source,
                importance=0.7,
                entities=[{
                    "id": "anno:mentions:Python",
                    "kind": "concept",
                    "name": "Python",
                }],
            )
        assert "anno:*" in str(exc_info.value)
        # 关键: chunk 没入库
        assert _count_chunks(mem, source) == 0, "chunk 应该被事务回滚"

    def test_long_concept_name_does_not_create_chunk(self, mem):
        """长 concept name → ValidationError → chunk 不入库."""
        source = "test_rollback_long"
        with pytest.raises(ValidationError):
            mem.remember(
                content="long concept name test content",
                source=source,
                importance=0.7,
                entities=[{
                    "id": "test_rollback_long_concept",
                    "kind": "concept",
                    "name": "imported sleep runs at Beijing time 02:00 to avoid daytime CPU contention",
                }],
            )
        assert _count_chunks(mem, source) == 0

    def test_unknown_kind_does_not_create_chunk(self, mem):
        """未知 kind → ValidationError → chunk 不入库."""
        source = "test_rollback_unknown_kind"
        with pytest.raises(ValidationError):
            mem.remember(
                content="unknown kind test content",
                source=source,
                importance=0.7,
                entities=[{
                    "id": "test_rollback_foo",
                    "kind": "garbage_kind",  # 不在 _NAMELESS_KINDS 也不在 whitelist
                    "name": "x",
                }],
            )
        assert _count_chunks(mem, source) == 0

    def test_second_bad_entity_does_not_create_chunk(self, mem):
        """多 entity, 第 2 个坏 → chunk 不入库 (不是只回滚第 2 个)."""
        source = "test_rollback_multi"
        with pytest.raises(ValidationError) as exc_info:
            mem.remember(
                content="multi entity test content",
                source=source,
                importance=0.7,
                entities=[
                    {  # 第 1 个干净
                        "id": "test_rollback_multi_clean",
                        "kind": "concept",
                        "name": "干净 concept",
                    },
                    {  # 第 2 个坏
                        "id": "anno:should_fail",
                        "kind": "concept",
                        "name": "fail",
                    },
                ],
            )
        assert "anno:*" in str(exc_info.value)
        assert _count_chunks(mem, source) == 0
        # 第 1 个干净 entity 也不应该入库 (事务整体回滚)
        row = mem._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
            ("test_rollback_multi_clean",),
        ).fetchone()
        assert row is None

    def test_clean_entities_create_chunk(self, mem):
        """干净 entity → chunk 入库 (regression: 修复不影响正常路径)."""
        source = "test_rollback_clean"
        cid = mem.remember(
            content="clean entity test content",
            source=source,
            importance=0.7,
            entities=[{
                "id": "test_rollback_clean_concept",
                "kind": "concept",
                "name": "正常 concept",
            }],
        )
        assert cid is not None
        assert _count_chunks(mem, source) == 1
        row = mem._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
            ("test_rollback_clean_concept",),
        ).fetchone()
        assert row is not None

    def test_failed_remember_does_not_leak_audit(self, mem):
        """ValidationError → audit_log 也不应污染 (purged_queue / forget 等才有 audit).

        注意: namespace guard 是 ValidationError 上抛, 不走 audit_log — 只有
        forget/remember 成功路径才写 audit. 这里验证失败路径 audit_log 没
        被异常路径污染.
        """
        source = "test_rollback_no_audit"
        audit_before = _count_audit(mem, source)
        with pytest.raises(ValidationError):
            mem.remember(
                content="audit pollution test",
                source=source,
                importance=0.7,
                entities=[{
                    "id": "anno:audit_test",
                    "kind": "concept",
                    "name": "x",
                }],
            )
        audit_after = _count_audit(mem, source)
        assert audit_after == audit_before, "ValidationError 不应污染 audit_log"
