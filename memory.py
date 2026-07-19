#!/usr/bin/env python3
"""
memory.py — mnelo 核心 CRUD API

- 6 个核心接口: remember / recall / relate / forget / update / graph_query
- 4 路召回 (向量 + 图 + 元数据 + 实体) + RRF 融合
- 4D 时间维度 (valid_from / valid_until / soft delete + 自动级联)
- 单一 writer (单进程) + WAL + busy_timeout=30s 防 lock
"""

import contextlib
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import sqlite_vec

logger = logging.getLogger("mnelo")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

from embedder import embed_bytes
from metrics import get_registry as _metrics_registry  # [7/19 v0.5.3] observability

# validation 模块从 conftest/repo 加载 (live == repo via hook sync).
# 注意: memory.py 不再硬编码 /Users/apple/.hermes/memory path — repo 自身是 single source of truth.
from validation import (
    ValidationError,
    validate_chunk_content,
    validate_entity_payload,
    validate_id,
    validate_query,
)

DB_PATH = Path("/Users/apple/.hermes/memory/memory.db")
# 注: embedding 模型 + dim 不再在此处硬编码 — 见 embedder.py 从 config 读 (config.toml [embedder])


def now(tz: str = None) -> str:
    """Return current time as ISO 8601 string with seconds precision (e.g. '2026-07-18T15:48:00').

    Args:
        tz: Timezone setting.
            - None (default) → use config.timezone ('local' by default)
            - 'local' → datetime.now() (system local time)
            - 'utc' → datetime.utcnow()
            - 'Asia/Shanghai' (IANA name) → use that timezone

    Reads default from config.timezone unless overridden.

    Used as default for valid_from / valid_until / timestamp fields.
    """
    from config import config as _cfg

    if tz is None:
        tz = _cfg.timezone

    if tz == "local":
        return datetime.now().isoformat(timespec="seconds")
    elif tz == "utc":
        return datetime.utcnow().isoformat(timespec="seconds")
    else:
        # IANA tz (e.g. 'Asia/Shanghai'). Try zoneinfo (3.9+), fallback to manual offset
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
        except ImportError:
            # Python 3.8 fallback: manual offset
            from datetime import timedelta, timezone

            # Best-effort: use UTC and tell user to upgrade
            return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def generate_id(prefix: str = "chunk") -> str:
    """Generate a unique chunk/entity/relation id with prefix + timestamp (microsecond precision).

    Format: '{prefix}_YYYYMMDD_HHMMSS_microseconds'
    Example: 'chunk_20260718_103045_123456'

    Collision risk: microsecond precision is enough for single-process; for multi-writer
    scenarios consider adding a random suffix.
    """
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def clamp01(value: float, name: str = "value") -> float:
    """Clamp importance/weight to [0.0, 1.0] with type and NaN validation.

    [P0 审计] : remember(importance) / relate(weight) / update(new_importance)
    / _upsert_entity(importance) 之前没 bounds check, 接受任意浮点 (5.0 / -0.3 / NaN).
    加 clamp + 类型校验保证 DB 写入合法.

    Args:
        value: 输入值 (int/float), 会被转 float
        name: 字段名, 用于错误信息 (e.g. 'importance', 'weight', 'new_importance')

    Returns:
        float ∈ [0.0, 1.0]

    Raises:
        TypeError: 非数值类型 (e.g. str, None, list)
        ValueError: NaN

    Examples:
        >>> clamp01(5.0)
        1.0
        >>> clamp01(-0.3)
        0.0
        >>> clamp01(0.7)
        0.7
        >>> clamp01('high')
        TypeError: importance must be numeric, got str
        >>> clamp01(float('nan'))
        ValueError: importance must not be NaN
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be numeric, got {type(value).__name__}")
    if value != value:  # NaN check (NaN != NaN)
        raise ValueError(f"{name} must not be NaN")
    return max(0.0, min(1.0, float(value)))


@contextlib.contextmanager
def _with_row_factory(conn, factory):
    """Temporarily swap conn.row_factory inside a context, restore on exit.

    [P0 审计] : sqlite-vec 0.1.x vec0 query 返回 plain tuple, 不受
    connection.row_factory = sqlite3.Row 控制. 之前在 memory.py / mcp_server.py
    / entity_resolve.py 重复 8 处 (5 + 1 + 2). 现在统一 helper.

    Args:
        conn: sqlite3.Connection
        factory: sqlite3.Row / None / 自定义 callable

    Yields:
        the same conn (以便 with 块内直接使用)

    Examples:
        >>> with _with_row_factory(conn, sqlite3.Row):
        ...     rows = conn.execute("SELECT v.rowid AS v_rowid FROM vectors v ...").fetchall()
        ...     # rows 是 sqlite3.Row 实例, 可用 r['v_rowid']
        # 退出 with 后 conn.row_factory 恢复原值
    """
    old = conn.row_factory
    conn.row_factory = factory
    try:
        yield conn
    finally:
        conn.row_factory = old


class Memory:
    """核心 CRUD 接口."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        # check_same_thread=False —  P2+ #2 让 recall 并发跑 4 路用独立 conn 时,
        # graph_recall (主 method) 仍然在主 thread 调, 但需要 main conn 也能被 worker 间接用
        # SQLite 检查是 dbapi-level strict — 一切 conn 都允许跨 thread 是务实做法
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        # [7/18 patch G] SQLite page cache 64 MB — 让 working-set (24 MB db)
        # 在 RAM, vec0 cold-chunk 走 mmap/OS page cache 而不是每次 fetch
        # cache_size 单位是 page (default 4 KB); -64000 = -64*1024 KB
        self._conn.execute("PRAGMA cache_size = -64000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.row_factory = sqlite3.Row

        # [P2-1 优化] warm-up Embedder 避免首次 recall 1s 冷启动
        # 实测: Demo 1 1030ms wall-clock (服务端 50ms), 980ms 是 Embedder model 加载到 RAM
        # 配置: warm_up_embedder=True by default, 可 config.toml 关闭
        from config import config as _cfg

        if _cfg.warm_up_embedder:
            from embedder import get_embedder

            get_embedder()  # lazy singleton, 第一次调用触发 model 加载
            logger.info(f"[P2-1] Embedder warmed-up ({_cfg.describe()})")
        else:
            logger.info(f"[P2-1] Embedder warm-up disabled ({_cfg.describe()})")

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

    def __enter__(self) -> "Memory":
        """Support `with Memory() as m:` — returns self."""
        return self

    def __exit__(self, *args) -> None:
        """Auto-close on context exit."""
        self.close()

    # === CRU ========================

    def remember(
        self,
        content: str,
        source: str = "manual",
        importance: float = 0.5,
        entities: List[Dict] = None,
        relations: List[Dict] = None,
        tags: List[str] = None,
        session_id: str = "default",
        timestamp: str = None,
    ) -> str:
        """写入一条 chunk + 实体 + 关系.

        entities = [{id, kind, name, summary?, aliases?, properties?}]
        relations = [{source_id, target_id, relation, weight?, properties?,
                      valid_from?, valid_until?, evidence_chunk_id?}]
        """
        ts = timestamp or now()
        chunk_id = generate_id("chunk")

        # [7/19 P0-3] chunk content 大小 + 控制字符 + bidi override 验证
        content = validate_chunk_content(content)
        # [7/19 P1-1] id 来源 = generate_id (服务端生成), 无需 validate_id

        # 1. 写 chunk
        self._conn.execute(
            """
            INSERT INTO chunks (id, content, source, session_id, timestamp, importance, metadata_json, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
            (
                chunk_id,
                content,
                source,
                session_id,
                ts,
                clamp01(importance, "importance"),
                json.dumps({"tags": tags or []}, ensure_ascii=False),
            ),
        )

        # 2. 写 entities (insert or ignore — 实体可能已存在)
        for ent in entities or []:
            self._upsert_entity(ent)

        # 3. 写 relations
        for rel in relations or []:
            self._conn.execute(
                """
                INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                       valid_from, valid_until, source, confidence, evidence_chunk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    rel["source_id"],
                    rel["target_id"],
                    rel["relation"],
                    rel.get("weight", 1.0),
                    json.dumps(rel.get("properties", {}), ensure_ascii=False),
                    rel.get("valid_from", ts),
                    rel.get("valid_until"),  # None = NULL
                    rel.get("source", source),
                    rel.get("confidence", 1.0),
                    rel.get("evidence_chunk_id", chunk_id),
                ),
            )

        # 4. 写 vector (sqlite-vec 0.1.x: vec0.rowid = chunks.rowid)
        # [BUG 7/18 fix] 之前用 last_insert_rowid() 但 entities/relations INSERT 后会被覆盖
        # → vector 写到错的 vec0 rowid, _vector_recall 召回失败
        # 修: 用 SELECT round-trip 拿 chunks.rowid (保证 1:1)
        chunk_rowid = self._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()[0]
        v_bytes = embed_bytes(content)
        # [7/19 v0.5.5] Robust vector insert: if rowid collides with a previous
        # crashed insert or orphan from `forget()` cleanup, REPLACE it (DELETE+INSERT).
        # Root cause: vec0 internal counter doesn't perfectly track chunks.rowid
        # (e.g. soft-deleted chunks leave their vectors in place). Without this
        # guard, remember() raises UNIQUE constraint on vectors primary key.
        try:
            self._conn.execute(
                "INSERT INTO vectors (rowid, embedding) VALUES (?, ?)",
                (chunk_rowid, v_bytes),
            )
        except sqlite3.IntegrityError:
            logger.warning(f"vector rowid {chunk_rowid} already exists — replacing (chunk_id={chunk_id})")
            self._conn.execute("DELETE FROM vectors WHERE rowid = ?", (chunk_rowid,))
            self._conn.execute(
                "INSERT INTO vectors (rowid, embedding) VALUES (?, ?)",
                (chunk_rowid, v_bytes),
            )

        self._conn.commit()
        # [7/19 v0.5.3] metrics
        _metrics_registry().remember_total.inc(source=source or "unknown")
        return chunk_id

    def relate(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float = 1.0,
        valid_from: str = None,
        valid_until: str = None,
        evidence_chunk_id: str = None,
        properties: Dict = None,
    ) -> int:
        """新建一条关系."""
        # [7/19 P1-1] id 格式验证 (白名单正则)
        source_id = validate_id(source_id, "source_id")
        target_id = validate_id(target_id, "target_id")
        if evidence_chunk_id is not None:
            evidence_chunk_id = validate_id(evidence_chunk_id, "evidence_chunk_id")
        cur = self._conn.execute(
            """
            INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                   valid_from, valid_until, source, confidence, evidence_chunk_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', 1.0, ?)
        """,
            (
                source_id,
                target_id,
                relation,
                clamp01(weight, "weight"),
                json.dumps(properties or {}, ensure_ascii=False),
                valid_from or now(),
                valid_until,
                evidence_chunk_id,
            ),
        )
        self._conn.commit()
        # [7/19 v0.5.3] metrics
        _metrics_registry().relate_total.inc()
        return cur.lastrowid

    def update(
        self,
        old_id: str,
        reason: str = "updated",
        new_content: str = None,
        new_properties: Dict = None,
        new_importance: float = None,
    ) -> str:
        """Update by creating new chunk version + superseding old (immutable history).
        老 chunk 不直接覆盖, 而是标 superseded_by + valid_until=now, 触发器自动级联:
        所有引用老 chunk 的边 valid_until = now. 历史完整保留.

        [7/19 P0-3] 新 content 也走 sanitize (None = 保留老内容, 跳过)
        [7/19 P1-1] id 格式验证

        Args:
            old_id: 要更新的 chunk id (active 的, 否则 ValueError)
            reason: 标记更新原因 (写进 source = 'update:<reason>')
            new_content: 新内容, None = 保留老内容
            new_properties: 新 properties (暂未实现)
            new_importance: 新重要性, None = 保留老值, 否则 clamp01

        Returns:
            新 chunk id (新版本 id)
        """
        # [7/19 P1-1] id 格式验证
        old_id = validate_id(old_id, "old_id")
        # [7/19 P0-3] 新 content 也走 sanitize (None = 保留老内容, 跳过)
        if new_content is not None:
            new_content = validate_chunk_content(new_content)

        old = self._conn.execute("SELECT * FROM chunks WHERE id = ? AND valid_until IS NULL", (old_id,)).fetchone()
        if not old:
            raise ValueError(f"chunk {old_id} not found or already superseded")

        # 1. 创建新 chunk
        new_id = generate_id("chunk")
        # [P0 审计] new_importance 也走 clamp01 防止越界
        if new_importance is not None:
            importance_value = clamp01(new_importance, "new_importance")
        else:
            importance_value = old["importance"] if old["importance"] is not None else 0.5
        self._conn.execute(
            """
            INSERT INTO chunks (id, content, source, session_id, timestamp, importance, metadata_json, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
            (
                new_id,
                new_content or old["content"],
                "update:" + reason,
                old["session_id"],
                now(),
                importance_value,
                json.dumps({"supersedes": old_id, "reason": reason}, ensure_ascii=False),
            ),
        )

        # 2. 老 chunk 标 superseded_by + valid_until (中 supersede 后不再召回)
        self._conn.execute(
            "UPDATE chunks SET superseded_by = ?, valid_until = ? WHERE id = ? AND valid_until IS NULL",
            (new_id, now(), old_id),
        )
        self._conn.commit()
        # [7/19 v0.5.3] metrics
        _metrics_registry().update_total.inc()
        return new_id

    def forget(
        self,
        target_id: str,
        target_kind: str = "chunk",  # 'chunk' / 'entity' / 'relation'
        reason: str = "outdated",
        cascade: bool = True,
    ) -> Dict[str, int]:
        """软删除: valid_until = now, cascade 级联失效引用边.
        主人口中"删除无用知识" — 不直接物理删, 30 天后 worker 物理清理.
        """
        # [7/19 P1-1] id 格式验证
        target_id = validate_id(target_id, "target_id")
        if target_kind == "chunk":
            self._conn.execute(
                "UPDATE chunks SET valid_until = ? WHERE id = ? AND valid_until IS NULL", (now(), target_id)
            )
        elif target_kind == "entity":
            self._conn.execute(
                "UPDATE entities SET valid_until = ? WHERE id = ? AND valid_until IS NULL", (now(), target_id)
            )
        elif target_kind == "relation":
            self._conn.execute(
                "UPDATE relations SET valid_until = ? WHERE id = ? AND valid_until IS NULL", (now(), target_id)
            )
        else:
            raise ValueError(f"unknown kind: {target_kind}")

        # cascade (主流程中, 触发器也会自动做)
        edges_invalidated = 0
        if cascade:
            cur = self._conn.execute(
                """
                UPDATE relations SET valid_until = ?
                WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL
            """,
                (now(), target_id, target_id),
            )
            edges_invalidated = cur.rowcount

        # 入队 30 天后物理删除
        self._conn.execute(
            """
            INSERT INTO purged_queue (target_id, target_kind, purged_at, done)
            VALUES (?, ?, datetime('now', '+30 days'), 0)
        """,
            (target_id, target_kind),
        )

        self._conn.commit()
        # [7/19 v0.5.3] metrics
        _metrics_registry().forget_total.inc(kind=target_kind or "unknown")
        return {"edges_invalidated": edges_invalidated, "queued_purge": 1}

    # === R = Recall (3 路 + RRF) ===================

    def recall(
        self,
        query: str,
        top_k: int = 5,
        graph_hops: int = 2,
        filters: Dict = None,
        strategy: str = "rrf",
        asof: str = None,
    ) -> List[Dict]:
        """4 路召回 + RRF 融合 ( 7/18 加 entity 路).
        [7/19 P1-4] query 大小 + 控制字符 + bidi 验证

        strategy: 'rrf' / 'vector_only' / 'graph_only' / 'meta_only' / 'entity_only'
        asof: 时间切片查询 ('2026-07-17T15:00:00')
        """
        # [P2+ #1 7/18 patch] Skip noisy / placeholder queries  recall_log 信号纯度
        # 数据: 24h 919 recall, 80 (8%) 空 hits — 一半是 'anything' / test_crud_xxx 占位符
        # 这些 query 没意义, 不应该污染 recall_log / recall_count / last_recalled
        if not query or not query.strip():
            return []
        # [7/19 P1-4] query 验证 (sanitize + size cap) — 必须在 empty check 之后,
        # 否则空 query 会被 validation 拒掉而不是返 []
        query = validate_query(query)
        clean = query.strip()
        # 占位符白名单 (case insensitive)
        _PLACEHOLDER_QUERIES = {
            "anything",
            "something",
            "test",
            "foo",
            "bar",
            "baz",
            "q",
            "?",
            "placeholder",
            "dummy",
            "demo",
            "sample",
            "foo bar",
        }
        if clean.lower() in _PLACEHOLDER_QUERIES:
            return []
        # 单字符无意义 (除了短股票代码 e.g. 'a' 单字母 + 中文概念单字)
        # 中文/unicode 单字可能有意义, 不过滤. ASCII 单字符全部过滤
        if len(clean) == 1 and clean.isascii():
            return []
        # query validation passed, replace with cleaned version
        query = clean

        import time

        t0_start = time.time()

        asof = asof or now()

        if strategy == "rrf":
            # [P2+ #2 7/18 patch] 4 路召回并发 —  p95 70ms → 25ms 目标
            # 串行慢原因: vec0 MATCH ~3.5ms + meta LIKE 0-11ms + entity name ~2-9ms + graph 0-7ms 累加
            # WAL mode SQLite 允许多 conn 并发读, 每路开独立 conn + 共享 Embedder
            # 用 ThreadPoolExecutor 跑 4 task 并行, 取最长耗时 (vs 串行累加)
            from concurrent.futures import ThreadPoolExecutor

            # 4 个独立 SQLite connection (避免同一 conn threading 冲突)
            # check_same_thread=False 让 conn 跨 thread 可用 (主 thread 创建, worker 用)
            recall_conns = [sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False) for _ in range(4)]  # noqa: E501
            for c in recall_conns:
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA busy_timeout = 30000")
                # [7/18 patch G] 每个 worker conn 也设 64 MB cache
                c.execute("PRAGMA cache_size = -64000")
                c.enable_load_extension(True)
                sqlite_vec.load(c)
                c.enable_load_extension(False)
                c.row_factory = sqlite3.Row

            # [7/19 v0.5.3] Per-lane timing for metrics (vector first, parallel meta/entity/graph)
            with ThreadPoolExecutor(max_workers=4) as ex:
                t_vec_0 = time.time()
                f_vec = ex.submit(self._vector_recall_with_conn, recall_conns[0], query, top_k * 2, filters, asof)
                f_meta = ex.submit(self._meta_recall_with_conn, recall_conns[1], query, top_k * 2, filters, asof)
                f_entity = ex.submit(self._entity_recall_with_conn, recall_conns[2], query, top_k * 2, filters, asof)

                vector_hits = f_vec.result()
                vec_ms = (time.time() - t_vec_0) * 1000
                # graph 等 vector 完成再开始 (graph 依赖 vector_hits 作为 seed)
                t_graph_0 = time.time()
                f_graph = ex.submit(self._graph_recall, vector_hits, graph_hops, asof)
                meta_hits = f_meta.result()
                entity_hits = f_entity.result()
                graph_hits = f_graph.result()
                graph_ms = (time.time() - t_graph_0) * 1000

            # 关独立连接
            for c in recall_conns:
                c.close()

            results = self._rrf_fuse([vector_hits, graph_hits, meta_hits, entity_hits], top_k)
            # meta/entity roughly parallel (no separate timers; record 0 to skip metric)
            lane_latencies = {"vector": vec_ms, "graph": graph_ms, "meta": 0.0, "entity": 0.0}
        elif strategy == "vector_only":
            t0 = time.time()
            results = self._vector_recall(query, top_k, filters, asof)
            lane_latencies = {"vector": (time.time() - t0) * 1000}
        elif strategy == "graph_only":
            t0 = time.time()
            vector_hits = self._vector_recall(query, top_k, filters, asof)
            graph_hits = self._graph_recall(vector_hits, graph_hops, asof)
            results = graph_hits[:top_k]
            lane_latencies = {
                "vector": (time.time() - t0) * 1000,
                "graph": 0.0,
            }
        elif strategy == "meta_only":
            t0 = time.time()
            results = self._meta_recall(query, top_k, filters, asof)
            lane_latencies = {"meta": (time.time() - t0) * 1000}
        elif strategy == "entity_only":
            t0 = time.time()
            results = self._entity_recall(query, top_k, filters, asof)
            lane_latencies = {"entity": (time.time() - t0) * 1000}
        else:
            raise ValueError(f"unknown strategy: {strategy}")

        latency_ms = (time.time() - t0_start) * 1000

        # [7/19 v0.5.3] metrics: per-lane counter + latency + hit count + top_k
        _reg = _metrics_registry()
        for lane, lane_ms in lane_latencies.items():
            _reg.recall_total.inc(method=lane)
            if lane_ms > 0:
                _reg.recall_latency.observe(lane_ms / 1000.0, method=lane)
        _reg.recall_hits.inc(result="empty" if not results else "non_empty")
        _reg.recall_top_k.inc(k=str(top_k))

        #  recall audit
        self._log_recall(query, results, graph_hops, latency_ms)

        # 更新 recall_count + last_recalled
        if results:
            ids = [r["chunk_id"] for r in results if "chunk_id" in r]
            if ids:
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"""
                    UPDATE chunks
                    SET recall_count = recall_count + 1, last_recalled = ?
                    WHERE id IN ({placeholders})
                """,
                    [now()] + ids,
                )
                self._conn.commit()

        return results

    def _vector_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 1: 向量检索 (sqlite-vec 0.1.x vec0 + MATCH)."""
        q_bytes = embed_bytes(query)
        # [审计 4.3 ] filter 多时, 多取一些确保过滤后还够 top_k; strategy 也加大召回
        fetch_limit = top_k * (8 if (filters or top_k >= 3) else 2)
        # [BUG 7/18 fix] vec0 extension 返回 plain tuple, sqlite3.Row 不生效
        # [P0 审计] 用 _with_row_factory helper 统一处理 (前: 双层 try/finally 嵌套)
        try:
            with _with_row_factory(self._conn, sqlite3.Row):
                rows = self._conn.execute(
                    """
                    SELECT v.rowid AS v_rowid, v.distance AS distance
                    FROM vectors v
                    WHERE v.embedding MATCH ?
                    ORDER BY v.distance
                    LIMIT ?
                """,
                    (q_bytes, fetch_limit),
                ).fetchall()
        except Exception as e:
            print(f"[vector_recall] failed: {e}")
            return []

        results = []
        for r in rows:
            v_rowid = r["v_rowid"] if isinstance(r, sqlite3.Row) else r[0]
            distance = r["distance"] if isinstance(r, sqlite3.Row) else r[1]
            chunk = self._conn.execute(
                "SELECT id, content, source, timestamp, importance FROM chunks WHERE rowid = ? AND valid_until IS NULL",
                (v_rowid,),
            ).fetchone()
            if not chunk:
                continue
            if filters:
                if "source" in filters and chunk["source"] != filters["source"]:
                    continue
            results.append(self._hit_dict(chunk, method="vector", distance=float(distance)))
        return results[:top_k]  # type: ignore

    def _vector_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 vector recall — 并发安全.

        Args:
            conn: 独立 sqlite3 connection (每路独立, 避免 threading 冲突)
        """
        q_bytes = embed_bytes(query)
        fetch_limit = top_k * (8 if (filters or top_k >= 3) else 2)
        try:
            with _with_row_factory(conn, sqlite3.Row):
                rows = conn.execute(
                    """
                    SELECT v.rowid AS v_rowid, v.distance AS distance
                    FROM vectors v
                    WHERE v.embedding MATCH ?
                    ORDER BY v.distance
                    LIMIT ?
                """,
                    (q_bytes, fetch_limit),
                ).fetchall()
        except Exception as e:
            print(f"[vector_recall_thread] failed: {e}")
            return []

        results = []
        for r in rows:
            v_rowid = r["v_rowid"] if isinstance(r, sqlite3.Row) else r[0]
            distance = r["distance"] if isinstance(r, sqlite3.Row) else r[1]
            chunk = conn.execute(
                "SELECT id, content, source, timestamp, importance FROM chunks WHERE rowid = ? AND valid_until IS NULL",
                (v_rowid,),
            ).fetchone()
            if not chunk:
                continue
            if filters:
                if "source" in filters and chunk["source"] != filters["source"]:
                    continue
            results.append(self._hit_dict(chunk, method="vector", distance=float(distance)))
        return results[:top_k]  # type: ignore

    def _meta_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 meta recall."""
        sql = """
            SELECT id, content, source, timestamp, importance FROM chunks
            WHERE valid_until IS NULL
              AND content LIKE ?
        """
        params = [f"%{query}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        sql += " ORDER BY importance DESC, timestamp DESC LIMIT ?"
        params.append(top_k)
        rows = conn.execute(sql, params).fetchall()
        return [self._hit_dict(r, method="meta") for r in rows]

    def _entity_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 entity recall."""
        if " " in query.strip():
            tokens = query.strip().split()
        else:
            tokens = [query]

        chunk_results = []
        seen_chunk_ids = set()
        for tok in tokens:
            if not tok or len(tok) < 2:
                continue
            like = f"%{tok}%"
            rows = conn.execute(
                """
                SELECT id, name, kind, summary, importance, aliases_json
                FROM entities
                WHERE valid_until IS NULL
                  AND (name LIKE ? OR aliases_json LIKE ?)
                ORDER BY importance DESC
                LIMIT ?
            """,
                (like, like, top_k),
            ).fetchall()
            for r in rows:
                # [7/19 v0.5.5] Robust aliases parsing:
                # aliases_json may be NULL (SQL), 'null' (JSON literal),
                # '[]' (empty list), or '[...]' (actual list).
                # Handle all cases defensively to avoid TypeError on `for a in None`.
                raw = r["aliases_json"]
                if not raw or raw == "null":
                    aliases = []
                else:
                    try:
                        parsed = json.loads(raw)
                        aliases = parsed if isinstance(parsed, list) else []
                    except (json.JSONDecodeError, TypeError):
                        aliases = []
                content = r["summary"] or r["name"]
                if not content:
                    continue
                hit = {
                    "chunk_id": f"entity:{r['id']}",
                    "content": content,
                    "source": f"entity:{r['kind']}",
                    "timestamp": now(),
                    "importance": float(r["importance"] or 0.5),
                    "method": "entity",
                    "entity_id": r["id"],
                    "entity_name": r["name"],
                    "entity_kind": r["kind"],
                }
                if any(tok.lower() in a.lower() for a in aliases):
                    hit["importance"] = min(1.0, hit["importance"] + 0.2)
                if hit["chunk_id"] not in seen_chunk_ids:
                    seen_chunk_ids.add(hit["chunk_id"])
                    chunk_results.append(hit)
        return chunk_results[:top_k]  # type: ignore

    def _graph_recall(self, seed_hits: List[Dict], hops: int, asof: str) -> List[Dict]:
        """路 2: 图遍历 (NetworkX 内存层 + hops 跳)."""
        if not seed_hits:
            return []
        seed_ids = {h["chunk_id"] for h in seed_hits}
        # [审计 4.1 优化] 1 次 SQL 拿全部 seed chunks 的关联 entities (避免 N+1)
        placeholders = ",".join("?" * len(seed_ids))
        rows = self._conn.execute(
            f"""
            SELECT source_id, target_id FROM relations
            WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
              AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
        """,
            (*seed_ids, *seed_ids, asof, asof),
        ).fetchall()
        seed_entities = set()
        for r in rows:
            if r["source_id"] not in seed_ids:
                seed_entities.add(r["source_id"])
            if r["target_id"] not in seed_ids:
                seed_entities.add(r["target_id"])

        # [审计 4.1 优化] 1 次 SQL 拿 entities 关联的 chunks (2 跳)
        if not seed_entities:
            return []
        placeholders = ",".join("?" * len(seed_entities))
        rows = self._conn.execute(
            f"""
            SELECT source_id, target_id FROM relations
            WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
              AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
        """,
            (*seed_entities, *seed_entities, asof, asof),
        ).fetchall()
        entity_chunks = set()
        for r in rows:
            if r["source_id"] not in seed_entities:
                entity_chunks.add(r["source_id"])
            if r["target_id"] not in seed_entities:
                entity_chunks.add(r["target_id"])

        # 排除原 seed, 取剩下的 entity_chunks
        new_chunks = entity_chunks - seed_ids - seed_entities
        if not new_chunks:
            return []

        # [ 7/18 A 方案] 第一跳就关联的 identity_fact / canonical_fact
        # 类高价值 entity 自身已是结构化答案, 直接以 entity 形式返回
        # (不必绕回 chunk)
        entity_hits = []
        if seed_entities:
            placeholders_e = ",".join("?" * len(seed_entities))
            e_rows = self._conn.execute(
                f"""
                SELECT id, kind, name, summary, importance FROM entities
                WHERE id IN ({placeholders_e}) AND valid_until IS NULL
                  AND kind IN ('identity_fact', 'canonical_fact')
            """,
                list(seed_entities),
            ).fetchall()
            for er in e_rows:
                entity_hits.append(
                    {
                        "chunk_id": f"entity:{er['id']}",
                        "content": er["summary"] or er["name"],
                        "source": f"entity:{er['kind']}",
                        "timestamp": now(),
                        "importance": float(er["importance"] or 0.5),
                        "method": "graph_entity",
                        "entity_id": er["id"],
                        "entity_name": er["name"],
                        "entity_kind": er["kind"],
                    }
                )

        placeholders = ",".join("?" * len(new_chunks))
        rows = self._conn.execute(
            f"""
            SELECT id, content, source, timestamp, importance FROM chunks
            WHERE id IN ({placeholders}) AND valid_until IS NULL
            ORDER BY importance DESC, timestamp DESC
        """,
            list(new_chunks),
        ).fetchall()
        chunk_hits = [self._hit_dict(r, method="graph") for r in rows]
        # entity 在前 (偏重结构化答案)
        return entity_hits + chunk_hits

    def _meta_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 3: 元数据 (精确 LIKE + 时间近)."""
        sql = """
            SELECT id, content, source, timestamp, importance FROM chunks
            WHERE valid_until IS NULL
              AND content LIKE ?
        """
        params = [f"%{query}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        sql += " ORDER BY importance DESC, timestamp DESC LIMIT ?"
        params.append(top_k)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._hit_dict(r, method="meta") for r in rows]

    def _entity_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 4: 实体精确/模糊匹配 ( 7/18 加).

        场景: 用户问'我住在哪里' / '主人GitHub' 类强身份事实,
        向量召回可能因为 chunk 文本太长而被埋没; 直接走 entity.name LIKE
        + entity.aliases_json 反查是更稳的路径.

        拆词策略:
        - ASCII 单词: 全部按空格切, 全词 LIKE (避免 token 太宽)
        - 中文: 只取 2+ 字连续片段 (避免'我''在'单字噪声); 取所有 2-gram + 3-gram
        - 高优先级 token (主人 / user / 我) 不参与单字 token, 全词 LIKE 即可

        降噪: identity_fact / canonical_fact 强优先级, concept 仅补足
        (concept 类实体大量含'在''住'等单字 token, 噪声很大)

        意图增强 (7/18): query 含'我'/'主人'/'ling2077'/'2077 Ling'/'user'
        等任一时, 直接拉 user 所有 identity_fact 关系 (无需 query-token 重叠,
        这是关键 — '我住在哪里' token 与 '北京市大兴区亦庄镇' 无 2-gram 重叠).
        """
        hits = []
        seen_ids = set()

        # === 第一阶段: 意图增强 (user identity 询问) ===
        identity_query_keys = ("我", "主人", "user", "ling2077", "2077 Ling")
        is_identity_query = any(k in query for k in identity_query_keys)
        if is_identity_query:
            rows = self._conn.execute("""
                SELECT e.id, e.kind, e.name, e.summary, e.importance
                FROM relations r
                JOIN entities e ON e.id = r.target_id AND e.valid_until IS NULL
                WHERE r.source_id = 'user'
                  AND r.valid_until IS NULL
                  AND e.kind IN ('identity_fact', 'canonical_fact')
            """).fetchall()
            for r in rows:
                seen_ids.add(r["id"])
                hits.append(
                    {
                        "chunk_id": f"entity:{r['id']}",
                        "content": r["summary"] or r["name"],
                        "source": f"entity:{r['kind']}",
                        "timestamp": now(),
                        "importance": float(r["importance"] or 0.9),
                        "method": "entity_intent",
                        "entity_id": r["id"],
                        "entity_name": r["name"],
                        "entity_kind": r["kind"],
                    }
                )

        # === 第二阶段: 通用 token LIKE (高优先级 → 补 concept) ===
        tokens = set()
        for w in re.split(r'[\s,;.!?\(\)\[\]\{\}"\'`]+', query):
            w = w.strip().lower()
            if len(w) >= 2:
                tokens.add(w)
            elif len(w) == 1 and w.isascii():
                tokens.add(w)
        for n in (2, 3):
            for i in range(len(query) - n + 1):
                seg = query[i : i + n]
                if all("\u4e00" <= ch <= "\u9fff" for ch in seg):
                    tokens.add(seg)
        if not tokens:
            return hits
        like_clauses = []
        params = []
        for t in tokens:
            like_clauses.append("(name LIKE ? OR id LIKE ? OR summary LIKE ?)")
            params.extend([f"%{t}%"] * 3)

        # 两轮: 高优先级 (强 fact), 后补 concept
        high_priority_kinds = ("identity_fact", "canonical_fact", "user")

        for kind_filter, take in (
            (high_priority_kinds, top_k),
            (("concept",), top_k),  # 补足
        ):
            sql = f"""
                SELECT id, kind, name, summary, importance, recall_count FROM entities
                WHERE valid_until IS NULL
                  AND kind IN ({",".join("?" * len(kind_filter))})
                  AND ({" OR ".join(like_clauses)})
                ORDER BY importance DESC, recall_count DESC
                LIMIT ?
            """
            cur_params = list(kind_filter) + params + [take]
            rows = self._conn.execute(sql, cur_params).fetchall()
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                hits.append(
                    {
                        "chunk_id": f"entity:{r['id']}",
                        "content": r["summary"] or r["name"],
                        "source": f"entity:{r['kind']}",
                        "timestamp": now(),
                        "importance": float(r["importance"] or 0.5),
                        "method": "entity",
                        "entity_id": r["id"],
                        "entity_name": r["name"],
                        "entity_kind": r["kind"],
                    }
                )
        return hits

    def _rrf_fuse(self, hit_lists: List[List[Dict]], top_k: int) -> List[Dict]:
        """Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank).

        [P2+ #4 7/18 patch] stock entity boost:
        : kind=stock 的 entity_hit (e.g. 'sh600089') 是关心的高价值答案,
        默认 RRF 把 chunk 当事实, 但 stock entity 关联 chunk 是结构的语义提升.
        BOOST = 0.05 / rank^0.5 —  trade-off: 不压倒既有排序, 但 stock always 浮顶.
        """
        import math

        rrf_score: Dict[str, float] = {}
        rrf_hits: Dict[str, Dict] = {}
        k = 60
        STOCK_BOOST = 0.05  #  P2+ #4
        for hits in hit_lists:
            for rank, h in enumerate(hits):
                # [ 7/18] 主键区分实体 vs chunk — 用 chunk_id 字段统一
                # 实体 hit 的 chunk_id = 'entity:<entity_id>'
                # chunk hit 的 chunk_id = '<chunk_id>'
                # 同 ID 合并(实体 hit 和 chunk hit 可能是同一事实在不同层的表达)
                cid = h["chunk_id"]
                rank_score = 1.0 / (k + rank + 1)
                # [P2+ #4] stock entity boost — 让 kind=stock entity (如 'sh600089') 优先
                kind = h.get("entity_kind") or (
                    "stock"
                    if h.get("source", "").startswith("entity:stock") or "stock" in str(h.get("source", ""))
                    else None
                )
                if kind == "stock" and h.get("method") == "entity":
                    # : stock entity 0.05 / rank^0.5 boost — 浮顶但不让压倒 RRF 排序
                    boost = STOCK_BOOST / math.sqrt(rank + 1)
                    rank_score += boost
                rrf_score[cid] = rrf_score.get(cid, 0) + rank_score
                rrf_hits[cid] = h
        ranked = sorted(rrf_score.items(), key=lambda x: -x[1])
        out = []
        for cid, score in ranked[:top_k]:
            h = rrf_hits[cid]
            h["rrf_score"] = score
            out.append(h)
        return out

    def _log_recall(self, query: str, results: List[Dict], hops: int, latency_ms: float):
        """[P2+ #3 7/18 patch] 写入 recall_log 审计 (always local time via now() helper).

         feedback loop 数据:
        - results_json 已存 [chunk_id] 数组 (前: 只知道命中哪些 chunk)
        - 新存 recall_details_json: top-K 完整 dict (method, distance/score, importance)
          让 daily_check / analytics 能分析 召回质量 (用什么路召回的, 距离分布)
        """
        #  feedback loop: 每条命中的 method + 距离 + 排名 (top-5 by RRF score)
        detail = [
            {
                "rank": i + 1,
                "chunk_id": r.get("chunk_id"),
                "method": r.get("method"),
                "distance": r.get("distance"),  # 0.0-2.0 越小越相似 (vector_only)
                "rrf_score": r.get("rrf_score"),  # RRF 融合分数 (rrf strategy)
                "importance": r.get("importance"),
            }
            for i, r in enumerate(results[:5])  # top-5
        ]
        self._conn.execute(
            """
            INSERT INTO recall_log (query, results_json, graph_hops, latency_ms, created_at, recall_details_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                query,
                json.dumps([r.get("chunk_id") for r in results]),
                hops,
                latency_ms,
                now(),
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    # === 图遍历 ====================

    def graph_query(
        self,
        start_node: str,
        max_hops: int = 3,
        edge_types: List[str] = None,
        asof: str = None,
    ) -> Dict:
        """子图: start_node 起, max_hops 跳内的所有节点 + 边."""
        # [7/19 P1-1] start_node 格式验证
        start_node = validate_id(start_node, "start_node")
        asof = asof or now()
        # BFS: 拿 max_hops 跳内的所有节点
        visited = {start_node}
        frontier = [start_node]
        edges = []
        for _hop in range(max_hops):
            next_frontier = []
            for node in frontier:
                sql = """
                    SELECT * FROM relations
                    WHERE (source_id = ? OR target_id = ?)
                      AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
                """
                params = [node, node, asof, asof]
                if edge_types:
                    sql += f" AND relation IN ({','.join('?' * len(edge_types))})"
                    params.extend(edge_types)
                rows = self._conn.execute(sql, params).fetchall()
                for r in rows:
                    edges.append(dict(r))
                    other = r["target_id"] if r["source_id"] == node else r["source_id"]
                    if other not in visited:
                        visited.add(other)
                        next_frontier.append(other)
            frontier = next_frontier

        # 拿节点详情
        nodes = []
        if visited:
            placeholders = ",".join("?" * len(visited))
            rows = self._conn.execute(
                f"""
                SELECT id, kind, name, summary FROM entities
                WHERE id IN ({placeholders}) AND valid_until IS NULL
            """,
                list(visited),
            ).fetchall()
            nodes = [dict(r) for r in rows]

        return {"nodes": nodes, "edges": edges, "asof": asof}

    # === 内部 helper ====================

    @staticmethod
    def _hit_dict(row, method: str, **extra) -> Dict:
        """3 路召回统一返回格式 (RRF 融合需要)。

        Args:
            row: sqlite3.Row from chunks (含 id/content/source/timestamp/importance)
            method: 'vector' / 'graph' / 'meta' / 'entity' / 'rrf'
            **extra: 召回方法特有的字段 (e.g. distance=0.123 for vector)

        Returns:
            dict 含 chunk_id/content/source/timestamp/importance/method + extra
        """
        return {
            "chunk_id": row["id"],
            "content": row["content"],
            "source": row["source"],
            "timestamp": row["timestamp"],
            "importance": row["importance"],
            "method": method,
            **extra,
        }

    def _upsert_entity(self, ent: Dict) -> None:
        """Insert or update entity, preserving valid_until=NULL latest.

        If entity with id=ent['id'] exists and is active (valid_until IS NULL),
        update its name/summary/aliases/properties via COALESCE (None = keep old).
        Otherwise INSERT new entity with importance clamped to [0.0, 1.0].

        Args:
            ent: dict with keys:
                - id (str, required): entity id (e.g. 'sh600089', 'identity:')
                - kind (str, required): 'stock'/'concept'/'identity_fact'/etc
                - name, summary (str, optional): human-readable
                - aliases (list, optional): known aliases for entity_resolve
                - properties (dict, optional): free-form metadata
                - source (str, optional): defaults to 'manual'
                - importance (float, optional): defaults to 0.5, clamped
        """
        # [7/19 P1-1 + P1-2 + P1-5] entity 整体清洗 (id 验证 + name/summary/kind 剥离控制 + bidi)
        ent = validate_entity_payload(ent)
        existing = self._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL", (ent["id"],)
        ).fetchone()
        if existing:
            # [7/19 P1-2] identity_fact 类实体拒绝覆盖 name/aliases/properties (防伪造主人身份)
            # 只能新增 (valid_until 旧版 + 新版)
            existing_kind = self._conn.execute(
                "SELECT kind FROM entities WHERE id = ? AND valid_until IS NULL", (ent["id"],)
            ).fetchone()
            if existing_kind and existing_kind["kind"] == "identity_fact":
                raise ValidationError(
                    "entity.identity_fact", "identity_fact entities are immutable; create a new version instead"
                )
            # 更新 fields
            self._conn.execute(
                """
                UPDATE entities
                SET name = COALESCE(?, name),
                    summary = COALESCE(?, summary),
                    aliases_json = COALESCE(?, aliases_json),
                    properties_json = COALESCE(?, properties_json)
                WHERE id = ? AND valid_until IS NULL
            """,
                (
                    ent.get("name"),
                    ent.get("summary"),
                    json.dumps(ent.get("aliases", []), ensure_ascii=False) if "aliases" in ent else None,
                    json.dumps(ent.get("properties", {}), ensure_ascii=False) if "properties" in ent else None,
                    ent["id"],
                ),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO entities (id, kind, name, summary, aliases_json, properties_json,
                                      source, importance, valid_from, valid_until)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
                (
                    ent["id"],
                    ent["kind"],
                    ent.get("name"),
                    ent.get("summary"),
                    json.dumps(ent.get("aliases", []), ensure_ascii=False),
                    json.dumps(ent.get("properties", {}), ensure_ascii=False),
                    ent.get("source", "manual"),
                    clamp01(ent.get("importance", 0.5), "entities[].importance"),
                    now(),
                ),
            )

    # === 统计 ====================

    # [7/19 P2-4] 显式白名单, 防止以后误把 user input 传进来 → SQL injection
    _ALLOWED_TABLES = frozenset({"entities", "chunks", "relations"})

    def stats(self) -> Dict:
        """统计."""
        stats = {}
        for t in self._ALLOWED_TABLES:  # 永远是 3 个白名单字符串
            total = self._conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            active = self._conn.execute(f"SELECT count(*) FROM {t} WHERE valid_until IS NULL").fetchone()[0]
            stats[t] = {"total": total, "active": active, "deleted": total - active}
        stats["vectors"] = self._conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        stats["recall_log"] = self._conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]
        return stats


# === 自测 ===
if __name__ == "__main__":
    with Memory() as m:
        # 1. remember
        cid = m.remember(
            content="测试: sh600089 建仓 12000 @ 18.96",
            source="master:0029",
            importance=0.9,
            entities=[
                {
                    "id": "sh600089",
                    "kind": "stock",
                    "name": "特变电工",
                    "aliases": ["特变电工", "TBEA"],
                    "properties": {"ticker": "sh600089", "sector": "公用事业"},
                },
                {"id": "master_2077_ling", "kind": "person", "name": "主人 2077"},
            ],
            relations=[
                {
                    "source_id": "master_2077_ling",
                    "target_id": "sh600089",
                    "relation": "_建仓_于",
                    "weight": 1.0,
                    "properties": {"quantity": 12000, "price": 18.96, "amount": 227520},
                },
            ],
        )
        print(f"✅ remember → chunk_id: {cid}")

        # 2. relate
        rid = m.relate("master_2077_ling", "sh600089", "_关注", weight=0.7, evidence_chunk_id=cid)
        print(f"✅ relate → relation_id: {rid}")

        # 3. recall
        results = m.recall("sh600089 特变电工", top_k=3)
        print(f"✅ recall → {len(results)} hits")
        for r in results:
            print(f"  - {r['method']} | score={r.get('rrf_score', r.get('distance', '?')):.3f} | {r['content'][:60]}")

        # 4. graph_query
        graph = m.graph_query("sh600089", max_hops=2)
        print(f"✅ graph_query → {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        # 5. stats
        stats = m.stats()
        print(f"✅ stats: {stats}")

        # 6. update
        new_cid = m.update(cid, reason="修正", new_content="测试修正: sh600089 实际 7,800")
        print(f"✅ update → new chunk_id: {new_cid}")

        # 7. forget
        f = m.forget(rid, target_kind="relation", reason="outdated")
        print(f"✅ forget → {f}")

        # 8. recall again
        results = m.recall("sh600089", top_k=3)
        print(f"✅ recall after updates → {len(results)} hits")
        for r in results:
            print(f"  - {r['method']} | {r['content'][:60]}")
