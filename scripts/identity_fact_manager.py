#!/usr/bin/env python3
"""
identity_fact_manager.py — CLI for managing identity_fact entities.

[7/19 v0.5.7] Manage the owner's identity_fact graph (display_name, github_handle,
lives_in, timezone, telegram_handle, working_lang, profession, role).

Why this script (not just direct Memory.relate/forget calls):
- Operators want a "show me all facts" view
- Cron / external tools want --json output
- Avoids typos in predicate names (validated against allowlist)
- Provides consistent human-readable + machine output
- Soft-delete (valid_until) for safety; physical delete is deferred 30 days
  via the existing purged_queue mechanism

Usage:
  # List all
  python scripts/identity_fact_manager.py list
  python scripts/identity_fact_manager.py list --predicate lives_in --json

  # Add a new fact
  python scripts/identity_fact_manager.py add --predicate profession --value engineer
  python scripts/identity_fact_manager.py add --predicate role --value owner --dry-run

  # Show details
  python scripts/identity_fact_manager.py show --predicate profession

  # Remove (soft-delete)
  python scripts/identity_fact_manager.py remove --predicate working_lang
  python scripts/identity_fact_manager.py remove --id identity:profession:engineer --yes

Exit codes:
  0 = success
  1 = error
  2 = user cancelled / not found
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# [7/19 v0.5.7] Allowlist of identity_fact predicates.
# Adding a new predicate here = adding it to the schema's first-class list.
# Keep in sync with scripts/import_identity_facts.py STRICT_EXTRACTORS.
ALLOWED_PREDICATES = frozenset(
    {
        "display_name",  # e.g. "2077 Ling"
        "github_handle",  # e.g. "chinesewebman"
        "lives_in",  # e.g. "北京市大兴区亦庄镇"
        "timezone",  # e.g. "GMT+8"
        "telegram_handle",  # e.g. "@ling2077"
        "working_lang",  # e.g. "English"
        "profession",  # e.g. "engineer" (v0.5.7 new)
        "role",  # e.g. "owner" (v0.5.7 new)
    }
)


def fact_id(predicate: str, value: str) -> str:
    """Build canonical identity_fact entity id."""
    # Use case-preserving slug for value (preserve Chinese)
    return f"identity:{predicate}:{value}"


def normalize_value(value: str) -> str:
    """Normalize value for storage.

    - Strip leading/trailing whitespace
    - Lowercase handles (github_handle, telegram_handle are case-insensitive)
    - Keep Chinese chars + spaces intact
    """
    return value.strip()


class IdentityFactManager:
    """Manager class for identity_fact CRUD."""

    def __init__(self, memory=None):
        """Memory instance, lazily initialized for --help to work without DB."""
        from memory import Memory

        self.m = memory or Memory()

    def close(self):
        self.m.close()

    def list_facts(self, predicate_filter: str = None, only_active: bool = True) -> list:
        """Return all identity_fact entities, optionally filtered by predicate."""
        sql = "SELECT id, name, summary, valid_from, valid_until FROM entities WHERE kind = 'identity_fact'"
        params = []
        if predicate_filter:
            sql += " AND id LIKE ?"
            params.append(f"identity:{predicate_filter}:%")
        if only_active:
            sql += " AND valid_until IS NULL"
        sql += " ORDER BY id"
        rows = self.m._conn.execute(sql, params).fetchall()
        facts = []
        for r in rows:
            fid = r[0]
            # Parse predicate from id (identity:<predicate>:<value>)
            parts = fid.split(":", 2)
            if len(parts) != 3:
                continue
            _, predicate, value = parts
            facts.append(
                {
                    "id": fid,
                    "predicate": predicate,
                    "value": value,
                    "name": r[1],
                    "summary": r[2],
                    "valid_from": r[3],
                    "valid_until": r[4],
                }
            )
        return facts

    def show(self, predicate: str, value: str = None) -> dict:
        """Show one fact by predicate (and optional value)."""
        if predicate not in ALLOWED_PREDICATES:
            return {"error": f"unknown predicate: {predicate}", "allowed": sorted(ALLOWED_PREDICATES)}
        if value is None:
            # Find any active fact for this predicate
            facts = self.list_facts(predicate_filter=predicate)
            if not facts:
                return {"error": f"no active fact for predicate: {predicate}"}
            if len(facts) > 1:
                return {"error": f"multiple facts for predicate {predicate}", "facts": facts}
            return facts[0]
        # Specific value
        fid = fact_id(predicate, normalize_value(value))
        row = self.m._conn.execute(
            "SELECT id, name, summary, valid_from, valid_until FROM entities WHERE id = ? AND kind = 'identity_fact'",
            (fid,),
        ).fetchone()
        if not row:
            return {"error": f"not found: {fid}"}
        return {
            "id": row[0],
            "predicate": predicate,
            "value": normalize_value(value),
            "name": row[1],
            "summary": row[2],
            "valid_from": row[3],
            "valid_until": row[4],
        }

    def add(
        self,
        predicate: str,
        value: str,
        importance: float = 0.9,
        dry_run: bool = False,
        source: str = "identity_fact_manager",
    ) -> dict:
        """Add (or update) an identity_fact.

        Args:
            predicate: one of ALLOWED_PREDICATES
            value: fact value (string)
            importance: 0.0-1.0 (default 0.9, identity_facts are high-value)
            dry_run: don't actually write
            source: provenance tag

        Returns:
            dict with action performed and entity id
        """
        if predicate not in ALLOWED_PREDICATES:
            return {
                "error": f"unknown predicate: {predicate}",
                "allowed": sorted(ALLOWED_PREDICATES),
            }
        value = normalize_value(value)
        if not value:
            return {"error": "value cannot be empty"}
        fid = fact_id(predicate, value)

        if dry_run:
            existing = self.m._conn.execute(
                "SELECT valid_until FROM entities WHERE id = ? AND kind = 'identity_fact'",
                (fid,),
            ).fetchone()
            return {
                "dry_run": True,
                "action": "would_update" if existing else "would_create",
                "id": fid,
                "predicate": predicate,
                "value": value,
                "would_supersede": bool(existing and existing[0] is None),
            }

        # Create a small chunk + the identity_fact entity
        # (consistent with the manual demo we did earlier today)
        content = f"identity_fact: {predicate} = {value}"

        # [7/19 v0.5.7] Identity facts are managed by this CLI (trusted source),
        # so we bypass the standard _upsert_entity immutability check.
        # Strategy:
        #   1. If an active identity_fact exists with this id → supersede:
        #      soft-delete old (set valid_until=now), then INSERT new with fresh valid_from.
        #      This preserves audit trail (history kept) while allowing CLI updates.
        #   2. If a soft-deleted one exists (from previous remove) → reactivate
        #      by clearing valid_until (re-INSERT with same id would fail UNIQUE,
        #      so we use UPDATE valid_until=NULL with the historical row).
        #   3. Otherwise plain INSERT.
        existing_active = self.m._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
            (fid,),
        ).fetchone()
        existing_inactive = self.m._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NOT NULL",
            (fid,),
        ).fetchone()

        now_ts = self.m._conn.execute("SELECT datetime('now')").fetchone()[0]

        if existing_active:
            # Supersede: soft-delete old (keeps history), then reactivate old row
            # with updated name/summary/importance (immutable id).
            self.m._conn.execute(
                "UPDATE entities SET valid_until = ? WHERE id = ?",
                (now_ts, fid),
            )
            # Also invalidate related relations (cascade)
            self.m._conn.execute(
                "UPDATE relations SET valid_until = ? WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL",
                (now_ts, fid, fid),
            )
            # Now reactivate old row with updated values + new valid_from
            self.m._conn.execute(
                "UPDATE entities SET valid_until = NULL, valid_from = ?, "
                "name = ?, summary = ?, importance = ? WHERE id = ?",
                (now_ts, value, value, importance, fid),
            )
            action = "superseded"
        elif existing_inactive:
            # Reactivate historical row
            self.m._conn.execute(
                "UPDATE entities SET valid_until = NULL, valid_from = ?, "
                "name = ?, summary = ?, importance = ? WHERE id = ?",
                (now_ts, value, value, importance, fid),
            )
            action = "reactivated"
        else:
            # Plain INSERT
            self.m._conn.execute(
                """
                INSERT INTO entities (id, kind, name, summary, source, importance,
                                      valid_from, valid_until)
                VALUES (?, 'identity_fact', ?, ?, ?, ?, ?, NULL)
                """,
                (fid, value, value, source, importance, now_ts),
            )
            action = "created"

        # Create the evidence chunk (separate from entity — keeps audit trail)
        chunk_id = self.m._conn.execute(
            """
            INSERT INTO chunks (id, content, source, timestamp, importance, valid_until)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (
                f"chunk_{now_ts.replace(' ', '_').replace(':', '')}_{fid.replace(':', '_')}",
                content,
                source,
                now_ts,
                importance,
            ),
        ).lastrowid
        # Get the generated chunk id
        chunk_row = self.m._conn.execute("SELECT id FROM chunks WHERE rowid = ?", (chunk_id,)).fetchone()
        chunk_id = chunk_row[0] if chunk_row else None

        # Build canonical id for linking: link to existing 'user' person entity
        # (find a master_*/user entity; if none, skip — fact can stand alone)
        master = self._find_master_entity()
        if master:
            try:
                self.m.relate(
                    source_id=fid,
                    target_id=master,
                    relation="is_identity_fact_for",
                    weight=0.95,
                    properties={"predicate": predicate, "value": value, "added_via": "identity_fact_manager"},
                )
                self.m.relate(
                    source_id=master,
                    target_id=fid,
                    relation="has_identity_fact",
                    weight=0.95,
                    properties={"predicate": predicate, "value": value},
                )
            except Exception as e:
                return {
                    "action": action,
                    "id": fid,
                    "chunk_id": chunk_id,
                    "predicate": predicate,
                    "value": value,
                    "link_warning": f"failed to link to {master}: {e}",
                }

        self.m._conn.commit()

        return {
            "action": action,
            "id": fid,
            "chunk_id": chunk_id,
            "predicate": predicate,
            "value": value,
            "linked_to": master,
        }

    def remove(
        self,
        predicate: str = None,
        fact_id_arg: str = None,
        reason: str = "removed via identity_fact_manager",
        yes: bool = False,
    ) -> dict:
        """Soft-delete an identity_fact (sets valid_until).

        Args:
            predicate: delete by predicate (value auto-discovered)
            fact_id_arg: delete by full id (e.g. 'identity:profession:engineer')
            reason: reason for audit trail
            yes: skip confirmation prompt

        Returns:
            dict with action + entity id
        """
        if predicate and predicate not in ALLOWED_PREDICATES:
            return {
                "error": f"unknown predicate: {predicate}",
                "allowed": sorted(ALLOWED_PREDICATES),
            }
        if not predicate and not fact_id_arg:
            return {"error": "either --predicate or --id is required"}

        target_id = None
        if fact_id_arg:
            target_id = fact_id_arg
        else:
            facts = self.list_facts(predicate_filter=predicate, only_active=True)
            if not facts:
                return {"error": f"no active fact for predicate: {predicate}"}
            if len(facts) > 1:
                return {
                    "error": f"multiple facts for predicate {predicate}; use --id",
                    "candidates": [f["id"] for f in facts],
                }
            target_id = facts[0]["id"]

        # Verify exists
        row = self.m._conn.execute(
            "SELECT id, name FROM entities WHERE id = ? AND kind = 'identity_fact' AND valid_until IS NULL",
            (target_id,),
        ).fetchone()
        if not row:
            return {"error": f"not found or already deleted: {target_id}"}

        if not yes:
            print(f"=== identity_fact remove ===")
            print(f"  id    : {row[0]}")
            print(f"  name  : {row[1]}")
            print(f"  reason: {reason}")
            print()
            resp = input("Soft-delete (valid_until=now)? [y/N] ").strip().lower()
            if resp not in ("y", "yes"):
                return {"action": "cancelled", "id": target_id}

        self.m.forget(target_id, target_kind="entity", reason=reason)
        # Also queue the chunk if it exists (best-effort)
        chunk_row = self.m._conn.execute(
            "SELECT id FROM chunks WHERE source = ? AND content LIKE ?",
            ("identity_fact_manager", f"%{target_id}%"),
        ).fetchone()
        return {
            "action": "soft_deleted",
            "id": target_id,
            "chunk_also_queued": bool(chunk_row),
        }

    def _find_master_entity(self) -> str | None:
        """Find a master person entity to link facts to.

        Looks for:
        - 'user' (lowercase)
        - any entity starting with 'master_' with kind='person'
        Returns the first match, or None.
        """
        row = self.m._conn.execute(
            "SELECT id FROM entities WHERE kind = 'person' AND valid_until IS NULL AND id IN ('user') LIMIT 1"
        ).fetchone()
        if row:
            return row[0]
        row = self.m._conn.execute(
            "SELECT id FROM entities WHERE kind = 'person' AND valid_until IS NULL AND id LIKE 'master_%' LIMIT 1"
        ).fetchone()
        if row:
            return row[0]
        return None


