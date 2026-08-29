"""[audit fix 4.1 2026-08-16] graph_query BFS N+1 → batch IN.

Owner fix priority #4 (perf hit, ~4.6s/day wasted on N+1).
Original: per-hop × per-frontier-node SELECT (60 round-trip for 3-hop frontier=20).
Fix: per-hop batch IN(...) (3 round-trip total).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mem(tmp_path):
    from memory import Memory

    db_path = tmp_path / "audit.db"
    m = Memory(db_path=db_path)
    yield m
    m.close()
    for ext in ["", ".usearch.index", ".usearch.state"]:
        p = db_path.parent / (db_path.name + ext)
        if p.exists():
            p.unlink()


def _build_chain(mem, depth):
    """Build entity chain: e0 -> e1 -> e2 -> ... -> eN (depth+1 entities, depth relations)."""
    ents = []
    for i in range(depth + 1):
        eid = f"ent_{i}"
        ents.append(eid)
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) VALUES (?, 'concept', ?, 'test_bfs', ?, NULL)",
            (eid, f"name_{i}", "2026-01-01T00:00:00"),
        )
    for i in range(depth):
        mem._conn.execute(
            "INSERT INTO relations (source_id, target_id, relation, source, valid_from, valid_until) VALUES (?, ?, 'links', 'test_bfs', ?, NULL)",
            (ents[i], ents[i + 1], "2026-01-01T00:00:00"),
        )
    mem._conn.commit()
    return ents


def test_graph_query_bfs_finds_chain(mem):
    """#4.1 fix: BFS reaches all nodes in chain via batched IN queries."""
    depth = 4
    ents = _build_chain(mem, depth)
    result = mem.graph_query(start_node=ents[0], max_hops=depth)
    # All nodes should be reached
    found_ids = {n["id"] for n in result["nodes"]}
    for e in ents:
        assert e in found_ids, f"BFS should reach {e}, found: {found_ids}"
    # All edges should be discovered
    assert len(result["edges"]) == depth, f"expected {depth} edges, got {len(result['edges'])}"


def test_graph_query_bfs_respects_max_hops(mem):
    """#4.1 fix: BFS doesn't overshoot max_hops."""
    depth = 5
    ents = _build_chain(mem, depth)
    # Only 2 hops — should NOT reach e5
    result = mem.graph_query(start_node=ents[0], max_hops=2)
    found_ids = {n["id"] for n in result["nodes"]}
    assert ents[0] in found_ids
    assert ents[1] in found_ids
    assert ents[2] in found_ids
    assert ents[3] not in found_ids, f"3-hop should not be reached with max_hops=2, got {found_ids}"


def test_graph_query_bfs_dedupes_no_infinite_loop(mem):
    """#4.1 fix: BFS doesn't add same node to next_frontier twice (no infinite loop)."""
    # Create cycle: a -> b -> c -> a
    for eid in ("cyc_a", "cyc_b", "cyc_c"):
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) VALUES (?, 'concept', ?, 'test_cyc', ?, NULL)",
            (eid, eid, "2026-01-01T00:00:00"),
        )
    for src, tgt in (("cyc_a", "cyc_b"), ("cyc_b", "cyc_c"), ("cyc_c", "cyc_a")):
        mem._conn.execute(
            "INSERT INTO relations (source_id, target_id, relation, source, valid_from, valid_until) VALUES (?, ?, 'links', 'test_cyc', ?, NULL)",
            (src, tgt, "2026-01-01T00:00:00"),
        )
    mem._conn.commit()

    result = mem.graph_query(start_node="cyc_a", max_hops=3)
    found_ids = {n["id"] for n in result["nodes"]}
    # Should reach all 3 but not duplicate
    assert found_ids == {"cyc_a", "cyc_b", "cyc_c"}
    # 3 unique edges (cycle)
    assert len(result["edges"]) == 3
