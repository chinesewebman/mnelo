#!/usr/bin/env python3
"""
[8/4 P1a E5] backfill_memory_type.py — 回填存量 chunks 的 memory_type.

DESIGN §5.2 P1a + TASKS_L2_EXTRACT §E5:
- 实际 4344/4344 chunks 100% fact (v0.3 报告 §2) — 写路径 P1a 已修 (Batch 2)
- 本脚本: 存量 chunks 一键升级 (确定性规则, 直接 UPDATE, 无 LLM)
- H0 落地后 L2 分类走提案链, 本脚本保持"一次性迁移"边界

用法:
    python3 backfill_memory_type.py --dry-run          # 看分类计数, 不动数据
    python3 backfill_memory_type.py --limit 100        # 真跑, 限制 100 chunk
    python3 backfill_memory_type.py                    # 真跑, 不限

设计原则 (§5.5 宁缺毋滥):
- 只回填 memory_type='fact' AND valid_until IS NULL 的 chunk (避开软删)
- 强标记命中才改; 无命中保持 fact
- 显式 fact 也是'fact', 不会被规则覆盖 (尊重原值)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# [7/21 fix] 跟 init_db.py 同款 path 解析 (config > env > default)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import resolve_db_path  # noqa: E402

# 复用 classify 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from classify import classify_memory_type  # noqa: E402


def get_db_path() -> Path:
    return resolve_db_path()


def query_candidates(con: sqlite3.Connection, limit: int | None, reclassify: bool = False) -> list[tuple[str, str, str]]:
    """取待回填 chunks.

    Args:
        reclassify: [8/4 v0.2] True = 回填所有非 fact chunk (还原误标); False = 只回填 fact
    """
    if reclassify:
        # [8/4 v0.2] 全表回填: 还原误标的 procedure/episode/decision/preference/ephemeral
        sql = "SELECT id, content, memory_type FROM chunks WHERE memory_type != 'fact' AND valid_until IS NULL"
    else:
        sql = "SELECT id, content, memory_type FROM chunks WHERE memory_type='fact' AND valid_until IS NULL"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [(r[0], r[1], r[2]) for r in con.execute(sql).fetchall()]


def run(dry_run: bool, limit: int | None, reclassify: bool = False) -> None:
    db_path = get_db_path()
    if not db_path.exists():
        print(f"❌ memory.db 不存在: {db_path}")
        sys.exit(1)

    print("=== backfill_memory_type (P1a E5 v0.2) ===")
    print(f"  DB: {db_path}")
    print(f"  mode: {'dry-run' if dry_run else 'APPLY'}")
    if reclassify:
        print("  [v0.2] reclassify=True: 回填所有非 fact chunk (还原 v0.1 误标)")
    if limit is not None:
        print(f"  limit: {limit}")
    print()

    con = sqlite3.connect(str(db_path))
    candidates = query_candidates(con, limit, reclassify=reclassify)
    print(f"[1] 候选 chunks (reclassify={reclassify}): {len(candidates)}")

    if not candidates:
        print("    无候选, 退出")
        con.close()
        return

    # 分类 (v0.2: candidates 是 (id, content, current_type) tuple)
    type_counts: Counter[str] = Counter()
    changes: list[tuple[str, str, str]] = []  # (chunk_id, current_type, new_type)
    for row in candidates:
        chunk_id, content, current_type = row
        inferred = classify_memory_type(content)
        new_type = inferred if inferred is not None else "fact"
        if new_type != current_type:
            type_counts[f"{current_type} -> {new_type}"] += 1
            changes.append((chunk_id, current_type, new_type))

    print()
    print("[2] 分类结果:")
    if not changes:
        print("    无变化 (所有 chunk 当前类型已正确)")
        con.close()
        return

    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t}: {n}")
    will_change = len(changes)
    will_stay = len(candidates) - will_change
    print(f"    will_change: {will_change} ({will_change * 100 / len(candidates):.1f}%)")
    print(f"    will_stay:   {will_stay} ({will_stay * 100 / len(candidates):.1f}%)")

    if dry_run:
        print()
        print("[3] dry-run: 不会改数据. 上面是预览.")
        con.close()
        return

    # 真跑: UPDATE
    print()
    print(f"[3] APPLY: UPDATE {len(changes)} chunks ...")
    cur = con.executemany(
        "UPDATE chunks SET memory_type = ? WHERE id = ? AND valid_until IS NULL",
        [(new_type, chunk_id) for chunk_id, _, new_type in changes],
    )
    con.commit()
    print(f"    UPDATE done: {cur.rowcount} rows")

    # 验证: 跑后分布
    print()
    print("[4] 跑后 memory_type 分布:")
    for r in con.execute("SELECT memory_type, COUNT(*) FROM chunks WHERE valid_until IS NULL GROUP BY memory_type ORDER BY 2 DESC").fetchall():
        print(f"    {r[0]}: {r[1]}")

    con.close()
    print()
    print("=== 完成 ===")


def main():
    parser = argparse.ArgumentParser(
        description="[P1a E5 v0.2] 回填存量 chunks 的 memory_type (确定性规则, 无 LLM)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只报数, 不 UPDATE (默认 False)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="限制回填数量 (默认无限制, 全表)",
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="[v0.2] 回填所有非 fact chunk (还原 v0.1 误标)",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, limit=args.limit, reclassify=args.reclassify)


if __name__ == "__main__":
    main()
