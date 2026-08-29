"""[audit fix #9 2026-08-16] _txn() nested SAVEPOINT.

Owner fix priority #3 (race P1, P1 #62 same source).
Original _txn uses BEGIN, so nested _txn(_txn) crashes with
OperationalError "within a transaction" / "no transaction is active".
Need: detect outer active txn → SAVEPOINT, else BEGIN.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mem(tmp_path):
    from memory import Memory, _txn

    db_path = tmp_path / "audit.db"
    m = Memory(db_path=db_path)
    yield m, _txn
    m.close()
    for ext in ["", ".usearch.index", ".usearch.state"]:
        p = db_path.parent / (db_path.name + ext)
        if p.exists():
            p.unlink()


def test_nested_txn_does_not_raise_when_inner_fails(mem):
    """#9 fix: nested _txn → outer wraps inner via SAVEPOINT, rollback inner only."""
    m, _txn = mem

    def outer_calls_inner():
        with _txn(m._conn):
            # inside outer, simulate inner _txn that raises
            try:
                with _txn(m._conn):
                    raise RuntimeError("inner boom")
            except RuntimeError:
                pass
            # outer should still be in active txn

    outer_calls_inner()
    # outer COMMIT succeeded → DB clean, no orphan writes
    m._conn.commit()


def test_nested_txn_outer_failure_rolls_back_everything(mem):
    """#9 fix: outer _txn raises → outer ROLLBACK, inner already rolled back itself."""
    m, _txn = mem
    cid = m.remember("test nested rollback")
    m._conn.commit()

    initial_count = m._conn.execute("SELECT COUNT(*) FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
    assert initial_count == 1

    # outer raises after inner commits → outer rollback reverts outer writes only
    # inner commits a no-op; outer rollback reverts the no-op
    with pytest.raises(RuntimeError, match="outer boom"):
        with _txn(m._conn):
            with _txn(m._conn):
                # inner commits cleanly
                pass
            raise RuntimeError("outer boom")

    # chunk from prior call (outside _txn) should still be there
    final_count = m._conn.execute("SELECT COUNT(*) FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
    assert final_count == 1, "outer rollback should not affect out-of-_txn writes"


def test_txn_no_active_outer_uses_begin(mem):
    """#9 fix: top-level _txn (no outer) still uses BEGIN — backward compat."""
    m, _txn = mem
    cid = m.remember("test top-level txn")
    m._conn.commit()

    # top-level txn: write succeeds and persists
    with _txn(m._conn):
        m._conn.execute("UPDATE chunks SET importance = 0.99 WHERE id = ?", (cid,))

    val = m._conn.execute("SELECT importance FROM chunks WHERE id = ?", (cid,)).fetchone()[0]
    assert val == 0.99
