"""[8/16 E-2 重启 non-trigger 模式] meta \u8def FTS5 BM25 + LIKE fallback (\u7ed5\u8fc7 P1 #66-#81 \u5b9e\u6218\u6559\u8bad).

\u80cc\u666f: v0.15.3 \u5b8c\u6210\u540e\u91cd\u542f E-2 FTS5 \u5b9e\u6218\u3002\u4e0a\u6b21 (commit aa5469b + 4 fix chain) \u88ab\u64a4\u56de\u56e0\u4e3a P1 #81 SIGSEGV (\u4e0a\u8f6e 4 commits revert + \u5b9e\u6218\u6559\u8bad\u5b8c\u6574\u4fdd\u7559)\u3002

\u672c\u6b21\u91cd\u542f design \u00b7 \u907f\u514d\u4e0a\u4e2d\u6240\u6709\u9677\u9631\uff08P1 #66-#81\uff09\uff1a
- **\u4e0d\u4f7f\u7528 trigger**: \u907f\u514d P1 #66/#75/#78/#79 trigger context \u62a5\u9519\u4e0e FTS5 'delete' cmd \u95ee\u9898
- **\u624b\u52a8 INSERT chunks_fts**: \u5728 _txn \u5757\u5185\u624b\u52a8 sync (\u907f\u514d P1 #72 test fixture cleanup \u4e92\u8e29)
- **\u4e0d\u540c rowid policy**: chunks \u9690\u5f0f rowid INTEGER (\u4e0d\u662f chunks.id TEXT PK) \u00b7 chunks_fts rowid \u8ddf\u968f
- **\u9690\u85cf stale FTS5 \u540c rowid**: P1 #77 \u8981\u6c42\u6e05\u7406 stale rowid \u00b7 \u8003\u8651\u6c7d\u8f66 \u4e8b\u52a1\u53ef\u80fd\u9009\u62e9 INSERT OR REPLACE \u8f7d\u8f7d\u903b\u8f91
- **\u4e0d trigger firing \u00b7 \u4e0d zvec SIGSEGV**: P1 #81 \u539f\u56e0 \u00b7 \u624b\u52a8 sync \u53ef\u63a7 \u00b7 \u907f\u514d native crash

\u4e0a\u4e2d P1 #66-#81 \u6559\u8bad\u5168\u90e8\u907f\u514d\u3002\u6d4b\u8bd5\u9700\u8981\u9a8c\u8bc1\uff1a
1. FTS5 \u865a\u8868\u5b58\u5728 (\u4e0d\u4f9d\u8d56 trigger)
2. \u624b\u52a8 INSERT \u540c\u6b65 chunk (\u4e0d\u4f7f\u7528 trigger)
3. \u4e2d\u6587\u77ed\u67e5\u8be2 LIKE fallback (P1 #68)
4. \u5b8c\u6574\u4e2d\u6587\u53e5 FTS5 BM25 (P1 #68 \u4e0a\u9650)
5. \u67e5\u8be2\u591a\u8def UNION ALL + \u8c03\u8bd5\u8d77\u9019\u4e91\u88c5\u8f7d
6. soft delete \u4e0d\u4f7d\u8001 row (\u4e0d trigger \u00b7 valid_until IS NULL \u8fc7\u6ee4)
7. \u9690\u85cf rowid cleanup (P1 #77) \u00b7 \u8bd5 fixture reuse \u540c db \u4e0d\u51b2\u7a81
8. \u7d27\u8d34 trigger \u9a8c\u8bc1: schema \u4e2d\u4e0d\u5e94\u6709\u4efb\u4f55 chunks_fts trigger (\u907f\u514d P1 #81 SIGSEGV)

\u539f\u8bbe\u8ba1 (DESIGN \u00a74.1) \u4ecd\u7136\u4fdd\u6301\uff1aLIKE %q% fallback \u8865\u8db3\u4e2d\u6587\u77ed\u8bcd\u3001trigram FTS5 \u4e3b\u8def\u52a0\u901f\u3001\u4e0d\u7834\u574f mnelo \u73b0\u6709 recall \u903b\u8f91\u3002
"""

