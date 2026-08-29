#!/usr/bin/env /Users/apple/hermes-agent/venv/bin/python3
"""
rebuild_vectors.py — backfill missing vectors into the search index.

[2026-08-29 created] Use when active chunks in DB exceed vectors in search index
(memory_stats reports vectors < chunks.active). Common causes:
  - Search index was rebuilt but writes after the rebuild didn't re-add (orphan drift)
  - Embedder was unavailable during a batch of writes (e.g. test runs without model)
  - Backend migration (zvec → usearch or vice versa) without re-embedding

This script:
  1. Finds active chunks without vectors in the index
  2. Embeds them via the current embedder (BAAI/bge-small-zh-v1.5)
  3. Adds to the search index (zvec or usearch, whichever the server uses)
  4. Writes to memory_stats: vectors should equal active chunks

⚠️ CRITICAL: This script bypasses the running MCP server's search-index LOCK.
The MCP server holds a write-lock on zvec collection / usearch index file.
Running this against the LIVE DB while the server is running will:
  - usearch: race condition (file-based, may corrupt index → .corrupt-NNN backup)
  - zvec: "Can't lock read-write collection" RuntimeError

WORKFLOW:
  1. Stop MCP server: launchctl unload ~/Library/LaunchAgents/ai.mnelo.mcp.plist
  2. Run this script: python3 scripts/rebuild_vectors.py --yes
  3. Start MCP server: launchctl load ~/Library/LaunchAgents/ai.mnelo.mcp.plist
  4. Verify: curl ... memory_stats → vectors should equal chunks.active

Usage:
  python3 scripts/rebuild_vectors.py --dry-run       # show what would be added
  python3 scripts/rebuild_vectors.py --yes           # actually rebuild (requires server stopped)
  python3 scripts/rebuild_vectors.py --yes --limit 50   # only first 50 (testing)
  python3 scripts/rebuild_vectors.py --yes --skip-contains   # re-add all (idempotent overwrite)
  python3 scripts/rebuild_vectors.py --json         # machine-readable output

Exit codes:
  0 = success (or dry-run completed)
  1 = rebuild error (see stderr)
  2 = server still running (refused to avoid index corruption)
"""

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def check_server_running(port: int = 8086) -> bool:
    """Refuse to run if MCP server is alive (would corrupt the index)."""
    import socket

    try:
        s = socket.socket()
        s.settimeout(1)
        rc = s.connect_ex(("127.0.0.1", port))
        s.close()
        return rc == 0
    except Exception:
        return False


