#!/usr/bin/env python3
"""
migrate_stock_namespace_2026_08_09.py — [8/9 review B1 台] namespace guard 数据迁移.

[策略] 1. 单纯裸 id ticker (无冲突, 15 个) → UPDATE entities.id 加 stock: 前缀,
        同步 UPDATE relations.source_id/target_id.
      2. 重复对 (裸 id 跟 stock: 前缀同义, 3 对) → 把裸 id 上的 relations 改指向
        stock:<id> (保留新格式), 然后 DELETE 裸 id entity.
      3. audit_log 留痕 (pass_name='namespace_migration',
        action_type='migrate_<pass>', ref_type='entity', status='applied').
      4. dry-run 默认, --yes 真跑.

[撤销] memory_audit_undo(<audit_id>) 走 revert_sql 还原. 或直接 restore_db.py
       恢复最近 snapshot.

[已知坑] - main_block_demo_stock_* (329 行) 不动 — 它们的 id 是 'main_block_demo_*'
            form, 跟正常 ticker (sh6/sz0) 不冲突, 跟 namespace guard 无关.
         - benchmark_user 跟 sh600028 等历史 83,034 条 relations 整体 UPDATE id.
           SQLite UPDATE 效率 OK, 一次 83K 行 < 1s.

[2026-08-09 plan] 单纯 15 个 + 重复 3 对 = 18 个 entity 改. 82414 条 relations
                  source_id/target_id 同步.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path


DB_PATH = Path("~/.hermes/memory/memory.db").expanduser()


def migrate(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """执行迁移. 返回统计 dict."""

    # 单纯 15 个裸 id ticker (kind='stock', id LIKE 'sh%' OR 'sz%', 不带 stock: 前缀, 无重复)
    bare_ids = [r[0] for r in conn.execute("SELECT id FROM entities WHERE kind='stock' AND (id LIKE 'sh%' OR id LIKE 'sz%') AND id NOT LIKE 'stock:%' ORDER BY id").fetchall()]

    # 已带 stock: 前缀 ticker
    ns_ids = [r[0] for r in conn.execute("SELECT id FROM entities WHERE kind='stock' AND id LIKE 'stock:%' ORDER BY id").fetchall()]

    # 重复对 (裸 id 跟 stock: 前缀同时存在)
    dup_pairs = [(bid, f"stock:{bid}") for bid in bare_ids if f"stock:{bid}" in ns_ids]

    # 单纯裸 id (无冲突)
    only_bare = [bid for bid in bare_ids if f"stock:{bid}" not in ns_ids]

    stats = {
        "only_bare": only_bare,
        "dup_pairs": dup_pairs,
        "total_entities_to_migrate": len(only_bare) + len(dup_pairs),
        "dry_run": dry_run,
    }

    print(f"=== 单纯裸 id (加前缀) ===")
    print(f"  计数: {len(only_bare)}")
    for bid in only_bare:
        print(f"    {bid} → stock:{bid}")
    print()
    print(f"=== 重复对 (合并到带前缀) ===")
    print(f"  计数: {len(dup_pairs)}")
    for bid, nid in dup_pairs:
        print(f"    {bid} → {nid} (合并, 保留 {nid})")
    print()

    if dry_run:
        print("=== DRY RUN 模式 ===")
        # 计算预估 relations 改写行数
        all_bare = only_bare + [bid for bid, _ in dup_pairs]
        placeholders = ",".join("?" * len(all_bare))
        rels_count = conn.execute(
            f"SELECT COUNT(*) FROM relations WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            all_bare + all_bare,
        ).fetchone()[0]
        stats["would_update_relations"] = rels_count
        print(f"  预估 relations 行数改写: {rels_count}")
        return stats

    # === 真跑 ===
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    audit_ids = []

    # 阶段 1: 单纯 15 个裸 id → 加前缀
    print(f"=== 阶段 1: 单纯 {len(only_bare)} 个裸 id 加 stock: 前缀 ===")
    for bid in only_bare:
        nid = f"stock:{bid}"
        # entities 表 id 改
        cur = conn.execute(
            "UPDATE entities SET id = ?, updated_at = ? WHERE id = ? AND valid_until IS NULL",
            (nid, ts, bid),
        )
        e_upd = cur.rowcount
        # relations source_id 同步
        cur = conn.execute("UPDATE relations SET source_id = ? WHERE source_id = ?", (nid, bid))
        r_src = cur.rowcount
        # relations target_id 同步
        cur = conn.execute("UPDATE relations SET target_id = ? WHERE target_id = ?", (nid, bid))
        r_tgt = cur.rowcount
        # audit_log 留痕
        cur = conn.execute(
            "INSERT INTO audit_log (run_id, pass_name, action_type, ref_type, ref_id, "
            "before_json, after_json, llm_used, status, created_at, revert_sql) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'applied', ?, ?)",
            (
                f"namespace_migration_{int(time.time())}_{bid}",
                "namespace_migration",
                "migrate_only_bare",
                "entity",
                nid,
                f'{{"id": "{bid}"}}',
                f'{{"id": "{nid}"}}',
                ts,
                # revert_sql: 还原 id
                f"UPDATE entities SET id = '{bid}' WHERE id = '{nid}' AND valid_until IS NULL; "
                f"UPDATE relations SET source_id = '{bid}' WHERE source_id = '{nid}'; "
                f"UPDATE relations SET target_id = '{bid}' WHERE target_id = '{nid}';",
            ),
        )
        audit_ids.append(cur.lastrowid)
        print(f"  ✅ {bid} → {nid}: entities={e_upd}, relations_src={r_src}, relations_tgt={r_tgt}")

    # 阶段 2: 3 对重复 → 合并
    print()
    print(f"=== 阶段 2: {len(dup_pairs)} 对重复合并 (删裸 id, relations 改目标) ===")
    for bid, nid in dup_pairs:
        # relations source_id 改指向 nid
        cur = conn.execute("UPDATE relations SET source_id = ? WHERE source_id = ?", (nid, bid))
        r_src = cur.rowcount
        # relations target_id 改指向 nid
        cur = conn.execute("UPDATE relations SET target_id = ? WHERE target_id = ?", (nid, bid))
        r_tgt = cur.rowcount
        # 删裸 id entity (硬删 — stock:<id> 已有正确行, 软删会留 valid_until 残留)
        # 但 schema 没说 DELETE, 让我看 — entities 是允许 DELETE 的 (purge 真清)
        # 走 DELETE 不用 audit revert_sql (target 已 migrated, 没法逆转).
        # 留 audit_log 留痕
        cur = conn.execute(
            "INSERT INTO audit_log (run_id, pass_name, action_type, ref_type, ref_id, "
            "before_json, after_json, llm_used, status, created_at, revert_sql) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'applied', ?, NULL)",
            (
                f"namespace_migration_{int(time.time())}_{bid}_merge",
                "namespace_migration",
                "migrate_dup_merge",
                "entity",
                bid,
                f'{{"id": "{bid}", "merged_into": "{nid}"}}',
                f'{{"id": "{nid}", "relations_absorbed": {r_src + r_tgt}}}',
                ts,
            ),
        )
        audit_ids.append(cur.lastrowid)
        # 删裸 id (NOTE: 物理删, revert_sql NULL, 恢复走 snapshot)
        conn.execute("DELETE FROM entities WHERE id = ?", (bid,))
        print(f"  ✅ {bid} → merged into {nid}: relations_src={r_src}, relations_tgt={r_tgt}, deleted {bid}")

    conn.commit()
    print()
    print(f"✅ 迁移完成. 写了 {len(audit_ids)} 条 audit_log.")
    stats["audit_ids"] = audit_ids
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="[8/9 review B1 台] stock namespace guard 数据迁移")
    parser.add_argument("--db", default=str(DB_PATH), help="mnelo db path")
    parser.add_argument("--dry-run", action="store_true", help="只看名单, 不改 db")
    parser.add_argument("--yes", action="store_true", help="真跑 (默认 dry-run)")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"✗ db not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        if not args.yes:
            migrate(conn, dry_run=True)
            print()
            print("=== DRY RUN 模式 — 没改 db. --yes 真跑 ===")
            return 0
        stats = migrate(conn, dry_run=False)
        print()
        print(f"=== 真跑完成 ===")
        print(f"  entity 改: {stats['total_entities_to_migrate']}")
        print(f"  audit_log: {len(stats.get('audit_ids', []))} 条")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
