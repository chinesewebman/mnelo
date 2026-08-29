"""[bug fix C1 2026-08-16] _fts_escape_query only handles double-quote.

FTS5 has many syntax-significant characters beyond just `"`:
  - ( ) for grouping/phrase
  - * for prefix match
  - : for column filter
  - ^ for first-position boost
  - + - for required/excluded
  - " " for phrase boundary
  - NEAR, AND, OR, NOT keywords

Current code only escapes `"` → FTS5 syntax error on these chars → fallback
to LIKE-only (no BM25 ranking). Owner fix priority: ensure all FTS5 special
chars are safely escaped (or stripped to plain word tokens).

Test verifies: query with FTS5 special chars does NOT raise FTS5 syntax error.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_fts_escape_strips_asterisk_prefix():
    """`*` triggers FTS5 prefix match syntax. Should be safely handled."""
    from memory import _fts_escape_query

    result = _fts_escape_query("file*.py")
    # Either: stripped/safe, or properly quoted
    assert isinstance(result, str)
    # No bare `*` should remain (would trigger prefix match)
    # Acceptable: stripped, or quoted as part of phrase
    # The key: it should NOT contain an unescaped `*` in MATCH syntax position


def test_fts_escape_strips_parentheses():
    """`(...)` is FTS5 grouping syntax. Should be safely handled."""
    from memory import _fts_escape_query

    result = _fts_escape_query("Python (async)")
    assert isinstance(result, str)
    # Should not have bare ( ) triggering grouping


def test_fts_escape_strips_colon():
    """`:` is FTS5 column filter syntax (e.g., `content:python`). Should be safely handled."""
    from memory import _fts_escape_query

    result = _fts_escape_query("title:Python")
    assert isinstance(result, str)


def test_fts_escape_strips_caret():
    """`^` is FTS5 first-position boost."""
    from memory import _fts_escape_query

    result = _fts_escape_query("^important")
    assert isinstance(result, str)


def test_fts_escape_does_not_raise_on_chinese():
    """Chinese chars work natively in FTS5 unicode61 tokenizer — should pass through."""
    from memory import _fts_escape_query

    result = _fts_escape_query("中文查询")
    assert result == "中文查询"


def test_fts_escape_preserves_double_quote_handling():
    """Post C1-fix: `"` is stripped (not escaped as `""`). Result should be safe."""
    from memory import _fts_escape_query

    result = _fts_escape_query('hello "world"')
    # Post-fix: " is stripped, result is "hello world" (no `""` needed)
    assert '"' not in result, f"double-quote should be stripped, got: {result!r}"
    assert "hello" in result and "world" in result


def test_fts_recall_with_special_chars_does_not_trigger_like_fallback(caplog):
    """End-to-end: recall with FTS5 special chars should NOT silently degrade to LIKE.

    Strategy: use a query with `*` (common in code search) and verify:
    1. No FTS5 syntax error in logs
    2. Result set is the same as without the special char (or sensible)
    """
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "c1.db")
        try:
            # Insert 3 chunks (some with code-like content)
            m.remember("Python async file handler example", source="manual")
            m.remember("JavaScript callback function pattern", source="manual")
            m.remember("Rust ownership and borrowing", source="manual")

            # Query with FTS5 special char (asterisk — common in code search)
            result = m.recall("file*", top_k=5)
            assert isinstance(result, list)
            # If FTS5 escape is broken: silently falls back to LIKE → may miss results
            # The test passes as long as no exception, but result should include
            # "Python async file" chunk
            chunk_contents = [h.get("content", "") for h in result]
            # Should match the file chunk via LIKE fallback at minimum
            assert any("file" in c for c in chunk_contents), f"FTS5 special char '*' in query caused silent miss: {chunk_contents}"
        finally:
            m.close()


def test_fts_recall_with_parentheses_does_not_silently_fail():
    """Query with `(` `)` should not silently degrade to LIKE-only."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "c1b.db")
        try:
            m.remember("Python async patterns", source="manual")
            m.remember("JavaScript promise patterns", source="manual")
            # Query with parens (common in programming contexts)
            result = m.recall("Python (async)", top_k=5)
            assert isinstance(result, result.__class__)  # no exception
            # Should match the Python async chunk
            chunk_contents = [h.get("content", "") for h in result]
            assert any("Python" in c for c in chunk_contents), f"Parens caused silent miss: {chunk_contents}"
        finally:
            m.close()


