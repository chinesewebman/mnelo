#!/usr/bin/env /Users/apple/hermes-agent/venv/bin/python3
"""
cleanup_test_fixtures.py — soft-delete test-fixture entities/chunks that
pollute production memory and degrade recall quality.

[2026-08-29 created] Use when recall surfaces repeated test entity hits
(e.g. host:test_crud_*_sh600089) for production queries. These are leftovers
from CI test runs that didn't clean up after themselves.

Matches by ID/source prefix patterns:
  - source starts with 'test_cov', 'update:test_', 'identity_fact_manager',
    'test:e2e_', 'upgrade-log:', 'r8_'
  - id starts with 'chunk:e2e-', 'host:test_crud', 'master_*' (only if
    memory_type is 'ephemeral' or importance < 0.3 — protect real masters)

This script:
  1. Finds test-fixture entities + chunks via SQL filter
  2. For each: UPDATE entities.valid_until = now() (soft delete)
  3. For chunks: also UPDATE valid_until
  4. Add to purged_queue with 30-day grace (matches standard cleanup)
  5. Writes audit_log entry per forget (pass_name='forced_forget',
    action_type='explicit_softdelete')

SAFETY: dry-run by default. Use --yes to actually delete.
Designed to be re-runnable (idempotent — already-deleted targets are skipped).

Usage:
  python3 scripts/cleanup_test_fixtures.py                    # dry-run, show what would be deleted
  python3 scripts/cleanup_test_fixtures.py --yes            # actually delete
  python3 scripts/cleanup_test_fixtures.py --limit 100      # only first 100 (testing)
  python3 scripts/cleanup_test_fixtures.py --json            # machine-readable output

Exit codes:
  0 = success (or dry-run completed)
  1 = script error
  2 = user cancelled
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# Source-prefix patterns that indicate test fixtures (NOT real production data)
# Each pattern is matched as: source LIKE 'pattern%'
TEST_SOURCE_PATTERNS = [
    "test_cov",  # round6 test coverage work
    "test:e2e_",  # end-to-end test logs
    "update:test_",  # test_update fixtures
    "identity_fact_manager",  # identity_fact_manager test runs (lots of duplicate rows)
    "upgrade-log:",  # one-off upgrade log entries
    "r8_",  # r8 test pattern
]

# ID-prefix patterns
TEST_ID_PATTERNS = [
    "chunk:e2e-",  # e2e chunk fixtures
    "host:test_crud",  # test CRUD entity hosts
    "loop:m5-forget-",  # m5 forget-loop test fixtures (persistent since 8/24)
    "loop:e2e-",  # e2e loop test fixtures (persistent since 8/24)
    "loop:m5-",  # m5-* test loops
]

# ID-prefix patterns that ONLY match if kind matches expected test kind
# (Protects real masters that happen to use a similar prefix)
TEST_ID_KIND_PATTERNS = [
    # e2e task fixtures: any date prefix, but specifically the 'e2e-' component
    # (real production tasks don't typically have 'e2e' in their ID)
    ("task:%e2e-%", "task"),
    ("task:%-e2e-%", "task"),  # date-e2e-name format
]


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def find_test_fixtures(conn) -> dict:
    """Return counts of test-fixture entities and chunks that would be cleaned."""
    # Build WHERE clause for sources
    src_clauses = " OR ".join(["source LIKE ?" for _ in TEST_SOURCE_PATTERNS])
    src_params = [f"{p}%" for p in TEST_SOURCE_PATTERNS]

    # Active entities (valid_until IS NULL) matching test patterns
    entity_query = f"""
        SELECT id, kind, name, importance, source
        FROM entities
        WHERE valid_until IS NULL
          AND ({src_clauses})
        ORDER BY id
    """
    entities = conn.execute(entity_query, src_params).fetchall()

    # Active chunks matching test patterns
    chunk_query = f"""
        SELECT id, source, importance, memory_type
        FROM chunks
        WHERE valid_until IS NULL
          AND ({src_clauses})
        ORDER BY id
    """
    chunks = conn.execute(chunk_query, src_params).fetchall()

    # Also catch id-based patterns (host:test_crud, chunk:e2e-)
    id_clauses = " OR ".join(["id LIKE ?" for _ in TEST_ID_PATTERNS])
    id_params = [f"{p}%" for p in TEST_ID_PATTERNS]
    more_entities = (
        conn.execute(
            f"""
        SELECT id, kind, name, importance, source
        FROM entities
        WHERE valid_until IS NULL
          AND ({id_clauses})
          AND id NOT IN ({",".join("?" * len(entities))})
    """,
            id_params + [e[0] for e in entities],
        ).fetchall()
        if entities
        else conn.execute(
            f"""
        SELECT id, kind, name, importance, source
        FROM entities
        WHERE valid_until IS NULL
          AND ({id_clauses})
    """,
            id_params,
        ).fetchall()
    )

    more_chunks = (
        conn.execute(
            f"""
        SELECT id, source, importance, memory_type
        FROM chunks
        WHERE valid_until IS NULL
          AND ({id_clauses})
          AND id NOT IN ({",".join("?" * len(chunks))})
    """,
            id_params + [c[0] for c in chunks],
        ).fetchall()
        if chunks
        else conn.execute(
            f"""
        SELECT id, source, importance, memory_type
        FROM chunks
        WHERE valid_until IS NULL
          AND ({id_clauses})
    """,
            id_params,
        ).fetchall()
    )

    entities.extend(more_entities)
    chunks.extend(more_chunks)

    # Kind-gated patterns (only match if id pattern AND kind match)
    if TEST_ID_KIND_PATTERNS:
        kind_clauses = " OR ".join(["(id LIKE ? AND kind = ?)" for _ in TEST_ID_KIND_PATTERNS])
        kind_params = []
        for pat, kind in TEST_ID_KIND_PATTERNS:
            kind_params.extend([f"{pat}%", kind])
        more_kind_entities = (
            conn.execute(
                f"""
            SELECT id, kind, name, importance, source
            FROM entities
            WHERE valid_until IS NULL
              AND ({kind_clauses})
              AND id NOT IN ({",".join("?" * len(entities))})
        """,
                kind_params + [e[0] for e in entities],
            ).fetchall()
            if entities
            else conn.execute(
                f"""
            SELECT id, kind, name, importance, source
            FROM entities
            WHERE valid_until IS NULL
              AND ({kind_clauses})
        """,
                kind_params,
            ).fetchall()
        )
        entities.extend(more_kind_entities)

    return {
        "entities": [{"id": e[0], "kind": e[1], "name": e[2], "importance": e[3], "source": e[4]} for e in entities],
        "chunks": [{"id": c[0], "source": c[1], "importance": c[2], "memory_type": c[3]} for c in chunks],
    }


def forget_entity(conn, entity_id: str, now_ts: str) -> None:
    """Soft-delete entity, cascade to relations, write audit_log."""
    # Already soft-deleted? skip
    existing = conn.execute("SELECT 1 FROM purged_queue WHERE target_id=? AND done=0 LIMIT 1", (entity_id,)).fetchone()
    if existing:
        return

    # Soft-delete entity row
    conn.execute("UPDATE entities SET valid_until=? WHERE id=? AND valid_until IS NULL", (now_ts, entity_id))

    # Cascade to relations referencing this entity
    conn.execute(
        """
        UPDATE relations SET valid_until=?
        WHERE (source_id=? OR target_id=?) AND valid_until IS NULL
    """,
        (now_ts, entity_id, entity_id),
    )

    # Add to purged_queue (30-day grace)
    conn.execute("INSERT INTO purged_queue (target_id, target_kind, purged_at, done) VALUES (?, 'entity', ?, 0)", (entity_id, now_ts))

    # Audit log
    conn.execute(
        """
        INSERT INTO audit_log (
            run_id, pass_name, action_type, ref_type, ref_id,
            before_json, after_json, confidence, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            f"test_fixture_cleanup-{now_ts}",
            "forced_forget",
            "explicit_softdelete",
            "entity",
            entity_id,
            json.dumps({"source": "test_fixture"}),
            json.dumps({"reason": "test-fixture cleanup 2026-08-29", "forgotten_at": now_ts}),
            1.0,
            "applied",
            now_ts,
        ),
    )


