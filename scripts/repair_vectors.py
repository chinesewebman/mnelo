#!/usr/bin/env python3
"""
repair_vectors.py — 修复 sqlite-vec 0.1.x + remember() 错位的 vec0 rowid.

[7/18 P2 fix]
[Bug]: remember() 之前用 last_insert_rowid() 拿 chunks.rowid, 但 entities/relations
INSERT 后 last_insert_rowid 被覆盖. 导致 ~40% chunk 的 vec0 rowid 错位.
向量召回因此漏召回那部分 chunk (~1500 条历史数据).

[修法]:
1. 找所有未同步的 chunk (chunks WHERE NOT EXISTS vec0 rowid = chunks.rowid)
2. 重新嵌入 content
3. INSERT vec0 用正确 rowid

: 不重嵌入不用embedding 模型很难, 必须用 bge-small-zh 重嵌入
但只针对错位的 chunks (~1591 条), 每次 embed ~1.3ms, ~2 sec

运行:  /Users/apple/hermes-agent/venv/bin/python3 repair_vectors.py [--dry-run]
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')
MEMORY_DIR = Path('/Users/apple/.hermes/memory')

sys.path.insert(0, str(MEMORY_DIR))


def repair(dry_run=True):
    """Re-sync vec0 rowid with chunks rowid."""
    try:
        import sqlite_vec
    except ImportError:
        print('WARN: sqlite-vec not installed. Run: pip install sqlite-vec')
        sys.exit(1)

    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.execute('PRAGMA journal_mode = WAL')
    con.execute('PRAGMA busy_timeout = 30000')
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.row_factory = sqlite3.Row

    # 1. 找未同步的 chunks
    miss = con.execute('''
        SELECT c.rowid, c.id, c.content
        FROM chunks c
        LEFT JOIN vectors v ON v.rowid = c.rowid
        WHERE c.valid_until IS NULL AND v.rowid IS NULL
        ORDER BY c.rowid DESC
    ''').fetchall()
    print(f'未同步 chunks: {len(miss)} 条')

    if not miss:
        print('✅ 无需修复')
        return

    if dry_run:
        print(f'  (dry-run 模式, 不实际改) — {len(miss)} 条需修复')
        print()
        # 抽 3 条示例
        for r in miss[:3]:
            print(f"  chunk rowid={r['rowid']} id={r['id']}")
            print(f"    content: {r['content'][:80]}")
        return

    # 2. 实际修复: 从 embedding 中找 + INSERT
    from embedder import embed_bytes  # loaded from same dir

    fixed = 0
    failed = 0
    for r in miss:
        try:
            v_bytes = embed_bytes(r['content'])
            con.execute(
                'INSERT INTO vectors (rowid, embedding) VALUES (?, ?)',
                (r['rowid'], v_bytes),
            )
            fixed += 1
            if fixed % 50 == 0:
                print(f'  修复进度: {fixed}/{len(miss)}')
                con.commit()
        except Exception as e:
            failed += 1
            print(f'  ❌ rowid={r["rowid"]} embed fail: {e}')
    con.commit()

    # 3. 验证
    after = con.execute('''
        SELECT COUNT(*) FROM chunks c
        LEFT JOIN vectors v ON v.rowid = c.rowid
        WHERE c.valid_until IS NULL AND v.rowid IS NULL
    ''').fetchone()[0]

    print(f'\n✅ 修复完成: {fixed} 条成功 / {failed} 条失败 / 残留 {after} 条未同步')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只检查, 不写入')
    args = ap.parse_args()
    repair(dry_run=args.dry_run)
