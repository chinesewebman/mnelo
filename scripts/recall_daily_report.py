#!/usr/bin/env python3
"""
recall_daily_report.py — mnelo 召回日报生成器 (PoC #3)

Owner: 主人 Ling · 启动 2026-08-29
Spec: /Users/apple/study/poc3-recall-telemetry.md

调用:
  python3 recall_daily_report.py [--days N] [--dry-run] [--deliver {telegram,local,stdout}]

设计: 纯 stdlib + sqlite3 + subprocess (调 mnelo MCP CLI). 无外部依赖.
MCP server 不通时降级: 仅 recall_log raw SQL, 日报里加 "stats 不可用" 标记.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Template

REPO_ROOT = Path("/Users/apple/projects/mnelo")
MEMORY_DB = REPO_ROOT / "memory.db"
TEMPLATE_PATH = Path("/Users/apple/study/templates/recall_daily.md")
SLOW_THRESHOLD_MS = 500
CST = timezone(timedelta(hours=8))


def log(msg: str) -> None:
    print(f"[recall_daily_report] {msg}", file=sys.stderr)


def call_mcp_recall_stats(days: int, group_by: str) -> dict | None:
    """调 mnelo MCP memory_recall_stats. 返回 None = MCP 不通."""
    try:
        proc = subprocess.run(
            ["mnelo", "call", "memory_recall_stats", "--json", json.dumps({"days": days, "group_by": group_by})],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            log(f"MCP call failed rc={proc.returncode}: {proc.stderr.strip()[:200]}")
            return None
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        log(f"MCP unavailable: {type(e).__name__}: {str(e)[:200]}")
        return None


def query_recall_log_raw(days: int) -> dict:
    """MCP 不通时的降级路径: 直接 SQL 查 recall_log."""
    if not MEMORY_DB.exists():
        log(f"memory.db not found at {MEMORY_DB}")
        return {"total": 0, "slow": [], "empty": [], "by_day": []}

    cutoff = (datetime.now(CST) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(MEMORY_DB), timeout=5.0)
    try:
        cur = conn.cursor()
        total = cur.execute("SELECT COUNT(*) FROM recall_log WHERE created_at >= ?", (cutoff,)).fetchone()[0]
        slow = cur.execute(
            """
            SELECT query, latency_ms FROM recall_log
            WHERE created_at >= ? AND latency_ms > ?
            ORDER BY latency_ms DESC LIMIT 5
        """,
            (cutoff, SLOW_THRESHOLD_MS),
        ).fetchall()
        empty = cur.execute(
            """
            SELECT query FROM recall_log
            WHERE created_at >= ? AND (results_json IS NULL OR results_json = '[]'
                  OR json_array_length(results_json) = 0)
            ORDER BY created_at DESC LIMIT 5
        """,
            (cutoff,),
        ).fetchall()
        by_day = cur.execute(
            """
            SELECT substr(created_at, 1, 10) AS day,
                   COUNT(*) AS cnt,
                   SUM(CASE WHEN results_json IS NULL OR results_json = '[]'
                            OR json_array_length(results_json) = 0 THEN 1 ELSE 0 END) AS empty
            FROM recall_log
            WHERE created_at >= ?
            GROUP BY day ORDER BY day DESC LIMIT 30
        """,
            (cutoff,),
        ).fetchall()
        return {
            "total": total,
            "slow": [{"query": r[0], "latency_ms": r[1]} for r in slow],
            "empty": [r[0] for r in empty],
            "by_day": [{"date": r[0], "count": r[1], "empty": r[2]} for r in by_day],
        }
    except sqlite3.Error as e:
        log(f"SQLite error: {e}")
        return {"total": 0, "slow": [], "empty": [], "by_day": []}
    finally:
        conn.close()


def build_report(days: int = 1) -> tuple[str, bool]:
    """组装日报 markdown. 返回 (markdown, stats_unavailable_flag)."""
    stats = call_mcp_recall_stats(days, "method")
    by_day_stats = call_mcp_recall_stats(days, "day") if stats else None
    stats_unavailable = stats is None
    raw = query_recall_log_raw(days) if stats_unavailable else None

    now = datetime.now(CST)
    ctx = {
        "date": now.strftime("%Y-%m-%d"),
        "window_start": (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
        "window_end": now.strftime("%Y-%m-%d %H:%M"),
    }

    if stats:
        totals = stats.get("totals", {})
        latency = stats.get("latency_ms", {})
        methods = stats.get("methods", {})
        ctx["total_recalls"] = totals.get("total_recalls", 0)
        ctx["avg_latency_ms"] = round(latency.get("avg", 0), 1)
        ctx["p95_latency_ms"] = round(latency.get("p95", 0), 1)
        ctx["empty_rate"] = round(totals.get("empty_rate", 0) * 100, 1)
        ctx["by_method"] = [
            {"method": m, "hits": v.get("hit_count", 0), "avg_rank": v.get("avg_rank", "—"), "avg_score": v.get("avg_rrf_score", v.get("avg_distance", "—"))} for m, v in methods.items()
        ]
        by_day_data = (by_day_stats or {}).get("by_day", [])
        ctx["by_day"] = [{"date": d.get("day"), "count": d.get("count", 0), "empty": d.get("empty", 0)} for d in by_day_data]
        raw_anomalies = query_recall_log_raw(days)
        ctx["slow_count"] = len(raw_anomalies["slow"])
        ctx["empty_count"] = len(raw_anomalies["empty"])
        ctx["slow_samples"] = [r["query"][:80] for r in raw_anomalies["slow"][:3]]
        ctx["empty_samples"] = raw_anomalies["empty"][:3]
    else:
        ctx["total_recalls"] = raw["total"]
        ctx["avg_latency_ms"] = 0
        ctx["p95_latency_ms"] = 0
        ctx["empty_rate"] = round((len(raw["empty"]) / raw["total"] * 100) if raw["total"] else 0, 1)
        ctx["by_method"] = []  # 模板里 {% if by_method %} else 分支兜底
        ctx["by_day"] = raw["by_day"][:7]
        ctx["slow_count"] = len(raw["slow"])
        ctx["empty_count"] = len(raw["empty"])
        ctx["slow_samples"] = [r["query"][:80] for r in raw["slow"][:3]]
        ctx["empty_samples"] = raw["empty"][:3]

    ctx["stats_unavailable"] = stats_unavailable
    tpl = Template(TEMPLATE_PATH.read_text(encoding="utf-8"), trim_blocks=True, lstrip_blocks=True)
    return tpl.render(**ctx), stats_unavailable


def main() -> int:
    parser = argparse.ArgumentParser(description="mnelo 召回日报生成器")
    parser.add_argument("--days", type=int, default=int(os.environ.get("RECALL_REPORT_DAYS", "1")))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--deliver", choices=["telegram", "local", "stdout"], default=os.environ.get("RECALL_REPORT_OUTPUT", "stdout"))
    args = parser.parse_args()

    log(f"start days={args.days} deliver={args.deliver} dry_run={args.dry_run}")
    markdown, stats_unavailable = build_report(days=args.days)
    print(markdown)
    if not args.dry_run and args.deliver == "telegram":
        log("delivery: telegram push wiring is Day 2+ (spec §3.2.3), skipping")
    log(f"done stats_unavailable={stats_unavailable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