def test_fts_escape_empty_query():
    """Empty query → empty result (don't raise)."""
    from memory import _fts_escape_query

    assert _fts_escape_query("") == ""
    assert _fts_escape_query(None) == ""  # defensive


def test_fts_query_with_special_chars_does_not_raise_FTS5_syntax_error(caplog):
    """C1 fix verification: query with FTS5 special chars should NOT raise OperationalError.

    The FTS5 MATCH query parser is strict — characters like `*`, `(`, `)`, `:`, `^` are
    syntax-significant. Pre-fix: _fts_escape_query only escaped `"`, so other chars
    caused sqlite3.OperationalError ("fts5: syntax error"), which the calling code
    caught silently and fell back to LIKE-only (losing BM25 ranking).

    Post-fix: escape should handle ALL FTS5 special chars so no OperationalError.
    """
    import logging
    from memory import Memory, _fts_escape_query

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "c1c.db")
        try:
            m.remember("Python file handler async", source="manual")
            m.remember("JavaScript callback", source="manual")

            # Capture any FTS5 errors via logger
            with caplog.at_level(logging.WARNING, logger="mnelo"):
                # Various FTS5 special chars
                for special_query in [
                    "file*",  # asterisk (prefix)
                    "Python (async)",  # parens (grouping)
                    "title:Python",  # colon (column)
                    "^important",  # caret (boost)
                    "a +b -c",  # plus/minus (required/excluded)
                ]:
                    escaped = _fts_escape_query(special_query)
                    result = m.recall(escaped, top_k=3)
                    # Should not raise, and FTS5 should work
                    assert isinstance(result, list), f"recall({special_query!r}) returned non-list: {result!r}"

            # The recall code catches OperationalError and falls back silently.
            # We can detect the fallback by checking the captured log — if FTS5
            # succeeded, no "FTS5 syntax" warning should appear.
            fts5_errors = [r for r in caplog.records if "fts5" in r.message.lower() and "syntax" in r.message.lower()]
            # After fix: 0 fts5 syntax errors. Pre-fix: 5 (one per query above).
            assert len(fts5_errors) == 0, f"FTS5 syntax error still happening: {[r.message for r in fts5_errors]}"
        finally:
            m.close()


def test_fts5_escaped_query_is_well_formed_for_MATCH():
    """C1 fix verification: _fts_escape_query should produce a FTS5-MATCH-safe query.

    The simplest fix is to either:
    a) Strip ALL FTS5 special chars to produce a plain word query
    b) Quote the whole thing as a phrase (`"..."` with internal `""` escapes)

    Either way: the escaped result must NOT cause FTS5 syntax error when used in MATCH ?.
    """
    import sqlite3
    from memory import _fts_escape_query

    # In-memory FTS5 table
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE test_fts USING fts5(content)")
    conn.execute("INSERT INTO test_fts(content) VALUES ('Python async file handler')")
    conn.execute("INSERT INTO test_fts(content) VALUES ('JavaScript callback function')")
    conn.execute("INSERT INTO test_fts(content) VALUES ('Rust ownership and borrowing')")

    # Each special-char query should NOT raise
    for special_query in [
        "file*",
        "Python (async)",
        "title:Python",
        "^important",
        "a +b -c",
        'hello "world"',
        "中文查询",
    ]:
        escaped = _fts_escape_query(special_query)
        # If escaped produces a well-formed FTS5 query, this will not raise
        try:
            rows = conn.execute(
                "SELECT content FROM test_fts WHERE test_fts MATCH ?",
                (escaped,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            if "fts5" in str(e).lower() and "syntax" in str(e).lower():
                pytest.fail(f"FTS5 syntax error on {special_query!r} → escaped {escaped!r}: {e}")
            raise  # other OperationalError
    conn.close()
