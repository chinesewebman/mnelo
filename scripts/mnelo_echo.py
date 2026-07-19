#!/usr/bin/env python3
"""
mnelo_echo.py — thin CLI wrapper that prints 🌳 feedback for mnelo operations.

Use instead of raw `python3 -c "..."` so the user gets a recognizable emoji
feedback line for path B operations (vs 🧠 for Hermes path A).

Usage:
    mnelo_echo.py remember "content here" [--source X] [--importance 0.7]
    mnelo_echo.py recall "query" [--top-k 5]
    mnelo_echo.py forget --id chunk_xxx [--reason Y]
    mnelo_echo.py stats

Output format (visible in terminal output):
    🌳 mnelo    +chunk_20260720_044932_343473  (importance=0.85, source=live_demo)
    🌳 mnelo    ~3 hits  "本地优先的记忆系统"  (top=meta rrf=0.032)
    🌳 mnelo    -chunk_20260720_044932_343473  (soft_deleted)
    🌳 mnelo    stats: chunks=5126 entities=4290 vectors=4108

Why this script:
    Hermes's `memory` tool (path A) prints `🧠 memory +...` for free because
    it's a registered core tool. mnelo (path B) is called via `terminal`
    (💻 emoji), so the user can't tell it apart from any other shell command.
    This wrapper makes path B visually distinct.

Requires: mnelo repo at /Users/apple/projects/mnelo (or MNELO_REPO env).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ECHO marker — change here to swap the emoji (e.g. 🔮 🌳 💎)
ECHO = "🌳"
LABEL = "mnelo"


def _setup_path() -> None:
    """Add mnelo repo to sys.path. MNELO_REPO env wins, else default location."""
    import os

    repo = os.environ.get("MNELO_REPO", "/Users/apple/projects/mnelo")
    if repo not in sys.path:
        sys.path.insert(0, repo)


def _fmt_remember(chunk_id: str, importance: float, source: str | None) -> str:
    src_part = f", source={source}" if source else ""
    return f"{ECHO} {LABEL}    +{chunk_id}  (importance={importance}{src_part})"


def _fmt_recall(query: str, hits: list[dict]) -> str:
    if not hits:
        return f'{ECHO} {LABEL}    ~0 hits  "{query}"'
    top = hits[0]
    return f'{ECHO} {LABEL}    ~{len(hits)} hits  "{query}"  (top={top["method"]} rrf={top.get("rrf_score", 0):.4f})'


def _fmt_forget(target: str, target_kind: str) -> str:
    return f"{ECHO} {LABEL}    -{target_kind}:{target}  (soft_deleted)"


def _fmt_stats(stats: dict) -> str:
    parts = []
    for tbl, s in stats.items():
        if isinstance(s, dict):
            parts.append(f"{tbl}={s.get('active', s.get('total', '?'))}")
        elif isinstance(s, (int, float, str)):
            parts.append(f"{tbl}={s}")
    return f"{ECHO} {LABEL}    stats: {' '.join(parts)}"


def cmd_remember(args) -> int:
    from memory import Memory

    m = Memory()
    try:
        cid = m.remember(
            content=args.content,
            source=args.source,
            importance=args.importance,
        )
        print(_fmt_remember(cid, args.importance, args.source))
        return 0
    finally:
        m.close()


def cmd_recall(args) -> int:
    from memory import Memory

    m = Memory()
    try:
        results = m.recall(args.query, top_k=args.top_k)
        print(_fmt_recall(args.query, results))
        if args.json:
            # Print JSON for further processing
            print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    finally:
        m.close()


def cmd_forget(args) -> int:
    from memory import Memory

    m = Memory()
    try:
        m.forget(args.id, target_kind=args.kind, reason=args.reason or "mnelo_echo")
        print(_fmt_forget(args.id, args.kind))
        return 0
    finally:
        m.close()


def cmd_stats(_args) -> int:
    from memory import Memory

    m = Memory()
    try:
        print(_fmt_stats(m.stats()))
        return 0
    finally:
        m.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mnelo path-B wrapper with 🌳 emoji feedback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # remember
    p_rem = sub.add_parser("remember", help="Store a chunk in mnelo")
    p_rem.add_argument("content", help="Text content to remember")
    p_rem.add_argument("--source", default="mnelo_echo", help="source label")
    p_rem.add_argument("--importance", type=float, default=0.5, help="0.0-1.0, default 0.5")
    p_rem.set_defaults(func=cmd_remember)

    # recall
    p_rec = sub.add_parser("recall", help="Recall chunks via 4-lane RRF")
    p_rec.add_argument("query", help="Search query")
    p_rec.add_argument("--top-k", type=int, default=5, help="default 5")
    p_rec.add_argument("--json", action="store_true", help="also print JSON")
    p_rec.set_defaults(func=cmd_recall)

    # forget
    p_for = sub.add_parser("forget", help="Soft-delete a chunk/entity")
    p_for.add_argument("--id", required=True, help="chunk or entity id")
    p_for.add_argument("--kind", default="chunk", choices=["chunk", "entity", "relation"])
    p_for.add_argument("--reason", default=None)
    p_for.set_defaults(func=cmd_forget)

    # stats
    p_st = sub.add_parser("stats", help="Show DB counts")
    p_st.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    _setup_path()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
