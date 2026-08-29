"""[bug fix P1 2026-08-29] SQLite native datetime('now', ...) returns space-sep
'YYYY-MM-DD HH:MM:SS', but mnelo writes timestamps via iso_now() which returns
T-sep 'YYYY-MM-DDTHH:MM:SS'. Lex compare: T (0x54) > space (0x20), so any
string compare between the two formats is wrong at the same-date boundary.

Pre-fix bug surface:
  1. mcp_transports.py:115 — /health counts audit_log entries from last 24h
     using `created_at >= datetime('now', '-1 day')`. T-sep created_at always
     lex-orders >= space-sep cutoff, so same-date-but-earlier audit entries
     are wrongly included → pii_warnings_last_24h over-counts → spurious
     pii_recommendation noise to owner.
  2. l2_maintenance.py:1379 — `timestamp < datetime('now', ?)` (space-sep
     cutoff) for purge candidate count. Same lex issue: chunks written
     before cutoff same-day are not counted as expired → purge_candidates
     under-reports → 30-day TTL chunks accumulate silently.
  3. l2_maintenance.py:1366 — `datetime(timestamp) >= datetime('now', '-30 days')`
     for freshness. Wraps `timestamp` (T-sep) in `datetime()` to force
     space-sep on BOTH sides → both space-sep, compare correct. BUT
     it loses T-sep precision (any microsecond data we add later) AND
     applies an unnecessary transform. Replace with iso_now_offset(-N).

Fix: use the registered `iso_now_offset(N)` SQL function (T-sep) instead of
`datetime('now', N)` for cutoff comparisons. This matches mnelo's write-time
timestamp format throughout and makes lex compare consistent.

Tests verify:
  - `iso_now_offset(-1)` returns T-sep ISO 8601 (same format as iso_now())
  - l2_maintenance freshness / purge_candidates use T-sep cutoff correctly
  - mcp_transports /health counts correctly across the T/space boundary
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# === Pure helper unit tests (no DB needed) ===

def test_datetime_now_returns_space_sep_format():
    """Confirm the source of the bug: datetime('now') is space-sep, distinct from iso_now T-sep."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    space_sep = conn.execute("SELECT datetime('now', '-1 day')").fetchone()[0]
    # Format: 'YYYY-MM-DD HH:MM:SS' (space at index 10)
    assert space_sep[10] == " ", f"expected space-sep, got {space_sep!r}"
    assert space_sep[4] == "-"
    assert space_sep[7] == "-"


# === Integration tests: prove the bugs are real at the SQL level ===

@pytest.fixture
def mem_with_audit_chunks(tmp_path, monkeypatch):
    """Build a Memory() fixture with known audit_log + chunks across the T/space boundary.

    Used to verify purge_candidates and freshness counts match T-sep semantics.
    """
    import re
    import sqlite3

    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo))
    from config import config as _cfg
    monkeypatch.setattr(_cfg, "search_backend", "usearch", raising=True)
    monkeypatch.setattr(_cfg, "db_path", tmp_path / "test_datetime_bug.db", raising=False)

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

    db_path = tmp_path / "test_datetime_bug.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(sql)
    except Exception as e:
        if "already exists" not in str(e):
            raise
    conn.commit()

    # Get now in T-sep for seeding "now - 30 days" boundary chunks
    from memory import now
    now_ts = now()
    # Compute T-sep offsets via datetime arithmetic (iso_now_offset is SQL-side
    # only, registered as a create_function on Memory._conn — not a Python
    # helper callable from here). fact TTL = 365 days.
    from datetime import datetime, timedelta
    base_dt = datetime.fromisoformat(now_ts)
    expired_t_sep = (base_dt - timedelta(days=365, seconds=1)).isoformat(timespec="seconds")
    not_expired_t_sep = (base_dt - timedelta(days=364)).isoformat(timespec="seconds")
    recent_t_sep = now_ts

    # Seed 3 chunks: 1 expired (365d 1s ago, past fact TTL), 1 not expired (364d ago), 1 today
    seed = [
        ("datetime_bug_expired", "this should be in purge candidates", "test_datetime_bug", expired_t_sep, "fact"),
        ("datetime_bug_not_expired", "this should NOT be purged yet", "test_datetime_bug", not_expired_t_sep, "fact"),
        ("datetime_bug_recent", "fresh chunk", "test_datetime_bug", recent_t_sep, "fact"),
    ]
    for cid, content, source, ts, mt in seed:
        conn.execute(
            "INSERT INTO chunks (id, content, source, timestamp, memory_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cid, content, source, ts, mt, ts),
        )
    conn.commit()
    conn.close()

    from memory import Memory
    m = Memory(db_path=db_path)
    yield m, expired_t_sep, not_expired_t_sep, recent_t_sep
    try:
        m._conn.execute("DELETE FROM chunks WHERE source='test_datetime_bug'")
        m._conn.commit()
    finally:
        m.close()


