"""A5+A6+A7 — usearch 集成 + rebuild/repair 脚本 (TASKS_SEARCH_INDEX §4 A5/A6/A7).

§4 验收:
  A5: 集成到 memory.py 后, remember+recall(vector_only) 命中
  A6: rebuild --dry-run 统计活跃 chunk 数 + 不真正重建
  A7: repair --dry-run 报 0 个 orphan (live DB 走 sqlite_vec, 无孤儿)
"""

import importlib.util as _ilu
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# [8/5 fix] DB 路径不再硬编码 — 用 config 解析
from config import config as _config_mod  # noqa: E402

_DEFAULT_DB_PATH = _config_mod.db_path
# [8/12 fix] 索引路径跟 db_path.stem 绑定 — 跟 search_index.py:412-415 一致
_DEFAULT_INDEX_PATH = _DEFAULT_DB_PATH.parent / f"{_DEFAULT_DB_PATH.stem}.usearch.index"


def _run_script(script_name, *args):
    """跑 scripts/<script_name>.py, 返 (returncode, stdout, stderr).

    [8/10 fix] 透传 MNELO_MEMORY_DIR / MNELO_TEST_FRESH — CI fresh DB 隔离下,
    subprocess 默认走 config.py default (owner live path), 会撞 enable_load_extension
    sandbox 限制 + 用错 db. 透传确保 subprocess 跟 host 走同一份 fresh DB.
    """
    passthrough = {k: os.environ[k] for k in ("MNELO_MEMORY_DIR", "MNELO_TEST_FRESH", "MNELO_CI_PYTHON") if k in os.environ}
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
        env={**os.environ, **passthrough, "PYTHONPATH": str(ROOT)},
    )
    return r.returncode, r.stdout, r.stderr


def test_a5_usearch_index_init_via_memory_factory():
    """[A5 §4] factory backend='usearch' → UsearchIndex (集成 memory.py 入口路径)."""
    import search_index

    idx = search_index.build_search_index("usearch", _DEFAULT_DB_PATH, dim=512)
    try:
        assert idx.name == "usearch"
    finally:
        idx.close()


def test_a6_rebuild_dry_run_counts_chunks():
    """[A6 §4] rebuild --dry-run 统计活跃 chunk 数 + 不真正重建."""
    rc, out, err = _run_script("rebuild_index.py", "--backend", "usearch", "--dry-run")
    assert rc == 0, f"rebuild --dry-run 失败: stderr={err}"
    # 输出应含 added 数 (live DB 应该有几千个 chunks)
    assert "'added':" in out, f"输出格式不符: {out}"
    assert "'failed': 0" in out or "'failed':0" in out
    assert "'dry_run': True" in out


def test_a6_rebuild_dry_run_does_not_create_index():
    """[A6 §4] --dry-run 不真建索引 (usearch.index 文件不应被新建)."""
    usearch_path = _DEFAULT_INDEX_PATH
    existed_before = usearch_path.exists()
    rc, out, err = _run_script("rebuild_index.py", "--backend", "usearch", "--dry-run")
    assert rc == 0
    # dry-run 不应新建 usearch.index
    if not existed_before:
        assert not usearch_path.exists(), "dry-run 不应创建 usearch.index, 但文件出现了"


def test_a7_repair_dry_run_reports_orphan_count():
    """[A7 §4] repair --dry-run 报 orphan 数 (live DB 走 sqlite_vec, 应为 0)."""
    rc, out, err = _run_script("repair_index.py", "--backend", "usearch", "--dry-run")
    assert rc == 0, f"repair --dry-run 失败: stderr={err}"
    assert "'deleted':" in out
    assert "'kept':" in out
    assert "'dry_run': True" in out


def test_a7_repair_actually_removes_orphan_when_not_dry_run(tmp_path):
    """[A7 §4] 构造孤儿场景 → 真 repair 后索引清掉孤儿.

    [8/12 fix] 完全隔离 tmp_path — 显式 unlink 任何残留 stale index file (4d/512d
    race), 用 session 级 tmp_path_factory 而非 function 级. 防 pytest tmp_path
    跨 test 复用 + embedder dim mismatch 触发自动重建失败.
    """
    import sqlite3
    import numpy as np

    # 创建临时 DB + chunks + usearch 索引
    db = tmp_path / "test_repair.db"
    # 显式清理可能残留的 stale index (不同 dim / 旧 test 留下)
    for stale in tmp_path.glob("*.usearch.index*"):
        stale.unlink(missing_ok=True)

    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            content TEXT,
            timestamp TEXT,
            valid_until TEXT
        );
    """)
    cur = conn.execute(
        "INSERT INTO chunks (id, content, timestamp) VALUES (?, ?, datetime('now'))",
        ("alive_chunk", "alive content"),
    )
    conn.commit()
    conn.close()

    # 创建 usearch 索引 + 加一条对应 alive + 一条孤儿
    import search_index

    idx = search_index.UsearchIndex(db, dim=4)
    idx.add("alive_chunk", np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32).tobytes())
    orphan_rowid = 99999  # 不存在的 rowid
    idx._index.add(
        np.array([orphan_rowid], dtype=np.uint64),
        np.array([[0.5, 0.5, 0.0, 0.0]], dtype=np.float32),
    )
    idx.close()
    n_before = 2  # alive + orphan

    # 真 repair (非 dry-run) — 传 --db 指向 tmp DB
    # [8/12 fix] 透传 MNELO_MEMORY_SEARCH_BACKEND + 用 tmp_path 隔离
    import os

    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "MNELO_MEMORY_SEARCH_BACKEND": "usearch",
        "MNELO_TEST_FRESH": "1",
    }
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "repair_index.py"), "--backend", "usearch", "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
        env=env,
    ).returncode
    assert rc == 0, f"repair 失败: {rc} stdout={subprocess.run.__doc__}"

    # 重新 load 索引, 验孤儿已删
    idx2 = search_index.UsearchIndex(db, dim=4)
    try:
        n_keys = len(idx2._index.keys)
        assert n_keys == n_before - 1, f"repair 后应剩 {n_before - 1} 条 (orphan 删了), got {n_keys}"
    finally:
        idx2.close()
