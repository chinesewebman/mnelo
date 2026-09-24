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


def _write_sidecar_for(index_path, ids):
    """按 _write_sidecar 的格式写一份"匹配"的 sidecar.

    目的: 让无防护的实现在开库时判定 not stale → 走 load 而不是重建 — 那才是崩的路径.
    格式若变, 这里退化成"sidecar 不匹配"(走重建分支), 用例仍能过, 只是鉴别力下降.
    """
    import hashlib
    import json

    sig = hashlib.md5(("|".join(ids) + f"|{len(ids)}").encode("utf-8"), usedforsecurity=False).hexdigest()
    index_path.with_name(index_path.name + ".verified.json").write_text(
        json.dumps({"signature": sig, "dtype": "f16", "dim": DIM, "built_at": "2026-09-24T00:00:00"}),
        encoding="utf-8",
    )


def _write_poison_index(index_path):
    """写出 usearch 的"毒文件": 曾 add 过、被 remove 到 0 条再 save 的 f16 索引.

    文件头特征 = count_present=0 且 count_deleted>0. load() 它正常 (size=0), 但紧接着
    第一次 add() 原生 SIGSEGV — usearch 自身行为, mnelo 只能不 load 它.
    """
    import numpy as np
    import usearch.index as ui

    poison = ui.Index(ndim=DIM, metric="cos", dtype="f16")
    poison.add(np.array([1], dtype=np.uint64), np.zeros((1, DIM), dtype=np.float32))
    poison.remove(np.array([1], dtype=np.uint64))
    poison.save(str(index_path))


def test_emptied_index_file_is_discarded(tmp_path):
    """[9/24 毒文件] 清空后落盘的 0 向量索引 → 开库时丢弃, 按无索引处理 (不 load).

    这里只断言丢弃行为: 无防护时该走 load (sidecar 匹配) → size=0, 断言 size=1 失败.
    真·add 崩溃在子进程用例里验证 (原生崩溃会把 pytest 一起带走).
    """
    db = _make_db(tmp_path, chunk_ids=("a",))
    index_path = tmp_path / f"{db.stem}.usearch.index"
    _write_poison_index(index_path)
    _write_sidecar_for(index_path, ["a"])

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 1  # 从 SQLite 重建 (而非加载空的毒文件)
        assert (tmp_path / f"{db.stem}.usearch.index.verified.json").exists()
    finally:
        idx.close()