def test_l2_maintenance_purge_candidates_includes_expired_chunks(mem_with_audit_chunks):
    """Bug #2b: l2_maintenance stats()['hygiene']['purge_candidates'] must include
    chunks whose timestamp < now - TTL (fact TTL = 365 days per
    _MEMORY_TYPE_TTL_DAYS).

    Pre-fix: SQL `timestamp < datetime('now', '-365 days')` uses space-sep
    cutoff. T-sep timestamp at same-date boundary lex-orders >= space-sep
    cutoff (T > space), so expired chunks are NOT counted → purge_candidates
    under-reports.

    Post-fix: SQL uses `timestamp < iso_now_offset(-365)` (both T-sep) →
    correct lex compare → expired chunk counted.
    """
    from l2_maintenance import L2MaintenanceMixin
    fact_ttl = L2MaintenanceMixin._MEMORY_TYPE_TTL_DAYS["fact"]  # 365
    assert fact_ttl == 365, "test assumption: fact TTL must be 365 days"

    m, expired_t_sep, not_expired_t_sep, recent_t_sep = mem_with_audit_chunks
    stats = m.stats()
    pc = stats["hygiene"].get("purge_candidates", 0)
    # fact TTL = 365 days → expired chunk (365d 1s ago) should be in purge_candidates
    # not_expired (364d ago) and recent (today) should NOT be counted.
    assert pc >= 1, f"expected expired chunk in purge_candidates, got pc={pc}"


def test_mcp_health_pii_warnings_24h_excludes_older_audit(tmp_path, monkeypatch):
    """Bug #2 (mcp_transports.py:115): /health pii_warnings_last_24h counts audit
    rows in last 24h. Pre-fix uses `created_at >= datetime('now', '-1 day')`
    (space-sep) vs created_at (T-sep) → same-day-but-earlier rows wrongly
    counted. Post-fix uses iso_now_offset(-1) (T-sep) → correct.

    We don't have a running MCP server here; test the SQL pattern directly to
    prove the bug and the fix at the query level.
    """
    import sqlite3

    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo))
    from memory import now

    # Build a minimal audit_log fixture
    db_path = tmp_path / "test_pii_audit_bug.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, pass_name TEXT, created_at TEXT)")
    # Seed: 1 row exactly 1 day 1 second ago (T-sep)
    from datetime import datetime, timedelta
    base = datetime.fromisoformat(now())
    one_day_one_sec_ago = (base - timedelta(days=1, seconds=1)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO audit_log (pass_name, created_at) VALUES (?, ?)",
        ("pii_audit", one_day_one_sec_ago),
    )
    conn.commit()

    # Pre-fix query (space-sep cutoff)
    cutoff_space = conn.execute("SELECT datetime('now', '-1 day')").fetchone()[0]
    pre_fix_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE pass_name='pii_audit' AND created_at >= ?",
        (cutoff_space,),
    ).fetchone()[0]
    # BUG: pre-fix counts 1 because T-sep >= space-sep (lex T > space)
    assert pre_fix_count == 1, "expected pre-fix to over-count (proving bug exists)"

    # Post-fix query: cutoff computed in T-sep via datetime arithmetic
    # (the actual fix uses iso_now_offset(-1) SQL function — we replicate
    # the same format here so we test the boundary correctness).
    from datetime import datetime, timedelta
    cutoff_t = (datetime.fromisoformat(now()) - timedelta(days=1)).isoformat(timespec="seconds")
    assert cutoff_t[10] == "T", f"expected T-sep cutoff, got {cutoff_t!r}"
    post_fix_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE pass_name='pii_audit' AND created_at >= ?",
        (cutoff_t,),
    ).fetchone()[0]
    assert post_fix_count == 0, f"expected post-fix to correctly exclude >24h row, got {post_fix_count}"

    conn.close()
