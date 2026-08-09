#!/usr/bin/env python3
"""
forget_junk_entities.py — 批量 soft-delete HonchoImporter 噪声 entity.

设计:
- 直连 SQLite (绕开 zvec LOCK, 因为 mcp_server 持 LOCK 跑 zvec)
- entity forget 不动 _index (memory.py:717-720 只 SQL UPDATE)
- 30 天 purge queue 入队 + audit_log 写 — 保留 audit_window 完整
- cascade 自动级联关系 (anno:* entity 大概率没真关系, 但仍走 cascade)

[8/8 P1] 主人授权 forget A 类全部 (~4147 条):
  - anno: 开头 (HonchoImporter NER 噪声, ~4124 条)
  - TOKEN_C_* (随机 token, ~70 条)
  - 长句子 / 路径 entity (~76 条)

用法:
  MNELO_HOME=~/.hermes python3 scripts/forget_junk_entities.py [--dry-run] [--limit N] [--pattern anno:]

[8/9 review B8 fix] audit_log 补 before_json + revert_sql (原 before_json=None
→ memory_audit_undo 读 revert_sql 为空 → ValueError).

[8/10 主人验证报告 fix] 原 revert_sql 用 INSERT OR IGNORE 撞 PK 静默跳过
(原行 valid_until 已非空, INSERT OR IGNORE 命中 → 0 恢复). 改为 UPDATE 风格
还原 valid_until = NULL, 跟 memory.py audit_undo UPDATE 路径一致.

[8/10 B2 review followup] undo 路径缺回归测试 — 主人提示 test_digest.py
只覆盖 UPDATE 风格, 这条路径没钉住. 整改建议: 在 tests/ 加 forget_junk
undo 端到端 (隔离 DB + forget_one + audit_undo + 验证 valid_until=NULL).
"""

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config as _config  # noqa: E402

DB_PATH = Path(_config.db_path)


