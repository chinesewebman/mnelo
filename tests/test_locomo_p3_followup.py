"""P3-followup tests for LoCoMo-style smoke benchmark.

[8/11 P3-followup] Tests cover the new `locomo` subcommand added on top of
chinesewebman/mnelo#11 (P3-benchmarks). The original dispatcher tests
remain in upstream.

Tests:
- dispatcher recognises locomo as a valid subcommand
- python -m benchmarks locomo --help shows locomo own help
- python -m benchmarks locomo --json PATH smoke runs and produces valid JSON
- locomo coverage + latency metrics are within sane bounds
- locomo cleans up its own seed data (idempotent)

NB: tests run subprocesses; CI default timeout is 120s per case.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run python -m benchmarks <args> from the repo cwd.

    Inherit the parent environment so Memory() can find its config / DB path
    the same way the existing tests do, but prepend REPO to PYTHONPATH so the
    benchmarks package is importable.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [PYTHON, "-m", "benchmarks", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
        env=env,
    )


class TestLocomoDispatcher:
    """Test that locomo is registered as a valid subcommand."""

    def test_locomo_is_a_known_subcommand(self):
        """python -m benchmarks locomo --help should NOT exit 2."""
        result = run_cli("locomo", "--help", timeout=30)
        assert result.returncode == 0, (
            f"locomo not registered: rc={result.returncode}, stderr={result.stderr}"
        )
        assert "--top-k" in result.stdout

    def test_unknown_subcommand_still_fails(self):
        """python -m benchmarks nosuch should still exit 2 (upstream behavior)."""
        result = run_cli("nosuch", timeout=30)
        assert result.returncode == 2
        assert "nosuch" in result.stderr or "usage" in result.stderr.lower()


class TestLocomoSmoke:
    """Test the locomo smoke benchmark end-to-end."""

    def test_locomo_runs_and_writes_json(self):
        """Smoke run: produces valid JSON with coverage + latency fields."""
        out_path = Path("/tmp/test_locomo_followup.json")
        if out_path.exists():
            out_path.unlink()
        result = run_cli("locomo", "--json", str(out_path), timeout=120)
        assert result.returncode == 0, f"locomo failed: {result.stderr}"
        assert "Mean coverage" in result.stdout
        assert out_path.exists()

        data = json.loads(out_path.read_text())
        assert "coverage" in data
        assert "mean" in data["coverage"]
        assert "per_scenario" in data["coverage"]
        assert isinstance(data["coverage"]["mean"], float)
        assert 0.0 <= data["coverage"]["mean"] <= 1.0
        assert "latency_ms" in data
        assert "p50" in data["latency_ms"]
        assert data["latency_ms"]["p50"] > 0
        out_path.unlink()

    def test_locomo_cleans_up(self):
        """locomo must clean up its own seed data (idempotent across runs)."""
        r1 = run_cli("locomo", timeout=120)
        assert r1.returncode == 0
        assert "deleted 9 chunks" in r1.stdout
        r2 = run_cli("locomo", timeout=120)
        assert r2.returncode == 0
        assert "deleted 9 chunks" in r2.stdout

    def test_locomo_scenarios_are_defined(self):
        """There should be at least 3 LoCoMo scenarios built-in."""
        from benchmarks.locomo import LOCOMO_SCENARIOS

        assert len(LOCOMO_SCENARIOS) >= 3
        for s in LOCOMO_SCENARIOS:
            assert "topic" in s
            assert "chunks" in s and len(s["chunks"]) >= 1
            assert "queries" in s and len(s["queries"]) >= 1
            assert "topic_keys" in s and len(s["topic_keys"]) >= 1


class TestLocomoImports:
    """Test that locomo module exposes the expected names."""

    def test_locomo_has_main(self):
        from benchmarks.locomo import main

        assert callable(main)

    def test_locomo_recall_coverage_helper(self):
        from benchmarks.locomo import recall_coverage

        # empty queries → 0.0
        assert recall_coverage(None, [], ["x"], top_k=5) == 0.0
