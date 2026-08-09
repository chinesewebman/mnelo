#!/usr/bin/env python3
"""
search_index.py — L1 检索层索引抽象 (DESIGN §3.6 / §8.3)

只抽象"向量索引"的 KNN 与写入; 召回业务逻辑 (asof / 过滤器 / RRF / lane 组合)
留在 memory.py。

[8/6 plan] 向量库必选二选一 + 分精度:
  - usearch → f16 精度 (兜底; Index dtype='f16', 2 字节/维, 自动 f32↔f16 cast)
  - zvec    → INT8 精度 (新 CPU 优先; auto 链上层, 通过 zvec schema DataType)
  - sqlite_vec 已出局 (vec0 表保留作 legacy, 给 migrate/repair/init_db 工具用)

⚠️ zvec 后端说明 (重要):
  - zvec 0.6 原生扩展要求较新 CPU 指令 (AVX2+)。在旧 CPU 上 `import zvec` 直接
    Illegal instruction 崩溃 (进程级, 非异常)。因此 import 前必须先探测 CPU:
    `zvec_available()` 用 `_cpu_has_avx2()` 前置探测, **无 AVX2 直接返回 False
    绝不 import** (避免 SIGILL 带崩整个 mnelo 进程)。平台探测不了才回退 try-import。
  - zvec 后端代码按 zvec 0.6 类型化 API (zvec.pyi + model/*.py) 编写,
    **尚未在目标机 (Mac ARM64) 实测** — 需在部署机上验证后启用。
    INT8 精度 API 用 DataType.VECTOR_INT8 假设 (见风险 9, 真实 API 待 zvec 文档核实).
  - 本机 Ivy Bridge 上 zvec SIGILL 不可用, 本环境验证基于 usearch + TestZvecBackendWithFake.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mnelo.index")


# ============================================================
# 统一命中结构
# ============================================================


@dataclass
class KNNHit:
    """向量召回命中 — chunk_id 是唯一标识 (与后端解耦)."""

    chunk_id: str
    distance: float


# ============================================================
# SearchIndex 抽象
# ============================================================


class SearchIndex(ABC):
    """向量索引抽象. 写入 (add/remove) + KNN + 后端感知孤儿清理 + size/contains."""

    @property
    @abstractmethod
    def name(self) -> str:
        """后端名: 'usearch' | 'zvec'."""

    @abstractmethod
    def knn(self, query_bytes: bytes, top_k: int, conn=None) -> List[KNNHit]:
        """KNN 检索: query_bytes (序列化 float32 向量) → 按距离升序的命中.

        只返回 chunk_id + distance; valid_until/asof/filters 过滤由 memory.py
        在 chunk 侧做 (保证与 lane 业务逻辑解耦).
        conn: usearch/zvec 都忽略 (翻译 rowid 时用自有 _conn).
        """

    @abstractmethod
    def add(self, chunk_id: str, vector_bytes: bytes, conn=None, content: Optional[str] = None) -> None:
        """索引一条 chunk 的向量. chunk_id 需先存在于 chunks 表. 幂等.

        content: usearch 忽略; zvec 填充 FTS 列.
        conn: 语义同 add; 后端忽略 (索引独立于 SQLite 事务).
        """

    @abstractmethod
    def remove(self, chunk_id: str, conn=None) -> None:
        """删除一条 chunk 的向量索引. 幂等 (不存在也 OK). conn 语义同 add."""

    @abstractmethod
    def size(self) -> int:
        """索引中当前向量条数 — stats 的 vectors 字段按实际后端计数 (8/5 主人 commit).

        usearch 数 HNSW 索引 (Index.size 属性, 非方法, 已踩坑);
        zvec 数 collection. 避免在非 sqlite_vec 后端下显示恒 0 的假象.
        """

    @abstractmethod
    def close(self) -> None:
        """释放资源 (连接/collection/index 持久化)."""

    @abstractmethod
    def contains(self, chunk_id: str, conn=None) -> bool:
        """该 chunk_id 的向量是否在索引中.

        [8/6 plan §2] 后端感知 — usearch 用 rowid + Index.keys; zvec 用 chunk_id
        直接遍历 iter_all. 跨测试断言统一走这个 API, 不再查 vec0 表.
        """

    @abstractmethod
    def cleanup_orphans(self, conn=None, dry_run: bool = False) -> Dict:
        """[8/6 plan §2] 后端感知孤儿向量清理.

        返回 {
            'soft_deleted_cleaned': int,  # 索引 entry 但 chunks.valid_until 非空
            'truly_orphan_cleaned': int,  # 索引 entry 但 chunks 行已删
            'vectors_remaining': int,     # 清理/扫描后索引剩余
            'dry_run': bool,
        }

        落盘交给 close(); 本方法不 save (purge worker 在活 server 同一进程
        内存态立即生效; CLI 路径走 maintain_vectors.py 子进程, 退出时 save).
        """


# ============================================================
# zvec 后端
# ============================================================


class ZvecIndex(SearchIndex):
    """zvec 后端 — 进程内嵌向量库 (DESIGN §8.3 升级档).

    ⚠️ 未在本环境实测 (CPU 不支持 zvec 原生指令)。按 zvec 0.6 API 编写,
    需在部署机 (Mac ARM64 / 新 x86) 上跑 search_index_smoke 验证后启用。
    本类的 add/remove/knn 与 UsearchIndex 语义对齐, 便于 memory.py 无感切换。

    [8/6 plan §2 精度] INT8 量化 — 通过 schema 字段 DataType.VECTOR_INT8 指定
    (API 假设; 真机部署前核实 zvec 0.6 文档: <https://zvec.org/docs/db/>).
    """

    def __init__(self, collection_path: Path, dim: int):
        self.collection_path = collection_path
        self.dim = dim
        self._col = None  # type: ignore
        # 延迟导入 — import zvec 在旧 CPU 上会崩, 由工厂子进程检测把关
        # 存为实例属性供 _create_schema 使用 (module 局部变量在方法间不可见)
        import zvec  # noqa: F401

        self._zvec = zvec
        # [8/6 fix] zvec 0.6 schema-first API: create_and_open(path, schema, option),
        # 不接受空 schema. 旧路径 create → create(schema) 已被 0.6 废弃.
        # 修法: 一次性 _build_schema() 拿 schema → create_and_open(path, schema)
        if collection_path.exists():
            self._col = zvec.open(str(collection_path))
        else:
            schema = self._build_schema()
            self._col = zvec.create_and_open(str(collection_path), schema)

    def _build_schema(self) -> Any:  # zvec.CollectionSchema forward-refed (lazy import in __init__)
        """建 schema: embedding (512d FP32 + HNSW) + content (FTS jieba) + memory_type + source."""
        zv = self._zvec
        # [8/6 fix] zvec 0.6 declarative API: CollectionSchema(name, fields=[...], vectors=[...])
        # 旧 method-style add_*_field 在 0.6 不存在. VECTOR_FP32 = 跟 sqlite-vec / usearch 同精度.
        # HNSW 用默认 HnswIndexParam() (ef_construction=200, m=16 文档 default).
        # FTS 用 jieba (中文 tokenizer, 主人偏好, 见 README §向量后端部署矩阵).
        schema = zv.CollectionSchema(
            name=self.collection_path.stem,
            fields=[
                zv.FieldSchema(name="content", data_type=zv.DataType.STRING, index_param=zv.FtsIndexParam(tokenizer_name="jieba")),
                zv.FieldSchema(name="memory_type", data_type=zv.DataType.STRING),
                zv.FieldSchema(name="source", data_type=zv.DataType.STRING),
            ],
            vectors=[
                zv.VectorSchema(name="embedding", data_type=zv.DataType.VECTOR_FP32, dimension=self.dim, index_param=zv.HnswIndexParam()),
            ],
        )
        return schema

    @property
    def name(self) -> str:
        return "zvec"

    @property
    def supports_fts(self) -> bool:
        return True

    def knn(self, query_bytes: bytes, top_k: int, conn=None) -> List[KNNHit]:
        # query_bytes 是 float32 序列化; zvec 接受 python list[float]
        vec = _deserialize_f32(query_bytes)
        zv = self._zvec
        docs = self._col.query(zv.Query(field_name="embedding", vector=vec, param=zv.HnswQueryParam(ef=top_k * 2)))
        hits = []
        for d in docs[:top_k]:
            # [8/6 fix] zvec doc id = chunks.rowid (int, 生产路径). 翻译回 chunk_id via SQLite.
            # 测试路径 (conn=None, fake zvec): d.id 可能是 chunk_id 本身 (非 int), 直接用.
            try:
                zvec_doc_id = int(d.id)
                if conn is not None:
                    row = conn.execute("SELECT id FROM chunks WHERE rowid = ?", (zvec_doc_id,)).fetchone()
                    if row is None:
                        continue
                    chunk_id = row[0]
                else:
                    # 生产路径但 conn=None 兜底 (不推荐)
                    chunk_id = str(zvec_doc_id)
            except (TypeError, ValueError):
                # 测试路径 / 旧 rebuild 残留 (chunk_id 直接当 zvec_id)
                chunk_id = str(d.id)
            hits.append(KNNHit(chunk_id=chunk_id, distance=float(d.score)))
        return hits

    def add(self, chunk_id: str, vector_bytes: bytes, conn=None, content: Optional[str] = None, memory_type: str = "", source: str = "") -> None:
        zv = self._zvec
        # [8/6 fix] zvec 0.6 schema 所有 STRING 字段 nullable=False (default), 必传.
        if (not content or not memory_type or not source) and conn is not None:
            row = conn.execute(
                "SELECT content, memory_type, source FROM chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
            if row:
                content = content or (row["content"] if hasattr(row, "keys") else row[0]) or ""
                memory_type = memory_type or (row["memory_type"] if hasattr(row, "keys") else row[1]) or "fact"
                source = source or (row["source"] if hasattr(row, "keys") else row[2]) or ""
        # [8/6 fix] zvec 0.6 doc id 必须 [A-Za-z0-9_-]; 但 mnelo chunk_id 含 ':' / 中文.
        # 实际方案: 用 chunks.rowid (int) 当 zvec doc id — 稳定 + 唯一 + 不丢信息.
        # knn() / remove() / contains() / cleanup_orphans() 都通过 rowid join 翻译.
        if conn is not None:
            row = conn.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
            if row is None:
                raise ValueError(f"chunk {chunk_id} not found in chunks table (no rowid)")
            zvec_id = str(row[0])
        else:
            # fallback (测试路径): sanitize. 不推荐生产用.
            zvec_id = chunk_id.replace(":", "_").replace(" ", "_")
        self._col.upsert(
            zv.Doc(
                id=zvec_id,
                fields={
                    "content": content or "",
                    "memory_type": memory_type or "fact",
                    "source": source or "",
                },
                vectors={"embedding": _deserialize_f32(vector_bytes)},
            )
        )

    def remove(self, chunk_id: str, conn=None) -> None:
        # [8/6 fix] zvec doc id = chunks.rowid, 翻译: chunk_id → rowid → zvec_id
        zvec_id = self._chunk_id_to_zvec_id(chunk_id, conn)
        if zvec_id is not None:
            self._col.delete([zvec_id])

    def _chunk_id_to_zvec_id(self, chunk_id: str, conn) -> Optional[str]:
        """[8/6 fix] chunk_id → chunks.rowid → zvec doc id (str). None = 没找到."""
        if conn is None:
            return chunk_id  # fallback (测试)
        row = conn.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        return str(row[0]) if row else None

    def size(self) -> int:
        """[8/5 主人 commit] zvec collection 文档数 (走 stats 属性 + iter_all 兜底)."""
        try:
            # zvec 0.6 stats 是 property 不是 method, 含 doc_count
            stats = self._col.stats
            if hasattr(stats, "doc_count"):
                return int(stats.doc_count)
        except Exception:
            pass
        try:
            return sum(1 for _ in self._col.iter_all())
        except Exception:
            return 0

    def fts(self, query: str, top_k: int, conn=None) -> List[str]:
        """zvec 原生 FTS BM25 → top-k chunk_id (仅排序). 过滤在 memory.py SQLite 侧.
        [8/6 fix] d.id 是 zvec doc id (= chunks.rowid), 翻译回 chunk_id via SQLite.
        """
        zv = self._zvec
        docs = self._col.query(
            zv.Query(
                field_name="content",
                fts=zv.Fts(match_string=query),
                param=zv.FtsQueryParam(default_operator="AND"),
            )
        )
        result: List[str] = []
        for d in docs[:top_k]:
            try:
                rowid_int = int(d.id)
            except (TypeError, ValueError):
                continue
            if conn is not None:
                row = conn.execute("SELECT id FROM chunks WHERE rowid = ?", (rowid_int,)).fetchone()
                if row:
                    result.append(row[0])
            else:
                result.append(str(d.id))
        return result

    def close(self) -> None:
        if self._col is not None:
            self._col.flush()

    # -------- [8/6 plan §2] 新方法 --------
    def contains(self, chunk_id: str, conn=None) -> bool:
        """[8/6 fix] chunk_id → rowid → 查 zvec collection 是否有对应 doc."""
        zvec_id = self._chunk_id_to_zvec_id(chunk_id, conn)
        if zvec_id is None:
            return False
        try:
            doc = self._col.fetch([zvec_id])
            return doc is not None and len(doc) > 0
        except Exception as e:
            logger.warning(f"[zvec.contains] fetch failed for {chunk_id}: {e}")
            return False

    def cleanup_orphans(self, conn=None, dry_run: bool = False) -> Dict:
        """遍历 iter_all → 对每个 zvec_id (= chunks.rowid) 查 chunks 表 → soft/orphan 分类 → delete.

        必须由调用方传 conn (zvec 不持 SQLite 连接; conn=None 时防御性返回全 0).
        """
        result = {
            "soft_deleted_cleaned": 0,
            "truly_orphan_cleaned": 0,
            "vectors_remaining": 0,
            "dry_run": dry_run,
        }
        if conn is None:
            logger.warning("[zvec.cleanup_orphans] conn is None — 防御性返回 (调用方应传 conn)")
            return result
        try:
            ids = [d.id for d in self._col.iter_all()]
        except Exception as e:
            logger.warning(f"[zvec.cleanup_orphans] iter_all failed: {e}")
            return result

        to_delete: List[str] = []
        for zvec_id in ids:
            try:
                rowid_int = int(zvec_id)
            except (TypeError, ValueError):
                # 旧 rebuild 残留 (chunk_id 直接当 zvec_id), 删掉 (不 join 找得到 rowid)
                result["truly_orphan_cleaned"] += 1
                to_delete.append(zvec_id)
                continue
            row = conn.execute("SELECT valid_until FROM chunks WHERE rowid = ?", (rowid_int,)).fetchone()
            if row is None:
                result["truly_orphan_cleaned"] += 1
                to_delete.append(zvec_id)
            elif row[0]:
                result["soft_deleted_cleaned"] += 1
                to_delete.append(zvec_id)

        if not dry_run and to_delete:
            try:
                self._col.delete(to_delete)
            except Exception as e:
                logger.warning(f"[zvec.cleanup_orphans] delete failed: {e}")

        if dry_run:
            result["vectors_remaining"] = len(ids)
        else:
            try:
                result["vectors_remaining"] = sum(1 for _ in self._col.iter_all())
            except Exception:
                result["vectors_remaining"] = -1
        return result


def _deserialize_f32(data: bytes) -> List[float]:
    """sqlite_vec.serialize_float32 → list[float]."""
    import struct

    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


# ============================================================
# usearch 后端 (硬件无关 HNSW — TASKS_SEARCH_INDEX §4 A1/A2)
# ============================================================


def usearch_available() -> bool:
    """[A1 §4] 进程内检测 usearch — 旧 CPU 不崩 (已实测), 只有 ImportError 可能.

    与 zvec_available 不同: usearch 在 Ivy Bridge 等老 x86_64 上可跑, 无需子进程隔离.
    """
    try:
        import usearch  # noqa: F401

        return True
    except ImportError:
        return False


class UsearchIndex(SearchIndex):
    """[A2 §4] usearch 后端 — HNSW, 硬件无关 (DESIGN §8.3 升级档, 本机 Ivy Bridge 可跑).

    [8/6 plan §2 精度] f16 量化 — Index(dtype='f16') 默认 2 字节/维,
    add/search 自动 f32↔f16 cast, KNN 查询不受影响. f16 是永久契约.

    [8/8 根因修复] 启动不再盲 load on-disk 索引: 原生 load 前先 Index.metadata()
    读文件头预检 (损坏/截断/错 dtype → 干净 ValueError, 不触发原生图 load),
    再比对 sidecar 指纹 (close 时写的 active chunk 集合签名) 判定 stale; 任一
    不过 → 自动从 SQLite (唯一事实源) 重建 f16 索引, 坏文件改名 .corrupt-<ts>
    留档. 由此根除两类启动事故:
      - 损坏/截断索引盲 load → 原生 abort (free(): corrupted unsorted chunks)
      - f32/f16 混合精度文件 load 后静默转 F32, 后续 add 写 f16 → 原生堆损坏
      - stale 索引 (rowid 不再对应 chunks / 漏最新 chunk) → 静默漏召回

    内部 id = chunks.rowid (同 sqlite_vec, 无独立映射表 — 避免双写不一致).
    """

    def __init__(self, db_path: Path, dim: int):
        self.db_path = db_path
        self.dim = dim
        self._index_path = db_path.parent / "usearch.index"
        # [8/8 根因修复] sidecar 指纹 — close() 记录 active chunk 集合签名,
        # 启动时比对 SQLite (唯一事实源), 不一致 → 判定索引 stale → 自动重建.
        self._meta_path = db_path.parent / "usearch.index.verified.json"
        # 映射查询用自有 sqlite 连接 (usearch 不在 memory.py 事务里)
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # usearch 索引: 已存在则 load, 否则新建
        from usearch.index import Index

        self._index = Index(ndim=dim, metric="cos", dtype="f16")
        # [8/6 M38 harden] 运行时断言 - 必须 f16, 防未来 PR 不慎改 dtype.
        # usearch 2.x dtype 在 Index.dtype 属性上返 ScalarKind 枚举 (eg
        # <ScalarKind.F16: 12>). 用 enum 名 (F16/F32/I8/B1x8) 判定, 不依赖
        # repr/str.
        # 主人口中 'all usearch ops must work in f16' 锁定 (8/6).
        actual_dtype = getattr(self._index, "dtype", None)
        actual_dtype_name = (
            actual_dtype.name  # ScalarKind.F16.name == "F16"
            if hasattr(actual_dtype, "name")
            else str(actual_dtype)
        )
        if actual_dtype_name.upper() != "F16":
            raise RuntimeError(f"UsearchIndex 必须 f16 (主人口中 8/6 锁定), got dtype={actual_dtype_name!r}. 改 dtype 之前请先走 design review + RUNBOOK §usearch-f16 章节.")
        # [8/8 根因修复] 不再盲 load on-disk 索引 — 原生 load 前先读文件头预检
        # (损坏/错 dtype 抛干净 ValueError, 不触发原生图 load), 再用 sidecar 指纹
        # 比对 SQLite (唯一事实源) 判定 stale; 任一不过 → 自动重建. 由此启动永不因
        # 坏索引 abort, 也永不静默漏数据.
        self._init_from_disk()

    @property
    def name(self) -> str:
        return "usearch"

    @property
    def supports_fts(self) -> bool:
        return False

    def knn(self, query_bytes: bytes, top_k: int, conn=None) -> List[KNNHit]:
        import numpy as np

        vec = np.frombuffer(query_bytes, dtype=np.float32)
        if vec.ndim == 1:
            vec = vec.reshape(1, -1)
        res = self._index.search(vec, top_k)
        c = conn or self._conn
        hits: List[KNNHit] = []
        for uid, dist in zip(res.keys, res.distances):
            row = c.execute("SELECT id FROM chunks WHERE rowid = ?", (int(uid),)).fetchone()
            if row:
                hits.append(KNNHit(chunk_id=row["id"], distance=float(dist)))
        return hits

    def add(self, chunk_id: str, vector_bytes: bytes, conn=None, content: Optional[str] = None) -> None:
        import numpy as np

        c = conn or self._conn
        row = c.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            logger.warning(f"[usearch.add] chunk {chunk_id} not found")
            return
        vec = np.frombuffer(vector_bytes, dtype=np.float32)
        if vec.ndim == 1:
            vec = vec.reshape(1, -1)
        ids = np.array([row["rowid"]], dtype=np.uint64)
        # [8/6 plan §C3 fix] 含 contains 早退 — 避免 remove+readd 在 usearch f16 下 SIGSEGV.
        # 原 try/except "Duplicate keys" → remove+add 路径在 usearch 2.x 有 f16 兼容性 bug
        # (remove 后立即 add 同一 rowid 偶发 _add_to_compiled SIGSEGV).
        # 用 set(keys) 而非 in keys: usearch IndexedKeys.__contains__ 偶发 SIGSEGV.
        existing = {int(k) for k in self._index.keys}
        if int(row["rowid"]) in existing:
            logger.debug(f"[usearch.add] rowid {row['rowid']} 已在索引, 跳过")
            return
        try:
            self._index.add(ids, vec)
        except RuntimeError as e:
            # 兜底: 即便 contains 漏判, 真正的 Duplicate 也能恢复
            if "Duplicate keys" not in str(e):
                raise
            logger.warning(f"[usearch.add] rowid {row['rowid']} exists — remove+readd (idempotent)")
            try:
                self._index.remove(ids)
            except Exception:
                pass
            self._index.add(ids, vec)
        # NOTE: usearch 索引仅在 close() 时 save() — 进程异常退出前 add 可能丢

    def remove(self, chunk_id: str, conn=None) -> None:
        import numpy as np

        c = conn or self._conn
        row = c.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row:
            self._index.remove(np.array([row["rowid"]], dtype=np.uint64))

    def size(self) -> int:
        """[8/5 主人 commit] Index.size 是 int 属性 (不是方法, 已踩坑)."""
        return self._index.size

    def close(self) -> None:
        self._index.save(self._index_path)  # 持久化 (f16 写入)
        # [8/8] 先 save 索引再写 sidecar — 若中途崩, sidecar 旧 → 下次启动自动重建 (安全).
        self._write_sidecar()
        self._conn.close()

    # -------- [8/6 plan §2] 新方法 --------
    def contains(self, chunk_id: str, conn=None) -> bool:
        """查 chunks 表拿 rowid → Index.keys 包含则 True.

        [8/6 fix] 用 set(keys) 而非 `in keys`: usearch IndexedKeys.__contains__
        偶发 SIGSEGV (同 add() 的规避, 见下), 统一走 set 成员判断.
        """
        c = conn or self._conn
        row = c.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return False
        return int(row["rowid"]) in {int(k) for k in self._index.keys}

    def cleanup_orphans(self, conn=None, dry_run: bool = False) -> Dict:
        """遍历 Index.keys (rowid) → 查 chunks 行:
            - 无行 → truly_orphan
            - valid_until 非空 → soft_deleted
            - 否则保留
        非 dry-run 时 remove. 不 save — 落盘交给 close().
        """
        import numpy as np

        result = {
            "soft_deleted_cleaned": 0,
            "truly_orphan_cleaned": 0,
            "vectors_remaining": 0,
            "dry_run": dry_run,
        }
        c = conn or self._conn
        rowids: List[int] = list(self._index.keys)
        to_remove: List[int] = []
        for rid in rowids:
            row = c.execute("SELECT valid_until FROM chunks WHERE rowid = ?", (rid,)).fetchone()
            if row is None:
                result["truly_orphan_cleaned"] += 1
                to_remove.append(rid)
                continue
            if row[0]:
                result["soft_deleted_cleaned"] += 1
                to_remove.append(rid)

        if not dry_run and to_remove:
            try:
                self._index.remove(np.array(to_remove, dtype=np.uint64))
            except RuntimeError as e:
                logger.warning(f"[usearch.cleanup_orphans] remove failed: {e}")
                for rid in to_remove:
                    try:
                        self._index.remove(np.array([rid], dtype=np.uint64))
                    except Exception:
                        pass

        if dry_run:
            result["vectors_remaining"] = len(rowids)
        else:
            result["vectors_remaining"] = len(self._index.keys)
        return result

    # -------- [8/8 根因修复] 启动预检 + 自动重建 (替代盲 load) --------

    def _init_from_disk(self) -> None:
        """加载磁盘索引前先校验, 校验不过自动重建.

        根因: 旧实现见 usearch.index 就 self._index.load() — 文件损坏/截断/
        错 dtype 时 load 本身原生 abort (free(): corrupted unsorted chunks);
        stale (rowid 不再对应 chunks 或漏最新 chunk) 则静默漏召回. 现在:
          1) Index.metadata(path) 读文件头预检 — 只解析头部, 损坏/垃圾文件
             抛干净 ValueError, 不触发原生图 load;
          2) sidecar 指纹比对 SQLite active 集合 — 不一致 = stale;
          3) 任一不过 → 自动从 SQLite 重建, 坏文件改名 .corrupt-<ts> 留档.
        由此启动永不因坏索引 abort, 也永不静默漏数据.
        """
        if not self._index_path.exists():
            return  # 全新索引, 无磁盘状态
        problems = self._validate_index_header()
        stale = False if problems else self._is_stale()
        if not problems and not stale:
            try:
                self._index.load(self._index_path)  # load 是实例方法
            except Exception as e:
                problems.append(f"load 异常: {type(e).__name__}: {e}")
            else:
                dtype_name = getattr(self._index.dtype, "name", str(self._index.dtype))
                if dtype_name.upper() != "F16":
                    problems.append(f"load 后精度 {dtype_name} ≠ f16 (混合精度触发原生堆损坏)")
        if problems or stale:
            self._auto_rebuild(problems or ["索引与 SQLite chunk 集合不一致 (stale)"])
        elif self._read_sidecar() is None:
            self._write_sidecar()  # 旧代码升级 / 手删 sidecar → 补写采纳 (避免下次误判 stale)

    def _validate_index_header(self) -> List[str]:
        """Index.metadata 只解析 usearch 文件头: 损坏/截断/垃圾 → 干净 ValueError
        (不会原生 abort). 返回问题列表, 空 = 头部可信, 可安全 load."""
        from usearch.index import Index

        try:
            meta = Index.metadata(self._index_path)
        except ValueError as e:
            return [f"文件头损坏/非 usearch 索引 (不盲 load): {e}"]
        except Exception as e:
            return [f"读文件头异常 {type(e).__name__}: {e}"]
        scalar = getattr(meta.get("kind_scalar"), "name", str(meta.get("kind_scalar")))
        if scalar.upper() != "F16":
            return [f"索引精度 {scalar} ≠ f16 (混合精度触发原生堆损坏)"]
        dims = meta.get("dimensions")
        if dims is not None and int(dims) != self.dim:
            return [f"索引维数 {dims} ≠ {self.dim} (embedding 模型/配置已变)"]
        return []

    def _is_stale(self) -> bool:
        """索引是否落后于 SQLite (事实源). sidecar 签名优先; 无 sidecar
        (旧代码升级/首次) 用文件头向量数 vs SQL active 数兜底."""
        side = self._read_sidecar()
        if side is not None:
            return side != self._chunk_set_signature()
        from usearch.index import Index

        try:
            meta = Index.metadata(self._index_path)
            return meta.get("count_present") != len(self._active_chunks())
        except Exception:
            return True

    def _active_chunks(self) -> List[str]:
        """active chunk id 列表 (SQLite 是唯一事实源). chunks 表不存在 → []."""
        if not self._chunks_table_exists():
            return []
        return [r["id"] for r in self._conn.execute("SELECT id FROM chunks WHERE valid_until IS NULL ORDER BY id")]

    def _chunks_table_exists(self) -> bool:
        row = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chunks'").fetchone()
        return bool(row)

    def _chunk_set_signature(self) -> str:
        """active chunk 集合稳定签名 = md5(排序 id 拼接 + 总数)."""
        import hashlib

        ids = self._active_chunks()
        return hashlib.md5(("|".join(ids) + f"|{len(ids)}").encode("utf-8")).hexdigest()

    def _read_sidecar(self) -> Optional[str]:
        try:
            import json

            return str(json.loads(self._meta_path.read_text(encoding="utf-8")).get("signature"))
        except Exception:
            return None

    def _write_sidecar(self) -> None:
        import json
        import time as _t

        data = {
            "signature": self._chunk_set_signature(),
            "dtype": "f16",
            "dim": self.dim,
            "built_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = self._meta_path.with_name(self._meta_path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self._meta_path)

    def _auto_rebuild(self, reasons: List[str]) -> None:
        """从 SQLite 重建 f16 索引. 坏文件改名 .corrupt-<ts> 留档, 全新 f16
        Index + 全量重嵌入 active chunks, 落盘 + 写 sidecar. 重建不可能
        (无 chunks 表 / embedder 挂 / 全 0 向量) → 抛 RuntimeError 手动兜底."""
        import time as _t

        logger.warning(f"[usearch] 索引 {self._index_path} 预检不过 → 自动重建. 原因: {'; '.join(reasons)}")
        if self._index_path.exists():
            # with_suffix 会把 ".index" 当后缀替换掉 → 必须用 with_name 追加保留原名
            backup = self._index_path.with_name(self._index_path.name + f".corrupt-{_t.strftime('%Y%m%d_%H%M%S')}")
            try:
                self._index_path.rename(backup)
                logger.warning(f"[usearch] 坏索引已备份 → {backup}")
            except OSError as e:
                logger.warning(f"[usearch] 备份坏索引失败 {e} — 直接覆盖重建")
        from usearch.index import Index

        self._index = Index(ndim=self.dim, metric="cos", dtype="f16")
        try:
            from embedder import embed
        except ImportError as e:
            raise RuntimeError(f"[usearch] 自动重建需要 embedder 但 import 失败 ({e}). 请手动: scripts/rebuild_index.py --backend usearch --fresh") from e
        import struct

        added, failed = 0, 0
        active = self._active_chunks()
        for cid in active:
            row = self._conn.execute("SELECT content FROM chunks WHERE id = ?", (cid,)).fetchone()
            if row is None:
                continue
            try:
                vec = embed(row["content"])
                self.add(cid, struct.pack(f"{len(vec)}f", *vec), conn=self._conn)
                added += 1
            except Exception as e:
                logger.warning(f"[usearch] 重建嵌入失败 {cid}: {e}")
                failed += 1
        if active and added == 0:
            raise RuntimeError(f"[usearch] 自动重建 0/{len(active)} 向量 — embedder 或 DB 异常. 请手动: scripts/rebuild_index.py --backend usearch --fresh")
        self._index.save(self._index_path)
        self._write_sidecar()
        if failed:
            logger.warning(f"[usearch] 自动重建完成: added={added}, failed={failed}")
        else:
            logger.info(f"[usearch] 自动重建完成: {added} 向量 → {self._index_path.name}")


# ============================================================
# 工厂 + 特性检测
# ============================================================


def _cpu_has_avx2() -> Optional[bool]:
    """探测当前 CPU 是否支持 AVX2 (zvec 0.6 原生扩展的硬性要求).

    返回 True/False 表示探测成功; None 表示当前平台无法探测 (调用方回退到
    import 试错). Linux 读 /proc/cpuinfo flags; macOS 查 sysctl
    machdep.cpu.leaf7_features.
    """
    try:
        if sys.platform == "linux":
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("flags"):
                        return "avx2" in line.lower()
            return None  # /proc/cpuinfo 无 flags 行 (异常环境)
        if sys.platform == "darwin":
            import subprocess

            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.leaf7_features"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return "avx2" in out.stdout.lower()
            return None  # sysctl 不可用 / 空输出
    except Exception as e:
        logger.warning(f"[cpu_has_avx2] 探测失败: {e}")
        return None
    return None


def zvec_available() -> bool:
    """检测 zvec 是否可导入 — 主进程 import 前先探测 CPU AVX2.

    ⚠️ zvec 0.6 原生扩展硬性要求 AVX2+; 无 AVX2 的 CPU 上 `import zvec` 是进程级
    SIGILL 崩溃 (不是可捕获的 Python 异常), try/except 拦不住, 会把整个 mnelo
    带崩. 因此在主进程 import 之前先探测 CPU: **明确无 AVX2 → 直接返回 False,
    绝不 import**, auto 链走 usearch. 探测不了 (None) 才回退 try-import.

    [8/6 fix] macOS 26 launchd 起的 MCP 进程 fork 子进程跑 zvec native .so mmap
    必现 BlockingIOError (Errno 35 = EAGAIN) — 子进程检测不可靠, 故保留主进程路径.
    """
    cpu_avx2 = _cpu_has_avx2()
    if cpu_avx2 is False:
        logger.info("[zvec_available] CPU 无 AVX2 (zvec 0.6 硬性要求) — 回落 usearch, 不 import zvec")
        return False
    try:
        import zvec  # noqa: F401

        return True
    except Exception as e:
        logger.warning(f"[zvec_available] main-process import failed: {type(e).__name__}: {e}")
        return False


# [8/6 plan §1] 向量库必选二选一 — usearch/zvec 都不可用时 RuntimeError.
def _pick_backend(requested: str, db_path: Path, dim: int) -> SearchIndex:
    """按 backend 字符串选择后端. requested 默认 'auto' → zvec (INT8, 优先) > usearch (f16); 都不可用抛 RuntimeError."""
    if requested == "auto":
        if zvec_available():
            return ZvecIndex(db_path.parent / "search_index.zv", dim)
        if usearch_available():
            logger.info("[search_index] auto: zvec 未装, 用 usearch (f16)")
            return UsearchIndex(db_path, dim)
        raise RuntimeError("向量库是必选依赖 — zvec 与 usearch 均不可用. 请 `pip install usearch>=2.26` 或 `pip install zvec`.")
    if requested == "zvec":
        if zvec_available():
            return ZvecIndex(db_path.parent / "search_index.zv", dim)
        raise RuntimeError("zvec 不可用 (本机可能缺 AVX2+ 指令). 改 'auto' 让 mnelo 回落 usearch, 或换支持 zvec 的部署机.")
    if requested == "usearch":
        if usearch_available():
            return UsearchIndex(db_path, dim)
        raise RuntimeError("usearch 未安装. `pip install 'usearch>=2.26'` 或改 'auto' 试 zvec.")
    logger.warning(f"[search_index] 未知 backend '{requested}', fallback 到 auto")
    return _pick_backend("auto", db_path, dim)


def build_search_index(backend: str, db_path: Path, dim: int) -> SearchIndex:
    """[8/6 plan §1] 按 config 构建索引后端. backend ∈ {auto, usearch, zvec}."""
    return _pick_backend(backend, db_path, dim)
