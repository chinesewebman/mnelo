"""tests/helpers.py — 共享后端感知清理 helper (8/6 plan §9).

跨测试用 cleanup_chunks(mem, ...) 替直接 DELETE FROM vectors / chunks:
  - 先 mem._index.remove(cid, conn=mem._conn) (走 usearch/zvec 索引)
  - 再 DELETE chunks 行
顺序关键: remove 内部要查 chunks 表拿 rowid → 必须先 remove 再 DELETE.
"""

from __future__ import annotations


def cleanup_chunks(mem, chunk_ids=None, source=None, source_pattern=None):
    """先 mem._index.remove(cid) 再 DELETE chunks 行. 各后端通用.

    Args:
        mem: Memory 实例
        chunk_ids: 显式 chunk_id 列表
        source: 精确 source 字符串
        source_pattern: LIKE 模式 (e.g. 'test_%')
    """
    ids = set(chunk_ids or [])
    if source:
        ids.update(r[0] for r in mem._conn.execute("SELECT id FROM chunks WHERE source = ?", (source,)).fetchall())
    if source_pattern:
        ids.update(r[0] for r in mem._conn.execute("SELECT id FROM chunks WHERE source LIKE ?", (source_pattern,)).fetchall())
    for cid in ids:
        try:
            mem._index.remove(cid, conn=mem._conn)
        except Exception:
            pass  # 幂等 (chunk 可能已不在索引)
        mem._conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
    mem._conn.commit()
