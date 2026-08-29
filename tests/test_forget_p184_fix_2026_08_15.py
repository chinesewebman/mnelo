"""[8/15 E-3 audit fix P1 #84] forget() \u8d70 _txn + zvec native \u8865\u507f\u673a\u5236.

\u80cc\u666f: v0.15.2 \u5ba1\u8ba1\u62ab\u9732\u2014 forget() \u591a\u6b65\u5199\u8def\u5f84:
  1. UPDATE chunks SET valid_until
  2. _index.remove (zvec native) - \u4e0d\u5728 SQLite \u4e8b\u52a1\u91cc
  3. UPDATE relations SET valid_until (cascade)
  4. INSERT purged_queue
  5. conn.commit

\u4e2d\u9014\u5931\u8d25 \u2192 \u90e8\u5206\u6570\u636e\u4e0d\u4e00\u81f4:
- graph \u8fd8\u5f15\u7528\u5df2 soft-deleted chunk (\u53ef\u80fd recall \u8d70 graph \u8def \u2192 \u9519)
- purged_queue \u4e22 \u2192 30\u5929\u540e\u4e0d\u4f1a\u88ab\u7269\u7406\u6e05\u7406

\u4fee\u590d (P1 #82 _upsert_entity + P1 #62 _txn nested \u5b9e\u6218\u8d44\u6e90):
- SQLite \u90e8\u5206\u8d70 with _txn(self._conn) \u5305\u88f9 (3 \u4e2a UPDATE + 1 INSERT)
- _index.remove() \u8d70 try/except \u00b7 SQLite ROLLBACK \u4ee5\u540e zvec native fail
  \u8d77 raise (\u8ba9 caller \u91cd\u8dd1)
- zvec fail \u4e0d\u662f \u201cfatal\u201d \u2014 \u53ea\u8bb0 logger.warning, \u4e0b\u6b21 _maintenance_run \u4f1a\u88ab \u2014...
  (lazy delete, mnelo \u539f\u8bbe\u8ba1 \u8c08\u8bba\u8fc7)

\u4ee5\u4e0a\u4e3a P1 #84 \u4fee\u590d\u7406\u8bba.

\u8003\u8651\u70b9 (\u8bbe\u8ba1\u4ea4\u6613 trade-off):
- \u4e25\u8c28: SQLite + zvec \u90fd\u540c\u6b65 commit \u2192 \u9700 2PC \u534f\u8bae (zvec WAL + \u8ddf\u968f SQLite commit) \u2192 \u590d\u6742
- \u7075\u6d3b: SQLite ROLLBACK \u4fdd\u8bc1\u9ed8\u8ba4\u4e00\u81f4, zvec fail \u8bb0 warning + \u540e\u53f0 lazy fix
- mnelo \u539f\u8bbe\u8ba1 (\u8c08\u8bba\u8fc7): chunks.soft_delete \u4fdd\u8bc1 recall query \u4f9d\u7136\u4e00\u81f4 (WHERE valid_until IS NULL),
  zvec stale vector \u53ea\u5f71\u54cd\u201c\u8d70 vector recall \u201d\u8def\u5f84, graph/meta/entity \u8def\u4ecd\u4e00\u81f4
  (vector recall \u8def filter valid_until \u540c\u65f6\u53ef\u590d\u7528).

\u9009\u9879: \u7075\u6d3b (\u53ef\u63a5\u53d7 + \u5b9e\u6218\u8db3\u591f\u4fdd\u8bc1\u5173\u952e\u4e00\u81f4).
"""

import pytest
import sqlite3 as _sqlite
from pathlib import Path
import tempfile


@pytest.fixture
def mem(tmp_path):
    """[P1 #84 fix] forget() \u8d70 SQLite _txn \u5305\u88f9 + zvec native \u8865\u507f.

    \u4e0d\u4f9d\u8d56 zvec (tmp_path + isolated db).
    """
    import re as _re

    db_path = tmp_path / "audit_p184.db"
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


def test_forget_soft_delete_within_txn(mem):
    """[P1 #84.1] forget() \u5fc5\u987b\u5728 _txn \u91cc\u8d70 UPDATE chunks.

    \u9a8c\u8bc1: forget(\u4e2a chunk) \u540e chunks.valid_until \u975e NULL.
    """
    from memory import now

    cid = mem.remember("test forget P1#84 chunk", source="audit", importance=0.5)
    assert cid
    mem.forget(cid, target_kind="chunk")
    row = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid,)).fetchone()
    assert row["valid_until"] is not None, "forget \u540e valid_until \u5e94\u8bbe\u4e3a now"


def test_forget_cascades_relations_within_txn(mem):
    """[P1 #84.2] forget(entity) cascade \u6240\u6709\u5f15\u7528\u8be5 entity \u7684 relations.

    \u9a8c\u8bc1: \u5f15\u7528\u8be5 entity \u7684 relations \u7684 valid_until \u540c\u4e3a now.
    """
    from memory import now

    eid = "audit_p184_entity"
    mem._conn.execute(
        "INSERT INTO entities (id, kind, name, valid_from) VALUES (?, ?, ?, ?)",
        (eid, "concept", "P1#84 Test", now()),
    )
    mem._conn.commit()
    rid = mem.relate(eid, "audit_p184_other", "test_rel", dedup_check=False)
    mem.forget(eid, target_kind="entity")
    row = mem._conn.execute("SELECT valid_until FROM relations WHERE id = ?", (rid,)).fetchone()
    assert row["valid_until"] is not None, "forget(entity) cascade relations valid_until"


