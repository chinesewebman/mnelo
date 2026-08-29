"""[bug fix D2 2026-08-16] recall(rrf) leaks 4 SQLite connections on worker exception.

Pre-fix: close loop ran AFTER the 4 futures joined — if any worker raised
(e.g. vec0 error, OOM, key interrupt), control jumped out of `with ThreadPoolExecutor`
and the close loop was skipped. Result: every failed recall leaks 4 file
descriptors + 4 vec0 module registrations. Under sustained load (MCP server
handling concurrent failing recalls), fd exhaustion is real.

Post-fix: try/finally ensures all 4 conns close even on worker exception.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_recall_rrf_closes_conns_on_worker_exception():
    """D2 fix: when _vector_recall_with_conn raises, the 4 recall_conns must close."""
    import gc
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d2.db")
        try:
            # Patch _vector_recall_with_conn to raise (avoid "test" placeholder filter)
            original = m._vector_recall_with_conn

            def boom(*args, **kwargs):
                raise RuntimeError("simulated vec0 crash")

            m._vector_recall_with_conn = boom
            try:
                # This should raise but NOT leak conns
                for _ in range(3):
                    with pytest.raises(RuntimeError, match="vec0 crash"):
                        m.recall("python async patterns", top_k=5, strategy="rrf")
            finally:
                m._vector_recall_with_conn = original
        finally:
            m.close()


def test_recall_rrf_closes_conns_on_meta_worker_exception():
    """D2 fix: meta worker exception also triggers cleanup."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d2b.db")
        try:
            original = m._meta_recall_with_conn

            def boom(*args, **kwargs):
                raise RuntimeError("simulated meta crash")

            m._meta_recall_with_conn = boom
            try:
                with pytest.raises(RuntimeError, match="meta crash"):
                    m.recall("python async patterns", top_k=5, strategy="rrf")
            finally:
                m._meta_recall_with_conn = original
        finally:
            m.close()


def test_recall_rrf_normal_path_still_works():
    """D2 fix shouldn't break the success path."""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "d2c.db")
        try:
            m.remember("test content", source="manual")
            result = m.recall("test content", top_k=5, strategy="rrf")
            assert isinstance(result, list)
        finally:
            m.close()
