#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init_db.py — 初始化 ~/.hermes/memory/memory.db

- WAL mode + busy_timeout=30s 防止 lock 复发
- [7/19] embedding 模型 + dim 从 config 读 (config.toml [embedder] 或 env override)
"""

import re
import sqlite3
import sqlite_vec
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# [7/21 fix] DB 路径从 config 解析 (env MNELO_MEMORY_DIR/MNELO_MEMORY_DB_PATH > ~/.hermes/memory)。
# SCHEMA_PATH: 优先 live 目录下的 schema.sql (旧部署), 回落 repo 自带 schema.sql。
from config import config as _config, resolve_db_path as _resolve_db_path

DB_PATH = _resolve_db_path()
_LIVE_DIR = DB_PATH.parent
_REPO_SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"
SCHEMA_PATH = _LIVE_DIR / "schema.sql" if (_LIVE_DIR / "schema.sql").exists() else _REPO_SCHEMA


def init():
    if DB_PATH.exists():
        # [7/19 P2-7] 只打印 basename, 不暴露绝对路径 (cron 输出可能 world-readable log)
        print(f"⚠️  memory.db 已存在: {DB_PATH.name}")
        print(f"   如要重置, 请先删: rm {DB_PATH.name}")
        sys.exit(1)

    # 读 embedder config — 失败回落到默认 (bge-small-zh, 512d)
    try:
        embed_model = _config.embedder_model
        embed_dim = _config.embedder_dim
        print(f"=== 0. Embedder config: {embed_model} ({embed_dim}d) ===")
    except Exception as e:
        print(f"⚠️  config 加载失败 ({e}), 回落默认 bge-small-zh-v1.5/512d")
        embed_model = "BAAI/bge-small-zh-v1.5"
        embed_dim = 512

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f"=== 1. 创建 memory.db ===")
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    print(f"=== 2. 启用 WAL + busy_timeout ===")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")

    print(f"=== 3. 执行 schema.sql (含 dim 占位符替换) ===")
    with open(SCHEMA_PATH) as f:
        sql = f.read()

    # [7/21 fix] embed_model 白名单校验 — 只允许 fastembed 模型名格式
    # (org/name: 字母数字 + `-_.`/)，拒绝任何引号/分号/换行，杜绝注入。
    # 非法值回落到默认模型, 而不是带病执行。
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", embed_model or ""):
        print(f"⚠️  embed_model {embed_model!r} 含非法字符, 回落 bge-small-zh-v1.5")
        embed_model = "BAAI/bge-small-zh-v1.5"
    # 占位符替换 — 必须跟 schema.sql 里的 {EMBED_DIM}/{EMBED_MODEL} 一致
    sql = sql.replace("{EMBED_DIM}", str(embed_dim))
    sql = sql.replace("{EMBED_MODEL}", embed_model)

    # [7/21 fix] 用 executescript 一次性执行。
    # 之前的 split(";") 逐条执行有 2 个 bug:
    #  1) 前导 `--` 注释的建表语句被 `stmt.startswith("--")` 整块跳过
    #  2) CREATE TRIGGER 内部含分号, 被 ; 拦腰截断
    # 结果: init_db.py 永远无法初始化 repo 自带的 schema.sql。
    # executescript 是 sqlite3 处理多语句的标准方式; embed_model 已白名单
    # 校验 + schema.sql 是 repo 自持文件, 注入面已关闭。
    conn.executescript(sql)
    conn.commit()

    print(f"=== 4. 验证表 ===")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    print(f"  表 (含虚拟): {tables}")

    print(f"=== 5. 验证触发器 ===")
    triggers = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall()]
    print(f"  触发器: {triggers}")

    print(f"=== 6. 验证 meta ===")
    for k, v in conn.execute("SELECT key, value FROM meta").fetchall():
        print(f"  {k} = {v}")

    print(f"=== 7. 验证 vec0 可用 (dim={embed_dim}) ===")
    test_emb = [0.0] * embed_dim  # dim 从 config 读, 不再硬编码 512
    test_bytes = sqlite_vec.serialize_float32(test_emb)
    conn.execute("INSERT INTO vectors (rowid, embedding) VALUES (?, ?)", (1, test_bytes))
    conn.execute("DELETE FROM vectors WHERE rowid = 1")
    conn.commit()

    conn.close()

    size_kb = DB_PATH.stat().st_size / 1024
    print()
    print(f"✅ 初始化完成: {DB_PATH} ({size_kb:.1f} KB)")
    print(f"   Embedder: {embed_model} ({embed_dim}d)")


if __name__ == "__main__":
    init()
