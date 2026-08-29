"""[8/8 根因修复] UsearchIndex 启动预检 + 自动重建.

背景 (free(): corrupted unsorted chunks 根治):
  旧实现见 usearch.index 存在就 self._index.load() — 文件损坏/截断/错 dtype
  (f32) 时 load 本身原生 abort; stale (索引落后 SQLite 事实源) 则静默漏召回.
  现在启动流程 (_init_from_disk):
    1) Index.metadata(path) 读文件头预检 — 只解析头部, 损坏/垃圾文件抛干净
       ValueError, 不触发原生图 load;
    2) sidecar 指纹 (usearch.index.verified.json) 比对 SQLite active 集合,
       不一致 = stale; 无 sidecar 时用 文件头向量数 vs SQL active 数 兜底;
    3) 任一不过 → 自动从 SQLite 重建 f16 索引, 坏文件改名 .corrupt-<ts> 留档.

本文件用真实 usearch 后端 + 无模型假 embedder (确定性 4 维向量), 验证预检
逻辑与自动重建行为, 不 load bge 模型.
"""

import importlib.util as _ilu
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SI_PATH = ROOT / "search_index.py"
sys.path.insert(0, str(ROOT))

DIM = 4  # 小维数加速测试; 契约上与 512 等价 (usearch cos 不限维)


def _load_search_index():
    if "search_index" in sys.modules:
        del sys.modules["search_index"]
    spec = _ilu.spec_from_file_location("search_index", SI_PATH)
    mod = _ilu.module_from_spec(spec)
    sys.modules["search_index"] = mod
    spec.loader.exec_module(mod)
    return mod


_si = _load_search_index()


def _fake_embed(text: str):
    """确定性假 embed — 用 text 的 sha256 前 DIM 字节 → [0,1] float 列表."""
    import hashlib

    h = hashlib.sha256(text.encode("utf-8")).digest()
    return [b / 255.0 for b in h[:DIM]]


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    """把 embedder.embed 换成假实现, 让自动重建不 load bge 模型."""
    import types

    fake = types.ModuleType("embedder")
    fake.embed = _fake_embed
    monkeypatch.setitem(sys.modules, "embedder", fake)


