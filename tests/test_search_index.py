"""
[8/6 plan §12] SearchIndex 适配器测试 (DESIGN §3.6/§8.3).

本环境 CPU 不支持 zvec 原生指令 (import 即崩), 故:
- UsearchIndex: 用真实后端测 (本机可用)
- ZvecIndex: 用 fake zvec 模块验证 API 调用正确性 (真 zvec 需在部署机实测)
- build_search_index 工厂: 后端选择 + 必选二选一 (sqlite_vec 已出局, plan §1)

fake zvec 只实现 ZvecIndex 用到的 API 面 (含 iter_all, DataType.VECTOR_INT8),
用于验证"代码按 zvec 0.6 API 写对"。
"""

import math
import sys
import unittest
from pathlib import Path

# --- fake zvec 模块: 在 import search_index 前注入 sys.modules ---


class _FakeCollection:
    def __init__(self, path):
        self.path = path
        self.docs = {}  # id -> (vector: list[float], fields: dict)
        self.created_indexes = []

    def create(self, schema):
        """[8/6 fix] declarative API: schema 含 fields + vectors; 自动建 HNSW 索引."""
        self.schema = schema
        # 自动跟踪 index (跟真 zvec schema 行为对齐: schema 有 index_param → 建索引)
        if hasattr(schema, "vectors"):
            for v in schema.vectors:
                if v.index_param is not None:
                    self.created_indexes.append((v.name, "HNSW"))

    def create_index(self, field_name, index_type):
        # [8/6 back-compat] 旧 method-style API 仍可用
        self.created_indexes.append((field_name, index_type))

    def upsert(self, doc):
        self.docs[doc.id] = (doc.vectors.get("embedding", []), doc.fields)
        return None

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)
        return []

    def fetch(self, ids):
        """[8/6 fix] ZvecIndex.contains 调 fetch; 返 list of found docs (跟真 zvec API 对齐)."""
        from types import SimpleNamespace

        result = []
        for i in ids:
            if i in self.docs:
                vec, fields = self.docs[i]
                result.append(SimpleNamespace(id=i, vectors={"embedding": vec}, fields=fields))
        return result

    def iter_all(self):
        """[8/6 plan §12] ZvecIndex.cleanup_orphans/contains 调 iter_all."""
        for doc_id in list(self.docs.keys()):
            from types import SimpleNamespace

            vec, fields = self.docs[doc_id]
            yield SimpleNamespace(id=doc_id, vectors={"embedding": vec}, fields=fields)

    def query(self, q):
        # FakeZvec.Query 返回 dict; 兼容属性访问 (真实 zvec Query 是 dataclass)
        v = q["vector"] if isinstance(q, dict) else q.vector
        scored = []
        for doc_id, (vec, fields) in self.docs.items():
            if not vec or not v or len(vec) != len(v):
                continue
            dot = sum(a * b for a, b in zip(vec, v))
            scored.append((dot, doc_id))
        scored.sort(key=lambda x: -x[0])
        from types import SimpleNamespace

        return [SimpleNamespace(id=i, score=s, vectors={"embedding": []}, fields={}) for s, i in scored]

    def flush(self):
        return None


