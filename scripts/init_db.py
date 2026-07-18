#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init_db.py — 初始化 ~/.hermes/memory/memory.db

[7/18 自建]
- 主人口中 7/18 拍板自建 KG, 替换 Mnemosyne
- WAL mode + busy_timeout=30s 防止 lock 复发
- [7/19] embedding 模型 + dim 从 config 读 (config.toml [embedder] 或 env override)
"""
import sqlite3
import sqlite_vec
import sys
from pathlib import Path

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')
SCHEMA_PATH = Path('/Users/apple/.hermes/memory/schema.sql')


def init():
    if DB_PATH.exists():
        # [7/19 P2-7] 只打印 basename, 不暴露绝对路径 (cron 输出可能 world-readable log)
        print(f'⚠️  memory.db 已存在: {DB_PATH.name}')
        print(f'   如要重置, 请先删: rm {DB_PATH.name}')
        sys.exit(1)

    # 读 embedder config — 失败回落到默认 (bge-small-zh, 512d)
    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from config import config as _config
        embed_model = _config.embedder_model
        embed_dim = _config.embedder_dim
        print(f'=== 0. Embedder config: {embed_model} ({embed_dim}d) ===')
    except Exception as e:
        print(f'⚠️  config 加载失败 ({e}), 回落默认 bge-small-zh-v1.5/512d')
        embed_model = 'BAAI/bge-small-zh-v1.5'
        embed_dim = 512

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    print(f'=== 1. 创建 memory.db ===')
    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    print(f'=== 2. 启用 WAL + busy_timeout ===')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA foreign_keys = ON')

    print(f'=== 3. 执行 schema.sql (含 dim 占位符替换) ===')
    with open(SCHEMA_PATH) as f:
        sql = f.read()
    # 占位符替换 — 必须跟 schema.sql 里的 {EMBED_DIM}/{EMBED_MODEL} 一致
    sql = sql.replace('{EMBED_DIM}', str(embed_dim))
    sql = sql.replace('{EMBED_MODEL}', embed_model.replace("'", "''"))  # SQL 单引号转义
    # [7/19 P2-8 fix] executescript 接受任意 ; 串接, 改用 split + execute per stmt
    # 防止恶意 schema.sql 注入额外 SQL (embed_model escape 防 SQL 单引号注入,
    # 但 ; 分隔符之前没有防御)
    for stmt in sql.split(';'):
        stmt = stmt.strip()
        if stmt and not stmt.startswith('--'):
            conn.execute(stmt)
    conn.commit()

    print(f'=== 4. 验证表 ===')
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print(f'  表 (含虚拟): {tables}')

    print(f'=== 5. 验证触发器 ===')
    triggers = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()]
    print(f'  触发器: {triggers}')

    print(f'=== 6. 验证 meta ===')
    for k, v in conn.execute("SELECT key, value FROM meta").fetchall():
        print(f'  {k} = {v}')

    print(f'=== 7. 验证 vec0 可用 (dim={embed_dim}) ===')
    test_emb = [0.0] * embed_dim  # dim 从 config 读, 不再硬编码 512
    test_bytes = sqlite_vec.serialize_float32(test_emb)
    conn.execute("INSERT INTO vectors (rowid, embedding) VALUES (?, ?)", (1, test_bytes))
    conn.execute("DELETE FROM vectors WHERE rowid = 1")
    conn.commit()

    conn.close()

    size_kb = DB_PATH.stat().st_size / 1024
    print()
    print(f'✅ 初始化完成: {DB_PATH} ({size_kb:.1f} KB)')
    print(f'   Embedder: {embed_model} ({embed_dim}d)')


if __name__ == '__main__':
    init()
