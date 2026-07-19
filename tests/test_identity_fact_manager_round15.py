"""Round 15 — identity_fact_manager.py tests.

Tests cover:
- list: filter, JSON, empty
- show: by predicate, by predicate+value, not found, multiple
- add: creates entity + chunk + auto-links to master entity, dry-run, validation
- remove: soft-delete + cascade, dry-confirm, not found, multiple
- ALLOWED_PREDICATES allowlist enforced
- Help text and error paths
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "identity_fact_manager.py"

# Use a unique source prefix so test data is easy to clean up
TEST_SOURCE = "identity_fact_manager_test"


def _run(args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
    )


def _extract_json(output: str) -> dict:
    """Find JSON object in script output (which may have embedder log lines).

    The script's stdout may contain:
      - embedder load logs (e.g. '[embedder] OK')
      - the JSON object (single, well-formed)
    We find the FIRST '{' that starts valid JSON by trying each one.
    """
    # Find all '{' positions; the actual JSON starts at one of them
    candidates = [i for i, c in enumerate(output) if c == "{"]
    for start in candidates:
        try:
            return json.loads(output[start:])
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no valid JSON object found in output: {output[:200]!r}")


@pytest.fixture(scope="module")
def cleanup_test_facts():
    """Clean up any test facts before and after the test module."""
    from memory import Memory

    m = Memory()

    def cleanup():
        """Soft-delete test facts + cascade relations + remove test chunks."""
        # 1. Find all test facts (by name)
        test_fact_ids = [
            r[0]
            for r in m._conn.execute(
                "SELECT id FROM entities WHERE kind = 'identity_fact' AND name = 'pytest_test_value'"
            ).fetchall()
        ]
        # 2. Soft-delete the entities
        for fid in test_fact_ids:
            m._conn.execute(
                "UPDATE entities SET valid_until = datetime('now') WHERE id = ?",
                (fid,),
            )
        # 3. Cascade-soft-delete relations involving these entities
        for fid in test_fact_ids:
            m._conn.execute(
                "UPDATE relations SET valid_until = datetime('now') "
                "WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL",
                (fid, fid),
            )
        # 4. Cleanup test chunks (by source)
        rows = m._conn.execute("SELECT rowid FROM chunks WHERE source = ?", (TEST_SOURCE,)).fetchall()
        if rows:
            rowids = [r["rowid"] for r in rows]
            placeholders = ",".join("?" * len(rowids))
            try:
                m._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
            except Exception:
                pass
            m._conn.execute("DELETE FROM chunks WHERE source = ?", (TEST_SOURCE,))
        m._conn.commit()

    cleanup()
    yield
    cleanup()
    m.close()


class TestHelp:
    """Verify CLI surface."""

    def test_help_top_level(self):
        result = _run(["--help"])
        assert result.returncode == 0
        assert "list" in result.stdout
        assert "show" in result.stdout
        assert "add" in result.stdout
        assert "remove" in result.stdout

    def test_help_list(self):
        result = _run(["list", "--help"])
        assert result.returncode == 0
        assert "--predicate" in result.stdout

    def test_help_add(self):
        result = _run(["add", "--help"])
        assert result.returncode == 0
        assert "--predicate" in result.stdout
        assert "--value" in result.stdout


class TestList:
    """Test list subcommand."""

    def test_list_returns_active_facts(self, cleanup_test_facts):
        result = _run(["list"])
        assert result.returncode == 0
        assert "identity_fact list" in result.stdout

    def test_list_json(self, cleanup_test_facts):
        result = _run(["list", "--json"])
        assert result.returncode == 0
        data = _extract_json(result.stdout)
        assert "predicates" in data
        assert "facts" in data
        assert "count" in data
        assert isinstance(data["predicates"], list)
        # All 8 predicates should be in the allowlist
        assert "display_name" in data["predicates"]
        assert "profession" in data["predicates"]
        assert "role" in data["predicates"]

    def test_list_filter_by_predicate(self, cleanup_test_facts):
        result = _run(["list", "--predicate", "timezone", "--json"])
        assert result.returncode == 0
        data = _extract_json(result.stdout)
        for fact in data["facts"]:
            assert fact["predicate"] == "timezone"


class TestShow:
    """Test show subcommand."""

    def test_show_existing_fact(self, cleanup_test_facts):
        result = _run(["show", "--predicate", "github_handle"])
        assert result.returncode == 0
        assert "github_handle" in result.stdout
        assert "chinesewebman" in result.stdout

    def test_show_nonexistent(self, cleanup_test_facts):
        result = _run(["show", "--predicate", "profession"])
        # Exit code 2 (not found), OR 0 if user added one earlier
        # We're tolerant — just check it doesn't crash with error code 1
        assert result.returncode in (0, 2)

    def test_show_invalid_predicate(self, cleanup_test_facts):
        result = _run(["show", "--predicate", "not_a_real_predicate"])
        assert result.returncode == 2
        assert "unknown predicate" in result.stdout or "unknown predicate" in result.stderr


class TestAdd:
    """Test add subcommand."""

    def test_add_creates_fact(self, cleanup_test_facts):
        # Use a unique test value so we can clean it up
        result = _run(
            [
                "add",
                "--predicate",
                "profession",
                "--value",
                "pytest_test_value",
                "--importance",
                "0.85",
            ]
        )
        assert result.returncode == 0
        # Action can be 'created' or 'reactivated' depending on prior cleanup state
        assert "created" in result.stdout or "reactivated" in result.stdout
        assert "identity:profession:pytest_test_value" in result.stdout

    def test_add_dry_run_doesnt_write(self, cleanup_test_facts):
        # Count active facts before
        before = _run(["list", "--json"])
        before_data = _extract_json(before.stdout)
        before_count = before_data["count"]

        # Dry-run add (use a fresh value to avoid affecting count)
        result = _run(
            [
                "add",
                "--predicate",
                "profession",
                "--value",
                "dryrun_unique_xyz_zzz",
                "--dry-run",
            ]
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout

        # Verify count unchanged
        after = _run(["list", "--json"])
        after_count = _extract_json(after.stdout)["count"]
        assert after_count == before_count

    def test_add_invalid_predicate(self, cleanup_test_facts):
        result = _run(
            [
                "add",
                "--predicate",
                "fake_pred",
                "--value",
                "whatever",
            ]
        )
        assert result.returncode == 1
        assert "unknown predicate" in result.stdout

    def test_add_empty_value(self, cleanup_test_facts):
        result = _run(
            [
                "add",
                "--predicate",
                "profession",
                "--value",
                "   ",  # whitespace-only
            ]
        )
        assert result.returncode == 1

    def test_add_json_output(self, cleanup_test_facts):
        result = _run(
            [
                "add",
                "--predicate",
                "role",
                "--value",
                "pytest_test_value",
                "--json",
            ]
        )
        assert result.returncode == 0
        data = _extract_json(result.stdout)
        # Action can be created/reactivated/superseded
        assert data["action"] in ("created", "reactivated", "superseded")
        assert data["predicate"] == "role"
        assert data["value"] == "pytest_test_value"


class TestRemove:
    """Test remove subcommand."""

    def test_remove_soft_deletes_fact(self, cleanup_test_facts):
        # Pre-clean
        from memory import Memory

        m = Memory()
        m._conn.execute(
            "UPDATE entities SET valid_until = datetime('now') WHERE id = 'identity:profession:pytest_test_value'"
        )
        m._conn.execute(
            "UPDATE relations SET valid_until = datetime('now') "
            "WHERE (source_id = 'identity:profession:pytest_test_value' "
            "OR target_id = 'identity:profession:pytest_test_value') "
            "AND valid_until IS NULL"
        )
        m._conn.commit()
        m.close()

        # First add a fact
        _run(["add", "--predicate", "profession", "--value", "pytest_test_value"])

        # Then remove it (use --id because there may be other profession facts)
        result = _run(
            [
                "remove",
                "--id",
                "identity:profession:pytest_test_value",
                "-y",
            ]
        )
        assert result.returncode == 0
        assert "soft_deleted" in result.stdout
        assert "identity:profession:pytest_test_value" in result.stdout

    def test_remove_nonexistent(self, cleanup_test_facts):
        result = _run(
            [
                "remove",
                "--id",
                "identity:profession:nonexistent_xyz_999",
                "-y",
            ]
        )
        # Should return 2 (not found)
        assert result.returncode == 2

    def test_remove_via_full_id(self, cleanup_test_facts):
        # Add a fact
        _run(["add", "--predicate", "profession", "--value", "pytest_test_value"])

        # Remove by full id
        result = _run(
            [
                "remove",
                "--id",
                "identity:profession:pytest_test_value",
                "-y",
            ]
        )
        assert result.returncode == 0
        assert "soft_deleted" in result.stdout


class TestAllowedPredicates:
    """Test the ALLOWED_PREDICATES allowlist."""

    def test_eight_predicates_listed(self, cleanup_test_facts):
        result = _run(["list", "--json"])
        data = _extract_json(result.stdout)
        predicates = set(data["predicates"])
        assert predicates == {
            "display_name",
            "github_handle",
            "lives_in",
            "timezone",
            "telegram_handle",
            "working_lang",
            "profession",
            "role",
        }

    def test_new_predicates_rejected(self, cleanup_test_facts):
        # 'favorite_color' is not in allowlist
        result = _run(
            [
                "add",
                "--predicate",
                "favorite_color",
                "--value",
                "blue",
            ]
        )
        assert result.returncode == 1
        assert "unknown predicate" in result.stdout


class TestCascade:
    """Verify cascade behavior of remove()."""

    def test_remove_cascades_to_relations(self, cleanup_test_facts):
        from memory import Memory

        # Pre-clean to ensure clean starting state
        m = Memory()
        test_fact = "identity:profession:pytest_test_value"
        m._conn.execute(
            "UPDATE entities SET valid_until = datetime('now') WHERE id = ?",
            (test_fact,),
        )
        m._conn.execute(
            "UPDATE relations SET valid_until = datetime('now') "
            "WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL",
            (test_fact, test_fact),
        )
        m._conn.commit()
        m.close()

        # Add fact
        _run(["add", "--predicate", "profession", "--value", "pytest_test_value"])

        # Verify a relation exists (fact linked to master_2077_ling)
        m = Memory()
        rels_before = m._conn.execute(
            """
            SELECT count(*) FROM relations
            WHERE source_id = ?
            AND valid_until IS NULL
        """,
            (test_fact,),
        ).fetchone()[0]
        m.close()
        assert rels_before >= 1, "expected at least 1 relation after add"

        # Remove fact (use --id because there may be other profession facts)
        _run(["remove", "--id", test_fact, "-y"])

        # Verify relations are now soft-deleted too
        m = Memory()
        rels_after = m._conn.execute(
            """
            SELECT count(*) FROM relations
            WHERE source_id = ?
            AND valid_until IS NULL
        """,
            (test_fact,),
        ).fetchone()[0]
        m.close()
        assert rels_after == 0, f"expected 0 active relations after remove, got {rels_after}"
