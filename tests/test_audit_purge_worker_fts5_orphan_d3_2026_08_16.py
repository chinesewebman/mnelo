"""[bug fix D3 2026-08-16] run_purge_worker leaves orphaned FTS5 rows.

Pre-fix: physical DELETE from chunks never cleaned up chunks_fts. FTS5 rows
accumulate for every forgotten chunk → unbounded FTS5 index bloat.

_post-fix: D3 purges FTS5 rowids BEFORE the physical DELETE from chunks.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_purge_worker_cleans_fts5_rows_for_deleted_chunks():
    """D3 fix: FTS5 rows removed when chunks physically deleted."""
    from datetime import datetime, timedelta
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d3.db")
        try:
            # Insert a chunk
            cid = m.remember("hello world unique token alpha", source="manual")
            # Verify FTS5 has the row
            fts_count_before = m._conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'hello'").fetchone()[0]
            assert fts_count_before == 1

            # Soft-delete via forget
            m.forget(cid, target_kind="chunk")

            # Force purged_queue entry to be due (set purged_at to >30 days past)
            # (iso_now_offset(30) = today + 30d, so we need < today-30d = >30d past)
            past = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
            m._conn.execute(
                "UPDATE purged_queue SET purged_at = ? WHERE target_id = ?",
                (past, cid),
            )
            m._conn.commit()

            # Run purge worker (default: cleans orphans, does real delete)
            stats = m.run_purge_worker(dry_run=False, clean_orphan_target_ids=True)
            assert stats["chunks_physically_deleted"] >= 1

            # Verify FTS5 is clean (no orphan rows for our query)
            fts_count_after = m._conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'hello'").fetchone()[0]
            assert fts_count_after == 0, f"FTS5 still has {fts_count_after} orphan rows"
        finally:
            m.close()


def test_purge_worker_no_op_when_nothing_due():
    """D3 fix: nothing to purge → no errors, no FTS5 changes."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d3b.db")
        try:
            m.remember("hello world", source="manual")
            stats = m.run_purge_worker(dry_run=True)
            # Dry run should report 0 deletes
            assert stats["chunks_physically_deleted"] == 0
        finally:
            m.close()