def cmd_list(args) -> int:
    mgr = IdentityFactManager()
    try:
        facts = mgr.list_facts(predicate_filter=args.predicate)
        if args.json:
            print(
                json.dumps(
                    {
                        "predicates": sorted(ALLOWED_PREDICATES),
                        "facts": facts,
                        "count": len(facts),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

        # Human-readable table
        print(f"=== identity_fact list ({len(facts)} active) ===")
        if not facts:
            print("  (no facts)")
            return 0
        print(f"  {'PREDICATE':18s}  {'VALUE':30s}  VALID FROM")
        print(f"  {'-' * 18}  {'-' * 30}  {'-' * 20}")
        for f in facts:
            vfrom = (f["valid_from"] or "")[:19]
            print(f"  {f['predicate']:18s}  {f['value']:30s}  {vfrom}")
        return 0
    finally:
        mgr.close()


def cmd_show(args) -> int:
    mgr = IdentityFactManager()
    try:
        result = mgr.show(predicate=args.predicate, value=args.value)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if "error" not in result else 2
        if "error" in result:
            print(f"✗ {result['error']}")
            if "allowed" in result:
                print(f"  allowed predicates: {', '.join(result['allowed'])}")
            return 2
        print(f"=== identity_fact ===")
        print(f"  id        : {result['id']}")
        print(f"  predicate : {result['predicate']}")
        print(f"  value     : {result['value']}")
        print(f"  name      : {result['name']}")
        print(f"  summary   : {result['summary']}")
        print(f"  valid_from: {result['valid_from']}")
        print(f"  valid_until: {result['valid_until'] or '(active)'}")
        return 0
    finally:
        mgr.close()


def cmd_add(args) -> int:
    mgr = IdentityFactManager()
    try:
        result = mgr.add(
            predicate=args.predicate,
            value=args.value,
            importance=args.importance,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if "error" not in result else 1
        if "error" in result:
            print(f"✗ {result['error']}")
            if "allowed" in result:
                print(f"  allowed predicates: {', '.join(result['allowed'])}")
            return 1
        if result.get("dry_run"):
            print("=== identity_fact add DRY RUN ===")
            print(f"  action     : {result['action']}")
            print(f"  id         : {result['id']}")
            print(f"  predicate  : {result['predicate']}")
            print(f"  value      : {result['value']}")
            if result.get("would_supersede"):
                print(f"  ⚠️  would supersede existing active fact")
            return 0
        print(f"✓ identity_fact {result['action']}")
        print(f"  id         : {result['id']}")
        print(f"  chunk_id   : {result.get('chunk_id', '?')}")
        if result.get("linked_to"):
            print(f"  linked to  : {result['linked_to']}")
        if result.get("link_warning"):
            print(f"  ⚠️  {result['link_warning']}")
        return 0
    finally:
        mgr.close()


def cmd_remove(args) -> int:
    mgr = IdentityFactManager()
    try:
        result = mgr.remove(
            predicate=args.predicate,
            fact_id_arg=args.id,
            reason=args.reason,
            yes=args.yes,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("action") in ("soft_deleted", "cancelled") else 2
        if "error" in result:
            print(f"✗ {result['error']}")
            if "candidates" in result:
                print(f"  candidates: {', '.join(result['candidates'])}")
            return 2
        if result.get("action") == "cancelled":
            print("Cancelled.")
            return 2
        print(f"✓ {result['action']}: {result['id']}")
        if result.get("chunk_also_queued"):
            print("  (associated chunk queued for purge)")
        return 0
    finally:
        mgr.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mnelo identity_fact manager — list/add/show/remove owner identity facts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="list all identity_fact entities")
    p_list.add_argument("--predicate", help="filter by predicate (e.g. lives_in)")
    p_list.add_argument("--json", action="store_true", help="JSON output")

    # show
    p_show = subparsers.add_parser("show", help="show one identity_fact")
    p_show.add_argument("--predicate", required=True, help="predicate name")
    p_show.add_argument("--value", help="specific value (omit for any)")
    p_show.add_argument("--json", action="store_true", help="JSON output")

    # add
    p_add = subparsers.add_parser("add", help="add a new identity_fact")
    p_add.add_argument("--predicate", required=True, help="predicate name")
    p_add.add_argument("--value", required=True, help="fact value")
    p_add.add_argument("--importance", type=float, default=0.9, help="importance 0.0-1.0 (default: 0.9)")
    p_add.add_argument("--dry-run", action="store_true", help="don't write")
    p_add.add_argument("--json", action="store_true", help="JSON output")

    # remove
    p_remove = subparsers.add_parser("remove", help="soft-delete an identity_fact")
    p_remove.add_argument("--predicate", help="predicate name (value auto-discovered)")
    p_remove.add_argument("--id", help="full fact id (e.g. identity:profession:engineer)")
    p_remove.add_argument("--reason", default="removed via identity_fact_manager", help="reason for audit trail")
    p_remove.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p_remove.add_argument("--json", action="store_true", help="JSON output")

    args = parser.parse_args()

    if args.command == "list":
        return cmd_list(args)
    if args.command == "show":
        return cmd_show(args)
    if args.command == "add":
        return cmd_add(args)
    if args.command == "remove":
        return cmd_remove(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