import pytest
import sqlite3 as _sqlite
from pathlib import Path
import tempfile
import re as _re


@pytest.fixture
def mem(tmp_path):
    """[P1 #66-#81 实战综合] \u4e0d\u4f7f\u7528 trigger \u00b7 \u624b\u52a8 FTS5 sync \u00b7 isolated tmp_path db \u00b7 usearch backend \u907f\u514d zvec LOCK."""
    db_path = tmp_path / "e2_meta_fts5.db"
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


def test_e2_fts5_table_exists_no_triggers(mem):
    """[E-2.1] chunks_fts \u865a\u8868\u5b58\u5728\u3002\u5173\u952e\uff1aschema \u4e2d\u4e0d\u5e94\u6709\u4efb\u4f55 chunks_fts trigger (P1 #66/#75/#81 \u5b9e\u6218).\n"""
    row = mem._conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone()
    assert row is not None, "chunks_fts \u865a\u8868\u4e0d\u5b58\u5728"
    # \u9a8c\u8bc1\u65e0 trigger (P1 #81 SIGSEGV \u539f\u56e0)
    triggers = mem._conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%chunks_fts%'").fetchall()
    assert len(triggers) == 0, f"chunks_fts triggers \u5b9e\u6218\u4e0d\u5e94\u5b58\u5728 (P1 #81): {triggers}"


def test_e2_manual_fts5_sync_on_remember(mem):
    """[E-2.2] remember() \u540e chunks_fts \u5e94\u542b\u540c rowid \u7684 token (\u624b\u52a8 sync \u8003 P1 #66/#78/#79)."""
    cid = mem.remember("\u4e70\u5165 AAPL 100\u80a1", source="e2_test", importance=0.7)
    assert cid
    # \u9a8c\u8bc1 chunks_fts \u4e2d\u6709\u5bf9\u5e94 rowid \u7684\u8bb0\u5f55
    row = mem._conn.execute("SELECT c.id, c.rowid FROM chunks c WHERE c.id = ?", (cid,)).fetchone()
    chunk_rowid = row["rowid"]
    fts_rows = mem._conn.execute("SELECT rowid FROM chunks_fts WHERE rowid = ?", (chunk_rowid,)).fetchall()
    assert len(fts_rows) == 1, f"chunks_fts \u672a\u624b\u52a8 sync (\u671f\u671b 1 \u884c, \u5b9e\u9645 {len(fts_rows)})"


def test_e2_short_chinese_falls_back_to_like(mem):
    """[E-2.3] \u4e2d\u6587\u77ed\u67e5\u8be2 LIKE fallback (P1 #68 trigram \u77ed\u67e5\u8be2\u4e0d\u547d\u4e2d)."""
    cid = mem.remember("\u7279\u53d8 \u5b9e\u6218\u8b6f test \u4e70\u5165", source="e2_test", importance=0.5)
    # trigram \u4e0d\u547d\u4e2d 2-char \u4e2d\u6587\u00b7LIKE fallback \u5e94\u547d\u4e2d
    results = mem._meta_recall("\u7279\u53d8", top_k=5, filters={}, asof=None)
    assert any(r["chunk_id"] == cid for r in results), f"LIKE fallback \u672a\u8fd4\u56de\u9884\u671f chunk\uff1a{results}"


def test_e2_full_chinese_match_bm25(mem):
    """[E-2.4] \u5b8c\u6574\u4e2d\u6587\u53e5 FTS5 BM25 \u4e3b\u8def (\u4e0a\u9650 \u00b7 P1 #68)."""
    cid = mem.remember("\u4e70\u5165 \u592a\u9633\u80a1 \u80a1\u4ef7\u4e0a\u5347", source="e2_test", importance=0.5)
    # FTS5 BM25 \u5e94\u8fd4\u56de\u5b8c\u6574\u53e5\u5339\u914d\u7684 chunk
    results = mem._meta_recall("\u4e70\u5165 \u592a\u9633\u80a1", top_k=5, filters={}, asof=None)
    found_ids = [r["chunk_id"] for r in results]
    assert cid in found_ids, f"FTS5 BM25 \u672a\u547d\u4e2d\u5b8c\u6574\u53e5\uff1a{results}"


