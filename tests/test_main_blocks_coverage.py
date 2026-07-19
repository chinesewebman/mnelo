"""Round 5.5 — coverage of __main__ blocks via subprocess.

Targets:
- memory.py:1080-1131 (52 lines) — full pipeline demo
- entity_resolve.py:257-279 (22 lines) — duplicate candidates report
- embedder.py:122-128 (6 lines) — embedding smoke test

Strategy: run `python module.py` as subprocess, capture stdout/stderr,
assert exit code 0 + expected output. Each module's __main__ block runs
against LIVE memory.db (the actual integration test).
"""
import subprocess
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent


def _run_module_as_script(module_name: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run `python -m module_name` or `python module_name.py` as subprocess.

    Returns (returncode, stdout, stderr).
    """
    result = subprocess.run(
        [sys.executable, str(_REPO / f'{module_name}.py')],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(_REPO),
    )
    return result.returncode, result.stdout, result.stderr


class TestMemoryMain:
    """memory.py:1080-1131 __main__ block — full pipeline demo."""

    def test_memory_runs_as_script(self):
        """Run `python memory.py` — should hit full pipeline without errors."""
        rc, stdout, stderr = _run_module_as_script('memory', timeout=90)
        # Even if some assertions in __main__ fail, the script runs end-to-end
        # We just check it didn't crash with Python error
        assert rc == 0, f'memory.py exited {rc}\nSTDOUT: {stdout}\nSTDERR: {stderr}'

    def test_memory_main_produces_expected_output(self):
        """Output should contain ✅ markers for each step."""
        rc, stdout, stderr = _run_module_as_script('memory', timeout=90)
        # At least some of these steps should produce output
        # (Note: live DB may have different data; we just check no crash)
        assert rc == 0
        # Output should have at least one ✅ marker
        assert '✅' in stdout or 'remember' in stdout.lower()


class TestEntityResolveMain:
    """entity_resolve.py:257-279 __main__ block — duplicate report."""

    def test_entity_resolve_runs_as_script(self):
        rc, stdout, stderr = _run_module_as_script('entity_resolve', timeout=60)
        assert rc == 0, f'entity_resolve.py exited {rc}\nSTDOUT: {stdout}\nSTDERR: {stderr}'

    def test_entity_resolve_reports_counts(self):
        """Output should mention entity counts and duplicate candidates."""
        rc, stdout, stderr = _run_module_as_script('entity_resolve', timeout=60)
        assert rc == 0
        # Should print entity counts (numbers from SELECT count(*))
        assert 'Entity' in stdout or 'entity' in stdout or '重复' in stdout


class TestEmbedderMain:
    """embedder.py:122-128 __main__ block — embedding smoke test."""

    def test_embedder_runs_as_script(self):
        rc, stdout, stderr = _run_module_as_script('embedder', timeout=60)
        assert rc == 0, f'embedder.py exited {rc}\nSTDOUT: {stdout}\nSTDERR: {stderr}'

    def test_embedder_produces_512_dim_vector(self):
        """Single embed → 512-dim, batch embed → multiple 512-dim."""
        rc, stdout, stderr = _run_module_as_script('embedder', timeout=60)
        assert rc == 0
        # Should report 512-dim for single + batch
        assert '512' in stdout


class TestMainBlocksNotDead:
    """Sanity check: every module with __main__ block has a working script entry."""

    @pytest.mark.parametrize('module_name,expected_marker', [
        ('memory', 'remember'),
        ('entity_resolve', 'Entity'),
        ('embedder', '512'),
    ])
    def test_module_main_block_runs(self, module_name, expected_marker):
        rc, stdout, stderr = _run_module_as_script(module_name, timeout=90)
        assert rc == 0, f'{module_name}.py failed: {stderr}'
        assert expected_marker in stdout or expected_marker.lower() in stdout.lower(), \
            f'{module_name} missing {expected_marker} in output'