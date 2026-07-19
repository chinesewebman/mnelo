#!/usr/bin/env python3
"""
Example 01 — Basic remember/recall.

Demonstrates the simplest mnelo workflow:
  1. remember() a chunk of text
  2. recall() it back via semantic search
  3. see the chunk_id returned and the rrf_score from RRF fusion

This is the foundation. Read this first.

What you'll see:
  - chunk_id like 'chunk_20260719_220000_123456'
  - rrf_score > 0.0 (RRF fuses 4 lanes; not all are needed for a hit)
  - content matches what you wrote
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memory import Memory


def main() -> int:
    # [1] Create a Memory instance. By default it uses MNELO_HOME from env.
    #     We tag all writes with source='example_01:<purpose>' so cleanup is easy.
    m = Memory()

    # Cleanup any leftover data from previous runs
    _cleanup(m)

    try:
        print("=== Example 01: Basic remember / recall ===\n")

        # [2] Write a chunk. remember() does a lot under the hood:
        #     - inserts into `chunks` table
        #     - extracts entity names (e.g. "engineer", "Beijing", "sh600089")
        #     - inserts into `entities` table
        #     - embeds the content (BAAI/bge-small-zh-v1.5, 512d)
        #     - inserts into `vectors` table (sqlite-vec)
        #     - extracts relations between entities (if any)
        #
        # Note: recall() returns chunks from the WHOLE DB, ranked by RRF.
        # To find YOUR chunk specifically, embed a unique sentinel phrase
        # so your chunk scores highest for that exact token.
        unique_phrase = "本地优先的 AI agent 记忆系统 mnelo zork_unique_sentinel_xyz"
        chunk_id = m.remember(
            content=f"mnelo 是一个{unique_phrase}，使用 SQLite + 4 路 RRF 召回。",
            source="example_01:basic",
            importance=0.7,
        )
        print(f"[remember] chunk_id: {chunk_id}\n")

        # [3] Read it back via recall() — exact token match (meta lane).
        print("[recall] query = 'zork_unique_sentinel_xyz' (exact token):")
        results = m.recall("zork_unique_sentinel_xyz", top_k=3)
        # Check if our chunk came back
        my_hit = next((r for r in results if r["chunk_id"] == chunk_id), None)
        if my_hit:
            print(f"  ✓ YOUR chunk at rank {results.index(my_hit) + 1}/{len(results)}")
            print(f"    method: {my_hit['method']}, score: {my_hit['rrf_score']:.4f}")
            print(f"    content: {my_hit['content'][:60]}…")
        else:
            print(f"  ✗ your chunk did NOT appear in top-3 (check: {chunk_id})")
        print()
        for i, r in enumerate(results, 1):
            marker = " ← YOURS" if r["chunk_id"] == chunk_id else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{marker}")
            print(f"      chunk_id: {r['chunk_id']}")
        print()

        # [4] Read it back via a semantic paraphrase.
        #     The vector lane uses BAAI/bge-small-zh-v1.5 embeddings; even
        #     if the words differ, vec0 cosine similarity returns a hit.
        print("[recall] query = 'mnelo 是啥' (semantic paraphrase):")
        results = m.recall("mnelo 是啥", top_k=3)
        my_hit = next((r for r in results if r["chunk_id"] == chunk_id), None)
        if my_hit:
            print(f"  ✓ YOUR chunk at rank {results.index(my_hit) + 1}/{len(results)}")
            print(f"    method: {my_hit['method']}, score: {my_hit['rrf_score']:.4f}")
        for i, r in enumerate(results, 1):
            marker = " ← YOURS" if r["chunk_id"] == chunk_id else ""
            print(f"  [{i}] method={r['method']:8s} score={r['rrf_score']:.4f}{marker}")
        print()

        # [5] Inspect what's in the DB directly (educational).
        print("[stats] chunk + entity + vector counts:")
        stats = m.stats()
        for tbl, s in stats.items():
            if isinstance(s, dict):
                print(f"  {tbl:12s}: total={s['total']:4d} active={s['active']:4d}")
            else:
                print(f"  {tbl:12s}: {s}")

        print("\n✓ done.")
        return 0
    finally:
        _cleanup(m)
        m.close()


def _cleanup(m: Memory) -> None:
    """Remove chunks + vectors created by this example."""
    rows = m._conn.execute("SELECT rowid FROM chunks WHERE source LIKE 'example_01:%'").fetchall()
    if rows:
        rowids = [r["rowid"] for r in rows]
        placeholders = ",".join("?" * len(rowids))
        try:
            m._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
        except Exception:
            pass  # vec0 DELETE may fail on missing rows; that's OK
    m._conn.execute("DELETE FROM chunks WHERE source LIKE 'example_01:%'")
    m._conn.execute("DELETE FROM entities WHERE source LIKE 'example_01:%'")
    m._conn.commit()


if __name__ == "__main__":
    sys.exit(main())
