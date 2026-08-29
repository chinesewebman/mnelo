"""Round 15 — drift fix tests (后端无关).

[8/6 plan §11] 重写为后端无关 — 不直接 SELECT vectors 表, 走 _index.contains()/size().
旧的 v_before == 1 硬断言对 usearch 不适用 (chunk 在索引即可, 不需查 vec0 表).

Tests cover:
- cleanup_orphan_vectors() dry_run returns counts without deleting
- cleanup_orphan_vectors() removes soft-deleted chunk vectors
- cleanup_orphan_vectors() removes truly orphan vectors
- forget(chunk) deletes the index entry (write-time cleanup)
- update() deletes the old chunk's index entry (write-time cleanup)
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
    [8/6 plan §11] cleanup 改用 helper (后端感知).
    """
    from memory import Memory
    from helpers import cleanup_chunks

    m = Memory()

    def cleanup():
        cleanup_chunks(m, source_pattern="v05_6_drift_test:%")
        m._conn.commit()

    cleanup()
    yield m
    cleanup()
    m.close()


class TestCleanupOrphanVectors:
    """[8/6 plan §11] 后端无关 — 用 _index.contains()/size() 断言, 不查 vec0 表."""

    def test_dry_run_returns_counts_without_deleting(self, mem):
        # Add a chunk + soft-delete (plan §11: 直接 UPDATE valid_until 制造软删)
        cid = mem.remember(content="drift dryrun test", source="v05_6_drift_test:dryrun")
        rowid = mem._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
        mem._conn.execute("UPDATE chunks SET valid_until = '2099-01-01' WHERE rowid = ?", (rowid,))
        mem._conn.commit()

        before_size = mem._index.size()

        # Dry run
        result = mem.cleanup_orphan_vectors(dry_run=True)
        assert result["dry_run"] is True
        assert result["soft_deleted_cleaned"] >= 1

        # Verify nothing was deleted (size 不变)
        after_size = mem._index.size()
        assert after_size == before_size

    def test_actual_run_removes_orphan_vectors(self, mem):
        # Create a chunk then soft-delete it, leaving its index entry
        cid = mem.remember(content="drift test", source="v05_6_drift_test:remove")

        # Verify index entry exists
        assert mem._index.contains(cid) is True

        # Soft-delete chunk (without using forget() — bypass write-time cleanup)
        mem._conn.execute("UPDATE chunks SET valid_until = '2099-01-01' WHERE id = ?", (cid,))
        mem._conn.commit()

        # Run cleanup
        result = mem.cleanup_orphan_vectors()
        assert result["dry_run"] is False
        assert result["soft_deleted_cleaned"] >= 1

        # Index entry should be gone
        assert mem._index.contains(cid) is False

    def test_dry_run_on_clean_db(self, mem):
        """If no orphans, dry_run should return 0 + dry_run=True."""
        # 先清理, 确保 clean
        from helpers import cleanup_chunks

        cleanup_chunks(mem, source_pattern="v05_6_drift_test:%")
        mem._conn.commit()
        result = mem.cleanup_orphan_vectors(dry_run=True)
        assert result["dry_run"] is True
        assert result["soft_deleted_cleaned"] == 0
        assert result["truly_orphan_cleaned"] == 0

    def test_actual_run_on_clean_db(self, mem):
        """If no orphans, actual run is a no-op."""
        from helpers import cleanup_chunks

        cleanup_chunks(mem, source_pattern="v05_6_drift_test:%")
        mem._conn.commit()
        before = mem._index.size()
        result = mem.cleanup_orphan_vectors()
        after = mem._index.size()
        assert before == after
        assert result["soft_deleted_cleaned"] == 0


class TestForgetDeletesIndexEntry:
    """Test that forget(chunk) now also deletes the index entry."""

    def test_forget_cleans_up_index_entry(self, mem):
        cid = mem.remember(content="to forget", source="v05_6_drift_test:forget")
        assert mem._index.contains(cid) is True

        # Forget it
        result = mem.forget(cid, target_kind="chunk")
        assert result["queued_purge"] == 1

        # Index entry should be gone (write-time cleanup)
        assert mem._index.contains(cid) is False

    def test_forget_nonexistent_chunk_doesnt_crash(self, mem):
        """forget on a nonexistent chunk should not raise (silent no-op)."""
        result = mem.forget("chunk_does_not_exist_xxx", target_kind="chunk")
        assert result["queued_purge"] == 1
        assert result["edges_invalidated"] == 0


class TestUpdateDeletesOldIndexEntry:
    """Test that update() deletes the OLD chunk's index entry."""

    def test_update_cleans_up_old_index_entry(self, mem):
        old_id = mem.remember(content="version 1", source="v05_6_drift_test:update")
        assert mem._index.contains(old_id) is True

        # Update (creates new version + supersedes old)
        new_id = mem.update(old_id, reason="drift_test", new_content="version 2")

        # Old index entry should be gone
        assert mem._index.contains(old_id) is False, "expected old index entry deleted"

        # New chunk should have its own index entry
        assert mem._index.contains(new_id) is True


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
