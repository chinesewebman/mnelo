"""
[8/6 M29 review-pass 3008480] 2 发现整改测试.

覆盖:
  M29.1 [中] _setup() 清理前缀跟 task_create 实际 id 匹配 (含日期前缀), 跑后无残留污染
  M29.2 [低] REPO 路径用 Path(__file__).resolve().parent.parent, 不硬编码作者本机路径
"""

import sys
from pathlib import Path

# [M29.2 fix] REPO 走 __file__ 解析, 不硬编码绝对路径
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import os

os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

import sqlite3
import memory


def test_m29_1_setup_prefix_matches_actual_ids():
    """[M29.1] _setup() 清理前缀覆盖 task_create 实际生成的 id 形式.

    task_create 生成 id 为 'task:YYYYMMDD-<slug>', 例如 'task:20260806-e2e-restock-1'.
    旧前缀 'task:e2e%' 匹配不到 (没日期前缀), 残留污染. 验证: 跑一遍测试,
    再跑 setup, 应清掉所有 e2e 相关行 (含日期前缀).
    """
    # 1. 建一些 e2e 测试用的 task + chunk
    m = memory.Memory()
    try:
        # [M29 fix] 用 e2e-prefix name — 跟 e2e fixture 清理范围匹配.
        # task_create 生成 id 为 'task:YYYYMMDD-<slug>', 例如 'task:20260806-e2e-test-m29'.
        # 旧 e2e fixture 只删 'task:e2e%' (没日期前缀) 漏删, 本 test 走 e2e prefix
        # 让 fixture 能命中.
        import task_states as ts

        r = ts.task_create(m._conn, name="e2e-test-m29", now="2026-08-06T15:00")
        tid = r["task_id"]
        m._conn.commit()
        # 校验 id 含日期前缀
        import re as _re

        assert _re.match(r"^task:\d{8}-e2e-", tid), f"expected task:YYYYMMDD-e2e-* id, got {tid}"
        # 插入 e2e chunk
        c = m._conn.execute(
            "INSERT OR IGNORE INTO chunks (id, content, source, memory_type, importance, valid_until, created_at, processed_at) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            ("chunk:e2e-test-m29", "test content", "test:e2e_m29_test", "episodic", 0.5, "2026-08-06T15:00"),
        )
        m._conn.commit()
    finally:
        m.close()

    # 2. 跑 test_m5_4 的 _setup (复用其 fixture)
    from tests.test_m5_4_e2e_purchase import _setup as e2e_setup

    e2e_setup()

    # 3. 校验残留: 本 task + 本 chunk 应被清掉
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        n_task = c.execute("SELECT COUNT(*) FROM task_states WHERE task_id LIKE 'task:%e2e-%' AND valid_until IS NULL").fetchone()[0]
        n_entity = c.execute("SELECT COUNT(*) FROM entities WHERE id LIKE 'task:%e2e-%' AND valid_until IS NULL").fetchone()[0]
        n_chunk = c.execute("SELECT COUNT(*) FROM chunks WHERE id LIKE 'chunk:e2e-%' OR source LIKE '%e2e-%'").fetchone()[0]
        # 同时校验 m5-stale-* 也清掉 (e2e setup 跨 test 不该受影响, 但本 test 不创建 m5-stale)
        # 我们自己刚创建的 m29 应该被清
        assert n_task == 0, f"e2e _setup 没清掉本 task (id 含日期前缀), found {n_task}"
        assert n_entity == 0, f"e2e _setup 没清掉本 entity, found {n_entity}"
        # chunk: 因 m29 用 prefix 'chunk:e2e-m29-test' 应被 'chunk:e2e-%' 匹配
        assert n_chunk == 0, f"e2e _setup 没清掉 chunk, found {n_chunk}"
    finally:
        c.close()


def test_m29_2_no_hardcoded_path():
    """[M29.2] REPO 路径走 __file__ 派生, 不硬编码绝对路径."""
    src = Path(__file__).read_text()
    assert "REPO = Path(__file__).resolve().parent.parent" in src, "REPO 应从 __file__ 派生, 不应硬编码绝对路径"
    import re as _re

    hardcoded = _re.search(r'REPO\s*=\s*Path\(["\']/', src)
    assert not hardcoded, f"REPO 硬编码绝对路径: {hardcoded.group() if hardcoded else None}"


def test_m29_1b_setup_cleans_chunks_not_just_entities():
    """[M29.1b] e2e _setup 删 chunks (旧版漏删, 残留破坏下个 conftest fixture)."""
    # 直接插 chunk 模拟 _remember_chunk 的 raw SQL 路径
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        c.execute(
            "INSERT OR IGNORE INTO chunks (id, content, source, memory_type, importance, valid_until, created_at, processed_at) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
            ("chunk:e2e-m29-chunks", "leak test", "test:e2e_m29_chunks", "episodic", 0.5, "2026-08-06T15:00"),
        )
        c.commit()
    finally:
        c.close()

    # 跑 e2e setup
    from tests.test_m5_4_e2e_purchase import _setup as e2e_setup

    e2e_setup()

    # 校验 chunk 被清
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        n = c.execute("SELECT COUNT(*) FROM chunks WHERE id='chunk:e2e-m29-chunks'").fetchone()[0]
        assert n == 0, f"e2e _setup 应清掉 chunk:e2e-m29-chunks, found {n}"
    finally:
        c.close()