def forget_chunk(conn, chunk_id: str, now_ts: str) -> None:
    """Soft-delete chunk (no cascade needed for chunks — relations point at entities)."""
    existing = conn.execute("SELECT 1 FROM purged_queue WHERE target_id=? AND done=0 LIMIT 1", (chunk_id,)).fetchone()
    if existing:
        return

    conn.execute("UPDATE chunks SET valid_until=? WHERE id=? AND valid_until IS NULL", (now_ts, chunk_id))

    conn.execute("INSERT INTO purged_queue (target_id, target_kind, purged_at, done) VALUES (?, 'chunk', ?, 0)", (chunk_id, now_ts))

    conn.execute(
        """
        INSERT INTO audit_log (
            run_id, pass_name, action_type, ref_type, ref_id,
            before_json, after_json, confidence, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            f"test_fixture_cleanup-{now_ts}",
            "forced_forget",
            "explicit_softdelete",
            "chunk",
            chunk_id,
            json.dumps({"source": "test_fixture"}),
            json.dumps({"reason": "test-fixture cleanup 2026-08-29", "forgotten_at": now_ts}),
            1.0,
            "applied",
            now_ts,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mnelo test-fixture cleanup — soft-delete leftover test entities/chunks.",
    )
    parser.add_argument("--dry-run", action="store_true", help="(default) show what would be deleted")
    parser.add_argument("--yes", "-y", action="store_true", help="actually delete (skips confirmation)")
    parser.add_argument("--limit", type=int, default=None, help="only process first N entities+chunks")
    parser.add_argument("--json", dest="as_json", action="store_true", help="JSON output")
    args = parser.parse_args()

    # Default to dry-run unless --yes is explicitly passed
    is_dry_run = not args.yes

    from config import config as _cfg

    DB_PATH = Path(_cfg.db_path)
    conn = sqlite3.connect(str(DB_PATH))
    conn.create_function("iso_now", 0, iso_now)

    fixtures = find_test_fixtures(conn)
    entities = fixtures["entities"]
    chunks = fixtures["chunks"]

    if args.limit:
        entities = entities[: args.limit]
        chunks = chunks[: args.limit]

    summary = {
        "dry_run": is_dry_run,
        "entities_to_clean": len(entities),
        "chunks_to_clean": len(chunks),
        "patterns": TEST_SOURCE_PATTERNS + TEST_ID_PATTERNS + [f"{p}+{k}" for p, k in TEST_ID_KIND_PATTERNS],
    }

    if is_dry_run:
        if args.as_json:
            print(json.dumps(summary, indent=2))
        else:
            print("=== test fixture cleanup DRY RUN (no changes) ===")
            print(f"  entity patterns: {TEST_SOURCE_PATTERNS}")
            print(f"  id patterns:     {TEST_ID_PATTERNS}")
            print(f"  id+kind patterns:{TEST_ID_KIND_PATTERNS}")
            print(f"  entities to clean: {len(entities)}")
            print(f"  chunks to clean:   {len(chunks)}")
            if entities:
                print("\n  first 10 entities:")
                for e in entities[:10]:
                    print(f"    - {e['id']:50}  kind={e['kind']:20}  source={e['source']}")
                if len(entities) > 10:
                    print(f"    ... and {len(entities) - 10} more")
            if chunks:
                print("\n  first 10 chunks:")
                for c in chunks[:10]:
                    print(f"    - {c['id']:50}  source={c['source']:30}  type={c['memory_type']}")
                if len(chunks) > 10:
                    print(f"    ... and {len(chunks) - 10} more")
        conn.close()
        return 0

    # Confirmation
    if not args.yes:
        print("=== test fixture cleanup plan ===")
        print(f"  entities: {len(entities)}")
        print(f"  chunks:   {len(chunks)}")
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Cancelled.")
            return 2

    print(f"Cleaning {len(entities)} entities + {len(chunks)} chunks...")
    t0 = time.time()
    now_ts = iso_now()
    ent_ok = 0
    chunk_ok = 0
    errors = []

    for e in entities:
        try:
            forget_entity(conn, e["id"], now_ts)
            ent_ok += 1
        except Exception as ex:
            errors.append({"target_id": e["id"], "error": str(ex)})

    for c in chunks:
        try:
            forget_chunk(conn, c["id"], now_ts)
            chunk_ok += 1
        except Exception as ex:
            errors.append({"target_id": c["id"], "error": str(ex)})

    conn.commit()
    elapsed = time.time() - t0

    print()
    print("=== test fixture cleanup RESULT ===")
    print(f"  entities cleaned: {ent_ok}/{len(entities)}")
    print(f"  chunks cleaned:   {chunk_ok}/{len(chunks)}")
    print(f"  elapsed:          {elapsed:.1f}s")
    print()
    print("⚠ NOTE: search index will be stale (orphan vectors will reference deleted chunks).")
    print("  Run scripts/rebuild_vectors.py after this to refresh the vector index.")

    conn.close()

    if errors:
        print(f"\n{len(errors)} errors (first 5):")
        for e in errors[:5]:
            print(f"  - {e['target_id']}: {e['error'][:100]}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
