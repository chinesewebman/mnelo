"""Round 12 follow-up — mnelo_echo.py wrapper tests.

Verify the 🌳-prefix CLI wrapper that gives mnelo operations a distinct
visual marker (vs 🧠 for Hermes memory tool). Tests check:
  - remember: prints 🌳 +chunk_id + importance + source
  - recall: prints 🌳 + hit count + top method + rrf
  - forget: prints 🌳 + target_kind:id + soft_deleted
  - stats: prints 🌳 + table=count summary
  - emoji is the configured ECHO constant (not hardcoded, easy to swap)
"""

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "mnelo_echo.py"


def run(args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
    )


def find_echo_line(output: str) -> str:
    """Extract the 🌳 (or configured ECHO) feedback line from output."""
    for line in output.split("\n"):
        line = line.strip()
        # Skip log lines (start with [embedder], [2026-, mnelo INFO, etc.)
        if line.startswith("🌳") or line.startswith(ECHO):
            return line
    return ""


ECHO = "🌳"  # Must match ECHO constant in mnelo_echo.py


class TestRemember:
    """remember: prints 🌳 +chunk_id with importance + source."""

    def test_remember_prints_echo(self):
        result = run(
            [
                "remember",
                "test_echo_unique_round15 — wrapper test",
                "--source",
                "test_echo_round15",
                "--importance",
                "0.65",
            ]
        )
        assert result.returncode == 0, f"exit {result.returncode}\n{result.stderr}"
        line = find_echo_line(result.stdout)
        assert line.startswith(f"{ECHO} mnelo"), f"missing echo: {line}"
        assert "+" in line, f"missing + marker: {line}"
        # chunk_id format: chunk_YYYYMMDD_HHMMSS_microsec
        assert re.search(r"\+chunk_\d{8}_\d{6}_\d{6}", line), f"no chunk_id: {line}"
        assert "importance=0.65" in line, f"missing importance: {line}"
        assert "source=test_echo_round15" in line, f"missing source: {line}"

    def test_remember_default_importance(self):
        """When --importance not given, default 0.5 should appear."""
        result = run(
            [
                "remember",
                "test_echo_default_importance_unique — wrapper test",
                "--source",
                "test_echo_round15",
            ]
        )
        line = find_echo_line(result.stdout)
        assert "importance=0.5" in line, f"expected default 0.5: {line}"


class TestRecall:
    """recall: prints 🌳 + hit count + top method + rrf."""

    def test_recall_prints_echo(self):
        result = run(["recall", "test_echo_unique_round15 wrapper", "--top-k", "3"])
        line = find_echo_line(result.stdout)
        assert line.startswith(f"{ECHO} mnelo"), f"missing echo: {line}"
        assert "~" in line, f"missing ~ marker (recall): {line}"
        assert "hits" in line, f"missing hits count: {line}"
        # Should include top=method name
        assert "top=" in line, f"missing top method: {line}"
        assert "rrf=" in line, f"missing rrf score: {line}"

    def test_recall_zero_hits_via_short_topk(self):
        """--top-k 0 should return 0 hits (no actual recall)."""
        result = run(["recall", "anything", "--top-k", "0"])
        line = find_echo_line(result.stdout)
        assert "~0 hits" in line, f"expected 0 hits with --top-k 0: {line}"

    def test_recall_json_output(self):
        """--json flag should print JSON after the echo line."""
        result = run(["recall", "test_echo_unique", "--top-k", "2", "--json"])
        # Echo line
        line = find_echo_line(result.stdout)
        assert line, "no echo line"
        # JSON object after the echo
        assert '"method"' in result.stdout, "missing method in JSON output"


class TestForget:
    """forget: prints 🌳 + target_kind:id + soft_deleted."""

    def test_forget_prints_echo(self):
        # First write a chunk to forget
        run(
            [
                "remember",
                "test_echo_forget_target — wrapper test",
                "--source",
                "test_echo_round15",
            ]
        )
        # Now forget it (using the chunk_id from the remember output)
        # Use a fresh remember to get its id, then forget it
        result = run(
            [
                "remember",
                "test_echo_forget_unique — wrapper test",
                "--source",
                "test_echo_round15",
            ]
        )
        write_line = find_echo_line(result.stdout)
        m = re.search(r"\+(chunk_\d{8}_\d{6}_\d{6})", write_line)
        assert m, f"could not parse chunk_id: {write_line}"
        cid = m.group(1)

        result = run(["forget", "--id", cid, "--kind", "chunk"])
        line = find_echo_line(result.stdout)
        assert line.startswith(f"{ECHO} mnelo"), f"missing echo: {line}"
        assert "-chunk:" in line or f"-{cid}" in line, f"missing chunk id: {line}"
        assert "soft_deleted" in line, f"missing soft_deleted: {line}"


class TestStats:
    """stats: prints 🌳 + table=count summary."""

    def test_stats_prints_echo(self):
        result = run(["stats"])
        line = find_echo_line(result.stdout)
        assert line.startswith(f"{ECHO} mnelo"), f"missing echo: {line}"
        assert "stats:" in line, f"missing stats marker: {line}"
        # Should include at least chunks + entities
        assert "chunks=" in line, f"missing chunks count: {line}"
        assert "entities=" in line, f"missing entities count: {line}"


class TestEchoIsConfigurable:
    """Verify ECHO constant is defined at module top (so it's easy to swap)."""

    def test_echo_is_module_constant(self):
        # Read the script source and check ECHO constant exists
        source = SCRIPT.read_text()
        # Look for the ECHO marker line with comment
        m = re.search(r'^ECHO\s*=\s*"([^"]+)"', source, re.MULTILINE)
        assert m, f"ECHO constant not found at module top"
        assert m.group(1) == ECHO, f"ECHO mismatch: {m.group(1)} vs {ECHO}"
        # Comment should mention "swap" (so future agents know it's configurable)
        assert "swap" in source.lower(), "ECHO should be documented as swappable"


class TestCleanup:
    """All test_echo_round15 chunks get cleaned up at module teardown."""

    @classmethod
    def teardown_class(cls):
        sys.path.insert(0, str(REPO))
        from memory import Memory

        m = Memory()
        try:
            rows = m._conn.execute("SELECT rowid FROM chunks WHERE source = 'test_echo_round15'").fetchall()
            if rows:
                rowids = [r[0] for r in rows]
                placeholders = ",".join("?" * len(rowids))
                try:
                    m._conn.execute(
                        f"DELETE FROM vectors WHERE rowid IN ({placeholders})",
                        rowids,
                    )
                except Exception:
                    pass
            m._conn.execute("DELETE FROM chunks WHERE source = 'test_echo_round15'")
            m._conn.commit()
        finally:
            m.close()