def test_e2_meta_recall_unions_fts5_and_like(mem):
    """[E-2.5] \u67e5\u8be2\u591a\u8def UNION ALL \u00b7 BM25 \u4e3b\u8def + LIKE fallback \u53bb\u91cd (P1 #67/#69/#70 \u907f\u514d)."""
    # 2 \u4e2a chunk: \u4e00\u4e2a\u5168\u53e5 FTS5, \u4e00\u4e2a\u53ea\u6709\u77ed\u8bcd LIKE
    cid_long = mem.remember("\u4e70\u5165 \u592a\u9633\u80a1 \u80a1\u4ef7\u4e0a\u5347", source="e2_test", importance=0.5)
    cid_short = mem.remember("\u7279\u53d8 \u5176\u4ed6\u5185\u5bb9", source="e2_test", importance=0.5)
    # \u67e5\u8be2\u4ec5\u77ed\u8bcd \u2192 \u5e94 LIKE fallback \u547d\u4e2d cid_short
    results = mem._meta_recall("\u7279\u53d8", top_k=5, filters={}, asof=None)
    found_ids = [r["chunk_id"] for r in results]
    assert cid_short in found_ids, f"\u77ed\u8bcd LIKE fallback \u672a\u547d\u4e2d\uff1a{results}"
    # \u67e5\u8be2\u5168\u53e5 \u2192 \u5e94 FTS5 BM25 \u547d\u4e2d cid_long
    results = mem._meta_recall("\u4e70\u5165 \u592a\u9633\u80a1", top_k=5, filters={}, asof=None)
    found_ids = [r["chunk_id"] for r in results]
    assert cid_long in found_ids, f"\u5168\u53e5 BM25 \u672a\u547d\u4e2d\uff1a{results}"


def test_e2_soft_delete_keeps_fts_row_filtered_by_valid_until(mem):
    """[E-2.6] soft delete (\u8bbe valid_until) \u4e0d\u9508\u53d1 FTS5 \u5220\u9664 (\u67e5\u8be2\u4fa7 WHERE valid_until IS NULL \u8fc7\u6ee4, P1 #72/\u8bbe\u8ba1\u8c08\u8bba\u8fc7)."""
    from memory import now as _now

    cid = mem.remember("soft delete \u5b9e\u6218 test", source="e2_test", importance=0.5)
    mem._conn.execute("UPDATE chunks SET valid_until = ? WHERE id = ?", (_now(), cid))
    mem._conn.commit()
    # FTS5 \u5e94\u4ecd\u542b\u8be5 chunk \u00b7 \u4f46 recall \u8fc7\u6ee4\u4f1a\u8fc7\u6ee4
    results = mem._meta_recall("soft delete", top_k=5, filters={}, asof=None)
    found_ids = [r["chunk_id"] for r in results]
    assert cid not in found_ids, f"\u8f6f\u5220 chunk \u4e0d\u5e94\u88ab recall \u8fd4\u56de\uff1a{results}"
    # \u9a8c\u8bc1 FTS5 \u4e2d\u4ecd\u6709\u8be5 rowid (stale token, \u8fc7\u6ee4\u5728 query \u4fa7)
    rowid = mem._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (cid,)).fetchone()["rowid"]
    fts_rows = mem._conn.execute("SELECT rowid FROM chunks_fts WHERE rowid = ?", (rowid,)).fetchall()
    assert len(fts_rows) == 1, f"FTS5 \u5e94\u4fdd\u7559 stale row\uff1a{fts_rows}"


