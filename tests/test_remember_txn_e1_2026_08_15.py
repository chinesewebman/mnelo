"""[8/15 E-1 P1] Memory.remember()/update() 必须显式事务包裹, 避免 vec0/usearch 漂移.

主人 DESIGN §1.2 #7: 写路径无显式事务/回滚. 修法:
  - _txn() contextmanager 显式 BEGIN/COMMIT/ROLLBACK
  - index.add 失败 → SQLite ROLLBACK → chunk 不入库
  - relations 失败 → SQLite ROLLBACK → chunk+entities 都不入库
  - update() 同样保护 (不再静默吞 embed 异常)

[测试矩阵]
  1. embed 失败 (monkeypatch index.add raise) → chunk 不入库
  2. relations FK 失败 (target_id 不存在) → chunk+entities 都不入库
  3. 正常路径 → 全部入库 (regression)
  4. update() embed 失败 → 老 chunk valid_until 仍是 NULL, 新 chunk 不入库,
     异常上抛 (不再静默吞)
"""

import importlib.util as _ilu
import sqlite3
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

from validation import ValidationError  # noqa: E402


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory with tmp_path db + usearch backend (no zvec LOCK)."""
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
        conn.executescript(sql)
    except Exception as e:
        if "already exists" not in str(e):
            raise
    conn.commit()
    conn.close()

    m = _memory_repo.Memory(db_path=db_path)
    yield m
    try:
        m._conn.execute("DELETE FROM chunks WHERE source LIKE 'test_txn_e1_%'")
        m._conn.execute("DELETE FROM entities WHERE id LIKE 'test_txn_e1_%' AND valid_until IS NULL")
        m._conn.commit()
    finally:
        m.close()


def _count_chunks(mem, source: str) -> int:
    return mem._conn.execute("SELECT COUNT(*) FROM chunks WHERE source = ?", (source,)).fetchone()[0]


def _count_entities(mem, eid_prefix: str) -> int:
    return mem._conn.execute(
        "SELECT COUNT(*) FROM entities WHERE id LIKE ? AND valid_until IS NULL",
        (f"{eid_prefix}%",),
    ).fetchone()[0]


class TestRememberTxnE1:
    """[8/15 E-1] remember() 显式事务 + index.add 失败回滚."""

    def test_index_add_failure_rolls_back_chunk(self, mem, monkeypatch):
        """[核心测试] index.add 抛异常 → chunk 不入库 (事务回滚).

        之前 bug 场景: remember() 在 line 475 调用 self._index.add(...),
        如果 add 失败 (e.g. usearch SIGSEGV / disk full / IO error),
        chunk INSERT 已经在 SQLite WAL 里, 但只有 line 510 才 commit.
        当前 conn 是单例 (mcp_server.py 复用), 下次 commit 可能把孤儿 chunk
        连同提交 → vec0 rowid 漂移.

        修复期望: 显式事务包 SQLite 操作, index.add 失败时 ROLLBACK,
        chunk 永不入库.
        """
        source = "test_txn_e1_embed_fail"

        def _boom(*a, **kw):
            raise RuntimeError("simulated index.add failure (disk full)")

        monkeypatch.setattr(mem._index, "add", _boom)

        with pytest.raises(RuntimeError, match="simulated index.add failure"):
            mem.remember(
                content="embed failure test content",
                source=source,
                importance=0.5,
            )

        # 关键断言: chunk 没入库 (事务回滚)
        assert _count_chunks(mem, source) == 0, "index.add 失败时 chunk 必须在事务里回滚, 不留孤儿"

    def test_relations_fk_failure_rolls_back_chunk_and_entities(self, mem):
        """[E-1.2] relations INSERT 失败 → chunk + entities 都不入库.

        利用 SQLite NOT NULL 约束: relations.source_id TEXT NOT NULL,
        传 None 必然 raise IntegrityError. 验证整段事务回滚.
        """
        source = "test_txn_e1_rel_fail"
        with pytest.raises(sqlite3.IntegrityError):
            mem.remember(
                content="relations failure test",
                source=source,
                importance=0.5,
                entities=[
                    {
                        "id": "test_txn_e1_rel_entity",
                        "kind": "concept",
                        "name": "rel fail test",
                    }
                ],
                relations=[
                    {
                        "source_id": None,  # NOT NULL 违反 → 必 raise
                        "target_id": "test_txn_e1_rel_entity",
                        "relation": "self_ref",
                    }
                ],
            )

        # 关键: chunk 和 entity 都不入库 (事务整体回滚)
        assert _count_chunks(mem, source) == 0, "relations 失败时 chunk 必须回滚"
        assert _count_entities(mem, "test_txn_e1_rel_entity") == 0, "relations 失败时 entity 也必须回滚"

    def test_clean_remember_works(self, mem):
        """[E-1.3] regression: 干净路径不受影响."""
        source = "test_txn_e1_clean"
        cid = mem.remember(
            content="clean txn test",
            source=source,
            importance=0.5,
            entities=[
                {
                    "id": "test_txn_e1_clean_entity",
                    "kind": "concept",
                    "name": "clean",
                }
            ],
        )
        assert cid is not None
        assert _count_chunks(mem, source) == 1
        assert _count_entities(mem, "test_txn_e1_clean_entity") == 1


class TestUpdateTxnE1:
    """[8/15 E-1] update() 显式事务 + index.add 失败回滚 + 不再静默吞异常."""

    def test_update_index_add_failure_rolls_back_and_raises(self, mem, monkeypatch):
        """update() 中新 chunk 嵌入失败 → 老 chunk valid_until 仍是 NULL,
        新 chunk 不入库, 异常上抛 (不再静默吞).

        之前 bug 场景: update() 顺序:
          1. INSERT 新 chunk
          2. UPDATE 老 chunk 标 superseded_by + valid_until
          3. index.remove(old)
          4. index.add(new)  ← 失败时 except 仅 logger.warning
          5. commit          ← 仍然 commit
        结果: 老 chunk 被错误软删, 新 chunk 入库但 vector 缺席, 召回断裂.

        修复期望: 显式事务, index.add 失败时 ROLLBACK, 老 chunk valid_until
        仍 NULL, 异常上抛供调用方感知.
        """
        source = "test_txn_e1_update_orig"
        old_cid = mem.remember(
            content="original content for update test",
            source=source,
            importance=0.5,
        )
        assert _count_chunks(mem, source) == 1

        # 让 index.add 失败
        def _boom(*a, **kw):
            raise RuntimeError("simulated update index.add failure")

        monkeypatch.setattr(mem._index, "add", _boom)

        # 异常必须上抛, 不能被静默吞
        with pytest.raises(RuntimeError, match="simulated update index.add"):
            mem.update(
                old_id=old_cid,
                new_content="updated content for txn test",
                reason="txn_e1_test",
            )

        # 关键: 老 chunk valid_until 仍为 NULL (没被标 superseded)
        old_row = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (old_cid,)).fetchone()
        assert old_row["valid_until"] is None, "update index 失败时老 chunk 不应被 superseded, 否则召回断裂"
        # 新 chunk 也没入库
        assert _count_chunks(mem, source) == 1, "update 失败时新 chunk 必须回滚, 仍只有 1 条"
