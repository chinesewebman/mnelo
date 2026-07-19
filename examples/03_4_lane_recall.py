#!/usr/bin/env python3
"""
Example 03 — 4-lane recall + RRF fusion.

Demonstrates the heart of mnelo: how 4 different recall lanes combine to give
better results than any single lane.

  - vector: vec0 cosine similarity on BAAI/bge-small-zh-v1.5 embeddings (512d)
  - graph:  BFS traversal in the entity-relation graph (1-2 hops)
  - meta:   FTS5 full-text search on chunk content + metadata
  - entity: fuzzy match against entity names + aliases

RRF (Reciprocal Rank Fusion) fuses the 4 ranked lists without needing to
normalize scores — the final score is sum of 1/(k+rank_i) for each lane that
returned the chunk. Read docs/ARCHITECTURE.md for the math.

What you'll see:
  - A chunk that's strongest on each lane (4 chunks, each tuned to one lane)
  - A query that should hit ALL 4 lanes via RRF
  - The fused top-k results
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
        print("=== Example 03: 4-lane recall + RRF fusion ===\n")

        # [1] Write 4 chunks, each tuned to a different lane.
        # Note the unique sentinel: example_03_uniq_zzz — only your chunks have it.

        # vector-strong: semantically similar to query, even though no exact word overlap
        chunk_vector = m.remember(
            content="example_03_uniq_zzz mnelo 用 SQLite + 向量索引来记忆主人说过的所有事情。",
            source="example_03:vector_lane",
            importance=0.7,
            tags=["example_03_vector"],
        )
        print(f"[1/4] vector-tuned chunk: {chunk_vector}")

        # graph-strong: references an entity that connects via graph
        chunk_graph = m.remember(
            content="example_03_uniq_zzz the example_03_stock_alpha relates to owner user directly.",
            source="example_03:graph_lane",
            importance=0.7,
            entities=[
                {
                    "id": "example_03_stock_alpha",
                    "kind": "stock",
                    "name": "Example Stock Alpha",
                    "summary": "Demo entity for graph lane",
                },
            ],
        )
        print(f"[2/4] graph-tuned chunk: {chunk_graph}")
        # Manual relation
        m.relate(
            source_id="user",
            target_id="example_03_stock_alpha",
            relation="owns",
            weight=0.9,
        )

        # meta-strong: exact token match for the query
        chunk_meta = m.remember(
            content="example_03_uniq_zzz token_match_keyword_zzzyyy for FTS5 exact match.",
            source="example_03:meta_lane",
            importance=0.7,
        )
        print(f"[3/4] meta-tuned chunk: {chunk_meta}")

        # entity-strong: entity name match
        chunk_entity = m.remember(
            content="example_03_uniq_zzz some content.",
            source="example_03:entity_lane",
            importance=0.7,
            entities=[
                {
                    "id": "example_03_token_unique_99",
                    "kind": "concept",
                    "name": "example_03_token_unique_99",
                    "summary": "Demo entity for entity lane",
                },
            ],
        )
        print(f"[4/4] entity-tuned chunk: {chunk_entity}")

        # [2] Run recall and demonstrate each lane.
        # Each lane is best hit by a query optimized for it.

        # META: exact token match
        print("\n=== META lane (FTS5 exact match) ===")
        print("[recall] query = 'token_match_keyword_zzzyyy':")
        results = m.recall("token_match_keyword_zzzyyy", top_k=5)
        for i, r in enumerate(results, 1):
            label = " ← meta-tuned" if r["chunk_id"] == chunk_meta else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{label}")

        # ENTITY: query that matches an entity name
        print("\n=== ENTITY lane (entity name match) ===")
        print("[recall] query = 'example_03_token_unique_99':")
        results = m.recall("example_03_token_unique_99", top_k=5)
        for i, r in enumerate(results, 1):
            label = " ← entity-tuned" if r["chunk_id"] == chunk_entity else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{label}")

        # GRAPH: query that triggers 1-hop traversal
        print("\n=== GRAPH lane (BFS from entity) ===")
        print("[recall] query = 'example_03_stock_alpha owner':")
        results = m.recall("example_03_stock_alpha owner", top_k=5)
        for i, r in enumerate(results, 1):
            label = " ← graph-tuned" if r["chunk_id"] == chunk_graph else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{label}")

        # VECTOR: query that's semantically similar (no exact word match)
        print("\n=== VECTOR lane (semantic similarity) ===")
        print("[recall] query = '怎么让 AI 记住对话' (semantic paraphrase of vector chunk):")
        results = m.recall("怎么让 AI 记住对话", top_k=5)
        for i, r in enumerate(results, 1):
            label = " ← vector-tuned" if r["chunk_id"] == chunk_vector else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{label}")

        # [3] Show which lanes hit each chunk overall (from above queries).
        print("\n=== Summary: each chunk's strongest lane ===")
        labels = {
            chunk_vector: "vector-tuned",
            chunk_graph: "graph-tuned",
            chunk_meta: "meta-tuned",
            chunk_entity: "entity-tuned",
        }
        all_results = (
            m.recall("token_match_keyword_zzzyyy", top_k=10)
            + m.recall("example_03_token_unique_99", top_k=10)
            + m.recall("example_03_stock_alpha owner", top_k=10)
            + m.recall("怎么让 AI 记住对话", top_k=10)
        )
        my_chunks = {chunk_vector, chunk_graph, chunk_meta, chunk_entity}
        lanes_per_chunk: dict = {}
        for r in all_results:
            if r["chunk_id"] in my_chunks:
                lanes_per_chunk.setdefault(r["chunk_id"], set()).add(r["method"])
        for cid, label in labels.items():
            lanes = lanes_per_chunk.get(cid, set())
            print(f"  {label}: {sorted(lanes) if lanes else 'NOT FOUND in any query'}")
        print()

        print("✓ done.")
        return 0
    finally:
        _cleanup(m)
        m.close()


def _cleanup(m: Memory) -> None:
    """Remove everything created by this example."""
    chunk_rows = m._conn.execute("SELECT rowid FROM chunks WHERE source LIKE 'example_03:%'").fetchall()
    if chunk_rows:
        rowids = [r["rowid"] for r in chunk_rows]
        placeholders = ",".join("?" * len(rowids))
        try:
            m._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
        except Exception:
            pass
    m._conn.execute("DELETE FROM chunks WHERE source LIKE 'example_03:%'")
    m._conn.execute("DELETE FROM entities WHERE source LIKE 'example_03:%'")
    for eid in ("example_03_stock_alpha", "example_03_token_unique_99"):
        m._conn.execute(
            "UPDATE entities SET valid_until = datetime('now') WHERE id = ?",
            (eid,),
        )
        m._conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
    for eid in ("example_03_stock_alpha", "example_03_token_unique_99"):
        m._conn.execute(
            "UPDATE relations SET valid_until = datetime('now') WHERE source_id = ? OR target_id = ?",
            (eid, eid),
        )
        m._conn.execute(
            "DELETE FROM relations WHERE source_id = ? OR target_id = ?",
            (eid, eid),
        )
    m._conn.commit()


if __name__ == "__main__":
    sys.exit(main())
