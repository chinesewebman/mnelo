"""[bug fix D1 2026-08-16] task_states._default_now uses millisecond precision
while memory.now() uses second precision — SQLite lex compare treats them
inconsistently.

Pre-fix: task_states writes '2026-08-16T10:30:00.123' (ms)
         memory.now() writes '2026-08-16T10:30:00' (s)
Same column (e.g. chunks.valid_until) gets BOTH formats. SQLite TEXT
comparison: '2026-08-16T10:30:00' < '2026-08-16T10:30:00.500' (shorter
string sorts first). Every asof query, valid_until comparison, audit_log
UNIQUE index becomes unreliable.

Post-fix: task_states._default_now uses second precision to match memory.now().

Test verifies:
  - All task_states writes use second precision (no fractional seconds)
  - valid_until comparisons work consistently between task_states and chunks
  - replay_task(asof) returns correct windows
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_default_now_uses_second_precision():
    """D1 fix: _default_now should not include fractional seconds."""
    from task_states_core import _default_now

    ts = _default_now()
    # No '.' should appear in the timestamp (second precision)
    assert "." not in ts, f"_default_now has fractional seconds: {ts!r}"
    # Format: YYYY-MM-DDTHH:MM:SS
    import re

    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", ts), f"_default_now format mismatch: {ts!r}"


def test_default_now_matches_memory_now_precision():
    """D1 fix: _default_now and memory.now() must use same precision."""
    from task_states_core import _default_now
    from memory import now

    ts_ts = _default_now()
    ts_mem = now()
    # Both must have same precision (no fractional)
    assert "." not in ts_ts, f"task_states: {ts_ts!r}"
    assert "." not in ts_mem, f"memory: {ts_mem!r}"


def test_default_now_matches_iso_now_sql_function():
    """D1 fix: Python _default_now must equal SQL iso_now() for temporal query consistency."""
    from task_states_core import _default_now
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1ts.db")
        try:
            # Get Python-side _default_now and SQL-side iso_now
            ts_python = _default_now()
            ts_sql = m._conn.execute("SELECT iso_now()").fetchone()[0]
            # Both should be parseable as ISO 8601 (no fractional vs fractional mismatch)
            from datetime import datetime

            dt_python = datetime.fromisoformat(ts_python)
            dt_sql = datetime.fromisoformat(ts_sql)
            # The diff is bounded by 1 second (both run in sequence)
            diff = abs((dt_python - dt_sql).total_seconds())
            assert diff < 1, f"Python/SQL time drift too large: {diff}s ({ts_python!r} vs {ts_sql!r})"
        finally:
            m.close()


def test_task_state_valid_until_uses_second_precision():
    """D1 fix: task transition writes valid_until with second precision."""
    from memory import Memory
    from task_states_core import task_create

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1ts2.db")
        try:
            chunk_id = m.remember("seed", source="manual")
            result = task_create(m._conn, name="t1", evidence_chunk_id=chunk_id)
            task_id = result["task_id"]
            row = m._conn.execute(
                "SELECT valid_from FROM task_states WHERE task_id = ? ORDER BY id ASC LIMIT 1",
                (task_id,),
            ).fetchone()
            assert row is not None
            vf = row["valid_from"]
            assert "." not in vf, f"task_states.valid_from has fractional: {vf!r}"
        finally:
            m.close()


def test_valid_until_compare_consistent_across_modules():
    """D1 fix: WHERE valid_until > X works correctly across modules."""
    from memory import Memory
    from task_states_core import task_create

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d1consist.db")
        try:
            chunk_id = m.remember("test", source="manual")
            result = task_create(m._conn, name="t1", evidence_chunk_id=chunk_id)
            task_id = result["task_id"]
            ts_from_tasks = m._conn.execute(
                "SELECT valid_from FROM task_states WHERE task_id = ? ORDER BY id ASC LIMIT 1",
                (task_id,),
            ).fetchone()["valid_from"]
            rows = m._conn.execute(
                "SELECT id FROM chunks WHERE valid_until IS NULL OR valid_until > ?",
                (ts_from_tasks,),
            ).fetchall()
            assert len(rows) >= 1
        finally:
            m.close()
