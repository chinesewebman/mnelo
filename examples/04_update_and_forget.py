#!/usr/bin/env python3
"""
Example 04 — update() and forget() (vector lifecycle).

Demonstrates the soft-delete + recreate pattern:
  1. remember() a chunk → vector is created in vec0
  2. update() the chunk → creates a new chunk version + supersedes old
                       → old chunk's vector is DELETED (write-time cleanup)
                       → new chunk's vector is INSERTED (so vector recall works)
  3. forget() the chunk → soft-delete (valid_until=now) + vector is DELETED
  4. verify no drift: vectors count = chunks count

Why this matters:
  - Vector drift (orphan vectors in vec0) is the #1 operational issue with
    sqlite-vec. mnelo prevents it by cleaning up at write time.
  - scripts/maintain_vectors.py exists for backfill on pre-v0.5.6 databases.

What you'll see:
  - 1 chunk written
  - 1 vector created
  - update() leaves 1 chunk + 1 vector (no drift!)
  - forget() leaves 0 chunks + 0 vectors (clean state)
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
        print("=== Example 04: update() + forget() + vector lifecycle ===\n")

        # [1] Remember a chunk
        print("[1] remember()")
        cid_v1 = m.remember(
            content="example_04_uniq_abc version 1 — first iteration of a memory.",
            source="example_04:versioned",
            importance=0.5,
        )
        print(f"  chunk_id: {cid_v1}")
        _print_vector_state(m, "after remember")

        # [2] Update to v2 — creates a new chunk + supersedes v1.
        # The OLD chunk's vector is deleted; the NEW chunk's vector is inserted.
        print("\n[2] update() v1 → v2")
        cid_v2 = m.update(
            cid_v1,
            reason="example_04_update",
            new_content="example_04_uniq_abc version 2 — refined iteration of a memory.",
        )
        print(f"  new chunk_id: {cid_v2}")
        _print_vector_state(m, "after update (should be 1 chunk + 1 vector, no drift)")

        # [3] Update to v3 — same again.
        print("\n[3] update() v2 → v3")
        cid_v3 = m.update(
            cid_v2,
            reason="example_04_update",
            new_content="example_04_uniq_abc version 3 — final iteration of a memory.",
        )
        print(f"  new chunk_id: {cid_v3}")
        _print_vector_state(m, "after update v3")

        # [4] Verify v1 and v2 are soft-deleted (valid_until IS NOT NULL).
        print("\n[4] verify version history:")
        rows = m._conn.execute(
            "SELECT id, valid_until, content, source FROM chunks "
            "WHERE source = 'example_04:versioned' OR source LIKE 'update:example_04_update%' "
            "ORDER BY rowid"
        ).fetchall()
        for r in rows:
            status = "ACTIVE" if r["valid_until"] is None else "superseded"
            print(f"  {r[0]:50s} [{status}] (source={r[3]})")
            print(f"    {r[2][:70]}…")

        # [5] Forget the current version (v3).
        print("\n[5] forget(v3)")
        result = m.forget(cid_v3, target_kind="chunk", reason="example_04_done")
        print(f"  result: {result}")
        _print_vector_state(m, "after forget (should be 0 chunks + 0 vectors)")

        # [6] Verify orphan vector cleanup runs (maintain_vectors.py is the
        # backfill tool for legacy DBs).
        print("\n[6] maintain_vectors.py — dry-run check:")
        mv_result = m.cleanup_orphan_vectors(dry_run=True)
        print(f"  soft-deleted to clean: {mv_result['soft_deleted_cleaned']}")
        print(f"  truly orphan to clean: {mv_result['truly_orphan_cleaned']}")
        print(f"  vectors remaining:     {mv_result['vectors_remaining']}")

        print("\n✓ done.")
        return 0
    finally:
        _cleanup(m)
        m.close()


def _print_vector_state(m: Memory, label: str) -> None:
    """Print current chunks / vectors count for this example."""
    # v0.5.6 update() sets source='update:<reason>' on the new chunk,
    # so we need to match both 'example_04:versioned' (v1) and 'update:example_04_update' (v2+).
    chunks = m._conn.execute(
        "SELECT count(*) FROM chunks WHERE source = 'example_04:versioned' OR source LIKE 'update:example_04_update%'"
    ).fetchone()[0]
    active_chunks = m._conn.execute(
        "SELECT count(*) FROM chunks "
        "WHERE (source = 'example_04:versioned' OR source LIKE 'update:example_04_update%') "
        "  AND valid_until IS NULL"
    ).fetchone()[0]
    # Vectors: join on rowid with chunks (since vec0 is keyed on chunks.rowid)
    vectors_for_my_chunks = m._conn.execute(
        "SELECT count(*) FROM vectors v "
        "JOIN chunks c ON c.rowid = v.rowid "
        "WHERE c.source = 'example_04:versioned' OR c.source LIKE 'update:example_04_update%'"
    ).fetchone()[0]
    drift = vectors_for_my_chunks - active_chunks
    print(f"  chunks (total/active): {chunks}/{active_chunks}")
    print(f"  vectors matching:      {vectors_for_my_chunks}")
    print(f"  drift (vecs - active): {drift}  ({label})")


def _cleanup(m: Memory) -> None:
    """Hard-delete everything created by this example."""
    chunk_rows = m._conn.execute(
        "SELECT rowid FROM chunks WHERE source LIKE 'example_04:%' OR source LIKE 'update:example_04_update%'"
    ).fetchall()
    if chunk_rows:
        rowids = [r["rowid"] for r in chunk_rows]
        placeholders = ",".join("?" * len(rowids))
        try:
            m._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
        except Exception:
            pass
    m._conn.execute("DELETE FROM chunks WHERE source LIKE 'example_04:%'")
    m._conn.execute("DELETE FROM chunks WHERE source LIKE 'update:example_04_update%'")
    m._conn.execute("DELETE FROM entities WHERE source LIKE 'example_04:%'")
    m._conn.commit()


if __name__ == "__main__":
    sys.exit(main())
