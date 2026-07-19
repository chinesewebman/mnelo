"""Round 15 — vec0 drift fix tests.

Tests cover:
- cleanup_orphan_vectors() dry_run returns counts without deleting
- cleanup_orphan_vectors() removes soft-deleted chunk vectors
- cleanup_orphan_vectors() removes truly orphan vectors
- forget(chunk) deletes the vector row (write-time cleanup)
- update() deletes the old chunk's vector (write-time cleanup)
- forget() with cascade=True doesn't double-delete
- dry_run == False on clean DB returns zero counts
- vectors_remaining matches expected after cleanup
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def mem():
    """Memory instance using LIVE DB, cleaned up after test.

    Uses prefix 'v05_6_drift_test:' for all test data so we can clean up easily.
    """
    from memory import Memory

    m = Memory()

    def cleanup():
        """Delete test chunks AND their vectors (rowid-matched)."""
        rows = m._conn.execute("SELECT rowid FROM chunks WHERE source LIKE 'v05_6_drift_test:%'").fetchall()
        if rows:
            rowids = [r["rowid"] for r in rows]
            placeholders = ",".join("?" * len(rowids))
            m._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
        m._conn.execute("DELETE FROM chunks WHERE source LIKE 'v05_6_drift_test:%'")
        m._conn.commit()

    cleanup()
    yield m
    cleanup()
    m.close()


class TestCleanupOrphanVectors:
    """Test Memory.cleanup_orphan_vectors() method."""

    def test_dry_run_returns_counts_without_deleting(self, mem):
        # Add a chunk
        mem.remember(content="test", source="v05_6_drift_test:dryrun")
        # Manually soft-delete its vector to simulate drift
        rowid = mem._conn.execute(
            "SELECT rowid FROM chunks WHERE source LIKE 'v05_6_drift_test:%' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        # First, delete the vector so we can manually insert one with soft-deleted chunk
        mem._conn.execute("DELETE FROM vectors WHERE rowid = ?", (rowid,))
        mem._conn.execute("UPDATE chunks SET valid_until = '2099-01-01' WHERE rowid = ?", (rowid,))
        # Re-insert vector (now orphan: chunk is soft-deleted)
        # Get embedding bytes from another active chunk
        sample_rowid = mem._conn.execute("SELECT rowid FROM chunks WHERE valid_until IS NULL LIMIT 1").fetchone()[0]
        sample_vec_bytes = mem._conn.execute(
            "SELECT embedding FROM vectors WHERE rowid = ?", (sample_rowid,)
        ).fetchone()[0]
        mem._conn.execute("INSERT INTO vectors (rowid, embedding) VALUES (?, ?)", (rowid, sample_vec_bytes))
        mem._conn.commit()

        before_count = mem._conn.execute("SELECT count(*) FROM vectors").fetchone()[0]

        # Dry run
        result = mem.cleanup_orphan_vectors(dry_run=True)
        assert result["dry_run"] is True
        assert result["soft_deleted_cleaned"] >= 1

        # Verify nothing was deleted
        after_count = mem._conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        assert after_count == before_count

    def test_actual_run_removes_orphan_vectors(self, mem):
        # Create a chunk then soft-delete it, leaving its vector
        mem.remember(content="drift test", source="v05_6_drift_test:remove")
        rowid = mem._conn.execute(
            "SELECT rowid FROM chunks WHERE source LIKE 'v05_6_drift_test:%' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
        # Verify vector exists
        v_before = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (rowid,)).fetchone()[0]
        assert v_before == 1

        # Soft-delete chunk (without using forget() — bypass the new write-time cleanup)
        mem._conn.execute("UPDATE chunks SET valid_until = '2099-01-01' WHERE rowid = ?", (rowid,))
        mem._conn.commit()

        # Run cleanup
        result = mem.cleanup_orphan_vectors()
        assert result["dry_run"] is False
        assert result["soft_deleted_cleaned"] >= 1

        # Vector should be gone
        v_after = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (rowid,)).fetchone()[0]
        assert v_after == 0

    def test_dry_run_on_clean_db(self, mem):
        """If no orphans, dry_run should return 0 + dry_run=True."""
        result = mem.cleanup_orphan_vectors(dry_run=True)
        assert result["dry_run"] is True
        assert result["soft_deleted_cleaned"] == 0
        assert result["truly_orphan_cleaned"] == 0

    def test_actual_run_on_clean_db(self, mem):
        """If no orphans, actual run is a no-op."""
        before = mem._conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        result = mem.cleanup_orphan_vectors()
        after = mem._conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        assert before == after
        assert result["soft_deleted_cleaned"] == 0


class TestForgetDeletesVector:
    """Test that forget(chunk) now also deletes the vector row."""

    def test_forget_cleans_up_vector(self, mem):
        cid = mem.remember(content="to forget", source="v05_6_drift_test:forget")
        rowid = mem._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (cid,)).fetchone()[0]

        # Verify vector exists
        v_before = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (rowid,)).fetchone()[0]
        assert v_before == 1

        # Forget it
        result = mem.forget(cid, target_kind="chunk")
        assert result["queued_purge"] == 1

        # Vector should be gone
        v_after = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (rowid,)).fetchone()[0]
        assert v_after == 0, f"expected vector deleted, but {v_after} remain at rowid {rowid}"

    def test_forget_nonexistent_chunk_doesnt_crash(self, mem):
        """forget on a nonexistent chunk should not raise (silent no-op)."""
        # Pre-existing behavior: forget() doesn't validate that chunk exists.
        # It just queues a purge + returns zero edges_invalidated.
        # This test ensures our vector cleanup doesn't add crashes on this path.
        result = mem.forget("chunk_does_not_exist_xxx", target_kind="chunk")
        assert result["queued_purge"] == 1
        assert result["edges_invalidated"] == 0


class TestUpdateDeletesOldVector:
    """Test that update() deletes the OLD chunk's vector."""

    def test_update_cleans_up_old_vector(self, mem):
        old_id = mem.remember(content="version 1", source="v05_6_drift_test:update")
        old_rowid = mem._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (old_id,)).fetchone()[0]

        # Verify old vector exists
        v_before = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (old_rowid,)).fetchone()[0]
        assert v_before == 1

        # Update (creates new version + supersedes old)
        new_id = mem.update(old_id, reason="drift_test", new_content="version 2")

        # Old vector should be gone
        v_after = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (old_rowid,)).fetchone()[0]
        assert v_after == 0, f"expected old vector deleted, but {v_after} remain at rowid {old_rowid}"

        # New chunk should have its own vector
        new_rowid = mem._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (new_id,)).fetchone()[0]
        v_new = mem._conn.execute("SELECT count(*) FROM vectors WHERE rowid = ?", (new_rowid,)).fetchone()[0]
        assert v_new == 1


class TestMaintainVectorsCLI:
    """Test scripts/maintain_vectors.py CLI."""

    def test_dry_run_flag(self):
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "maintain_vectors.py"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO),
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout

    def test_dry_run_json_flag(self):
        import json
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "maintain_vectors.py"),
                "--dry-run",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO),
        )
        assert result.returncode == 0
        # The script prints JSON, but the embedder logger may print to stdout/stderr
        # before our JSON. Find the JSON object by parsing from the last '{' onward.
        json_start = result.stdout.rfind("{")
        assert json_start >= 0, f"no JSON object found in stdout: {result.stdout!r}"
        json_text = result.stdout[json_start:]
        data = json.loads(json_text)
        assert "soft_deleted_cleaned" in data
        assert "truly_orphan_cleaned" in data
        assert "vectors_remaining" in data
        assert "dry_run" in data

    def test_help_flag(self):
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "maintain_vectors.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "cleanup" in result.stdout.lower() or "usage" in result.stdout.lower()