def test_emptied_index_file_does_not_crash_on_add(tmp_path):
    """[9/24 毒文件] 回归真身: 毒文件 + 之后 add 不得原生崩溃.

    用子进程跑 (原生 SIGSEGV 会把整个 pytest 进程带走, 那样测试是"消失"而不是"失败").
    毒文件必须由 mnelo 自己的 add→remove→close(save) 写出, 维度也必须是真的 512 —
    手搓的小维索引不触发该 bug, 用例会失去鉴别力 (已验证: 无防护时 rc=139).
    """
    import subprocess
    import sys as _sys

    db = _make_db(tmp_path, chunk_ids=("a",))

    script = """
import struct, sys, types
ROOT, DB, DIM = sys.argv[1], sys.argv[2], 512
sys.path.insert(0, ROOT)
fake = types.ModuleType("embedder")          # 免 bge 加载, 也免网络
fake.embed = lambda text: [0.001 * (i % 100) for i in range(DIM)]
sys.modules["embedder"] = fake
from pathlib import Path
from search_index import UsearchIndex
db = Path(DB)
vec = struct.pack(f"{DIM}f", *([0.5] * DIM))
i1 = UsearchIndex(db, DIM)
i1.add("a", vec, conn=i1._conn)
i1.remove("a", conn=i1._conn)
i1.close()                                    # 落盘 count_present=0 且 count_deleted>0 的索引
i2 = UsearchIndex(db, DIM)
i2.add("a", vec, conn=i2._conn)
print("OK size=%d" % i2.size())
"""
    proc = subprocess.run(
        [_sys.executable, "-c", script, str(ROOT), str(db)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程原生崩溃/失败 rc={proc.returncode}\n{proc.stderr[-800:]}"
    assert "OK size=1" in proc.stdout


def test_missing_index_file_with_chunks_auto_rebuilds(tmp_path):
    """[9/24 fix] 索引文件丢失但 SQLite 有 active chunk → 重建.

    回归: 旧实现 `if not self._index_path.exists(): return` 把"文件不在"当成
    "全新库", 于是索引文件一旦丢失 (改名/误删/崩溃未落盘) 向量通道就永久静默
    死亡 — live 库 8/12 改名后哑了一个月, 重启报 vectors=0 且无告警.
    """
    db = _make_db(tmp_path, chunk_ids=("a", "b"))
    assert not (tmp_path / f"{db.stem}.usearch.index").exists()  # 前置: 文件本就不在

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 2  # 旧行为: 0
        assert (tmp_path / f"{db.stem}.usearch.index").exists()
        assert (tmp_path / f"{db.stem}.usearch.index.verified.json").exists()
        # 没有坏文件可备份 → 不该凭空产生 .corrupt-*
        assert not list(tmp_path.glob(f"{db.stem}.usearch.index.corrupt-*"))
    finally:
        idx.close()


def test_missing_index_file_on_empty_db_stays_noop(tmp_path):
    """真·全新库 (无 chunk) → 不重建, 不产生任何索引文件 (防无谓重嵌入)."""
    db = _make_db(tmp_path)

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 0
        assert not (tmp_path / f"{db.stem}.usearch.index").exists()
        assert not (tmp_path / f"{db.stem}.usearch.index.verified.json").exists()
    finally:
        idx.close()


def test_missing_index_file_ignores_soft_deleted_chunks(tmp_path):
    """chunk 全部软删 (valid_until 非空) → active 为空 → 不重建.

    钉死判据必须是 `valid_until IS NULL`: 把软删的算进来会让空库反复重嵌入.
    """
    db = _make_db(tmp_path, chunk_ids=("a",))
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE chunks SET valid_until = datetime('now')")
    conn.commit()
    conn.close()

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 0
        assert not (tmp_path / f"{db.stem}.usearch.index").exists()
    finally:
        idx.close()


def test_missing_index_file_without_chunks_table_noop(tmp_path):
    """无 chunks 表 (极简/损坏 db) + 无索引文件 → 不重建, 也不 abort."""
    db = tmp_path / "empty.db"

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 0
    finally:
        idx.close()


def test_missing_index_file_over_startup_cap_degrades(tmp_path, monkeypatch, caplog):
    """active chunk 数超过启动重建上界 → 不重建、不 abort, 记 ERROR 提示手动跑.

    上界存在的原因: 大库全量重嵌入会把启动卡住几分钟. 索引文件缺失属异常状态,
    宁可降级 + 大声告警.
    """
    import logging

    db = _make_db(tmp_path, chunk_ids=("a", "b", "c"))
    monkeypatch.setattr(_si.UsearchIndex, "_MAX_STARTUP_REBUILD_CHUNKS", 2)

    with caplog.at_level(logging.ERROR, logger="mnelo.index"):
        idx = _si.UsearchIndex(db, DIM)  # 不抛
    try:
        assert idx.size() == 0  # 降级: 空索引
        assert not (tmp_path / f"{db.stem}.usearch.index").exists()  # 没重建
        assert "超过启动重建上界" in caplog.text
        assert "rebuild_index.py" in caplog.text
    finally:
        idx.close()


def test_missing_index_file_at_cap_still_rebuilds(tmp_path, monkeypatch):
    """恰好等于上界 → 仍重建 (边界: 判据是 `>` 不是 `>=`)."""
    db = _make_db(tmp_path, chunk_ids=("a", "b"))
    monkeypatch.setattr(_si.UsearchIndex, "_MAX_STARTUP_REBUILD_CHUNKS", 2)

    idx = _si.UsearchIndex(db, DIM)
    try:
        assert idx.size() == 2
    finally:
        idx.close()


def test_missing_index_file_rebuild_failure_degrades_not_aborts(tmp_path, monkeypatch, caplog):
    """重建失败 (embedder 挂) → 记 ERROR 但构造成功, 以空索引降级启动.

    契约: 本方法 docstring 承诺"启动永不因坏索引 abort", 缺文件触发的重建
    也必须守着 — 拒绝启动比降级运行更糟.
    """
    import logging
    import types

    db = _make_db(tmp_path, chunk_ids=("a",))
    broken = types.ModuleType("embedder")  # 无 embed attr → ImportError
    monkeypatch.setitem(sys.modules, "embedder", broken)

    with caplog.at_level(logging.ERROR, logger="mnelo.index"):
        idx = _si.UsearchIndex(db, DIM)  # 不抛
    try:
        assert idx.size() == 0
        assert "自动重建失败" in caplog.text
        assert "rebuild_index.py" in caplog.text  # 留手动兜底线索
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