def now_iso() -> str:
    """memory.py 里 now() 的简化版本, ISO8601 + tz."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def list_junk(conn: sqlite3.Connection, pattern: str, limit: int | None) -> list[tuple[str, str]]:
    """[8/8 P1 fix] HonchoImporter 噪声 entity 用 id LIKE 而非 name LIKE.
    实际 schema: id='anno:mentions:CLI', name='CLI'. name LIKE 'anno:%' = 0 匹配.
    """
    rows = conn.execute(
        "SELECT id, name FROM entities WHERE kind='concept' AND valid_until IS NULL AND id LIKE ?",
        (pattern + "%",),
    ).fetchall()
    if limit:
        rows = rows[:limit]
    return [(r[0], r[1]) for r in rows]


def forget_one(conn: sqlite3.Connection, eid: str, reason: str) -> tuple[int, int]:
    """模拟 Memory.forget(target_kind='entity') 路径, 不动 _index.

    Returns: (updated_entities, edges_invalidated).
    """
    ts = now_iso()
    # [8/9 review B8 fix] 在 UPDATE 前 SELECT 完整 entity 行, 存 before_json.
    # 原代码 before_json=None → memory_audit_undo 读 revert_sql 为空 → ValueError.
    before_row = conn.execute(
        "SELECT id, kind, name, summary, properties_json, aliases_json, source, importance, user_confirmed, created_at, valid_from FROM entities WHERE id = ? AND valid_until IS NULL",
        (eid,),
    ).fetchone()
    if before_row is None:
        return (0, 0)
    import json as _json

    before_json = _json.dumps(
        {
            "id": before_row[0],
            "kind": before_row[1],
            "name": before_row[2],
            "summary": before_row[3],
            "properties_json": before_row[4],
            "aliases_json": before_row[5],
            "source": before_row[6],
            "importance": before_row[7],
            "user_confirmed": before_row[8],
            "created_at": before_row[9],
            "valid_from": before_row[10],
        },
        ensure_ascii=False,
    )
    cur = conn.execute(
        "UPDATE entities SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
        (ts, eid),
    )
    updated = cur.rowcount
    if updated == 0:
        return (0, 0)
    cur = conn.execute(
        "UPDATE relations SET valid_until = ? WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL",
        (ts, eid, eid),
    )
    edges = cur.rowcount
    conn.execute(
        "INSERT INTO purged_queue (target_id, target_kind, purged_at, done) VALUES (?, 'entity', datetime('now', '+30 days'), 0)",
        (eid,),
    )
    # 写 audit_log (跟 L2 hygiene pass 同结构, status='applied').
    # before_json 是 entity 完整快照, undo 走 memory_audit_undo 还原.
    import uuid

    run_id = f"junk_forget_{uuid.uuid4().hex[:8]}_{int(time.time())}"
    # [8/10 主人验证报告 fix] revert_sql 用 UPDATE 风格 (还原 valid_until=NULL),
    # 不是 INSERT OR IGNORE. 原因是 forget_one 是软删 (原行 valid_until 非空),
    # INSERT OR IGNORE 撞 PK 静默跳过 → 0 恢复. UPDATE 走 ts 复合条件,
    # 跟 memory.py audit_undo UPDATE 路径一致, 跟 test_digest.py 已覆盖的
    # undo 风格对齐.
    eid_q = _json.dumps(eid)  # 转义 JSON 字符串防注入
    revert_sql = (
        f"UPDATE entities SET valid_until = NULL "
        f"WHERE id = {eid_q} AND valid_until = {_json.dumps(ts)}; "
        f"UPDATE relations SET valid_until = NULL "
        f"WHERE (source_id = {eid_q} OR target_id = {eid_q}) "
        f"AND valid_until = {_json.dumps(ts)};"
    )
    conn.execute(
        "INSERT INTO audit_log (run_id, pass_name, action_type, ref_type, ref_id, before_json, after_json, llm_used, status, created_at, revert_sql) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'applied', ?, ?)",
        (
            run_id,
            "manual_junk_forget",
            f"soft_delete_{reason}",
            "entity",
            eid,
            before_json,
            _json.dumps({"valid_until": ts, "edges_invalidated": edges}, ensure_ascii=False),
            ts,
            revert_sql,
        ),
    )
    return (updated, edges)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pattern", default="anno:")
    ap.add_argument("--reason", default="honcho_importer_noise_2026-08-08")
    args = ap.parse_args()

    print(f"[forget_junk] pattern={args.pattern!r} limit={args.limit} dry_run={args.dry_run}")

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    targets = list_junk(conn, args.pattern, args.limit)
    print(f"[forget_junk] matched {len(targets)} active entity:")
    for eid, ename in targets[:5]:
        print(f"  sample: {eid} | {ename[:60]}")
    if len(targets) > 5:
        print(f"  ... and {len(targets) - 5} more")

    if args.dry_run:
        conn.close()
        print("[forget_junk] DRY-RUN, no changes made.")
        return

    # 真 forget
    t0 = time.time()
    total_updated = 0
    total_edges = 0
    failed = 0
    # 每 100 条 commit 一次, 避免 long transaction 锁 WAL
    BATCH = 100
    for i, (eid, _) in enumerate(targets, 1):
        try:
            u, e = forget_one(conn, eid, args.reason)
            total_updated += u
            total_edges += e
        except Exception as exc:
            failed += 1
            print(f"[forget_junk] FAILED {eid}: {exc}")
        if i % BATCH == 0:
            conn.commit()
            print(f"[forget_junk] progress: {i}/{len(targets)} ({total_updated} entities, {total_edges} edges, {failed} failed)")
    conn.commit()
    elapsed = time.time() - t0
    print(f"[forget_junk] DONE: {total_updated}/{len(targets)} forgotten, {total_edges} edges invalidated, {failed} failed, {elapsed:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
