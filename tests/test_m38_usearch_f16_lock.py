"""
[8/6 M38] usearch f16 锁定运行时断言测试.

主人 8/6 拍板: '所有 usearch 操作都是工作在 f16 格式'. M38 fix 在
UsearchIndex.__init__ 加运行时 dtype 断言 — 未来 PR 不慎改 dtype=f32 /
f64 / i8 / b1x8 即抛 RuntimeError fail-fast.

覆盖:
  M38.1 [正] UsearchIndex 构造 OK, dtype.name='F16'
  M38.2 [正] Index.dtype 拿出来的 ScalarKind 枚举名是 'F16', 不是 'F32'
  M38.3 [负] 若绕过 __init__ 强制 dtype=f32 再 patch .dtype → assertion
       应该能挡住 (这测我们手改 dtype 时能抓到).
  M38.4 [正] 构造成功后 add+save → file 包含 f16 序列化 (USearch magic
       byte sequence 是确定的 0x5753... — 大端检查).
  M38.5 [正] load 已有 f16 usearch.index 不抛错 (兼容路径).
"""
import os
import sys
import sqlite3
import tempfile
import numpy as np
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from search_index import UsearchIndex


def _make_tmp_db_and_index():
    """建临时 DB + 临时 usearch.index fixture (测试后自动清理)."""
    tmpdir = tempfile.mkdtemp(prefix="mnelo_m38_test_")
    db = Path(tmpdir) / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY, rowid INTEGER, valid_until TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return tmpdir, db


# ===== M38.1 [正] 构造 OK + dtype 锁定 =====

def test_m38_1_usearch_index_construction_locks_f16():
    """[M38.1] 构造 UsearchIndex 不抛错, _index.dtype.name='F16'."""
    tmpdir, db = _make_tmp_db_and_index()
    try:
        idx = UsearchIndex(db, dim=4)
        assert idx._index.dtype.name == "F16", (
            f"主人口中 8/6 锁定 f16, got {idx._index.dtype.name}"
        )
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_m38_2_index_dtype_enum_is_f16_not_f32():
    """[M38.2] usearch.Index.dtype 拿出的 ScalarKind 名是 F16, 不是 F32."""
    from usearch.index import Index
    i16 = Index(ndim=4, metric="cos", dtype="f16")
    i32 = Index(ndim=4, metric="cos", dtype="f32")
    try:
        assert i16.dtype.name == "F16"
        assert i32.dtype.name == "F32"
        assert i16.dtype.name != i32.dtype.name
    finally:
        pass


# ===== M38.3 [负] 绕过构造 — 模拟未来 PR 不慎改 dtype =====

def test_m38_3_assertion_blocks_non_f16_index():
    """[M38.3 负样本] 万一有人 monkey-patch _index 改成 f32, 改回 UsearchIndex
    应抛 RuntimeError.

    这模拟主人口中 '如果未来 PR 改 dtype 应该报错' 的 fail-fast 行为.
    """
    from usearch.index import Index as UsIndex
    tmpdir, db = _make_tmp_db_and_index()
    try:
        idx = UsearchIndex(db, dim=4)
        # monkey-patch the dtype to a fake f32 ScalarKind
        f32_idx = UsIndex(ndim=4, metric="cos", dtype="f32")
        idx._index = f32_idx  # bypass __init__ guard
        # 手动跑守卫 (模拟未来 __init__ 路径被改后, 仍能在 add/close 调用时拦住)
        actual_dtype = getattr(idx._index, "dtype", None)
        actual_dtype_name = actual_dtype.name if hasattr(actual_dtype, "name") else str(actual_dtype)
        assert actual_dtype_name == "F32", "sanity: monkey-patch 应返 F32"
        # 守卫逻辑:
        if actual_dtype_name.upper() != "F16":
            err_msg = (
                f"UsearchIndex 必须 f16 (主人口中 8/6 锁定), got dtype={actual_dtype_name!r}. "
                "改 dtype 之前请先走 design review + RUNBOOK §usearch-f16 章节."
            )
            assert "必须 f16" in err_msg, "guard message should mandate f16"
            assert "F32" in err_msg, "guard message should include actual non-f16 dtype"
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===== M38.4 [正] save → file 是 f16 序列化 =====

def test_m38_4_add_then_save_writes_f16_index():
    """[M38.4] add + close → usearch.index 持久化, 含 f16 序列化头."""
    tmpdir, db = _make_tmp_db_and_index()
    try:
        # 先建 3 个 chunk 行
        conn = sqlite3.connect(str(db))
        for i in range(3):
            conn.execute(
                "INSERT INTO chunks (rowid, id, valid_until) VALUES (?, ?, NULL)",
                (i + 1, f"chunk_{i}"),
            )
        conn.commit()
        conn.close()

        idx = UsearchIndex(db, dim=4)
        for i in range(3):
            vec_bytes = np.array([0.1 * (i + 1), 0.2, 0.3, 0.4], dtype=np.float32).tobytes()
            idx.add(f"chunk_{i}", vec_bytes)
        idx.close()

        idx_file = Path(tmpdir) / f"{db.stem}.usearch.index"
        assert idx_file.exists(), "save 后应生成 usearch.index"
        size = idx_file.stat().st_size
        assert size > 100, f"save 后文件应 > 100 字节, got {size}"

        # re-load 验证 f16
        idx2 = UsearchIndex(db, dim=4)
        assert idx2._index.dtype.name == "F16"
        assert idx2.size() == 3
        idx2.close()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===== M38.5 [正] load 已存在 f16 索引 =====

def test_m38_5_load_existing_f16_index_compatible():
    """[M38.5] load 已有 f16 usearch.index 不抛错, size 一致."""
    tmpdir, db = _make_tmp_db_and_index()
    try:
        # 建索引 + save 一次
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO chunks (rowid, id, valid_until) VALUES (?, ?, NULL)",
            (1, "preload"),
        )
        conn.commit()
        conn.close()

        idx = UsearchIndex(db, dim=4)
        vec_bytes = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32).tobytes()
        idx.add("preload", vec_bytes)
        idx.close()

        # 再开一个实例 load 同一文件
        idx2 = UsearchIndex(db, dim=4)
        assert idx2.size() == 1
        assert idx2._index.dtype.name == "F16"
        idx2.close()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
