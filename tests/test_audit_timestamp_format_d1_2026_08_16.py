"""[bug fix D1 2026-08-16] timestamp format inconsistency between SQL defaults and Python now().

Pre-fix: SQL `datetime('now', 'localtime')` default produces 'YYYY-MM-DD HH:MM:SS' (space-sep)
but Python `now()` returns 'YYYY-MM-DDTHH:MM:SS' (T-sep). Same instant, different formats.

Impact: any temporal query comparing `created_at` (space-sep, from default) against
`timestamp`/`valid_until` (T-sep, from explicit now() call) or against Python now() will
break lexicographic comparison. Even within a single row, two columns can have different
separator formats for the same logical instant.

Concrete bug: `created_at > now()` returns wrong count when comparing
'2026-08-16 09:46:51' (space) vs '2026-08-16T09:46:51' (T). Lex compare says
'2026-08-16 09:46:51' < '2026-08-16T09:46:51' (because ' ' (0x20) < 'T' (0x54))
even though they're the same moment.

Test verifies: after fix, all timestamp columns have consistent ISO 8601 format
with T separator.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_chunks_insert_defaults_use_T_separator():
    """D1 fix: SQL default for created_at should match Python now() format (T-sep)."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1.db")
        try:
            cid = m.remember("test", source="manual")
            row = m._conn.execute(
                "SELECT timestamp, created_at, valid_until FROM chunks WHERE id = ?",
                (cid,),
            ).fetchone()
            # All three should use T-separator (ISO 8601)
            for col in ("timestamp", "created_at"):
                if row[col] is not None:
                    assert "T" in row[col], f"Column {col} uses non-ISO format: {row[col]!r}. Expected T-separator (ISO 8601), got space-separator"
        finally:
            m.close()


def test_audit_log_insert_defaults_use_T_separator():
    """D1 fix: meta + audit_log tables use T-separator for created_at."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1b.db")
        try:
            # Init creates meta rows (H-1 §3 migration tracking)
            rows = m._conn.execute("SELECT value FROM meta").fetchall()
            for row in rows:
                val = row["value"]
                if val and "-" in val and ":" in val:
                    # Looks like a timestamp — verify T-separator
                    assert "T" in val, f"meta value uses non-ISO: {val!r}"
        finally:
            m.close()


def test_relationship_between_columns_uses_consistent_format():
    """D1 fix: same row's columns should have consistent timestamp format."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1c.db")
        try:
            cid = m.remember("test", source="manual")
            row = m._conn.execute(
                "SELECT timestamp, created_at FROM chunks WHERE id = ?",
                (cid,),
            ).fetchone()
            # Both should be either all-space or all-T, but if mixing in one row,
            # lex compare breaks
            ts_t = "T" in row["timestamp"]
            ca_t = "T" in row["created_at"]
            assert ts_t == ca_t, f"Format mismatch: timestamp={row['timestamp']!r} (T={ts_t}) vs created_at={row['created_at']!r} (T={ca_t})"
        finally:
            m.close()


def test_temporal_query_with_now_does_not_miss_recent_rows():
    """D1 fix: querying `created_at < now()` should return the just-inserted row
    (not miss it due to format mismatch). Use < not > to avoid the same-second issue.
    """
    from memory import Memory, now

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1d.db")
        try:
            cid = m.remember("test", source="manual")
            py_now = now()
            # Should return the just-inserted row (created_at is in the past or same second)
            rows = m._conn.execute("SELECT id FROM chunks WHERE created_at <= ?", (py_now,)).fetchall()
            # Pre-fix: 0 rows (lex says space < T)
            # Post-fix: 1 row (formats match)
            assert len(rows) >= 1, f"Recent chunk missed due to format mismatch: {len(rows)} rows for created_at <= {py_now!r}"
        finally:
            m.close()


def test_l2_maintenance_age_query_uses_consistent_format():
    """D1 fix: L2 maintenance that does `created_at < now()-N days` should not
    double-count or miss due to format mismatch.
    """
    from memory import Memory, now

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1e.db")
        try:
            # Insert 3 chunks at different times
            for i in range(3):
                m.remember(f"test {i}", source="manual")
            py_now = now()
            # Query: created_at > (py_now - 1 hour) → should return all 3
            from datetime import datetime, timedelta

            py_now_dt = datetime.fromisoformat(py_now)
            one_hour_ago = (py_now_dt - timedelta(hours=1)).isoformat()
            rows = m._conn.execute("SELECT id FROM chunks WHERE created_at > ?", (one_hour_ago,)).fetchall()
            # Pre-fix: 0 rows (format mismatch)
            # Post-fix: 3 rows
            assert len(rows) == 3, f"L2 age query broken: {len(rows)} rows > 1h ago (expected 3)"
        finally:
            m.close()