def find_missing_chunks(conn, backend: str, dry_run: bool = False) -> list:
    """Return active chunks (valid_until IS NULL) not in the search index.

    ⚠️ zvec 0.6.0 API LIMITATION: Collection has no iter_all() / docs() method.
       Only col.stats.doc_count gives the count, not the IDs. So we use a
       count-vs-active approach: if stats.doc_count == total_active, assume
       all are present; if less, return ALL active chunks (the rebuild is
       idempotent via upsert). The deployed search_index.py has the same
       silent-fail bug (try/except on iter_all that doesn't exist).

    Returns [{chunk_id, rowid, content, source, memory_type, importance}, ...]
    """
    # Get all active chunks
    rows = conn.execute("""
        SELECT id, rowid, content, source, memory_type, importance, timestamp
        FROM chunks
        WHERE valid_until IS NULL
        ORDER BY timestamp DESC
    """).fetchall()

    all_active = [
        {
            "chunk_id": r[0],
            "rowid": int(r[1]),
            "content": r[2],
            "source": r[3] or "",
            "memory_type": r[4] or "fact",
            "importance": r[5],
            "timestamp": r[6],
        }
        for r in rows
    ]
    total_active = len(all_active)

    # Get current doc count from the index (no ID enumeration possible)
    indexed_count = 0
    if backend == "zvec":
        try:
            import zvec

            col_path = REPO / "memory.search_index.zv"
            col = zvec.open(str(col_path))
            indexed_count = int(col.stats.doc_count)
        except RuntimeError as e:
            if "Can't lock" in str(e):
                # In dry-run, server is expected to be running — fall back to
                # memory_stats via MCP. In real run, this is fatal.
                if dry_run:
                    # Can't open zvec (locked by MCP server, expected in dry-run)
                    # Best-effort fallback: assume backend may be empty or partial
                    # Report based on total_active vs a heuristic — user can
                    # stop server and re-run for exact count.
                    indexed_count = 0
                    print("⚠ cannot read zvec doc_count (server locked).", file=sys.stderr)
                    print("  For exact count: stop server, re-run dry-run.", file=sys.stderr)
                else:
                    print("✗ zvec collection is locked — likely the MCP server is running.", file=sys.stderr)
                    print("  Stop it first: launchctl unload ~/Library/LaunchAgents/ai.mnelo.mcp.plist", file=sys.stderr)
                    sys.exit(2)
            else:
                raise
        except (AttributeError, TypeError):
            # No stats property; assume all missing (rebuild from scratch)
            indexed_count = 0
    elif backend == "usearch":
        # usearch has Index.keys() — list of rowids
        try:
            from usearch.index import Index

            idx = Index(ndim=512, metric="cosine")
            idx_path = REPO / "memory.usearch.index"
            if idx_path.exists():
                idx.load(str(idx_path))
                indexed_count = len(idx.keys)
        except Exception:
            # Can't read usearch directly; fall back to Memory init
            from memory import Memory

            mem = Memory()
            for row in rows:
                if mem._index.contains(row[0], conn=conn):
                    indexed_count += 1
            mem.close()

    # If counts match, all are indexed
    if indexed_count >= total_active:
        return []
    # If less, return ALL active chunks for rebuild (idempotent)
    return all_active


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mnelo vector index rebuild — backfill missing vectors for active chunks.",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would be added (no changes)")
    parser.add_argument("--yes", "-y", action="store_true", help="skip confirmation prompt")
    parser.add_argument("--limit", type=int, default=None, help="only process first N chunks (testing)")
    parser.add_argument("--skip-contains", action="store_true", help="re-add ALL active chunks (overwrite existing vectors)")
    parser.add_argument("--json", dest="as_json", action="store_true", help="output JSON instead of human-readable table")
    parser.add_argument(
        "--wipe-and-rebuild", action="store_true", help="⚠ DESTRUCTIVE: wipe zvec collection entirely, then rebuild from scratch. Removes orphan vectors (referencing deleted chunks). Requires --yes."
    )
    args = parser.parse_args()

    if not args.dry_run and not args.yes:
        print("✗ Refusing to run without --yes or --dry-run", file=sys.stderr)
        print("  This script modifies the search index. Use --dry-run first.", file=sys.stderr)
        return 1

    # CRITICAL: refuse if server is running (would corrupt index)
    # Skip this check for dry-run since we're not modifying the index
    if not args.dry_run and check_server_running():
        print("✗ MCP server is running on port 8086 — refusing to proceed.", file=sys.stderr)
        print("  Stop it first: launchctl unload ~/Library/LaunchAgents/ai.mnelo.mcp.plist", file=sys.stderr)
        print("  Then restart after rebuild: launchctl load ~/Library/LaunchAgents/ai.mnelo.mcp.plist", file=sys.stderr)
        return 2

    # Direct SQLite access (no Memory() init — that would try to take zvec lock)
    import sqlite3

    from config import config as _cfg

    DB_PATH = Path(_cfg.db_path)

    def iso_now_local():
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    def iso_now_offset(days):
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    conn = sqlite3.connect(str(DB_PATH))
    conn.create_function("iso_now", 0, iso_now_local)
    conn.create_function("iso_now_offset", 1, iso_now_offset)

    # Detect backend
    backend = _cfg.search_backend
    if backend == "auto":
        # Resolve auto
        from search_index import usearch_available, zvec_available

        if zvec_available():
            backend = "zvec"
        elif usearch_available():
            backend = "usearch"
        else:
            print("✗ neither zvec nor usearch available", file=sys.stderr)
            return 1
    if not args.dry_run:
        print(f"detected backend: {backend}")

    # Find missing chunks
    # --wipe-and-rebuild implies --skip-contains (re-add everything after wipe)
    if args.wipe_and_rebuild:
        args.skip_contains = True

    if args.dry_run or args.skip_contains:
        # skip_contains means rebuild ALL active chunks (re-embed)
        # For dry-run, we still want to know what's missing
        missing = find_missing_chunks(conn, backend, dry_run=args.dry_run)
        if args.skip_contains:
            # Re-add everything (covers --skip-contains case)
            from config import config as _cfg

            conn2 = sqlite3.connect(str(_cfg.db_path))
            conn2.create_function("iso_now", 0, iso_now_local)
            conn2.create_function("iso_now_offset", 1, iso_now_offset)
            rows = conn2.execute("""
                SELECT id, rowid, content, source, memory_type, importance, timestamp
                FROM chunks WHERE valid_until IS NULL
                ORDER BY timestamp DESC
            """).fetchall()
            conn2.close()
            all_active = [
                {
                    "chunk_id": r[0],
                    "rowid": int(r[1]),
                    "content": r[2],
                    "source": r[3] or "",
                    "memory_type": r[4] or "fact",
                    "importance": r[5],
                    "timestamp": r[6],
                }
                for r in rows
            ]
            if args.dry_run:
                missing = all_active  # dry-run with skip-contains = show all
            else:
                missing = all_active  # real run with skip-contains = process all
    else:
        missing = find_missing_chunks(conn, backend, dry_run=args.dry_run)

    if args.limit:
        missing = missing[: args.limit]

    total_active = conn.execute("SELECT COUNT(*) FROM chunks WHERE valid_until IS NULL").fetchone()[0]
    currently_indexed = total_active - len(missing) + (0 if args.skip_contains else 0)
    # If --skip-contains, we'll re-add all; report the count we'd index
    will_index_count = len(missing) if not args.skip_contains else total_active

    summary = {
        "backend": backend,
        "total_active_chunks": total_active,
        "currently_indexed": currently_indexed if not args.skip_contains else "unknown (rebuild mode)",
        "missing_count": len(missing) if not args.skip_contains else total_active,
        "will_index": will_index_count,
        "dry_run": args.dry_run,
    }

    if args.dry_run:
        if args.as_json:
            print(json.dumps(summary, indent=2))
        else:
            print("=== vector index DRY RUN (no changes) ===")
            print(f"  backend: {summary['backend']}")
            print(f"  total active chunks: {summary['total_active_chunks']}")
            print(f"  currently indexed: {summary['currently_indexed']}")
            print(f"  missing (need rebuild): {summary['missing_count']}")
            print()
            if missing:
                print("  first 5 missing chunks:")
                for m in missing[:5]:
                    print(f"    - {m['chunk_id']}  source={m['source']}  imp={m['importance']}")
                if len(missing) > 5:
                    print(f"    ... and {len(missing) - 5} more")
        conn.close()
        return 0

    # Confirmation prompt
    if not args.yes:
        print("=== vector index rebuild plan ===")
        print(f"  backend: {summary['backend']}")
        print(f"  active chunks: {summary['total_active_chunks']}")
        print(f"  missing to embed: {summary['missing_count']}")
        if args.skip_contains:
            print(f"  ⚠ --skip-contains: re-embed ALL {summary['total_active_chunks']} active chunks")
        print()
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Cancelled.")
            return 2

    # === REAL REBUILD ===
    if args.wipe_and_rebuild:
        if not args.yes:
            print("✗ --wipe-and-rebuild requires --yes (destructive operation)", file=sys.stderr)
            return 2
        if backend != "zvec":
            print(f"✗ --wipe-and-rebuild only supported for zvec backend (got {backend})", file=sys.stderr)
            return 2
        print("⚠ --wipe-and-rebuild: removing entire zvec collection...")
        import shutil

        col_path = REPO / "memory.search_index.zv"
        if col_path.exists():
            shutil.rmtree(col_path)
            print(f"  removed {col_path}")
        else:
            print(f"  {col_path} not present (clean state)")
        # args.skip_contains was set to True earlier (line 207); missing list is already full

    print(f"Rebuilding {len(missing)} vectors...")
    from embedder import embed_bytes
    from search_index import UsearchIndex, ZvecIndex

    t0 = time.time()
    success = 0
    failed = 0
    errors = []

    # Build the index object (with proper lock)
    if backend == "zvec":
        index_path = REPO / "memory.search_index.zv"
        # dim from config
        dim = _cfg.embedder_dim
        index = ZvecIndex(index_path, dim)
    elif backend == "usearch":
        # UsearchIndex takes db_path (uses {db_path.stem}.usearch.index file)
        index = UsearchIndex(DB_PATH, _cfg.embedder_dim)
    else:
        print(f"✗ unsupported backend: {backend}", file=sys.stderr)
        return 1

    for i, m in enumerate(missing, 1):
        chunk_id = m["chunk_id"]
        content = m["content"]
        try:
            vector_bytes = embed_bytes(content)
            # API differs: zvec's add() takes memory_type + source; usearch's doesn't
            if backend == "zvec":
                index.add(
                    chunk_id=chunk_id,
                    vector_bytes=vector_bytes,
                    conn=conn,
                    content=content,
                    memory_type=m["memory_type"],
                    source=m["source"],
                )
            else:  # usearch
                index.add(
                    chunk_id=chunk_id,
                    vector_bytes=vector_bytes,
                    conn=conn,
                    content=content,
                )
            success += 1
            if i % 10 == 0 or i == len(missing):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed > 0 else 0
                print(f"  {i}/{len(missing)}  ({rate:.1f}/s)  last={chunk_id[:40]}")
        except Exception as e:
            failed += 1
            errors.append({"chunk_id": chunk_id, "error": str(e)})
            print(f"  ✗ {chunk_id}: {e}", file=sys.stderr)

    # Persist (zvec saves on close; usearch needs explicit save)
    try:
        index.close()  # ZvecIndex persists on close
    except Exception as e:
        print(f"⚠ close() error (may have already persisted): {e}", file=sys.stderr)

    elapsed = time.time() - t0
    print()
    print("=== vector index rebuild RESULT ===")
    print(f"  succeeded: {success}")
    print(f"  failed:    {failed}")
    print(f"  elapsed:   {elapsed:.1f}s ({success / elapsed if elapsed > 0 else 0:.1f}/s)")
    print(f"  vectors should now equal active chunks: {total_active}")

    conn.close()

    if errors:
        print(f"\n{len(errors)} errors (first 5):")
        for e in errors[:5]:
            print(f"  - {e['chunk_id']}: {e['error'][:100]}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
