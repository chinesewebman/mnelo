"""[bug fix P1 2026-08-29] LIKE pattern in recall queries doesn't escape SQL LIKE wildcards.

SQLite LIKE recognizes two wildcards:
  - % = any sequence (including empty)
  - _ = single character

If a user recall query contains `%` or `_`, the current code constructs
`f"%{query}%"` and passes it as a LIKE pattern parameter. SQLite then treats
the user's literal `%`/`_` as wildcards, causing:
  - Unexpected matches (over-recall)
  - Missed matches when query contains literal `_` (e.g. `snake_case`)

This breaks recall for any user query containing these chars. FTS5 has its own
escape (`_fts_escape_query`) — this is the LIKE-side equivalent.

Fix: introduce `_escape_like(query)` that escapes `\` to `\\`, then `%` to `\\%`,
then `_` to `\\_`. Apply it to all 5 LIKE pattern constructions in
memory_core.py (lines 1576 / 1646 / 1829 / 1909 / 1924 / 2052 / 1646).

Tests verify: `query='%'` no longer matches non-`%` content; `query='_'`
matches only literal underscore content; `query='snake_case'` matches
literal underscore; the helper itself round-trips ordinary text unchanged.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# === Pure helper unit tests (no DB needed) ===

def test_escape_like_returns_str():
    """Helper signature: str → str."""
    from memory import _escape_like
    assert isinstance(_escape_like("hello"), str)


def test_escape_like_passes_through_plain_text():
    """Ordinary text without special chars is unchanged."""
    from memory import _escape_like
    assert _escape_like("hello world") == "hello world"
    assert _escape_like("北京") == "北京"
    assert _escape_like("user@email.com") == "user@email.com"


def test_escape_like_escapes_percent():
    """Literal `%` in user query must be escaped to `\\%` so LIKE treats as literal."""
    from memory import _escape_like
    # Order matters: backslash first, then percent.
    # Source `%` → escaped `\%` (SQLite ESCAPE '\\')
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("%") == "\\%"
    assert _escape_like("a%b") == "a\\%b"


def test_escape_like_escapes_underscore():
    """Literal `_` must be escaped to `\\_`."""
    from memory import _escape_like
    assert _escape_like("snake_case") == "snake\\_case"
    assert _escape_like("_") == "\\_"
    assert _escape_like("a_b_c") == "a\\_b\\_c"


def test_escape_like_escapes_backslash_first():
    """If input has both backslash and %, escape backslash first so % becomes
    literal %, not part of an escape sequence."""
    from memory import _escape_like
    # `a\%` (user typed literal backslash + percent) → `a\\\%`
    # (SQLite ESCAPE '\\' reads: \\ is literal \, \% is literal %)
    assert _escape_like("a\\%") == "a\\\\\\%"


def test_escape_like_combined():
    """Combined special chars all escaped."""
    from memory import _escape_like
    assert _escape_like("foo_bar%baz") == "foo\\_bar\\%baz"


def test_escape_like_empty():
    """Empty input returns empty."""
    from memory import _escape_like
    assert _escape_like("") == ""


# === Integration tests: prove the bug is fixed at the SQL level ===

@pytest.fixture
def mem_with_chunks(tmp_path, monkeypatch):
    """Build a Memory() fixture with 4 known chunks, used to verify recall
    correctly handles LIKE wildcards.

    We use a tmp_path DB so the test is hermetic and doesn't touch prod.
    """
    import os
    import re
    import sqlite3

    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo))
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "search_backend", "usearch", raising=True)
    monkeypatch.setattr(_cfg, "db_path", tmp_path / "test_like_escape.db", raising=False)

    # Schema bootstrap (same pattern as E-1 fixture P1 #4)
    schema_path = repo / "schema.sql"
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

    db_path = tmp_path / "test_like_escape.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(sql)
    except Exception as e:
        if "already exists" not in str(e):
            raise
    conn.commit()

    # Seed: 4 chunks — content carefully chosen to disambiguate % / _ / plain.
    seed = [
        ("like_escape_seed_plain", "plain text without specials", "test_like_escape"),
        ("like_escape_seed_percent", "discount 100% off", "test_like_escape"),
        ("like_escape_seed_underscore", "snake_case variable name", "test_like_escape"),
        ("like_escape_seed_combined", "syntax: foo_bar%baz example", "test_like_escape"),
    ]
    for cid, content, source in seed:
        conn.execute(
            "INSERT INTO chunks (id, content, source, timestamp, created_at) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (cid, content, source),
        )
    conn.commit()
    conn.close()

    from memory import Memory
    m = Memory(db_path=db_path)
    yield m
    try:
        m._conn.execute("DELETE FROM chunks WHERE source='test_like_escape'")
        m._conn.commit()
    finally:
        m.close()


def _ids(hits):
    return sorted(h["chunk_id"] for h in hits)


def test_meta_recall_query_percent_only_matches_percent_content(mem_with_chunks):
    """`query='%'` (literal percent) must match only chunks with literal `%`,
    not all chunks. Pre-fix bug: matches all (LIKE wildcard)."""
    m = mem_with_chunks
    hits = m._meta_recall("%", top_k=10, filters=None, asof="2099-01-01T00:00:00")
    # Should match only chunks with literal % in content (2 of them).
    ids = _ids(hits)
    assert "like_escape_seed_percent" in ids
    assert "like_escape_seed_combined" in ids
    # Pre-fix bug: would also include plain + underscore (because LIKE %% = all).
    assert "like_escape_seed_plain" not in ids
    assert "like_escape_seed_underscore" not in ids


def test_meta_recall_query_underscore_only_matches_underscore_content(mem_with_chunks):
    """`query='_'` (literal underscore) must match only chunks with literal `_`,
    not all chunks. Pre-fix bug: matches all (LIKE single-char wildcard)."""
    m = mem_with_chunks
    hits = m._meta_recall("_", top_k=10, filters=None, asof="2099-01-01T00:00:00")
    ids = _ids(hits)
    assert "like_escape_seed_underscore" in ids
    assert "like_escape_seed_combined" in ids
    assert "like_escape_seed_plain" not in ids
    assert "like_escape_seed_percent" not in ids


def test_meta_recall_query_snake_case_matches_literal_underscore(mem_with_chunks):
    """`query='snake_case'` should match content with literal `_`. Pre-fix bug:
    `snake_case` as LIKE pattern = `snakeXcase` (X=any single char), which
    matches `snakeXcase` but not necessarily the literal `snake_case`.

    Post-fix: must match `snake_case variable name`."""
    m = mem_with_chunks
    hits = m._meta_recall("snake_case", top_k=10, filters=None, asof="2099-01-01T00:00:00")
    ids = _ids(hits)
    assert "like_escape_seed_underscore" in ids


def test_meta_recall_query_plain_text_unaffected(mem_with_chunks):
    """Regression: plain text recall still works (helper only affects special chars)."""
    m = mem_with_chunks
    hits = m._meta_recall("plain", top_k=10, filters=None, asof="2099-01-01T00:00:00")
    ids = _ids(hits)
    assert "like_escape_seed_plain" in ids
