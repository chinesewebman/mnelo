#!/usr/bin/env python3
"""A6 — scripts/rebuild_index.py (TASKS_SEARCH_INDEX §4 A6).

后端切换重建索引脚本. usage: python scripts/rebuild_index.py [--backend usearch|sqlite_vec|auto]

[8/5 主人决策] auto 优先级: zvec > usearch > sqlite_vec (同 build_search_index).

[§4 A6 验收] sqlite_vec → usearch 切换后跑此脚本, recall 命中率恢复.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import struct
import sys
from pathlib import Path

# 路径: scripts/ 在 repo 根的下一层; 父目录是 repo
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from embedder import embed  # noqa: E402
from search_index import build_search_index  # noqa: E402

# [8/5 fix] 默认 DB 路径从 config 解析 (env > config.toml > ~/.hermes/memory/memory.db)
from config import config as _config  # noqa: E402


def _iter_chunks(conn):
    """遍历所有 valid chunks (valid_until IS NULL), yield (chunk_id, content)."""
    rows = conn.execute("SELECT id, content FROM chunks WHERE valid_until IS NULL").fetchall()
    for r in rows:
        yield r["id"], r["content"]


def _load_embedding(content: str) -> bytes:
    """[A6 §4] 调 embedder 重新嵌入 → float32 bytes (与 sqlite-vec 兼容)."""
    vec = embed(content)
    return struct.pack(f"{len(vec)}f", *vec)


def _unlink_existing_index_files(db_path: Path) -> dict:
    """[8/6 plan §6] fresh=True 时清掉旧索引文件, 避免残留 rowid 干扰.
    [8/6 fix] zvec 0.6 collection 是目录不是文件, 用 shutil.rmtree 替代 unlink.
    """
    import shutil

    removed = []
    # usearch: 单文件 .index
    p_usearch = db_path.parent / "usearch.index"
    if p_usearch.exists():
        try:
            p_usearch.unlink()
            removed.append(str(p_usearch))
        except OSError as e:
            print(f"[rebuild] failed to unlink {p_usearch}: {e}", file=sys.stderr)
    # zvec: collection 目录
    p_zvec = db_path.parent / "search_index.zv"
    if p_zvec.exists():
        try:
            if p_zvec.is_dir():
                shutil.rmtree(p_zvec)
            else:
                p_zvec.unlink()
            removed.append(str(p_zvec))
        except OSError as e:
            print(f"[rebuild] failed to remove {p_zvec}: {e}", file=sys.stderr)
    return {"removed": removed}


def rebuild(backend: str, db_path: Path, dry_run: bool = False, fresh: bool = False) -> dict:
    """重建索引: 可选 fresh (清旧索引文件) → 全量 re-add. Returns stats dict."""
    if not db_path.exists():
        raise FileNotFoundError(f"db not found: {db_path}")

    removed_files: dict = {}
    if fresh and not dry_run:
        removed_files = _unlink_existing_index_files(db_path)

    if not dry_run:
        idx = build_search_index(backend, db_path, dim=512)
    else:
        idx = None

    added = 0
    failed = 0
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for cid, content in _iter_chunks(conn):
            if dry_run:
                added += 1
                continue
            try:
                vec = _load_embedding(content)
                idx.add(cid, vec, conn=conn, content=content)
                added += 1
            except Exception as e:
                print(f"[rebuild] failed for {cid}: {e}")
                failed += 1
        conn.close()
    finally:
        if idx is not None:
            idx.close()

    return {
        "backend": backend,
        "backend_resolved": idx.name if idx else "(dry-run)",
        "added": added,
        "failed": failed,
        "dry_run": dry_run,
        "fresh": fresh,
        "removed_files": removed_files,
    }


def main():
    ap = argparse.ArgumentParser(description="Rebuild mnelo search index (后端感知 usearch/zvec)")
    ap.add_argument("--backend", default="auto", choices=["auto", "usearch", "zvec"], help="目标后端 (默认 auto: zvec > usearch; 都不可用 RuntimeError)")
    ap.add_argument("--dry-run", action="store_true", help="只统计, 不真正重建")
    ap.add_argument("--fresh", action="store_true", help="先 unlink 旧索引文件 (usearch.index / search_index.zv), 全新重建")
    ap.add_argument("--db", default=None, help="db 路径 (默认从 config 解析)")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else Path(_config.db_path)
    stats = rebuild(args.backend, db_path, dry_run=args.dry_run, fresh=args.fresh)
    print(stats)


if __name__ == "__main__":
    main()