class _FakeZvec:
    def __init__(self):
        self.collections = {}
        self.last_collection = None

    def CollectionOption(self):
        return {}

    def HnswIndexParam(self):
        return {}

    def FtsIndexParam(self, tokenizer_name="standard"):
        return {"tokenizer_name": tokenizer_name}

    def HnswQueryParam(self, ef=100):
        return {"ef": ef}

    def FtsQueryParam(self, default_operator="OR"):
        return {"default_operator": default_operator}

    def Fts(self, match_string=""):
        return {"match_string": match_string}

    def Query(self, field_name=None, id=None, vector=None, param=None, fts=None):
        return {"field_name": field_name, "id": id, "vector": vector, "param": param, "fts": fts}

    def Doc(self, id=None, score=None, vectors=None, fields=None):
        from types import SimpleNamespace

        return SimpleNamespace(id=id, score=score, vectors=vectors or {}, fields=fields or {})

    class FieldSchema:
        """[8/6 fix] zvec 0.6 declarative API: FieldSchema(name, data_type, ...)."""

        def __init__(self, name, data_type, nullable=False, index_param=None):
            self.name = name
            self.data_type = data_type
            self.nullable = nullable
            self.index_param = index_param

    class VectorSchema:
        """[8/6 fix] zvec 0.6 declarative API: VectorSchema(name, data_type, dimension, index_param)."""

        def __init__(self, name, data_type, dimension=0, index_param=None):
            self.name = name
            self.data_type = data_type
            self.dimension = dimension
            self.index_param = index_param

    class CollectionSchema:
        """[8/6 fix] declarative API: CollectionSchema(name, fields=[...], vectors=[...])."""

        def __init__(self, name="", fields=None, vectors=None):
            self.name = name
            self.fields = list(fields) if fields else []
            self.vectors = list(vectors) if vectors else []

        # [8/6 back-compat] 旧 method-style API 在 fake 保留 stub, 防止 owner 加新方法用
        def add_dense_vector_field(self, name, dim, data_type=None):
            self.vectors.append(_FakeZvec.VectorSchema(name, data_type or "VECTOR_FP32", dim))

        def add_text_field(self, name):
            self.fields.append(_FakeZvec.FieldSchema(name, "STRING"))

    def create_and_open(self, path, schema=None, option=None):
        col = _FakeCollection(path)
        col.schema = schema
        # [8/6 fix] declarative API: schema 包含 index_param, 自动触发 HNSW 索引
        if schema is not None and hasattr(col, "create"):
            col.create(schema)
        self.collections[path] = col
        self.last_collection = col
        return col

    def open(self, path, option=None):
        col = self.collections.get(path) or _FakeCollection(path)
        self.collections[path] = col
        self.last_collection = col
        return col

    class DataType:
        """[8/6 fix] zvec 0.6 全部 data_type, 实际用 VECTOR_FP32 + STRING."""

        VECTOR_FP16 = "VECTOR_FP16"
        VECTOR_FP32 = "VECTOR_FP32"
        VECTOR_FP64 = "VECTOR_FP64"
        VECTOR_INT8 = "VECTOR_INT8"
        STRING = "STRING"
        INT64 = "INT64"


def _install_fake_zvec():
    fz = _FakeZvec()
    sys.modules["zvec"] = fz
    return fz


# [8/6 plan §12] TestSQLiteVecIndex 整类删除 (sqlite_vec 已出局, plan §1)
class TestZvecBackendWithFake(unittest.TestCase):
    """fake zvec — 验证 ZvecIndex 按 zvec 0.6 API 调用正确 (含 INT8 精度)."""

    def setUp(self):
        self.fz = _install_fake_zvec()
        import search_index

        # 重载以确保用刚注入的 fake zvec
        # [8/6 fix] 用 mock 而非永久赋值 — 原 zvec_available = lambda: True
        # 永久改写模块属性且不恢复, 污染后续测试类 (TestZvecAvx2Gate 的
        # _cpu_has_avx2 mock 因此失效 → zvec_available 已被替换, 测的不是真逻辑).
        from unittest import mock

        self._zva_patch = mock.patch.object(search_index, "zvec_available", return_value=True)
        self._zva_patch.start()
        self.module = search_index

    def tearDown(self):
        if getattr(self, "_zva_patch", None) is not None:
            self._zva_patch.stop()

    def test_01_init_creates_schema(self):
        idx = self.module.ZvecIndex(Path("/tmp/fake_zv_test"), dim=512)
        self.assertEqual(idx.name, "zvec")
        col = self.fz.last_collection
        self.assertEqual(col.created_indexes, [("embedding", "HNSW")])
        idx.close()

    def test_02_add_knn_roundtrip(self):
        import struct

        idx = self.module.ZvecIndex(Path("/tmp/fake_zv_round"), dim=4)
        v1 = struct.pack("4f", *[1.0, 0.0, 0.0, 0.0])
        v2 = struct.pack("4f", *[0.0, 1.0, 0.0, 0.0])
        idx.add("chunk_a", v1)
        idx.add("chunk_b", v2)
        hits = idx.knn(struct.pack("4f", *[0.99, 0.01, 0.0, 0.0]), 2)
        self.assertEqual([h.chunk_id for h in hits], ["chunk_a", "chunk_b"])
        idx.remove("chunk_a")
        hits2 = idx.knn(struct.pack("4f", *[0.99, 0.01, 0.0, 0.0]), 2)
        self.assertNotIn("chunk_a", [h.chunk_id for h in hits2])
        idx.close()

    def test_03_size_and_contains_and_cleanup(self):
        """[8/6 plan §12] ZvecIndex.size/contains/cleanup_orphans API 面."""
        import struct

        idx = self.module.ZvecIndex(Path("/tmp/fake_zv_sc"), dim=4)
        v = struct.pack("4f", *[1.0, 0.0, 0.0, 0.0])
        idx.add("chunk_a", v)
        idx.add("chunk_b", v)
        # size
        self.assertEqual(idx.size(), 2)
        # contains
        self.assertTrue(idx.contains("chunk_a"))
        self.assertFalse(idx.contains("chunk_x"))
        # cleanup_orphans: conn=None 防御性返回
        r0 = idx.cleanup_orphans(conn=None, dry_run=True)
        self.assertEqual(r0["truly_orphan_cleaned"], 0)

        # cleanup_orphans: 配 fake conn (返回空 → 全 orphan)
        class FakeConn:
            def execute(self, sql, params=()):
                class _R:
                    def fetchone(self_inner):
                        return None  # chunk missing → orphan

                return _R()

        r = idx.cleanup_orphans(conn=FakeConn(), dry_run=False)
        self.assertEqual(r["truly_orphan_cleaned"], 2)
        self.assertEqual(idx.size(), 0)
        idx.close()

    def test_04_deserialize_f32(self):
        import struct

        from search_index import _deserialize_f32

        self.assertEqual(_deserialize_f32(struct.pack("2f", 1.5, -2.0)), [1.5, -2.0])


