#!/usr/bin/env python3
"""
hermes-memory daily health check (~30s).
Runs at 02:00 daily via cron.

What it does:
1. Confirm MCP server alive (lsof 8086 + /health probe)
2. WAL checkpoint (PASSIVE — non-blocking, append-only safe)
3. Sample DB stats (entities/chunks/relations counts, db size, WAL size)
4. Write report to ~/.hermes/cron/output/<job_id>_YYYYMMDD_HHMMSS.txt
5. Alert to telegram ONLY if degraded (delivery='telegram')

Why daily instead of weekly:
- WAL autocheckpoint is 1000 pages (~4MB). With trinity_daily Part 1-5 + occasional
  cron, WAL grows 4MB in ~24h. PASSIVE checkpoint flushes it cleanly.
- Daily ~30s self-check costs <1M tokens/month — far cheaper than a forgotten
  WAL bloat that later takes minutes to clean.

Exit codes:
  0 = ok
  1 = degraded (something off, alert sent)
  2 = failed (MCP down or DB inaccessible)
"""
import json
import os

# [7/19 P1-6] health_check report 文件默认 0600, 不让其他本地 user 看 DB stats
os.umask(0o077)
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# [7/18 patch F] i18n — import t() as _t for message resolution
# mnelo_locale (not stdlib 'locale') to avoid namespace conflict
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from mnelo_locale import t as _t
except ImportError:
    # fallback if mnelo_locale.py missing
    def _t(msg_id, **kwargs):
        return msg_id.format(**kwargs) if kwargs else msg_id

# Paths
DB_PATH = Path("/Users/apple/.hermes/memory/memory.db")
MCP_PORT = 8086
MCP_HOST = "127.0.0.1"
OUTPUT_DIR = Path("/Users/apple/.hermes/cron/output")
JOB_ID = "mnelo_daily_check"
BJT = timezone(timedelta(hours=8))


