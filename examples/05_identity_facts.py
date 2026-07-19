#!/usr/bin/env python3
"""
Example 05 — identity_fact_manager.py CLI walkthrough.

Demonstrates the operational CLI for managing identity_fact entities
(display_name, github_handle, lives_in, timezone, telegram_handle,
working_lang, profession, role).

This example shells out to the CLI (subprocess) so you see exactly what an
operator would see.

What you'll see:
  - list: enumerate current facts
  - add:  create a profession fact (auto-linked to master entity)
  - show: look up the new fact
  - remove: soft-delete the fact
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "identity_fact_manager.py"


def run(args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run identity_fact_manager.py with given args."""
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
    )


def extract_json(output: str) -> dict:
    """Find first valid JSON object in output (skip log lines)."""
    candidates = [i for i, c in enumerate(output) if c == "{"]
    for start in candidates:
        try:
            return json.loads(output[start:])
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no JSON found: {output[:200]!r}")


def main() -> int:
    # Add repo to path for memory import (in main() not at top, to keep
    # the script's importable surface clean).
    sys.path.insert(0, str(REPO))

    print("=== Example 05: identity_fact_manager.py CLI ===\n")

    # Pre-clean any leftover example_05 facts
    from memory import Memory

    m = Memory()
    m._conn.execute(
        "UPDATE entities SET valid_until = datetime('now') WHERE id = 'identity:profession:example_05_value'"
    )
    m._conn.execute(
        "UPDATE relations SET valid_until = datetime('now') "
        "WHERE (source_id = 'identity:profession:example_05_value' "
        "       OR target_id = 'identity:profession:example_05_value') "
        "  AND valid_until IS NULL"
    )
    m._conn.commit()
    m.close()

    try:
        # [1] list — show current state of identity_facts.
        print("[1] list active identity_facts:")
        result = run(["list"])
        # Print only the human-readable table (skip embedder logs)
        if "identity_fact list" in result.stdout:
            table_start = result.stdout.index("identity_fact list")
            print(result.stdout[table_start:].rstrip())
        print()

        # [2] add — create a profession fact with a unique value.
        print("[2] add profession = 'example_05_value':")
        result = run(
            [
                "add",
                "--predicate",
                "profession",
                "--value",
                "example_05_value",
                "--importance",
                "0.85",
            ]
        )
        if "identity_fact" in result.stdout:
            table_start = result.stdout.index("✓")
            print(result.stdout[table_start:].rstrip())
        else:
            print(result.stdout)
        print()

        # [3] show — look up the new fact.
        print("[3] show --predicate profession:")
        result = run(["show", "--predicate", "profession", "--value", "example_05_value"])
        if "identity_fact ===" in result.stdout:
            table_start = result.stdout.index("identity_fact ===")
            print(result.stdout[table_start:].rstrip())
        else:
            print(result.stdout)
        print()

        # [4] list — see it now in the list.
        print("[4] list (after add):")
        result = run(["list"])
        if "identity_fact list" in result.stdout:
            table_start = result.stdout.index("identity_fact list")
            print(result.stdout[table_start:].rstrip())
        print()

        # [5] JSON output — for cron / monitoring scripts.
        print("[5] list --json (programmatic consumption):")
        result = run(["list", "--json"])
        data = extract_json(result.stdout)
        print(f"  predicates: {data['predicates']}")
        print(f"  count:      {data['count']}")
        # Find our fact
        my_fact = next(
            (f for f in data["facts"] if f["value"] == "example_05_value"),
            None,
        )
        if my_fact:
            print(f"  ✓ found: {my_fact}")
        else:
            print(f"  ✗ NOT found")
        print()

        # [6] remove — soft-delete.
        print("[6] remove --id identity:profession:example_05_value -y:")
        result = run(
            [
                "remove",
                "--id",
                "identity:profession:example_05_value",
                "-y",
            ]
        )
        if "soft_deleted" in result.stdout:
            table_start = result.stdout.index("✓")
            print(result.stdout[table_start:].rstrip())
        print()

        # [7] Verify it's gone from list.
        print("[7] list (after remove):")
        result = run(["list"])
        if "identity_fact list" in result.stdout:
            table_start = result.stdout.index("identity_fact list")
            print(result.stdout[table_start:].rstrip())

        print("\n✓ done.")
        return 0
    finally:
        # Hard cleanup: soft-delete + remove from purged_queue (we don't
        # want example data lingering in production DB).
        m = Memory()
        m._conn.execute(
            "DELETE FROM chunks WHERE source = 'identity_fact_manager' AND content LIKE '%example_05_value%'"
        )
        m._conn.execute(
            "UPDATE entities SET valid_until = datetime('now') WHERE id = 'identity:profession:example_05_value'"
        )
        m._conn.execute("DELETE FROM entities WHERE id = 'identity:profession:example_05_value'")
        m._conn.execute(
            "DELETE FROM relations WHERE source_id = 'identity:profession:example_05_value' "
            "OR target_id = 'identity:profession:example_05_value'"
        )
        m._conn.execute("DELETE FROM purged_queue WHERE target_id = 'identity:profession:example_05_value'")
        m._conn.commit()
        m.close()


if __name__ == "__main__":
    sys.exit(main())