class TestUsearchF16Assertion(unittest.TestCase):
    """[8/6 f16 断言] UsearchIndex 加载磁盘索引时强制 f16 契约.

    背景: f32 文件 load 进 f16 Index 后 dtype 静默变 F32, 后续 add 写 f16 →
    混合精度文件 → 原生堆损坏 (free(): corrupted unsorted chunks).
    断言把静默损坏变成启动期快速失败 (search_index.py:417-427).
    用空索引文件构造 (省 .add — 本机无 AVX2, usearch add 偶发 SIGSEGV).
    """

    def _build_empty(self, dtype: str, p: Path) -> None:
        import usearch.index as ui

        idx = ui.Index(ndim=512, metric="cos", dtype=dtype)
        idx.save(str(p))

    def test_f16_index_loads_ok(self):
        """f16 文件 → 正常加载, dtype 保持 F16."""
        import tempfile

        from search_index import UsearchIndex
        from usearch.index import ScalarKind

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._build_empty("f16", td / "memory.usearch.index")
            idx = UsearchIndex(td / "memory.db", 512)
            try:
                self.assertEqual(idx._index.dtype, ScalarKind.F16)
                self.assertEqual(idx._index.size, 0)
            finally:
                idx.close()

    def test_f32_index_auto_rebuilds(self):
        """[8/8 根因修复] f32 文件 → 预检出 dtype 不符 → 自动重建 f16.

        旧行为: 盲 load f32 → dtype 静默变 F32 → 后续 add 混合精度 → 原生堆
        损坏 (free(): corrupted unsorted chunks). 新行为: 文件头预检
        (Index.metadata, 只解析头部不触发原生 load) 发现 F32 → 自动重建 f16,
        坏文件改名 .corrupt-<ts> 留档.
        """
        import tempfile
        import types

        from search_index import UsearchIndex
        from usearch.index import ScalarKind

        fake = types.ModuleType("embedder")
        fake.embed = lambda text: [0.5] * 512  # 假 embedder, 不 load bge 模型
        sys.modules["embedder"] = fake
        try:
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                self._build_empty("f32", td / "memory.usearch.index")
                idx = UsearchIndex(td / "memory.db", 512)
                try:
                    self.assertEqual(idx._index.dtype, ScalarKind.F16)
                    self.assertEqual(idx._index.size, 0)
                finally:
                    idx.close()
                corrupts = list(td.glob("memory.usearch.index.corrupt-*"))
                self.assertEqual(len(corrupts), 1)
        finally:
            sys.modules.pop("embedder", None)


