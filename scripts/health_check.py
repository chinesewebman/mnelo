#!/usr/bin/env python3
"""
mnelo daily health check (~30s).
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
from datetime import datetime, timedelta, timezone
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


# Paths — [7/21 fix] 不再硬编码, 从 config/env 解析 (env > ~/.hermes/...)
from config import resolve_db_path as _resolve_db_path

DB_PATH = _resolve_db_path()
MCP_PORT = 8086
MCP_HOST = "127.0.0.1"
OUTPUT_DIR = Path(os.environ.get("MNELO_CRON_OUTPUT_DIR", str(Path.home() / ".hermes" / "cron" / "output")))
JOB_ID = "mnelo_daily_check"
BJT = timezone(timedelta(hours=8))


def check_mcp_alive():
    """Returns (alive: bool, pid: int|None, uptime_sec: int|None).

    双探测: lsof (默认 PATH 在 /usr/sbin) + ps (兜底, 不依赖 PATH).
    macOS sandbox 环境可能 PATH 缺 /usr/sbin, lsof 报 FileNotFoundError.
    """
    pid_str = None
    # 探测 1: lsof (PATH 完整时)
    for lsof_path in ("lsof", "/usr/sbin/lsof", "/usr/bin/lsof"):
        try:
            result = subprocess.run(
                [lsof_path, "-tiTCP:%d" % MCP_PORT, "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                pid_str = result.stdout.strip().split("\n")[0]
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    # 探测 2: ps 兜底 — mcp_server.py 进程 + LISTEN 该端口
    if not pid_str:
        try:
            ps = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # 候选: mcp_server.py 行
            for line in ps.stdout.split("\n"):
                if "mcp_server.py" not in line:
                    continue
                try:
                    pid_candidate = int(line.split()[0])
                    # 二次验证: lsof 该 pid 持有 MCP_PORT
                    for lsof_path in ("lsof", "/usr/sbin/lsof", "/usr/bin/lsof"):
                        try:
                            lsof_pid = subprocess.run(
                                [lsof_path, "-tiTCP:%d" % MCP_PORT, "-a", "-p", str(pid_candidate)],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            if lsof_pid.returncode == 0 and lsof_pid.stdout.strip():
                                pid_str = lsof_pid.stdout.strip().split("\n")[0]
                                break
                        except (FileNotFoundError, subprocess.TimeoutExpired):
                            continue
                    if pid_str:
                        break
                except (ValueError, IndexError):
                    continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    if not pid_str:
        return (False, None, None)
    # [M37 defensive] 解析 pid 一次, 后续复用避免 except 内重复 int(). 防御性
    # 加固: 若 lsof 输出非数字 (理论场景; lsof -t 仅返 pid 数字, 实际行为不可触发),
    # 不应再次 int() 再抛, 应如实在 except 内报 DOWN.
    try:
        pid = int(pid_str)
    except (ValueError, TypeError):
        return (False, None, None)
    try:
        ps = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        etime = ps.stdout.strip()
        uptime_sec = parse_etime(etime)
        return (True, pid, uptime_sec)
    except Exception:
        return (True, pid, None)


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
    + kind 分布, 让 daily_check 输出可读的统计 (不只是 size/count).
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
                row = con.execute(f"SELECT COUNT(*) FROM {table} WHERE valid_until IS NULL").fetchone()
                out[f"{table}_active"] = row[0]
                total = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                out[f"{table}_total"] = total
            except sqlite3.OperationalError:
                out[f"{table}_active"] = None
                out[f"{table}_total"] = None

        # vectors 数按实际 search 后端计数 — usearch/zvec 下 sqlite_vec 的
        # vectors_rowids 恒 0, 只数 vec0 是显示假象 (8/5).
        # 注意: 不调 si.close() — usearch 的 close() 会 save() 写盘, 可能覆盖
        # 运行中 server 内存里未落盘的 add; 本进程是短命 CLI, 退出即释放.
        try:
            from config import config as _cfg
            from search_index import build_search_index as _bi

            si = _bi(_cfg.search_backend, db_path, _cfg.embedder_dim)
            v = si.size()
            out["vectors_total"] = v
            out["vectors_active"] = v
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

        # [P1-v2 审计后] recall_log 聚合 (24h window, 查质量)
        try:
            cutoff = (datetime.now(BJT) - timedelta(hours=24)).isoformat()
            row = con.execute(
                """
                SELECT COUNT(*),
                       AVG(latency_ms),
                       MIN(latency_ms),
                       MAX(latency_ms)
                FROM recall_log
                WHERE created_at > ?
            """,
                (cutoff,),
            ).fetchone()
            cnt, avg_lat, min_lat, max_lat = row
            out["recall_24h_count"] = cnt or 0
            out["recall_24h_avg_latency_ms"] = round(avg_lat, 1) if avg_lat else 0
            out["recall_24h_min_latency_ms"] = round(min_lat, 1) if min_lat else 0
            out["recall_24h_max_latency_ms"] = round(max_lat, 1) if max_lat else 0

            # latency p50 / p95 (percentile via SQL)
            if cnt and cnt >= 5:
                lat_rows = con.execute(
                    """
                    SELECT latency_ms FROM recall_log
                    WHERE created_at > ?
                    ORDER BY latency_ms
                """,
                    (cutoff,),
                ).fetchall()
                lats = [r[0] for r in lat_rows if r[0] is not None]
                if lats:
                    p50_idx = int(len(lats) * 0.50)
                    p95_idx = int(len(lats) * 0.95)
                    out["recall_24h_p50_ms"] = round(lats[p50_idx], 1) if p50_idx < len(lats) else 0
                    out["recall_24h_p95_ms"] = round(lats[p95_idx], 1) if p95_idx < len(lats) else 0

            # 空 hits 数 (results_json 是 '[]' 或 'null')
            out["recall_24h_empty_count"] = con.execute(
                """
                SELECT COUNT(*) FROM recall_log
                WHERE created_at > ?
                  AND (results_json = '[]' OR results_json IS NULL OR results_json = 'null')
            """,
                (cutoff,),
            ).fetchone()[0]
        except Exception as e:
            out["recall_24h_error"] = str(e)[:120]

        # [P1-v2 审计后] kind 分布 ( entity kind 单一化预警)
        try:
            kind_rows = con.execute("""
                SELECT kind, COUNT(*) as cnt
                FROM entities
                WHERE valid_until IS NULL
                GROUP BY kind
                ORDER BY cnt DESC
                LIMIT 10
            """).fetchall()
            out["entity_kind_distribution"] = [{"kind": r["kind"] or "(null)", "count": r["cnt"]} for r in kind_rows]
            # 单一化预警: 主导 kind > 70% = P1 注意
            total = sum(r["cnt"] for r in kind_rows)
            if total and kind_rows[0]["cnt"] / total > 0.70 and len(kind_rows) > 1:
                out["kind_diversity_warning"] = f"{kind_rows[0]['kind']} 占 {kind_rows[0]['cnt'] * 100.0 / total:.1f}% —  kind 单一化, 考虑提升其他 kind 占比"
                # [7/18 patch F] i18n — expose concept_pct for msg format
                out["concept_pct"] = round(kind_rows[0]["cnt"] * 100.0 / total, 1)
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
        "alive": alive,
        "pid": pid,
        "uptime_sec": uptime,
    }
    if not alive:
        degraded = True
        report["checks"]["mcp_server"]["error"] = f"port {MCP_PORT} not listening"

    # 2. WAL checkpoint
    try:
        busy, log, ckpt = wal_checkpoint(DB_PATH)
        report["checks"]["wal_checkpoint"] = {
            "busy": bool(busy),
            "log_pages_before": log,
            "checkpointed_pages": ckpt,
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

    # 3.5 Search backend (DESIGN §3.6/§8.3)
    try:
        from config import config as _cfg
        from search_index import usearch_available, zvec_available

        want = _cfg.search_backend
        # [8/6 plan §8] 向量库必选二选一; auto 走可用性链解析 active.
        # sqlite_vec 已出局, 不再是降级兜底 — 都不可用 → unavailable + degraded.
        if want == "auto":
            if zvec_available():
                active = "zvec"
            elif usearch_available():
                active = "usearch"
            else:
                active = "unavailable"
                degraded = True
            report["checks"]["search_backend"] = {"configured": "auto", "active": active}
        elif want == "zvec":
            ok = zvec_available()
            report["checks"]["search_backend"] = {
                "configured": "zvec",
                "active": "zvec" if ok else "unavailable",
                "zvec_available": ok,
            }
            if not ok:
                degraded = True  # 配置了 zvec 但不可用 → 整体不可用 (无回落)
        elif want == "usearch":
            ok = usearch_available()
            report["checks"]["search_backend"] = {
                "configured": "usearch",
                "active": "usearch" if ok else "unavailable",
                "usearch_available": ok,
            }
            if not ok:
                degraded = True
        else:
            # 未知值 (config coerce 兜底一般不会到这里)
            report["checks"]["search_backend"] = {"configured": want, "active": "unavailable"}
            degraded = True
    except Exception as e:
        report["checks"]["search_backend"] = {"error": str(e)}
        degraded = True

    # 4. Write report
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_file = datetime.now(BJT).strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"{JOB_ID}_{ts_file}.txt"

    lines = [
        _t("check.banner", ts=now),
        "=" * 50,
    ]
    sb = report["checks"].get("search_backend", {})
    configured = sb.get("configured")
    active = sb.get("active")
    if configured == "auto":
        if active == "zvec":
            lines.append("✅ Search backend — auto → zvec (HNSW + FTS, INT8)")
        elif active == "usearch":
            lines.append("✅ Search backend — auto → usearch (HNSW, f16)")
        else:
            lines.append("❌ Search backend — auto 但 zvec/usearch 均不可用")
    elif configured == "zvec" and active == "zvec":
        lines.append("✅ Search backend — zvec (HNSW + FTS, INT8)")
    elif configured == "zvec" and active == "unavailable":
        lines.append("❌ Search backend — configured zvec, 不可用 (CPU 不支持)")
    elif configured == "usearch" and active == "usearch":
        lines.append("✅ Search backend — usearch (HNSW, f16)")
    elif configured == "usearch" and active == "unavailable":
        lines.append("❌ Search backend — configured usearch, 未安装")
    else:
        lines.append(f"❌ Search backend — configured {configured}, active {active}")
    mcp = report["checks"]["mcp_server"]
    if mcp["alive"]:
        uptime_h = mcp["uptime_sec"] / 3600 if mcp["uptime_sec"] else 0
        # [7/18 patch F] i18n — check.mcp_alive msg_id
        lines.append(_t("check.mcp_alive", pid=mcp["pid"], uptime=f"{uptime_h:.1f}h"))
    else:
        lines.append(f"❌ MCP server DOWN — port {MCP_PORT} not listening")

    wc = report["checks"]["wal_checkpoint"]
    if "error" not in wc:
        # [7/18 patch F] i18n — check.wal_checkpoint msg_id
        lines.append(_t("check.wal_checkpoint", done=wc["checkpointed_pages"], total=wc["log_pages_before"]) + (" (busy — checkpoint deferred)" if wc["busy"] else ""))
    else:
        lines.append(f"❌ WAL checkpoint error — {wc['error']}")

    s = report["checks"]["db_stats"]
    if "error" not in s:
        # [7/18 patch F] i18n — check.db_stats msg_id
        lines.append(
            _t(
                "check.db_stats",
                e_a=s["entities_active"],
                e_t=s["entities_total"],
                c_a=s["chunks_active"],
                c_t=s["chunks_total"],
                r_a=s["relations_active"],
                r_t=s["relations_total"],
                v=s.get("vectors_total", "?"),
            )
        )
        # [7/18 patch F] i18n — check.db_size msg_id
        lines.append(
            _t(
                "check.db_size",
                db=format_size(s["db_size_bytes"]),
                wal=format_size(s["wal_size_bytes"]),
                shm=format_size(s.get("shm_size_bytes")),
                mode=s["journal_mode"],
            )
        )

        # [P1-v2 审计后] recall_log 24h 聚合
        if "recall_24h_count" in s:
            rc = s["recall_24h_count"]
            empty = s.get("recall_24h_empty_count", 0)
            empty_pct = (empty * 100.0 / rc) if rc else 0
            p50 = s.get("recall_24h_p50_ms", "?")
            p95 = s.get("recall_24h_p95_ms", "?")
            avg = s.get("recall_24h_avg_latency_ms", "?")
            # [7/18 patch F] i18n — check.recall_24h msg_id
            lines.append(
                _t(
                    "check.recall_24h",
                    count=rc,
                    empty=empty,
                    pct=empty_pct,
                    p50=p50,
                    p95=p95,
                    avg=avg,
                )
            )

        # [P1-v2 审计后] kind 分布 (TOP 3 + 单一化预警)
        if "entity_kind_distribution" in s:
            kd = s["entity_kind_distribution"][:3]
            kind_str = ", ".join(f"{k['kind']}={k['count']}" for k in kd)
            # [7/18 patch F] i18n — check.kind_top msg_id
            lines.append(_t("check.kind_top", kinds=kind_str))
            if "kind_diversity_warning" in s:
                # [7/18 patch F] i18n — check.kind_skewed msg_id
                lines.append(_t("check.kind_skewed", pct=s.get("concept_pct", "?")))

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
