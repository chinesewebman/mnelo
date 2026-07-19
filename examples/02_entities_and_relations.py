#!/usr/bin/env python3
"""
Example 02 — Entities and relations (the knowledge graph).

Demonstrates the structured side of mnelo:
  1. remember() with entities=[] parameter — declare entities explicitly
  2. relate() — manually create edges between entities
  3. recall() with a query that hits the graph lane via entity name

Why use entities[] instead of letting mnelo auto-extract?
  - Auto-extraction uses regex (simplified NER) — works for English/Chinese names
  - Explicit entities[] gives you precise control over kinds and aliases
  - Some entities (e.g. stock codes, technical IDs) need explicit kind tagging

What you'll see:
  - Two entities created: 'mnelo_project' (kind=concept) and 'sh600089' (kind=stock)
  - One relation: mnelo_project --implemented_for--> sh600089
  - Graph recall returns both chunks (seed + 1-hop traversal)
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memory import Memory


def main() -> int:
    m = Memory()
    _cleanup(m)
    try:
        print("=== Example 02: Entities and relations ===\n")

        # [1] remember() with explicit entities[] — declares entity kinds.
        # Use unique ids with example prefix so they don't collide with real data
        # (real stock codes like sh600089 already exist in LIVE DB).
        chunk_id = m.remember(
            content="example_02_graph_demo example_02_graph_unique_xyz456 — "
            "mnelo_project_demo 项目为 example_stock_002 提供 AI 记忆支持 demo。",
            source="example_02:graph",
            importance=0.7,
            entities=[
                {
                    "id": "mnelo_project_demo",
                    "kind": "concept",
                    "name": "mnelo project demo",
                    "summary": "An example concept entity for demonstration",
                },
                {
                    "id": "example_stock_002",
                    "kind": "stock",
                    "name": "Example Stock 002",
                    "summary": "A demo stock entity (not a real ticker)",
                },
            ],
        )
        print(f"[remember] chunk_id: {chunk_id}\n")

        # [2] Manually create a relation between entities.
        rel_id = m.relate(
            source_id="mnelo_project_demo",
            target_id="example_stock_002",
            relation="implemented_for",
            weight=0.9,
            properties={"use_case": "stock decision memory"},
        )
        print(f"[relate] mnelo_project_demo --implemented_for--> example_stock_002 (rel_id={rel_id})\n")

        # [3] Inspect the entities + relations.
        print("[db] entities created by this example:")
        rows = m._conn.execute(
            "SELECT id, kind, name FROM entities "
            "WHERE id IN ('mnelo_project_demo', 'example_stock_002') AND valid_until IS NULL"
        ).fetchall()
        for r in rows:
            print(f"  {r[0]:30s} kind={r[1]:10s} name={r[2]}")

        print("\n[db] relations created by this example:")
        rows = m._conn.execute(
            "SELECT source_id, relation, weight, target_id FROM relations "
            "WHERE source_id = 'mnelo_project_demo' OR target_id = 'mnelo_project_demo'"
        ).fetchall()
        for r in rows:
            print(f"  {r[0]} --[{r[1]} (w={r[2]})]--> {r[3]}")
        print()

        # [4] Query that hits the graph lane via entity name.
        #     "example_stock_002" matches the entity id → entity lane returns it as a
        #     seed → graph lane 1-hops to mnelo_project_demo → 2-hops to the
        #     chunk that mentions both.
        print("[recall] query = 'example_stock_002' (graph 1-hop):")
        results = m.recall("example_stock_002", top_k=5)
        my_hits = [r for r in results if "example_02_graph_unique_xyz456" in r["content"]]
        print(f"  Your chunk: {len(my_hits)}/{len(results)} top-5 hits")
        for i, r in enumerate(results, 1):
            marker = ""
            if "example_02_graph_unique_xyz456" in r["content"]:
                marker = " ← YOURS"
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{marker}")
        print()

        # [5] Query from the project side — should also hit graph.
        print("[recall] query = 'mnelo_project_demo implemented_for' (graph + entity):")
        results = m.recall("mnelo_project_demo implemented_for", top_k=5)
        my_hits = [r for r in results if "example_02_graph_unique_xyz456" in r["content"]]
        print(f"  Your chunk: {len(my_hits)}/{len(results)} top-5 hits")
        for i, r in enumerate(results, 1):
            marker = " ← YOURS" if "example_02_graph_unique_xyz456" in r["content"] else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{marker}")
        print()

        print("✓ done.")
        return 0
    finally:
        _cleanup(m)
        m.close()


def _cleanup(m: Memory) -> None:
    """Remove chunks, vectors, entities, and relations created by this example."""
    # Find chunks
    chunk_rows = m._conn.execute("SELECT rowid FROM chunks WHERE source LIKE 'example_02:%'").fetchall()
    if chunk_rows:
        rowids = [r["rowid"] for r in chunk_rows]
        placeholders = ",".join("?" * len(rowids))
        try:
            m._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
        except Exception:
            pass
    m._conn.execute("DELETE FROM chunks WHERE source LIKE 'example_02:%'")
    # Entities created by this example (note: example_stock_002 may collide
    # with real data if reused, but we use unique ids here)
    m._conn.execute("DELETE FROM entities WHERE source LIKE 'example_02:%'")
    # For our example-specific entities: hard-delete (they're ours, not real).
    for example_eid in ("mnelo_project_demo", "example_stock_002"):
        m._conn.execute(
            "UPDATE entities SET valid_until = datetime('now') WHERE id = ?",
            (example_eid,),
        )
        m._conn.execute("DELETE FROM entities WHERE id = ?", (example_eid,))
    # Relations
    for example_eid in ("mnelo_project_demo", "example_stock_002"):
        m._conn.execute(
            "UPDATE relations SET valid_until = datetime('now') WHERE source_id = ? OR target_id = ?",
            (example_eid, example_eid),
        )
        m._conn.execute(
            "DELETE FROM relations WHERE source_id = ? OR target_id = ?",
            (example_eid, example_eid),
        )
    m._conn.commit()


if __name__ == "__main__":
    sys.exit(main())
