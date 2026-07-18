#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init_db.py — 初始化 ~/.hermes/memory/memory.db

[7/18 自建]
- 主人口中 7/18 拍板自建 KG, 替换 Mnemosyne
- WAL mode + busy_timeout=30s 防止 lock 复发
"""
import sqlite3
import sqlite_vec
import sys
from pathlib import Path

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')
SCHEMA_PATH = Path('/Users/apple/.hermes/memory/schema.sql')


def init():
    if DB_PATH.exists():
        print(f'⚠️  memory.db 已存在: {DB_PATH}')
        print(f'   如要重置, 请先删: rm {DB_PATH}')
        sys.exit(1)

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

    print(f'=== 3. 执行 schema.sql ===')
    with open(SCHEMA_PATH) as f:
        sql = f.read()
    conn.executescript(sql)
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

    print(f'=== 7. 验证 vec0 可用 ===')
    test_emb = [0.0] * 512
    test_bytes = sqlite_vec.serialize_float32(test_emb)
    conn.execute("INSERT INTO vectors (rowid, embedding) VALUES (?, ?)", (1, test_bytes))
    conn.execute("DELETE FROM vectors WHERE rowid = 1")
    conn.commit()

    conn.close()

    size_kb = DB_PATH.stat().st_size / 1024
    print()
    print(f'✅ 初始化完成: {DB_PATH} ({size_kb:.1f} KB)')


if __name__ == '__main__':
    init()
