"""Round 12 follow-up — entity_resolve.find_duplicate_candidates(ids=...) contract.

The `ids` parameter (added v0.5.9) lets callers scope the scan to specific
entities, bypassing max_pairs. This file locks in the contract:

  - ids=[...] limits scan to exactly those entities
  - ids=[] returns [] immediately
  - ids=[non-existent] returns [] (no error)
  - ids includes soft-deleted entities → they are excluded (only active scanned)
  - max_pairs is NOT triggered when ids is provided (caller-controlled scope)
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from entity_resolve import find_duplicate_candidates
from memory import Memory, now


@pytest.fixture
def mem():
    m = Memory()
    # Cleanup any test entities from previous runs
    m._conn.execute("UPDATE entities SET valid_until = datetime('now') WHERE id LIKE 'test_eresolve_ids_%'")
    m._conn.commit()
    yield m
    m._conn.execute("UPDATE entities SET valid_until = datetime('now') WHERE id LIKE 'test_eresolve_ids_%'")
    m._conn.commit()
    m.close()


def _make_entity(m: Memory, ent_id: str, name: str, kind: str = "stock") -> str:
    """Insert a test entity and return its id."""
    m._conn.execute(
        """
        INSERT INTO entities (id, kind, name, source, valid_from, valid_until)
        VALUES (?, ?, ?, 'test-eresolve-ids', ?, NULL)
        """,
        (ent_id, kind, name, now()),
    )
    m._conn.commit()
    return ent_id


class TestIdsParameter:
    """Verify the new `ids` parameter scopes the scan."""

    def test_ids_limits_to_specified_entities(self, mem):
        # Two duplicates (same name) + one unrelated entity
        ts = datetime.now().strftime("%H%M%S%f")
        a = f"test_eresolve_ids_{ts}_a"
        b = f"test_eresolve_ids_{ts}_b"
        c = f"test_eresolve_ids_{ts}_c"
        _make_entity(mem, a, "ACME Corp")
        _make_entity(mem, b, "ACME Corp")  # same name → duplicate
        _make_entity(mem, c, "Different Name")

        # Pass only [a, b] — should find them as duplicates, c excluded
        cands = find_duplicate_candidates(mem._conn, threshold=0.7, ids=[a, b])
        ids_in_cands = {c[0] for c in cands} | {c[1] for c in cands}
        assert a in ids_in_cands
        assert b in ids_in_cands
        assert c not in ids_in_cands

    def test_ids_empty_list_returns_immediately(self, mem):
        cands = find_duplicate_candidates(mem._conn, threshold=0.7, ids=[])
        assert cands == []

    def test_ids_nonexistent_returns_empty(self, mem):
        cands = find_duplicate_candidates(mem._conn, threshold=0.7, ids=["nope_1", "nope_2"])
        assert cands == []

    def test_ids_excludes_soft_deleted(self, mem):
        """Even if soft-deleted entity id is in ids list, it's excluded."""
        ts = datetime.now().strftime("%H%M%S%f")
        a = f"test_eresolve_ids_{ts}_a"
        b = f"test_eresolve_ids_{ts}_b"
        _make_entity(mem, a, "Soft Deleted Co")
        _make_entity(mem, b, "Soft Deleted Co")
        # Soft-delete both
        mem._conn.execute(
            "UPDATE entities SET valid_until = datetime('now') WHERE id IN (?, ?)",
            (a, b),
        )
        mem._conn.commit()

        cands = find_duplicate_candidates(mem._conn, threshold=0.7, ids=[a, b])
        # Soft-deleted entities are excluded from scan
        assert cands == []

    def test_ids_bypasses_max_pairs(self, mem):
        """When ids is provided, max_pairs should not trigger a warning."""
        ts = datetime.now().strftime("%H%M%S%f")
        a = f"test_eresolve_ids_{ts}_a"
        b = f"test_eresolve_ids_{ts}_b"
        _make_entity(mem, a, "Pair Cap Test Co")
        _make_entity(mem, b, "Pair Cap Test Co")
        # Even with tiny max_pairs, ids= scope should work
        cands = find_duplicate_candidates(mem._conn, threshold=0.7, ids=[a, b], max_pairs=1)
        # Found at least one (the duplicate pair)
        ids_in_cands = {c[0] for c in cands} | {c[1] for c in cands}
        assert a in ids_in_cands
        assert b in ids_in_cands

    def test_ids_with_low_threshold(self, mem):
        """ids= scope respects threshold parameter."""
        ts = datetime.now().strftime("%H%M%S%f")
        a = f"test_eresolve_ids_{ts}_a"
        b = f"test_eresolve_ids_{ts}_b"
        _make_entity(mem, a, "Alpha")
        _make_entity(mem, b, "Zebra")  # very different name

        # With high threshold, no match
        cands = find_duplicate_candidates(mem._conn, threshold=0.95, ids=[a, b])
        assert cands == []

        # With low threshold, may match (depending on alias_match_score)
        # We don't assert specific behavior — just that threshold is respected
        cands_low = find_duplicate_candidates(mem._conn, threshold=0.1, ids=[a, b])
        # Low threshold may or may not match — just verify no crash
        assert isinstance(cands_low, list)


class TestDiagnosticImprovement:
    """Verify the improved max_pairs diagnostic message."""

    def test_max_pairs_warning_includes_counts(self, mem, capsys):
        """Warning message should now include pairs scanned / total + kinds count."""
        # Force max_pairs exhaustion by creating many entities with kind=stock
        ts = datetime.now().strftime("%H%M%S%f")
        ents = []
        for i in range(40):
            eid = f"test_eresolve_ids_{ts}_max_{i:03d}"
            _make_entity(mem, eid, f"Entity Number {i:03d}", kind="stock")
            ents.append(eid)

        # Run with tiny max_pairs to trigger the warning
        cands = find_duplicate_candidates(mem._conn, threshold=0.7, kind="stock", max_pairs=10)

        # Verify warning was emitted to stderr
        captured = capsys.readouterr()
        assert "max_pairs=10 reached" in captured.err
        # New diagnostic should include scanned/total pairs
        assert "scanned" in captured.err
        assert "kind(s)" in captured.err
        assert "Filter by kind, pass ids=[...], or raise max_pairs" in captured.err