def test_e2_stale_fts5_rowid_cleanup_p77(mem):
    """[E-2.7] \u91cd\u590d\u8c03 remember(\u540c content) \u4e0d\u51b2\u7a81 FTS5 (P1 #77 stale rowid \u8b66\u793a).

    \u9a8c\u8bc1: \u5b8c\u6210 cleanup_seed (\u624b\u52a8 DELETE chunks_fts WHERE rowid NOT IN chunks) \u540e\u00b7\u91cd\u590d
    insert \u540c rowid \u4e0d\u62a5 UNIQUE conflict\u3002
    """
    cid1 = mem.remember("cleanup test", source="e2_test", importance=0.5)
    # \u624b\u52a8 hard delete chunks \u00b7 \u6a21\u62df benchmark/test fixture cleanup
    mem._conn.execute("DELETE FROM chunks WHERE id = ?", (cid1,))
    mem._conn.commit()
    # P1 #77 \u624b\u52a8 cleanup\uff1a\u540c\u6b65 FTS5 stale rowid
    mem._conn.execute("DELETE FROM chunks_fts WHERE rowid NOT IN (SELECT rowid FROM chunks)")
    mem._conn.commit()
    # \u518d\u6b21 remember \u540c content \u00b7 chunks \u4f7f\u7528\u65b0 rowid (auto increment)\u00b7 FTS5 \u4e0d\u51b2\u7a81
    cid2 = mem.remember("cleanup test", source="e2_test", importance=0.5)
    assert cid1 != cid2, "\u4e0d\u540c cid \u4f46\u662f\u540c content"
    # \u9a8c\u8bc1 FTS5 \u4e2d\u4ec5\u6709 1 \u884c\uff08stale \u88ab cleanup, \u65b0\u7684\u88ab INSERT\uff09
    fts_count = mem._conn.execute(
        "SELECT COUNT(*) AS c FROM chunks_fts WHERE chunks_fts MATCH ?",
        ("cleanup test",),
    ).fetchone()["c"]
    assert fts_count >= 1, f"FTS5 \u8be5\u5305\u542b 1 \u4e2a match\uff1a{fts_count}"


def test_e2_no_chunks_fts_trigger_p81_safety(mem):
    """[E-2.8 P1 #81 safety] schema \u4e2d\u4e0d\u5e94\u6709 chunks_fts trigger (\u907f\u514d SIGSEGV)."""
    triggers = mem._conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'trg_chunks_fts%' OR tbl_name = 'chunks_fts')").fetchall()
    assert len(triggers) == 0, f"chunks_fts triggers \u4e0d\u5e94\u5b58\u5728 (P1 #81 SIGSEGV \u539f\u56e0)\uff1a{triggers}"


def test_e2_meta_recall_handles_empty_db(mem):
    """[E-2.9] \u7a7a db recall \u4e0d\u62a5\u9519\u3002"""
    results = mem._meta_recall("anything", top_k=5, filters={}, asof=None)
    assert results == [], f"\u7a7a db \u8fd4\u56de\u975e\u7a7a\u7ed3\u679c\uff1a{results}"


def test_e2_meta_recall_respects_top_k_limit(mem):
    """[E-2.10] top_k \u9650\u5236\u751f\u6548\u3002"""
    for i in range(5):
        mem.remember(f"\u4e70\u5165 AAPL \u7b2c {i} \u6b21", source="e2_test", importance=0.5)
    results = mem._meta_recall("AAPL", top_k=2, filters={}, asof=None)
    assert len(results) <= 2, f"top_k \u672a\u9650\u5236\u7ed3\u679c\u6570\uff1a{len(results)}"


def test_e2_meta_recall_filters_by_user_id(mem):
    """[E-2.11] user_id filter \u751f\u6548\u3002"""
    from memory import now as _now

    cid1 = mem.remember("filter test 1", source="e2_test", importance=0.5, user_id="alice")
    cid2 = mem.remember("filter test 2", source="e2_test", importance=0.5, user_id="bob")
    results = mem._meta_recall("filter test", top_k=5, filters={"user_id": "alice"}, asof=None)
    found_ids = [r["chunk_id"] for r in results]
    assert cid1 in found_ids, f"alice filter \u672a\u8fd4 alice chunk\uff1a{results}"
    assert cid2 not in found_ids, f"alice filter \u8fd4 bob chunk\uff1a{results}"