def test_forget_inserts_purged_queue(mem):
    """[P1 #84.3] forget() \u63d2\u5165 purged_queue (\u4e0d\u80fd\u5728 _txn rollback \u540e\u4e22)."""
    cid = mem.remember("test forget queue", source="audit", importance=0.5)
    mem.forget(cid, target_kind="chunk")
    rows = mem._conn.execute(
        "SELECT target_id, target_kind, done FROM purged_queue WHERE target_id = ?",
        (cid,),
    ).fetchall()
    assert len(rows) == 1, f"purged_queue \u5e94\u6709\u4e00\u884c, \u5b9e\u9645 {len(rows)}"
    assert rows[0]["target_kind"] == "chunk"
    assert rows[0]["done"] == 0


def test_forget_idempotent_no_double_queue(mem):
    """[P1 #84.4] forget(\u540c\u4e00 chunk \u4e24\u6b21) \u4e0d\u91cd\u590d\u63d2\u5165 purged_queue.

    \u9a8c\u8bc1: \u7b2c\u4e8c\u6b21 forget \u662f no-op (\u539f chunk \u5df2 valid_until IS NOT NULL, _conn\u4e2d
    \u5224\u65ad\u4f1a\u8df3\u8fc7 UPDATE \u4f46\u4ecd\u4f1a INSERT purged_queue).\n    \u5982\u679c\u8be5\u63d2\u5165\u91cd\u590d, 30\u5929\u540e worker \u4f1a\u540c chunk \u5904\u7406\u4e24\u6b21.\n"""
    cid = mem.remember("test idempotent", source="audit", importance=0.5)
    mem.forget(cid, target_kind="chunk")
    mem.forget(cid, target_kind="chunk")  # 2nd no-op
    rows = mem._conn.execute("SELECT COUNT(*) AS c FROM purged_queue WHERE target_id = ?", (cid,)).fetchone()
    # Allow <=1 (idempotent ideal). \u5b9e\u9645\u5141\u8bb8 2 (\u8001 logic \u53ef\u80fd 2 \u884c).
    # P1 #84 fix \u540e: \u5e94\u4e3a 1 (\u4e8c\u6b21 \u53ea \u4e00 \u6b21\u751f\u6548 INSERT).
    assert rows["c"] <= 1, f"\u91cd\u590d\u63d2\u5165 {rows['c']} \u6b21, \u5e94 \u4ec5 1"


def test_forget_with_txn_rollback_zvec_native_fail(mem, monkeypatch):
    """[P1 #84.5] zvec _index.remove() \u5931\u8d25 \u4e0d\u5e94\u62a5 SQLite \u4e8b\u52a1 ROLLBACK.

    \u9a8c\u8bc1: monkeypatch _index.remove \u629b\u5f02\u5e38 \u2192 forget() \u8fd4\u54cd\u5e94 \u4e0d raise,
    SQLite \u90e8\u5206\u4ecd commit (\u7075\u6d3b \u7b56\u7565).\n    chunks.valid_until \u4ecd\u8bbe\u7f6e\u4e3a now (\u4fdd\u8bc1 graph \u4e00\u81f4),
    zvec stale vector \u540e\u53f0 lazy \u5904\u7406.
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("zvec native crash simulated")

    monkeypatch.setattr(mem._index, "remove", _boom)
    cid = mem.remember("test zvec fail", source="audit", importance=0.5)
    try:
        result = mem.forget(cid, target_kind="chunk")
    except RuntimeError:
        pytest.fail("forget() \u4e0d\u5e94\u4f20\u9012 zvec native \u9519\u8bef\u4e00\u81f4 raise (P1 #84 \u7075\u6d3b\u7b56\u7565)")
    # SQLite commit OK
    row = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid,)).fetchone()
    assert row["valid_until"] is not None, "zvec fail \u540e SQLite \u90e8\u5206\u5e94 commit"
    # purged_queue INSERT
    rows = mem._conn.execute("SELECT COUNT(*) AS c FROM purged_queue WHERE target_id = ?", (cid,)).fetchone()
    assert rows["c"] >= 1, "zvec fail \u540e\u5e94\u4ecd\u63d2\u5165 purged_queue"


def test_forget_does_not_affect_other_chunks(mem):
    """[P1 #84.6] forget(\u4e00\u4e2a chunk) \u4e0d\u5e71\u627a\u5176\u4ed6 chunk.\n    \u9a8c\u8bc1: \u72ec\u7acb forget \u4e0d\u8de8\u8d8a.\n"""
    cid1 = mem.remember("chunk 1", source="audit", importance=0.5)
    cid2 = mem.remember("chunk 2", source="audit", importance=0.5)
    mem.forget(cid1, target_kind="chunk")
    row2 = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid2,)).fetchone()
    assert row2["valid_until"] is None, "\u5176\u4ed6 chunk \u4e0d\u5e94\u88ab\u5f71\u54cd"
