#!/usr/bin/env python3
"""
cleanup_demo_entities_2026_08_09.py — [8/9 燕如 P5 反馈] 清 demo entities.

燕如 8/9 P5 报告: mnelo recall 不带 source filter 时看到 ~8 个 demo stock
entities. 数据盘点 demo 实际有 658 条 (~88% of active entities), 都是
main_block_demo_* prefix, 来自 v0.5.x 早期 main_block_demo_stock / person 教程脚本.

[策略] 软删 (走 Memory.forget → valid_until = now + audit_log 留痕 + 进
purged_queue), 走 30 天 audit_undo window.
[为什么软删不用物理 DELETE] SOUL §mnelo ops #3: purge_backlog 是 TTL 30 天延迟
队列 design, destructive 真清绕过 audit window 失去 memory_audit_undo 保护.

[8/9 review B7 fix] 原代码 raw SQL UPDATE + plain file audit, 不进 audit_log 表
不排队 purged_queue, undo 机制不兑现. 改走 Memory.forget 接口:
  - 自动写 audit_log (pass_name='forced_forget', action_type='demo_cleanup',
    ref_type='entity', status='applied', created_at=now())
  - 自动排队 purged_queue (30 天 TTL 后真清)
  - undo 走标准 memory_audit_undo(<audit_id>)
[时间戳] 原代码 datetime('now','localtime') 空格分隔破坏 asof 回放字典序.
now() 默认 ISO 8601 无空格.

[过滤] id LIKE 'main_block_demo_%' (658 条都属此 prefix). user_confirmed=0
不能当 demo 标志 (主人真实 stock 也都是 user_confirmed=0).

[已知坑]
- main_block_demo_* 是 7/19 早期 main_block tutorial 脚本产物.
  清理后 script 05_main_block_demo_*.py 等 main_block.py 教程脚本会建
  新的 demo, 报冲突. 教程脚本应该也清掉或改 demo id 模板.
- 软删后 30 天内 memory_audit_undo 可恢复 (audit_log 留痕 + purged_queue 排队).
- 跑前用 --dry-run 看名单, --yes 真跑.
"""

import argparse
import sys
from pathlib import Path


DB_PATH = Path("~/.hermes/memory/memory.db").expanduser()
DEMO_PREFIX = "main_block_demo_"


def get_demo_entities(m) -> list:
    """返回所有 active demo entities (id 匹配 main_block_demo_*)."""
    return m._conn.execute(
        "SELECT id, kind, source, importance, user_confirmed FROM entities WHERE valid_until IS NULL AND id LIKE ? ORDER BY id",
        (DEMO_PREFIX + "%",),
    ).fetchall()


def main() -> int:
    parser = argparse.ArgumentParser(description="[8/9 燕如 P5 反馈] 清 demo entities (main_block_demo_*)")
    parser.add_argument("--db", default=str(DB_PATH), help="mnelo db path")
    parser.add_argument("--dry-run", action="store_true", help="只列名单, 不改 db")
    parser.add_argument("--yes", action="store_true", help="真删 (默认 dry-run)")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"✗ db not found: {db_path}", file=sys.stderr)
        return 1

    # [8/9 review B7 fix] 用 Memory.forget 接口, 不 raw SQL. 接口自动写
    # audit_log + purged_queue, undo 机制可兑现.
    from memory import Memory

    m = Memory(db_path=db_path)
    try:
        demo_entities = get_demo_entities(m)
        if not demo_entities:
            print("✓ no demo entities to clean")
            return 0

        # 按 kind 分组统计
        by_kind: dict = {}
        for ent in demo_entities:
            by_kind[ent[1]] = by_kind.get(ent[1], 0) + 1

        print(f"=== Demo entities cleanup ===")
        print(f"db: {db_path}")
        print(f"total demo entities: {len(demo_entities)}")
        print(f"by kind:")
        for kind, cnt in sorted(by_kind.items()):
            print(f"  {kind}: {cnt}")
        print()

        # 显示前 10 个样本
        print(f"sample (first 10):")
        for ent in demo_entities[:10]:
            print(f"  {ent[0]} | {ent[1]} | src={ent[2] or '-'} | imp={ent[3]} | confirmed={ent[4]}")
        if len(demo_entities) > 10:
            print(f"  ... and {len(demo_entities) - 10} more")
        print()

        if not args.yes:
            print("=== DRY RUN (--yes to actually delete) ===")
            print(f"将软删 {len(demo_entities)} 条 entities (走 Memory.forget 接口)")
            print(f"audit_log + purged_queue 自动写, 30 天内 memory_audit_undo 可恢复")
            return 0

        # 真删 — 走 Memory.forget, 自动 audit_log + purged_queue
        print(f"=== DELETING (via Memory.forget interface) ===")
        for i, ent in enumerate(demo_entities, 1):
            try:
                m.forget(
                    target_id=ent[0],
                    target_kind="entity",
                    reason="demo_cleanup_2026_08_09",
                )
            except Exception as e:
                print(f"  ⚠️  {ent[0]} forget failed: {e}", file=sys.stderr)
                continue
            if i % 100 == 0:
                print(f"  {i}/{len(demo_entities)} deleted")
        m._conn.commit()
        print(f"✓ {len(demo_entities)} entities soft-deleted (audit_log + purged_queue)")
        print(f"30 天内可用 memory_audit_undo(<audit_id>) 恢复")
        return 0
    finally:
        m._conn.close()


if __name__ == "__main__":
    sys.exit(main())
