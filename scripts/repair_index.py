#!/usr/bin/env python3
"""A7 — scripts/repair_index.py (TASKS_SEARCH_INDEX §4 A7 + 8/6 plan §5).

索引修复 + 完整性校验 (orphan vector 删除).
Q1 孤儿向量: usearch/zvec 索引写入在 SQLite 事务外 — 若 remember SQLite 侧最终
ROLLBACK, 索引留下指向不存在 chunk 的向量.

[8/6 plan] 向量库二选一: 删 SQLiteVecIndex 分支. backend ∈ {auto, usearch, zvec}.

[8/9 a7 fix] 不再硬编码 dim=512 — 探测磁盘已有 usearch 索引的真实维度
(从 usearch 文件头 Index.metadata 读). 否则磁盘索引维度 ≠ 512 时,
UsearchIndex.__init__ 预检不过 → 触发 _auto_rebuild 全量重建 (孤儿被
顺带丢掉, repair 报 deleted:0 误导; 重建 embedder 512 维写低维索引还会崩).
repair 语义是"清孤儿", 不该顺带重建.

usage: python scripts/repair_index.py [--backend usearch|zvec|auto] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config as _config  # noqa: E402
from search_index import (  # noqa: E402
    UsearchIndex,
    ZvecIndex,
    build_search_index,
    zvec_available,
)


def _iter_index_ids_usearch(idx: UsearchIndex):
    """遍历 usearch 索引内所有 id (uint64 rowid)."""
    return [int(k) for k in idx._index.keys]


def _iter_index_ids_zvec(idx: ZvecIndex):
    """zvec: doc.id 直接是 chunk_id."""
    try:
        return [d.id for d in idx._col.iter_all()]
    except Exception as e:
        print(f"[repair_index] zvec.iter_all failed: {e}", file=sys.stderr)
        return []


def _resolve_backend(requested: str) -> str:
    """最终后端解析, 镜像 search_index._pick_backend 优先级 (auto: zvec > usearch).

    repair 需要先知道最终后端, 才能决定探测哪个索引文件 (usearch.index 头 vs zvec 集合).
    """
    if requested == "auto":
        return "zvec" if zvec_available() else "usearch"
    if requested in ("usearch", "zvec"):
        return requested
    # 未知值 → _pick_backend 的兜底也是 auto 链
    return "zvec" if zvec_available() else "usearch"


def _probe_usearch_dim(db_path: Path) -> int:
    """探测磁盘已有 usearch 索引的真实维度 (文件头), 防硬编码 512 触发 _auto_rebuild.

    读不到/损坏 → 回落 512 (此时 UsearchIndex 预检失败, _auto_rebuild 正常兜底重建).
    """
    # [8/12 fix] 索引路径跟 db_path.stem 绑定 — 跟 search_index.py:412 一致
    idx_path = db_path.parent / f"{db_path.stem}.usearch.index"
    if not idx_path.exists():
        return 512
    try:
        from usearch.index import Index

        dims = Index.metadata(idx_path).get("dimensions")
        if dims is not None:
            return int(dims)
    except Exception as e:
        print(f"[repair_index] 探测 usearch 索引维度失败 ({e}), 回落 512", file=sys.stderr)
    return 512


def repair(backend: str, db_path: Path, dry_run: bool = False) -> dict:
    """遍历索引 → 查 SQLite 活跃 chunks → 删无对应项 (orphan). Returns stats."""
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")

    resolved = _resolve_backend(backend)
    # [8/9 a7 fix] usearch 探真实维度 (孤儿删除在磁盘索引上做, 不应触发重建);
    # zvec 集合 schema 固定 512 (embedder 默认), 且无维度校验重建, 保持原样.
    dim = _probe_usearch_dim(db_path) if resolved == "usearch" else 512
    idx = build_search_index(resolved, db_path, dim=dim)
    deleted = 0
    kept = 0
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # 取所有活跃 chunk_id
        alive = {r[0] for r in conn.execute("SELECT id FROM chunks WHERE valid_until IS NULL")}
        if isinstance(idx, UsearchIndex):
            rowids = _iter_index_ids_usearch(idx)
            import numpy as _np

            for rid in rowids:
                row = conn.execute(
                    "SELECT id FROM chunks WHERE rowid = ? AND valid_until IS NULL",
                    (rid,),
                ).fetchone()
                if row:
                    kept += 1
                else:
                    if not dry_run:
                        idx._index.remove(_np.array([rid], dtype=_np.uint64))
                    deleted += 1
        elif isinstance(idx, ZvecIndex):
            ids = _iter_index_ids_zvec(idx)
            for cid in ids:
                if cid in alive:
                    kept += 1
                else:
                    if not dry_run:
                        idx._col.delete([cid])
                    deleted += 1
        conn.close()
    finally:
        idx.close()

    return {
        "backend": backend,
        "backend_resolved": idx.name,
        "kept": kept,
        "deleted": deleted,
        "dry_run": dry_run,
    }


def main():
    ap = argparse.ArgumentParser(description="Repair mnelo search index (orphan vector cleanup). [8/6] 后端感知 (usearch/zvec).")
    ap.add_argument("--backend", default="auto", choices=["auto", "usearch", "zvec"], help="目标后端 (默认 auto: zvec > usearch; 都不可用 RuntimeError)")
    ap.add_argument("--dry-run", action="store_true", help="只报数, 不真删")
    ap.add_argument("--db", default=None, help="db 路径 (默认从 config 解析)")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else Path(_config.db_path)
    stats = repair(args.backend, db_path, dry_run=args.dry_run)
    print(stats)


if __name__ == "__main__":
    main()