def _make_db(tmp_path, chunk_ids=()):
    db = tmp_path / "usearch.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            content TEXT,
            timestamp TEXT,
            valid_until TEXT
        );
    """)
    for cid in chunk_ids:
        conn.execute(
            "INSERT INTO chunks (id, content, timestamp) VALUES (?, ?, datetime('now'))",
            (cid, f"content {cid}"),
        )
    conn.commit()
    conn.close()
    return db


def _insert(db, cid):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO chunks (id, content, timestamp) VALUES (?, ?, datetime('now'))",
        (cid, f"content {cid}"),
    )
    conn.commit()
    conn.close()


def _add_all(idx, db, cids):
    """把 cids 逐条嵌入并 add 进索引 (等价 rebuild_index 的向量路径)."""
    for cid in cids:
        vec = _fake_embed(f"content {cid}")
        idx.add(cid, struct.pack(f"{DIM}f", *vec), conn=idx._conn)


def test_clean_reopen_no_rebuild(tmp_path):
    """正常 close/reopen → 不重建 (无 .corrupt-*), 数据在, sidecar 已写."""
    db = _make_db(tmp_path, chunk_ids=("a", "b"))
    idx = _si.UsearchIndex(db, DIM)
    _add_all(idx, db, ("a", "b"))
    idx.close()
    assert (tmp_path / f"{db.stem}.usearch.index").exists()
    assert (tmp_path / f"{db.stem}.usearch.index.verified.json").exists()

    idx2 = _si.UsearchIndex(db, DIM)
    try:
        assert idx2.size() == 2
        assert not list(tmp_path.glob(f"{db.stem}.usearch.index.corrupt-*"))
    finally:
        idx2.close()


def test_corrupt_file_auto_rebuilds(tmp_path):
    """垃圾文件 → 文件头预检 ValueError (不盲 load abort) → 自动重建."""
    db = _make_db(tmp_path, chunk_ids=("a", "b"))
    garbage = b"garbage not usearch " * 10
    (tmp_path / f"{db.stem}.usearch.index").write_bytes(garbage)

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 2  # 从 SQLite 重建, 数据不丢
        corrupts = list(tmp_path.glob(f"{db.stem}.usearch.index.corrupt-*"))
        assert len(corrupts) == 1
        assert corrupts[0].read_bytes() == garbage  # 坏文件留档
        assert (tmp_path / f"{db.stem}.usearch.index.verified.json").exists()
    finally:
        idx.close()


def test_f32_file_auto_rebuilds_to_f16(tmp_path):
    """f32 文件 → 预检出 dtype ≠ f16 → 自动重建为 f16 (混合精度根治)."""
    import usearch.index as ui

    db = _make_db(tmp_path, chunk_ids=("a", "b"))
    ui.Index(ndim=DIM, metric="cos", dtype="f32").save(str(tmp_path / f"{db.stem}.usearch.index"))

    idx = _si.UsearchIndex(db, DIM)
    try:
        from usearch.index import ScalarKind

        assert idx._index.dtype == ScalarKind.F16
        assert idx.size() == 2
        assert len(list(tmp_path.glob(f"{db.stem}.usearch.index.corrupt-*"))) == 1
    finally:
        idx.close()


def test_stale_index_auto_rebuilds(tmp_path):
    """sidecar 指纹不一致 (SQLite 新增 chunk 但索引没跟上) → stale → 自动重建."""
    db = _make_db(tmp_path, chunk_ids=("a",))
    idx = _si.UsearchIndex(db, DIM)
    _add_all(idx, db, ("a",))
    idx.close()  # sidecar sig = {a}

    _insert(db, "b")  # SQLite 事实源新增, 索引没跟上

    idx2 = _si.UsearchIndex(db, DIM)
    try:
        assert idx2.size() == 2
        assert len(list(tmp_path.glob(f"{db.stem}.usearch.index.corrupt-*"))) == 1
    finally:
        idx2.close()


def test_no_sidecar_count_fallback_rebuilds(tmp_path):
    """无 sidecar (旧代码升级/手删) → count 兜底: 向量数 ≠ active 数 → 重建."""
    db = _make_db(tmp_path, chunk_ids=("a",))
    idx = _si.UsearchIndex(db, DIM)
    _add_all(idx, db, ("a",))
    idx.close()
    (tmp_path / f"{db.stem}.usearch.index.verified.json").unlink()  # 手删 sidecar
    _insert(db, "b")

    idx2 = _si.UsearchIndex(db, DIM)
    try:
        assert idx2.size() == 2
    finally:
        idx2.close()


def test_no_chunks_table_tolerance(tmp_path):
    """chunks 表不存在 (损坏/极简 db) + 有效 f16 索引 → 不 abort, 正常加载."""
    import usearch.index as ui

    db = tmp_path / "empty.db"  # 不存在 → sqlite 自动建空库
    ui.Index(ndim=DIM, metric="cos", dtype="f16").save(str(tmp_path / f"{db.stem}.usearch.index"))

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 0
        assert not list(tmp_path.glob(f"{db.stem}.usearch.index.corrupt-*"))
    finally:
        idx.close()


def test_rebuild_without_embedder_raises(tmp_path, monkeypatch):
    """重建需要 embedder 但 import 不到 → RuntimeError 提示手动 rebuild_index."""
    import types

    db = _make_db(tmp_path, chunk_ids=("a",))
    (tmp_path / f"{db.stem}.usearch.index").write_bytes(b"garbage " * 10)
    broken = types.ModuleType("embedder")  # 无 embed attr → ImportError
    monkeypatch.setitem(sys.modules, "embedder", broken)

    with pytest.raises(RuntimeError) as e:
        _si.UsearchIndex(db, DIM)
    assert "rebuild_index.py" in str(e.value)
