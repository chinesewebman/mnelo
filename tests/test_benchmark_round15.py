"""Round 15 — benchmark.py helper tests.

Tests cover:
- percentile() edge cases (empty, single value, exact boundaries)
- benchmark seed/cleanup doesn't leak data (idempotent across runs)
- benchmark respects --chunks and --queries flags
- benchmark produces valid JSON output

NOTE: These tests use the LIVE DB (Memory() defaults to /Users/apple/.hermes/memory)
since benchmark.py seeds + cleans up its own source-prefix-scoped data.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BENCH_SCRIPT = REPO / "scripts" / "benchmark.py"


class TestPercentile:
    """Test the percentile() helper imported from benchmark."""

    def test_empty_list(self):
        from scripts.benchmark import percentile

        assert percentile([], 50) == 0.0
        assert percentile([], 95) == 0.0

    def test_single_value(self):
        from scripts.benchmark import percentile

        assert percentile([42.0], 50) == 42.0
        assert percentile([42.0], 99) == 42.0

    def test_exact_median(self):
        from scripts.benchmark import percentile

        assert percentile([1, 2, 3, 4, 5], 50) == 3.0

    def test_p95(self):
        from scripts.benchmark import percentile

        # 100 values 1-100, p95 should be ~95.05 (linear interp)
        values = list(range(1, 101))
        p95 = percentile(values, 95)
        assert 94 <= p95 <= 96

    def test_p99(self):
        from scripts.benchmark import percentile

        values = list(range(1, 101))
        p99 = percentile(values, 99)
        assert 98 <= p99 <= 100

    def test_min_max_boundaries(self):
        from scripts.benchmark import percentile

        values = [10, 20, 30, 40, 50]
        assert percentile(values, 0) == 10
        assert percentile(values, 100) == 50


class TestBenchmarkCLI:
    """Test the benchmark CLI flags + JSON output."""

    def test_help_flag(self):
        """--help should exit 0 and show usage."""
        result = subprocess.run(
            [sys.executable, str(BENCH_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "--chunks" in result.stdout

    def test_invalid_chunks(self):
        """--chunks < 100 should fail with error."""
        result = subprocess.run(
            [sys.executable, str(BENCH_SCRIPT), "--chunks", "10"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "error" in result.stderr.lower() or "chunks" in result.stderr.lower()

    def test_invalid_queries(self):
        """--queries < 10 should fail with error."""
        result = subprocess.run(
            [sys.executable, str(BENCH_SCRIPT), "--queries", "5"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "queries" in result.stderr.lower()

    def test_small_benchmark_runs(self):
        """Smoke test: --chunks 200 --queries 15 should complete + output JSON."""
        out_path = Path("/tmp/test_bench_small.json")
        if out_path.exists():
            out_path.unlink()
        result = subprocess.run(
            [
                sys.executable,
                str(BENCH_SCRIPT),
                "--chunks",
                "200",
                "--queries",
                "15",
                "--json",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(REPO),
        )
        assert result.returncode == 0, f"benchmark failed: {result.stderr}"
        assert "p50:" in result.stdout
        assert "p95:" in result.stdout
        assert out_path.exists()
        # Validate JSON shape
        data = json.loads(out_path.read_text())
        assert data["config"]["n_chunks"] == 200
        assert data["config"]["n_queries"] == 15
        assert "recall" in data
        assert "p50_ms" in data["recall"]
        assert "p95_ms" in data["recall"]
        assert "final_db_stats" in data
        out_path.unlink()

    def test_benchmark_is_idempotent(self):
        """Running benchmark twice should not leak data (cleanup works)."""
        # Run twice with different sizes
        result1 = subprocess.run(
            [sys.executable, str(BENCH_SCRIPT), "--chunks", "100", "--queries", "10"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO),
        )
        assert result1.returncode == 0
        result2 = subprocess.run(
            [sys.executable, str(BENCH_SCRIPT), "--chunks", "100", "--queries", "10"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO),
        )
        assert result2.returncode == 0
        # Both should report successful cleanup
        assert "deleted 100 chunks" in result1.stdout
        assert "deleted 100 chunks" in result2.stdout


class TestBenchmarkQueries:
    """Test the BENCHMARK_QUERIES list."""

    def test_query_count_at_least_50(self):
        """Need >= 50 unique queries to support --queries up to 100 with cycling."""
        from scripts.benchmark import BENCHMARK_QUERIES

        assert len(BENCHMARK_QUERIES) >= 50

    def test_queries_are_strings(self):
        from scripts.benchmark import BENCHMARK_QUERIES

        for q in BENCHMARK_QUERIES:
            assert isinstance(q, str)
            assert len(q) > 0

    def test_queries_diverse(self):
        """Mix of stock codes, names, English, Chinese — not all the same type."""
        from scripts.benchmark import BENCHMARK_QUERIES

        # At least 5 Chinese characters (entity-style)
        chinese_count = sum(1 for q in BENCHMARK_QUERIES if any("\u4e00" <= c <= "\u9fff" for c in q))
        # At least 5 stock code-style (lowercase letters + digits)
        stock_count = sum(
            1 for q in BENCHMARK_QUERIES if any(c.isdigit() for c in q) and any(c.isalpha() and c.islower() for c in q)
        )
        assert chinese_count >= 5
        assert stock_count >= 5
