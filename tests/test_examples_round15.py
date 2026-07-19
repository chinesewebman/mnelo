"""Round 15 — examples/ tests.

Verify each example script runs to completion without errors and produces
expected output markers. The examples themselves are user-facing; these tests
just confirm they don't break.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run_example(name: str) -> subprocess.CompletedProcess:
    """Run an example script."""
    return subprocess.run(
        [sys.executable, str(REPO / "examples" / name)],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestExamples:
    """Verify each example runs cleanly + emits expected markers."""

    def test_01_runs_to_completion(self):
        result = _run_example("01_basic_remember_recall.py")
        assert result.returncode == 0, f"exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        assert "✓ done." in result.stdout
        assert "[remember] chunk_id:" in result.stdout
        # Should mention YOUR chunk at rank 1
        assert "← YOURS" in result.stdout

    def test_02_runs_to_completion(self):
        result = _run_example("02_entities_and_relations.py")
        assert result.returncode == 0, f"exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        assert "✓ done." in result.stdout
        assert "mnelo_project_demo" in result.stdout
        assert "example_stock_002" in result.stdout

    def test_03_runs_to_completion(self):
        result = _run_example("03_4_lane_recall.py")
        assert result.returncode == 0, f"exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        assert "✓ done." in result.stdout
        assert "META lane" in result.stdout
        assert "VECTOR lane" in result.stdout
        assert "GRAPH lane" in result.stdout
        assert "ENTITY lane" in result.stdout

    def test_04_runs_to_completion(self):
        result = _run_example("04_update_and_forget.py")
        assert result.returncode == 0, f"exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        assert "✓ done." in result.stdout
        assert "drift (vecs - active):" in result.stdout
        # Verify drift = 0 in key places
        drift_lines = [line for line in result.stdout.split("\n") if "drift (vecs - active):" in line]
        for line in drift_lines:
            # Format: "  drift (vecs - active): 0  (label)"
            assert " 0 " in line or " 0 (" in line, f"drift non-zero: {line}"

    def test_05_runs_to_completion(self):
        result = _run_example("05_identity_facts.py")
        assert result.returncode == 0, f"exit {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        assert "✓ done." in result.stdout
        # Should show add → list → show → list --json → remove → list
        assert "[1] list" in result.stdout
        assert "[2] add" in result.stdout
        assert "[3] show" in result.stdout
        assert "[5] list --json" in result.stdout
        assert "[6] remove" in result.stdout

    def test_readme_exists(self):
        readme = REPO / "examples" / "README.md"
        assert readme.exists()
        content = readme.read_text()
        # Should mention all 5 examples
        for i in range(1, 6):
            assert f"{i:02d}_" in content, f"example {i:02d} not in README"


class TestCleanup:
    """Verify cleanup actually removes data."""

    def test_no_example_data_left_behind(self):
        """After all 5 examples run, no `example_0N:` chunks should remain."""
        # Run all examples first
        for n in range(1, 6):
            examples_dir = REPO / "examples"
            for f in examples_dir.iterdir():
                if f.name.startswith(f"0{n}_") and f.name.endswith(".py"):
                    _run_example(f.name)
                    break

        # Check
        sys.path.insert(0, str(REPO))
        from memory import Memory

        m = Memory()
        try:
            rows = m._conn.execute("SELECT id, source FROM chunks WHERE source LIKE 'example_%'").fetchall()
            # If cleanup is broken, this would have rows
            leftovers = [r for r in rows if not r["source"].startswith("identity_fact_manager")]
            assert not leftovers, f"cleanup left {len(leftovers)} example chunks: {leftovers[:3]}"

            ents = m._conn.execute(
                "SELECT id FROM entities "
                "WHERE id LIKE 'mnelo_project_demo' OR id LIKE 'example_stock_002' "
                "OR id LIKE 'example_03_%' OR id LIKE 'identity:profession:example_05_%'"
            ).fetchall()
            assert not ents, f"cleanup left {len(ents)} example entities: {[e[0] for e in ents[:3]]}"
        finally:
            m.close()