def check_mcp_alive():
    """Returns (alive: bool, pid: int|None, uptime_sec: int|None)."""
    try:
        # lsof is the source of truth — listens means it's actually serving
        result = subprocess.run(
            ["lsof", "-tiTCP:%d" % MCP_PORT, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        pid_str = result.stdout.strip().split("\n")[0] if result.stdout.strip() else None
        if not pid_str:
            return (False, None, None)
        pid = int(pid_str)
        # etime
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True, timeout=5,
        )
        etime = ps.stdout.strip()
        # Parse [[DD-]HH:]MM:SS
        uptime_sec = parse_etime(etime)
        return (True, pid, uptime_sec)
    except Exception as e:
        return (False, None, None)


def parse_etime(s):
    """Parse ps etime (e.g. '5-03:14:22', '04:18:30', '45:23') → seconds."""
    try:
        s = s.strip()
        if "-" in s:
            days, rest = s.split("-", 1)
            days = int(days)
        else:
            days = 0
            rest = s
        parts = rest.split(":")
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, sec = 0, int(parts[0]), int(parts[1])
        else:
            return None
        return days * 86400 + h * 3600 + m * 60 + sec
    except Exception:
        return None


def wal_checkpoint(db_path):
    """PASSIVE checkpoint — returns (busy, log, checkpointed) from PRAGMA wal_checkpoint(PASSIVE)."""
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        row = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return row  # (busy, log_pages, checkpointed_pages)
    finally:
        con.close()


def db_stats(db_path):
    """Read entities/chunks/relations counts + size + WAL + recall_log aggregates.

    [P1-v2 审计后] 加 recall_log 聚合 (last 24h 总 recall 数, 空 hits 数, latency p50/p95)
    + kind 分布, 让 daily_check 输出实战可读的统计 (不只是 size/count).
    """
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        # [P1-v2 审计后] 设 row_factory 让 dict-style 访问能工作
        con.row_factory = sqlite3.Row
        out = {}
        # Regular tables — count + active count
        # [7/19 P2-4] 显式白名单, 防止以后误把 user input 传进来 → SQL injection
        for table in ("entities", "chunks", "relations"):
            try:
                row = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE valid_until IS NULL"
                ).fetchone()
                out[f"{table}_active"] = row[0]
                total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                out[f"{table}_total"] = total
            except sqlite3.OperationalError:
                out[f"{table}_active"] = None
                out[f"{table}_total"] = None

        # vec0 virtual table — load sqlite-vec, count via vectors_rowids()
        try:
            import sqlite_vec
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.enable_load_extension(False)
            row = con.execute("SELECT COUNT(*) FROM vectors_rowids").fetchone()
            out["vectors_total"] = row[0]
            out["vectors_active"] = row[0]
        except Exception as e:
            out["vectors_total"] = None
            out["vectors_active"] = None
            out["vectors_error"] = str(e)[:120]

        out["db_size_bytes"] = db_path.stat().st_size if db_path.exists() else None
        wal = db_path.with_suffix(".db-wal")
        shm = db_path.with_suffix(".db-shm")
        out["wal_size_bytes"] = wal.stat().st_size if wal.exists() else None
        out["shm_size_bytes"] = shm.stat().st_size if shm.exists() else None

        # journal_mode (sanity)
        out["journal_mode"] = con.execute("PRAGMA journal_mode").fetchone()[0]

        # [P1-v2 审计后] recall_log 聚合 (24h window, 实战查实战质量)
        try:
            cutoff = (datetime.now(BJT) - timedelta(hours=24)).isoformat()
            row = con.execute("""
                SELECT COUNT(*),
                       AVG(latency_ms),
                       MIN(latency_ms),
                       MAX(latency_ms)
                FROM recall_log
                WHERE created_at > ?
            """, (cutoff,)).fetchone()
            cnt, avg_lat, min_lat, max_lat = row
            out["recall_24h_count"] = cnt or 0
            out["recall_24h_avg_latency_ms"] = round(avg_lat, 1) if avg_lat else 0
            out["recall_24h_min_latency_ms"] = round(min_lat, 1) if min_lat else 0
            out["recall_24h_max_latency_ms"] = round(max_lat, 1) if max_lat else 0

            # latency p50 / p95 (percentile via SQL)
            if cnt and cnt >= 5:
                lat_rows = con.execute("""
                    SELECT latency_ms FROM recall_log
                    WHERE created_at > ?
                    ORDER BY latency_ms
                """, (cutoff,)).fetchall()
                lats = [r[0] for r in lat_rows if r[0] is not None]
                if lats:
                    p50_idx = int(len(lats) * 0.50)
                    p95_idx = int(len(lats) * 0.95)
                    out["recall_24h_p50_ms"] = round(lats[p50_idx], 1) if p50_idx < len(lats) else 0
                    out["recall_24h_p95_ms"] = round(lats[p95_idx], 1) if p95_idx < len(lats) else 0

            # 空 hits 数 (results_json 是 '[]' 或 'null')
            out["recall_24h_empty_count"] = con.execute("""
                SELECT COUNT(*) FROM recall_log
                WHERE created_at > ?
                  AND (results_json = '[]' OR results_json IS NULL OR results_json = 'null')
            """, (cutoff,)).fetchone()[0]
        except Exception as e:
            out["recall_24h_error"] = str(e)[:120]

        # [P1-v2 审计后] kind 分布 (实战 entity kind 单一化预警)
        try:
            kind_rows = con.execute("""
                SELECT kind, COUNT(*) as cnt
                FROM entities
                WHERE valid_until IS NULL
                GROUP BY kind
                ORDER BY cnt DESC
                LIMIT 10
            """).fetchall()
            out["entity_kind_distribution"] = [
                {"kind": r["kind"] or "(null)", "count": r["cnt"]}
                for r in kind_rows
            ]
            # 单一化预警: 主导 kind > 70% = P1 注意
            total = sum(r["cnt"] for r in kind_rows)
            if total and kind_rows[0]["cnt"] / total > 0.70 and len(kind_rows) > 1:
                out["kind_diversity_warning"] = (
                    f"{kind_rows[0]['kind']} 占 {kind_rows[0]['cnt']*100.0/total:.1f}% — "
                    f"实战 kind 单一化, 考虑提升其他 kind 占比"
                )
                # [7/18 patch F] i18n — expose concept_pct for msg format
                out["concept_pct"] = round(kind_rows[0]['cnt'] * 100.0 / total, 1)
        except Exception as e:
            out["entity_kind_error"] = str(e)[:120]

        return out
    finally:
        con.close()


def format_size(n):
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}T"