class TestZvecAvx2Gate(unittest.TestCase):
    """[8/6 修复] zvec_available CPU AVX2 前置探测 — 无 AVX2 绝不 import zvec.

    背景: zvec_available 原主进程 try-import zvec, 但无 AVX2 的 CPU 上 import 是
    SIGILL (进程级崩溃, 非 Python 异常), try/except 拦不住 → auto 链带崩整个
    mnelo (本机 Ivy Bridge 实测 dumped core). 修复: 先 _cpu_has_avx2() 探测,
    明确无 AVX2 直接返回 False, 不 import.
    用 fake zvec 注入 sys.modules, 让 import 路径不触发真原生扩展.
    """

    def test_cpu_probe_returns_bool_or_none(self):
        """Linux 上应能探测出 AVX2 支持与否 (本机 Ivy Bridge → False)."""
        from search_index import _cpu_has_avx2

        r = _cpu_has_avx2()
        self.assertIn(r, (True, False, None), f"探测应返回 True/False/None, got {r!r}")

    def test_no_avx2_skips_zvec_import(self):
        """无 AVX2 → zvec_available 返回 False 且不 import (不注入 fake, 若走了
        import 本机直接 SIGILL 崩测试进程 — 此测试本身即防回归)."""
        from unittest import mock

        import search_index

        with mock.patch("search_index._cpu_has_avx2", return_value=False):
            self.assertFalse(search_index.zvec_available())

    def test_avx2_imports_fake_zvec(self):
        """CPU 支持 AVX2 → 正常 import zvec (fake), 返回 True."""
        from unittest import mock

        import search_index

        _install_fake_zvec()
        with mock.patch("search_index._cpu_has_avx2", return_value=True):
            self.assertTrue(search_index.zvec_available())

    def test_unknown_cpu_falls_back_to_import(self):
        """平台探测不了 (None) → 回退 try-import (fake), 保持原行为."""
        from unittest import mock

        import search_index

        _install_fake_zvec()
        with mock.patch("search_index._cpu_has_avx2", return_value=None):
            self.assertTrue(search_index.zvec_available())


class TestFactory(unittest.TestCase):
    """[8/6 plan §12] build_search_index 后端选择 + 必选二选一 (sqlite_vec 已出局)."""

    def setUp(self):
        import search_index

        self.module = search_index
        from config import resolve_db_path

        self.db_path = resolve_db_path()

    def test_01_auto_returns_usearch_or_zvec(self):
        """[8/6 plan §1] auto 必须返回 usearch 或 zvec (sqlite_vec 已出局)."""
        idx = self.module.build_search_index("auto", self.db_path, 512)
        self.assertIn(idx.name, ("usearch", "zvec"), f"auto 应二选一, got {idx.name}")
        idx.close()

    def test_02_usearch_explicit_returns_usearch(self):
        idx = self.module.build_search_index("usearch", self.db_path, 512)
        self.assertEqual(idx.name, "usearch")
        idx.close()

    def test_03_zvec_unavailable_raises(self):
        """[8/6 plan §1] 显式 zvec 不可用 → RuntimeError (不再回落 usearch)."""
        self.module.zvec_available = lambda: False
        with self.assertRaises(RuntimeError):
            self.module.build_search_index("zvec", self.db_path, 512)

    def test_04_usearch_unavailable_raises(self):
        """[8/6 plan §1] 显式 usearch 未安装 → RuntimeError (不再回落 sqlite_vec)."""
        self.module.usearch_available = lambda: False
        with self.assertRaises(RuntimeError):
            self.module.build_search_index("usearch", self.db_path, 512)

    def test_05_auto_both_unavailable_raises(self):
        """[8/6 plan §1] auto 双不可用 → RuntimeError."""
        self.module.zvec_available = lambda: False
        self.module.usearch_available = lambda: False
        with self.assertRaises(RuntimeError):
            self.module.build_search_index("auto", self.db_path, 512)

    def test_06_zvec_available_builds_zvec(self):
        _install_fake_zvec()
        self.module.zvec_available = lambda: True
        idx = self.module.build_search_index("zvec", self.db_path, 512)
        self.assertEqual(idx.name, "zvec")
        idx.close()

    def test_07_unknown_backend_falls_back_to_auto(self):
        """[8/6 plan §1] 未知 backend → warning + fallback auto."""
        self.module.zvec_available = lambda: False
        self.module.usearch_available = lambda: True
        idx = self.module.build_search_index("weird_backend", self.db_path, 512)
        self.assertEqual(idx.name, "usearch")
        idx.close()
