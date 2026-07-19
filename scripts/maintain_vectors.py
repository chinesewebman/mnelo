#!/usr/bin/env python3
"""
maintain_vectors.py — vec0 cleanup maintenance tool.

[7/19 v0.5.6] Runs Memory.cleanup_orphan_vectors() on the LIVE DB.
Removes two categories of wasted storage:
  1. Vectors for soft-deleted chunks (valid_until IS NOT NULL) — these
     are filtered out by recall so they serve no purpose.
  2. Truly orphan vectors (vec0 rowid doesn't match any chunks rowid) —
     from crashed inserts or manual SQL.

Usage:
  python scripts/maintain_vectors.py              # cleanup (with confirmation)
  python scripts/maintain_vectors.py --dry-run    # show what would be deleted
  python scripts/maintain_vectors.py --yes        # skip confirmation prompt
  python scripts/maintain_vectors.py --json       # machine-readable output

Exit codes:
  0 = success
  1 = cleanup error
  2 = user cancelled

Why this script (not just `Memory.cleanup_orphan_vectors()` in code):
- Operators want a "show me the count" mode before destructive deletes
- Cron / launchd can call it directly without writing Python glue
- Output is friendly for both humans (table) and automation (--json)
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mnelo vec0 cleanup — remove orphan vectors (soft-deleted chunks + truly orphan rowids)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show counts without deleting (default: False)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip confirmation prompt (default: False)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="output JSON instead of human-readable table (default: False)",
    )
    args = parser.parse_args()

    from memory import Memory

    memory = Memory()
    try:
        if args.dry_run:
            result = memory.cleanup_orphan_vectors(dry_run=True)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print("=== vec0 cleanup DRY RUN (no changes) ===")
                print(f"  soft-deleted chunks (vectors to remove): {result['soft_deleted_cleaned']}")
                print(f"  truly orphan vectors (to remove):       {result['truly_orphan_cleaned']}")
                print(f"  vectors remaining:                      {result['vectors_remaining']}")
                print()
                print(f"  Total would-be-deletions: {result['soft_deleted_cleaned'] + result['truly_orphan_cleaned']}")
            return 0

        # Real run with confirmation
        dry = memory.cleanup_orphan_vectors(dry_run=True)
        if (dry["soft_deleted_cleaned"] + dry["truly_orphan_cleaned"]) == 0:
            if args.json:
                print(json.dumps({"status": "clean", "cleaned": 0}))
            else:
                print("✓ vec0 is clean — nothing to delete.")
            return 0

        if not args.yes:
            print("=== vec0 cleanup plan ===")
            print(f"  soft-deleted chunks: {dry['soft_deleted_cleaned']} vectors")
            print(f"  truly orphan vectors: {dry['truly_orphan_cleaned']} vectors")
            print(f"  Total to delete: {dry['soft_deleted_cleaned'] + dry['truly_orphan_cleaned']}")
            print()
            resp = input("Proceed? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                print("Cancelled.")
                return 2

        result = memory.cleanup_orphan_vectors()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print("=== vec0 cleanup RESULT ===")
            print(f"  soft-deleted chunks cleaned: {result['soft_deleted_cleaned']}")
            print(f"  truly orphan vectors cleaned: {result['truly_orphan_cleaned']}")
            print(f"  vectors remaining:           {result['vectors_remaining']}")
            print(
                f"  reduction:                   {result['soft_deleted_cleaned'] + result['truly_orphan_cleaned']} vectors freed"
            )
        return 0
    except Exception as e:
        print(f"✗ cleanup failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1
    finally:
        memory.close()


if __name__ == "__main__":
    sys.exit(main())