def main():
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S BJT")
    report = {"timestamp": now, "checks": {}}
    degraded = False

    # 1. MCP alive
    alive, pid, uptime = check_mcp_alive()
    report["checks"]["mcp_server"] = {
        "alive": alive, "pid": pid, "uptime_sec": uptime,
    }
    if not alive:
        degraded = True
        report["checks"]["mcp_server"]["error"] = f"port {MCP_PORT} not listening"

    # 2. WAL checkpoint
    try:
        busy, log, ckpt = wal_checkpoint(DB_PATH)
        report["checks"]["wal_checkpoint"] = {
            "busy": bool(busy), "log_pages_before": log, "checkpointed_pages": ckpt,
        }
        if busy:
            degraded = True  # checkpoint deferred = WAL pressure
    except Exception as e:
        report["checks"]["wal_checkpoint"] = {"error": str(e)}
        degraded = True

    # 3. DB stats
    try:
        stats = db_stats(DB_PATH)
        report["checks"]["db_stats"] = stats

        # Soft alerts
        if stats.get("db_size_bytes") and stats["db_size_bytes"] > 200 * 1024 * 1024:
            report["checks"]["db_stats"]["warning"] = "db > 200MB"
            degraded = True
        if stats.get("wal_size_bytes") and stats["wal_size_bytes"] > 50 * 1024 * 1024:
            report["checks"]["db_stats"]["warning"] = "wal > 50MB after checkpoint"
            degraded = True
    except Exception as e:
        report["checks"]["db_stats"] = {"error": str(e)}
        degraded = True

    # 4. Write report
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_file = datetime.now(BJT).strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"{JOB_ID}_{ts_file}.txt"

    lines = [
        _t("check.banner", ts=now),
        "=" * 50,
    ]
    mcp = report["checks"]["mcp_server"]
    if mcp["alive"]:
        uptime_h = mcp["uptime_sec"] / 3600 if mcp["uptime_sec"] else 0
        # [7/18 patch F] i18n — check.mcp_alive msg_id
        lines.append(_t("check.mcp_alive", pid=mcp['pid'], uptime=f"{uptime_h:.1f}h"))
    else:
        lines.append(f"❌ MCP server DOWN — port {MCP_PORT} not listening")

    wc = report["checks"]["wal_checkpoint"]
    if "error" not in wc:
        # [7/18 patch F] i18n — check.wal_checkpoint msg_id
        lines.append(
            _t("check.wal_checkpoint",
                done=wc['checkpointed_pages'],
                total=wc['log_pages_before'])
            + (" (busy — checkpoint deferred)" if wc["busy"] else "")
        )
    else:
        lines.append(f"❌ WAL checkpoint error — {wc['error']}")

    s = report["checks"]["db_stats"]
    if "error" not in s:
        # [7/18 patch F] i18n — check.db_stats msg_id
        lines.append(_t(
            "check.db_stats",
            e_a=s['entities_active'], e_t=s['entities_total'],
            c_a=s['chunks_active'], c_t=s['chunks_total'],
            r_a=s['relations_active'], r_t=s['relations_total'],
            v=s.get('vectors_total', '?'),
        ))
        # [7/18 patch F] i18n — check.db_size msg_id
        lines.append(_t(
            "check.db_size",
            db=format_size(s['db_size_bytes']),
            wal=format_size(s['wal_size_bytes']),
            shm=format_size(s.get('shm_size_bytes')),
            mode=s['journal_mode'],
        ))

        # [P1-v2 审计后] recall_log 24h 聚合
        if "recall_24h_count" in s:
            rc = s['recall_24h_count']
            empty = s.get('recall_24h_empty_count', 0)
            empty_pct = (empty * 100.0 / rc) if rc else 0
            p50 = s.get('recall_24h_p50_ms', '?')
            p95 = s.get('recall_24h_p95_ms', '?')
            avg = s.get('recall_24h_avg_latency_ms', '?')
            # [7/18 patch F] i18n — check.recall_24h msg_id
            lines.append(_t(
                "check.recall_24h",
                count=rc, empty=empty, pct=empty_pct,
                p50=p50, p95=p95, avg=avg,
            ))

        # [P1-v2 审计后] kind 分布 (TOP 3 + 单一化预警)
        if "entity_kind_distribution" in s:
            kd = s["entity_kind_distribution"][:3]
            kind_str = ", ".join(f"{k['kind']}={k['count']}" for k in kd)
            # [7/18 patch F] i18n — check.kind_top msg_id
            lines.append(_t("check.kind_top", kinds=kind_str))
            if "kind_diversity_warning" in s:
                # [7/18 patch F] i18n — check.kind_skewed msg_id
                lines.append(_t("check.kind_skewed", pct=s.get('concept_pct', '?')))

        if "warning" in s:
            lines.append(f"⚠️  {s['warning']}")
    else:
        lines.append(f"❌ DB stats error — {s['error']}")

    report_text = "\n".join(lines) + "\n"
    report_path.write_text(report_text)
    # Also keep the JSON form for programmatic consumers
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # 5. Echo to stdout (cron captures this when no_agent=False)
    print(report_text)

    # Exit code → delivery decision (cronjob reads this)
    sys.exit(1 if degraded else 0)


if __name__ == "__main__":
    main()
