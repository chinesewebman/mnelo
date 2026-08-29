"""
RecallEngine mixin — extracted from MemoryCore (god-class split).

11 methods moved verbatim via AST-based extractor.
Compose via: class Memory(MemoryCore, RecallEngine, ...):
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Dict, List

from embedder import embed_bytes
from metrics import get_registry as _metrics_registry

# === Caller-specified imports (verified needed) ===
from validation import validate_query

# [_MISSING sentinel] defined in memory_core.py AND memory.py for filter-scope
# 'value not provided' marker. Recall methods reference it as a bare name.
_MISSING = object()

# === STILL-MISSING names (caller decision required) ===
# - ENTITY_BOOST
# - ThreadPoolExecutor
# - _PLACEHOLDER_QUERIES
# - _cfg
# - _ent_scope_active_1
# - _ent_scope_active_2
# - _ent_scope_filters_1
# - _ent_scope_filters_2
# - _entity_scope_active
# - _entity_scope_filters
# - _now_ts
# - _reg
# - _scope_active
# - _scope_filters
# - _t
# - a
# - agent_id_filter
# - agent_id_filter_norm
# - aliases
# - all
# - any
# - asof
# - boost
# - boost_kinds
# - c
# - c_meta
# - ch
# - chunk
# - chunk_hits
# - chunk_ids
# - chunk_results
# - chunk_rows
# - chunks_by_id
# - cid
# - clean
# - conn
# - content
# - cur_params
# - deduped
# - detail
# - e_rows
# - entity_chunks
# - entity_hits
# - er
# - ex
# - extra
# - f_entity
# - f_graph
# - f_meta
# - f_vec
# - fetch_limit
# - filtered_rows
# - filters
# - fts_filter_clauses
# - fts_filter_params
# - fts_params
# - fts_sql
# - fts_where
# - graph_hits
# - graph_hops
# - graph_ms
# - h
# - high_priority_kinds
# - hit
# - hit_lists
# - hits
# - hops
# - i
# - identity_query_keys
# - ids
# - intent
# - is_identity_query
# - k
# - kept
# - kept_rows
# - kind
# - kind_filter
# - knn_hits
# - lane
# - lane_latencies
# - lane_ms
# - latency_ms
# - like
# - like_clauses
# - like_filter_clauses
# - like_filter_params
# - like_params
# - like_sql
# - like_where
# - m
# - meta_hits
# - meta_obj
# - method
# - n
# - new_chunks
# - out
# - pair
# - params
# - parsed
# - placeholders
# - placeholders_e
# - q_bytes
# - query
# - r
# - rank
# - rank_score
# - ranked
# - raw
# - recall_conns
# - results
# - row
# - rows
# - rrf_hits
# - rrf_methods
# - rrf_score
# - run_id_filter
# - run_id_filter_norm
# - score
# - seed_entities
# - seed_hits
# - seed_ids
# - seen
# - seen_chunk_ids
# - seen_ids
# - seg
# - sql
# - strategy
# - t
# - t0
# - t0_start
# - t_graph_0
# - t_vec_0
# - tok
# - tokens
# - top_k
# - union_params
# - union_sql
# - user_id_filter
# - user_id_filter_norm
# - v
# - vec_ms
# - vector_hits
# - w
# - x


class RecallEngine:
    """Mixin providing extracted methods.

    Methods expect self to provide the same attrs as MemoryCore
    (see memory_core.py: _conn, _index, db_path, etc.).
    """

    # === extracted: recall (was L1315-1478) ===
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
        from memory import _load_vec0_module, now  # lazy import — avoid circular at module load

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
                # [8/10 fix] 每个 worker conn 走 _load_vec0_module(), CI 上 enable_load_extension
                # 被 strip 时自动 fallback 到 ctypes (init 阶段已注册 auto-extension, 通常走它).
                _load_vec0_module(c, context="recall-worker")
                c.row_factory = sqlite3.Row

            # [7/19 v0.5.3] Per-lane timing for metrics (vector first, parallel meta/entity/graph)
            # [bug fix D2 2026-08-16] Use try/finally to close recall_conns even if
            # any f.result() raises. Pre-fix: close loop was AFTER the futures
            # joined — worker exception skipped close, leaking 4 SQLite connections
            # + vec0 module registrations per failed recall. Sustained load →
            # fd exhaustion.
            try:
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
            finally:
                # 关独立连接 — finally ensures close even on worker exception
                for c in recall_conns:
                    try:
                        c.close()
                    except Exception:
                        pass  # defensive — best-effort close on shutdown

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

    # === extracted: _vector_recall (was L1480-1483) ===
    def _vector_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 1: 向量检索 (SearchIndex 适配器, DESIGN §3.6)."""

        return self._vector_recall_with_conn(self._conn, query, top_k, filters, asof)

    # === extracted: _vector_recall_with_conn (was L1485-1563) ===
    def _vector_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] vector recall — 索引 KNN 走 SearchIndex 适配器.

        [P0 2026-08-11] scoping IDs: 当 filters 含 agent_id, KNN 召回的
        chunk 必须在 metadata_json 里有相同 agent_id (json_extract NULL
        不匹配 → 旧数据无 agent_id 的 chunk 自动保留, 不误过滤).

        Args:
            conn: 独立 sqlite3 connection (每路独立; 用于 chunk 侧查询)
        """
        from memory import norm_memory_type  # lazy import — avoid circular at module load

        q_bytes = embed_bytes(query)
        # [审计 4.3 ] filter 多时, 多取一些确保过滤后还够 top_k; strategy 也加大召回
        fetch_limit = top_k * (8 if (filters or top_k >= 3) else 2)
        knn_hits = self._index.knn(q_bytes, fetch_limit, conn=conn)
        if not knn_hits:
            return []

        # [P0 2026-08-11] scoping IDs: 一次 SQL 把 chunk 元数据 + agent_id 拿回来.
        # 在 Python 侧过滤 agent_id (避免每行一次 json_extract SQL).
        # [audit fix #7 2026-08-16] user_id / run_id filter recall 也走 Python 侧
        # post-filter (跟 agent_id 同款 — json_extract NULL 兼容旧数据).
        agent_id_filter = (filters or {}).get("agent_id")
        agent_id_filter_norm = agent_id_filter if agent_id_filter is not None else _MISSING
        user_id_filter = (filters or {}).get("user_id")
        user_id_filter_norm = user_id_filter if user_id_filter is not None else _MISSING
        run_id_filter = (filters or {}).get("run_id")
        run_id_filter_norm = run_id_filter if run_id_filter is not None else _MISSING
        _scope_filters = (
            ("agent_id", agent_id_filter_norm),
            ("user_id", user_id_filter_norm),
            ("run_id", run_id_filter_norm),
        )
        _scope_active = [pair for pair in _scope_filters if pair[1] is not _MISSING]

        # [audit fix 4.2 2026-08-16] batch fetch — 1 次 SQL 拿全 knn_hits 的 chunk
        # 原 N+1: 每 knn_hit 1 次 SELECT (top_k=10 → 10 round-trip).
        # 现在: 1 次 IN(...) SELECT 拿全 batch.
        chunk_ids = [h.chunk_id for h in knn_hits]
        placeholders = ",".join("?" * len(chunk_ids))
        chunk_rows = conn.execute(
            f"""
            SELECT id, content, memory_type, source, timestamp, importance, metadata_json FROM chunks
            WHERE id IN ({placeholders}) AND (valid_until IS NULL OR valid_until > ?)
            """,
            (*chunk_ids, asof),
        ).fetchall()
        # Build dict by chunk_id for O(1) lookup
        chunks_by_id = {r["id"]: r for r in chunk_rows}

        results = []
        for hit in knn_hits:
            # [7/21 fix] asof: chunk 在 asof 时点有效 = valid_until IS NULL OR > asof
            # [P0 2026-08-11] 同时拿 metadata_json, Python 侧 json 解析 agent_id
            chunk = chunks_by_id.get(hit.chunk_id)
            if not chunk:
                continue
            if filters:
                if "source" in filters and chunk["source"] != filters["source"]:
                    continue
                if "type" in filters and chunk["memory_type"] != norm_memory_type(filters["type"]):
                    continue
                # [P0 2026-08-11] agent_id filter — 旧数据 metadata_json=NULL
                # 或不含 agent_id → JSON 解出 None → 不等于 filter, 保留.
                # [audit fix #7 2026-08-16] 同款 pattern 扩 user_id / run_id.
                if _scope_active:
                    raw = chunk["metadata_json"]
                    if raw is None or raw == "":
                        continue
                    try:
                        meta_obj = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    # 所有 active scope filter 必须 match (any mismatch → skip)
                    if any(meta_obj.get(k) != v for k, v in _scope_active):
                        continue
            results.append(self._hit_dict(chunk, method="vector", distance=float(hit.distance)))
        return results[:top_k]

    # === extracted: _meta_recall_with_conn (was L1565-1628) ===
    def _meta_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 meta recall.

        [P0 2026-08-11] scoping IDs: 当 filters 含 agent_id, SQL 走
        json_extract(metadata_json, '$.agent_id') = ? 过滤. NULL metadata_json
        或缺 agent_id → json_extract 返回 NULL → != filter → 自动保留
        (旧数据不误过滤).

        [P2 2026-08-11] temporal reasoning: detect_query_intent(query) 决定
        SQL 加成 (跟 P0 scoping 共存, 加 AS 额外约束, 不替换).
          - current_state: AND valid_until IS NULL (强制当前态)
          - upcoming:      AND timestamp > ? (未来事件, 用 now() 作 asof 基准)
          - historical:    不排斥 valid_until (supersede 历史浮出, 默认 ASof 仍过滤 < now)
          - soft_recency:  默认行为 (不变)
        """
        from memory import detect_query_intent, norm_memory_type, now  # lazy import — avoid circular at module load

        # [7/21 fix] asof: 只看 asof 时点仍有效的 chunk
        # [P0 2026-08-11] scoping: agent_id 走 json_extract SQL 过滤 (NULL 不误过滤)
        # [bug fix P1 2026-08-29] ESCAPE '\\' 让 _escape_like() 的 % 和 _ 转义生效
        sql = """
            SELECT id, content, memory_type, source, timestamp, importance FROM chunks
            WHERE (valid_until IS NULL OR valid_until > ?)
              AND content LIKE ? ESCAPE '\\'
        """
        from memory import _escape_like  # [bug fix P1 2026-08-29] LIKE wildcard escape

        params = [asof, f"%{_escape_like(query)}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        if filters and "type" in filters:
            sql += " AND memory_type = ?"
            params.append(norm_memory_type(filters["type"]))
        if filters and "agent_id" in filters:
            # [P0 2026-08-11] json_extract 路径: '$.agent_id'
            # NULL metadata_json 或缺键 → json_extract 返回 NULL → 不 = filter.
            # 这天然保证旧数据兼容.
            sql += " AND json_extract(metadata_json, '$.agent_id') = ?"
            params.append(filters["agent_id"])
        # [audit fix #7 2026-08-16] user_id / run_id 同款 json_extract SQL filter
        if filters and "user_id" in filters:
            sql += " AND json_extract(metadata_json, '$.user_id') = ?"
            params.append(filters["user_id"])
        if filters and "run_id" in filters:
            sql += " AND json_extract(metadata_json, '$.run_id') = ?"
            params.append(filters["run_id"])
        # [P2 2026-08-11] temporal intent 加成 — 用 detect_query_intent
        # 注意: recall() 入口处已 validate_query(), 这里 query 非空.
        intent = detect_query_intent(query)
        # [P2 2026-08-11] upcoming 用 asof 作基准. 当 caller 传 asof=None, 跟 SQL
        # 主条件一致归一为 now() (避免 NULL 比较失败).
        if intent == "upcoming":
            _now_ts = asof if asof else now()
            sql += " AND timestamp > ?"
            params.append(_now_ts)
        elif intent == "current_state":
            # 强制当前态: valid_until 必须 IS NULL (排除已 supersede)
            sql += " AND valid_until IS NULL"
        # historical / soft_recency: 不加约束 (历史浮出, 默认行为)
        sql += " ORDER BY importance DESC, timestamp DESC LIMIT ?"
        params.append(top_k)
        rows = conn.execute(sql, params).fetchall()
        return [self._hit_dict(r, method="meta") for r in rows]

    # === extracted: _entity_recall_with_conn (was L1630-1734) ===
    def _entity_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 entity recall.

        [P0 2026-08-11] scoping IDs: entity_recall 默认走 entities 表 token
        LIKE (强身份事实); 加 agent_id filter 后, 关联 chunk 必须
        metadata_json.agent_id = filter (json_extract). entity → chunk 关联
        在 relations 表 (自引用 evidence relation: src=entity_id, tgt=entity_id,
        evidence_chunk_id=chunk_id, 见 3027 行). LEFT JOIN 让老 entity (无
        evidence relation) 保留 — c_meta NULL → 旧数据兼容.
        """
        from memory import _escape_like, norm_memory_type, now  # [bug fix P1 2026-08-29] _escape_like

        if " " in query.strip():
            tokens = query.strip().split()
        else:
            tokens = [query]

        chunk_results = []
        seen_chunk_ids = set()
        for tok in tokens:
            if not tok or len(tok) < 2:
                continue
            # [bug fix P1 2026-08-29] escape LIKE wildcards (% _) so user's literal
            # % and _ aren't interpreted as wildcards. ESCAPE '\\' must be in SQL.
            like = f"%{_escape_like(tok)}%"
            # [7/21 fix] asof: entity 在 asof 时点有效 = valid_from <= asof AND (valid_until IS NULL OR > asof)
            # [P0 2026-08-11] LEFT JOIN relations (self-ref) → chunks 拿 metadata_json.
            sql = """
                SELECT e.id, e.name, e.kind, e.summary, e.importance, e.aliases_json, c.metadata_json AS c_meta
                FROM entities e
                LEFT JOIN relations r ON r.source_id = e.id AND r.target_id = e.id
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE (e.valid_from IS NULL OR e.valid_from <= ?)
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                  AND (e.name LIKE ? ESCAPE '\\' OR e.aliases_json LIKE ? ESCAPE '\\')
            """
            params = [asof, asof, like, like]
            if filters and "type" in filters:
                sql += " AND e.memory_type = ?"
                params.append(norm_memory_type(filters["type"]))
            if filters and "agent_id" in filters:
                # [P0 2026-08-11] SQL 没法直接 json_extract (chunk 可能不存在);
                # Python 侧 post-filter, NULL metadata_json 保留 (旧数据兼容).
                pass  # 见下面 post-filter 循环
            sql += " ORDER BY e.importance DESC LIMIT ?"
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
            # [P0 2026-08-11] agent_id post-filter (SQL LEFT JOIN 后 Python 侧 filter)
            # [audit fix #7 2026-08-16] user_id / run_id 同款 post-filter
            _entity_scope_filters = (
                ("agent_id", (filters or {}).get("agent_id")),
                ("user_id", (filters or {}).get("user_id")),
                ("run_id", (filters or {}).get("run_id")),
            )
            _entity_scope_active = [(k, v) for k, v in _entity_scope_filters if v is not None]
            if _entity_scope_active:
                kept = []
                for r in rows:
                    c_meta = r["c_meta"]
                    if c_meta is None or c_meta == "":
                        # 无关联 chunk / 空 metadata_json → 保留 (旧数据兼容)
                        kept.append(r)
                        continue
                    try:
                        parsed = json.loads(c_meta)
                    except (json.JSONDecodeError, TypeError):
                        kept.append(r)  # 解析失败保留 (defensive)
                        continue
                    if all(parsed.get(k) == v for k, v in _entity_scope_active):
                        kept.append(r)
                rows = kept
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
        return chunk_results[:top_k]

    # === extracted: _graph_recall (was L1736-1825) ===
    def _graph_recall(self, seed_hits: List[Dict], hops: int, asof: str) -> List[Dict]:
        """路 2: 图遍历 (NetworkX 内存层 + hops 跳)."""
        from memory import now  # lazy import — avoid circular at module load

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

    # === extracted: _meta_recall (was L1827-1959) ===
    def _meta_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 3: 元数据 (精确 LIKE + 时间近).

        [P0 2026-08-11] scoping IDs: 与 _meta_recall_with_conn 同语义.
        agent_id 走 json_extract SQL 过滤; NULL metadata_json / 缺 agent_id
        保留 (旧数据兼容). 这是 meta_only 策略走的 sequential fallback,
        必须跟并行 _with_conn 行为一致 — 漏一路即失败 (P0 验收).

        [P2 2026-08-11] temporal reasoning: 与 _meta_recall_with_conn 同语义.
        current_state / upcoming 加 SQL 约束; historical / soft_recency 默认.
        """
        from memory import _escape_like, detect_query_intent, norm_memory_type, now  # [bug fix P1 2026-08-29] _escape_like

        # [7/21 fix] asof: 只看 asof 时点仍有效的 chunk
        # [P0 2026-08-11] scoping: agent_id 走 json_extract SQL 过滤
        # [bug fix P1 2026-08-29] ESCAPE '\\' 让 _escape_like() 的 % 和 _ 转义生效
        sql = """
            SELECT id, content, memory_type, source, timestamp, importance FROM chunks
            WHERE (valid_until IS NULL OR valid_until > ?)
              AND content LIKE ? ESCAPE '\\'
        """
        params = [asof, f"%{_escape_like(query)}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        if filters and "type" in filters:
            sql += " AND memory_type = ?"
            params.append(norm_memory_type(filters["type"]))
        if filters and "agent_id" in filters:
            sql += " AND json_extract(metadata_json, '$.agent_id') = ?"
            params.append(filters["agent_id"])
        # [8/16 P1 #91 fix] user_id filter 也在 metadata_json · 跟 agent_id 平行
        if filters and "user_id" in filters:
            sql += " AND json_extract(metadata_json, '$.user_id') = ?"
            params.append(filters["user_id"])
        # [8/16 audit-2 #7 fix] run_id filter 同 user_id 模式
        if filters and "run_id" in filters:
            sql += " AND json_extract(metadata_json, '$.run_id') = ?"
            params.append(filters["run_id"])
        # [8/16 E-2 重启 non-trigger] FTS5 BM25 主路 + LIKE fallback.
        # 设计哲学 (上轮 P1 #67/#68/#69/#70 实战教諛· 避免 P1 #81 SIGSEGV):
        # - BM25 路: 完整中文句 · bm25 ASC 优先·importance DESC 加权·LIMIT 外移
        # - LIKE 路: 2-char 中文短查 · 符号 token · 起這云 fallback
        # - UNION ALL 后包 subquery (避免 P1 #69 ORDER BY/LIMIT 报错)· 去重走 Python set
        from memory import _fts_escape_query  # [P1 #63] lazy · 避免 circular

        # 状态变量：source / type / agent_id / user_id / run_id filter · temporal intent · valid_until
        fts_filter_clauses = []
        fts_filter_params = []
        like_filter_clauses = []
        like_filter_params = []
        if filters and "source" in filters:
            fts_filter_clauses.append("c.source = ?")
            fts_filter_params.append(filters["source"])
            like_filter_clauses.append("source = ?")
            like_filter_params.append(filters["source"])
        if filters and "type" in filters:
            _t = norm_memory_type(filters["type"])
            fts_filter_clauses.append("c.memory_type = ?")
            fts_filter_params.append(_t)
            like_filter_clauses.append("memory_type = ?")
            like_filter_params.append(_t)
        if filters and "agent_id" in filters:
            fts_filter_clauses.append("json_extract(c.metadata_json, '$.agent_id') = ?")
            fts_filter_params.append(filters["agent_id"])
            like_filter_clauses.append("json_extract(metadata_json, '$.agent_id') = ?")
            like_filter_params.append(filters["agent_id"])
        if filters and "user_id" in filters:  # [8/16 P1 #91 fix]
            fts_filter_clauses.append("json_extract(c.metadata_json, '$.user_id') = ?")
            fts_filter_params.append(filters["user_id"])
            like_filter_clauses.append("json_extract(metadata_json, '$.user_id') = ?")
            like_filter_params.append(filters["user_id"])
        if filters and "run_id" in filters:  # [8/16 audit-2 #7 fix]
            fts_filter_clauses.append("json_extract(c.metadata_json, '$.run_id') = ?")
            fts_filter_params.append(filters["run_id"])
            like_filter_clauses.append("json_extract(metadata_json, '$.run_id') = ?")
            like_filter_params.append(filters["run_id"])
        # [P2 2026-08-11] temporal intent 加成 — 跟 _meta_recall_with_conn 同源
        intent = detect_query_intent(query)
        if intent == "upcoming":
            _now_ts = asof if asof else now()
            fts_filter_clauses.append("c.timestamp > ?")
            fts_filter_params.append(_now_ts)
            like_filter_clauses.append("timestamp > ?")
            like_filter_params.append(_now_ts)
        elif intent == "current_state":
            fts_filter_clauses.append("c.valid_until IS NULL")
            like_filter_clauses.append("valid_until IS NULL")

        fts_where = " AND ".join(["(c.valid_until IS NULL OR c.valid_until > ?)", *fts_filter_clauses]) or "(c.valid_until IS NULL OR c.valid_until > ?)"
        like_where = " AND ".join(["(valid_until IS NULL OR valid_until > ?)", *like_filter_clauses]) or "(valid_until IS NULL OR valid_until > ?)"

        # FTS5 BM25 路：SELECT id, content, ..., bm25(chunks_fts) · LIMIT 外移
        fts_sql = (
            "SELECT c.id, c.content, c.memory_type, c.source, c.timestamp, c.importance, "
            "       bm25(chunks_fts) AS fts_score "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? AND " + fts_where
        )
        fts_params = [_fts_escape_query(query), asof, *fts_filter_params]
        # LIKE fallback 路：SELECT id, content, ..., 0.0 AS fts_score
        # [bug fix P1 2026-08-29] ESCAPE '\' 让 _escape_like() 的 % 和 _ 转义生效
        like_sql = "SELECT id, content, memory_type, source, timestamp, importance, 0.0 AS fts_score FROM chunks WHERE " + like_where + " AND content LIKE ? ESCAPE '\\'"
        like_params = [asof, *like_filter_params, f"%{_escape_like(query)}%"]

        # UNION ALL 包 subquery (P1 #67/#69 避免) · 去重 (P1 #70 params 数量 严格匹配)
        union_sql = f"SELECT * FROM ({fts_sql} UNION ALL {like_sql}) ORDER BY 6 ASC, 5 DESC LIMIT ?"
        # fts_sql 选 bm25 ASC (越低越相关) · ORDER BY 6 = fts_score · 5 = importance
        union_params = tuple(fts_params) + tuple(like_params) + (top_k * 2,)

        try:
            rows = self._conn.execute(union_sql, union_params).fetchall()
        except Exception:
            # FTS5 不存在或 LIKE 单路 fallback (跨 schema 兼容)
            # [bug fix P1 2026-08-29] ESCAPE '\' 让 _escape_like() 的 % 和 _ 转义生效
            rows = self._conn.execute(
                "SELECT id, content, memory_type, source, timestamp, importance "
                "FROM chunks WHERE (valid_until IS NULL OR valid_until > ?) "
                "AND content LIKE ? ESCAPE '\\' ORDER BY importance DESC, timestamp DESC LIMIT ?",
                (asof, f"%{_escape_like(query)}%", top_k),
            ).fetchall()

        # 去重：同 id 优先 FTS5 (BM25 更智能排序) · 实际 LIMIT top_k
        seen = set()
        deduped = []
        for r in rows:
            cid = r["id"]
            if cid in seen:
                continue
            seen.add(cid)
            deduped.append(r)
        rows = deduped[:top_k]
        return [self._hit_dict(r, method="meta") for r in rows]

    # === extracted: _entity_recall (was L1961-2148) ===
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
        from memory import _escape_like, norm_memory_type, now  # [bug fix P1 2026-08-29] _escape_like

        hits = []
        seen_ids = set()

        # === 第一阶段: 意图增强 (user identity 询问) ===
        identity_query_keys = ("我", "主人", "user", "ling2077", "2077 Ling")
        is_identity_query = any(k in query for k in identity_query_keys)
        if is_identity_query:
            # [7/21 fix] asof: 只取 asof 时点仍有效的 entity/relation
            # [P0 2026-08-11] scoping: LEFT JOIN chunks 拿 metadata_json.
            # entity → chunk 关联在 relations.evidence_chunk_id (3027 行
            # 创建, evidence 关系: src=entity_id, tgt=entity_id, evidence_chunk_id=chunk_id).
            # LEFT JOIN 让老 entity (无 evidence relation) 保留 — c.id NULL
            # → c.metadata_json NULL → json_extract NULL → 不匹配 filter → 保留.
            rows = self._conn.execute(
                """
                SELECT e.id, e.kind, e.name, e.summary, e.importance, c.metadata_json AS c_meta
                FROM relations r
                JOIN entities e ON e.id = r.target_id
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE r.source_id = 'user'
                  AND (r.valid_until IS NULL OR r.valid_until > ?)
                  AND e.kind IN ('identity_fact', 'canonical_fact')
            """,
                (asof, asof),
            ).fetchall()
            # [P0 2026-08-11] agent_id filter — SQL 已经 LEFT JOIN chunks,
            # 但 chunk 可能不存在 (老 entity); 改为 Python 侧 post-filter.
            # NULL metadata_json / 缺 agent_id 的 chunk 保留 (旧数据兼容).
            # [audit fix #7 2026-08-16] user_id / run_id 同款 post-filter
            _ent_scope_filters_1 = (
                ("agent_id", (filters or {}).get("agent_id")),
                ("user_id", (filters or {}).get("user_id")),
                ("run_id", (filters or {}).get("run_id")),
            )
            _ent_scope_active_1 = [(k, v) for k, v in _ent_scope_filters_1 if v is not None]
            if _ent_scope_active_1:
                kept_rows = []
                for r in rows:
                    c_meta = r["c_meta"]
                    if c_meta is None or c_meta == "":
                        # 无关联 chunk 或空 metadata_json → 保留 (旧数据兼容)
                        kept_rows.append(r)
                        continue
                    try:
                        parsed = json.loads(c_meta)
                    except (json.JSONDecodeError, TypeError):
                        kept_rows.append(r)  # 解析失败也保留 (defensive)
                        continue
                    if all(parsed.get(k) == v for k, v in _ent_scope_active_1):
                        kept_rows.append(r)
                rows = kept_rows
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
        # [audit fix #7 2026-08-16] rows 在 phase 1 (identity_query) 内才定义,
        # phase 2 独立跑 (is_identity_query=False) 时必须初始化否则 UnboundLocalError.
        if not is_identity_query:
            rows = []
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
            # [P0 2026-08-11] 限定 e.id / e.name / e.summary — JOIN 后 'id' 歧义.
            # [bug fix P1 2026-08-29] ESCAPE '\' 让 _escape_like() 的 % 和 _ 转义生效
            like_clauses.append("(e.name LIKE ? ESCAPE '\\' OR e.id LIKE ? ESCAPE '\\' OR e.summary LIKE ? ESCAPE '\\')")
            params.extend([f"%{_escape_like(t)}%"] * 3)

        # 两轮: 高优先级 (强 fact), 后补 concept
        high_priority_kinds = ("identity_fact", "canonical_fact", "user")

        for kind_filter, _take in (
            (high_priority_kinds, top_k),
            (("concept",), top_k),  # 补足
        ):
            # [7/21 fix] asof: (valid_from IS NULL OR valid_from <= ?) 兼容无 valid_from 的旧数据
            # [P0 2026-08-11] scoping: LEFT JOIN chunks via relations.evidence_chunk_id
            # (entity → chunk 关联在 relations 表). LEFT JOIN 让老 entity 保留 —
            # c_meta NULL → post-filter 保留 (旧数据兼容).
            # [8/29 PR-B fix] production bug: phase 2 sql + cur_params 构造后未 execute,
            # rows 保留 phase 1 的值 (is_identity_query=True 时是 identity_fact/canonical_fact/user
            # 结果, False 时是 []). 导致 kind='concept' 的 entity 永远不进 candidate set.
            # 跟 phase 1 pattern (line 1974) 对比可知漏了 execute + fetchall.
            sql = f"""
                SELECT e.id, e.kind, e.name, e.summary, e.importance, e.recall_count, c.metadata_json AS c_meta
                FROM entities e
                LEFT JOIN relations r ON r.source_id = e.id AND r.target_id = e.id
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE (e.valid_from IS NULL OR e.valid_from <= ?)
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                  AND e.kind IN ({",".join("?" * len(kind_filter))})
                  AND ({" OR ".join(like_clauses)})
            """
            cur_params = [asof, asof] + list(kind_filter) + params
            if filters and "type" in filters:
                sql += " AND e.memory_type = ?"
                cur_params.append(norm_memory_type(filters["type"]))
            rows = self._conn.execute(sql, cur_params).fetchall()
            # [P0 2026-08-11] agent_id filter — SQL 不直接 json_extract (entity
            # 可能没关联 chunk); 改 Python 侧 post-filter 同第一阶段.
            # [audit fix #7 2026-08-16] user_id / run_id 同款 post-filter
            _ent_scope_filters_2 = (
                ("agent_id", (filters or {}).get("agent_id")),
                ("user_id", (filters or {}).get("user_id")),
                ("run_id", (filters or {}).get("run_id")),
            )
            _ent_scope_active_2 = [(k, v) for k, v in _ent_scope_filters_2 if v is not None]
            if _ent_scope_active_2:
                filtered_rows = []
                for r in rows:
                    c_meta = r["c_meta"]
                    if c_meta is None or c_meta == "":
                        filtered_rows.append(r)  # 旧数据/无关联 chunk 保留
                        continue
                    try:
                        parsed = json.loads(c_meta)
                    except (json.JSONDecodeError, TypeError):
                        filtered_rows.append(r)
                        continue
                    if all(parsed.get(k) == v for k, v in _ent_scope_active_2):
                        filtered_rows.append(r)
                rows = filtered_rows
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

    # === extracted: _rrf_fuse (was L2150-2208) ===
    def _rrf_fuse(self, hit_lists: List[List[Dict]], top_k: int) -> List[Dict]:
        """Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank).

        [P2+ #4 7/18 patch] stock entity boost:
        : kind=stock 的 entity_hit (e.g. 'sh600089') 是关心的高价值答案,
        默认 RRF 把 chunk 当事实, 但 stock entity 关联 chunk 是结构的语义提升.
        BOOST = 0.05 / rank^0.5 —  trade-off: 不压倒既有排序, 但 stock always 浮顶.
        """

        from memory import _kind_from_source  # lazy import — avoid circular at module load

        rrf_score: Dict[str, float] = {}
        rrf_hits: Dict[str, Dict] = {}
        # [8/15 E-4] methods accumulator — 同 chunk_id 多路命中时, 必须 accumulate
        # 所有参与的 method. 之前 `rrf_hits[cid] = h` 直接覆盖导致 lane 后者
        # 覆盖前者 (e.g. entity 永远在 hit_lists 末尾, 覆盖 vector/graph/meta),
        # 污染 recall_log.recall_details_json.method 字段, 拖累 E-3
        # memory_recall_stats method 分布数据 (DESIGN §1.2 #4).
        rrf_methods: Dict[str, List[str]] = {}
        k = 60
        # [8/5 普适化] RRF 实体 boost — 可配置 kind 清单 (config [recall].boost_kinds / env
        # MNELO_MEMORY_RECALL_BOOST_KINDS)。默认 ['stock'] 兼容旧行为; 设自己领域的
        # kind (product/category/...) 或 [] 禁用。机制通用: 已知品类代码实体命中浮顶。
        from config import config as _cfg

        boost_kinds = getattr(_cfg, "recall_boost_kinds", ["stock"])
        ENTITY_BOOST = 0.05
        for hits in hit_lists:
            for rank, h in enumerate(hits):
                # [ 7/18] 主键区分实体 vs chunk — 用 chunk_id 字段统一
                # 实体 hit 的 chunk_id = 'entity:<entity_id>'
                # chunk hit 的 chunk_id = '<chunk_id>'
                # 同 ID 合并(实体 hit 和 chunk hit 可能是同一事实在不同层的表达)
                cid = h["chunk_id"]
                rank_score = 1.0 / (k + rank + 1)
                # [P2+ #4 泛化] 实体 boost — 从 entity_kind 或 source 前缀 'entity:<kind>' 推导 kind
                kind = h.get("entity_kind") or _kind_from_source(h.get("source", ""))
                if kind and kind in boost_kinds and h.get("method") == "entity":
                    # 0.05 / rank^0.5 boost — 浮顶但不压倒 RRF 排序
                    boost = ENTITY_BOOST / math.sqrt(rank + 1)
                    rank_score += boost
                rrf_score[cid] = rrf_score.get(cid, 0) + rank_score
                # [8/15 E-4] 首次见 → 直接 set, 后续 → accumulate (保持第一路 hit 数据)
                if cid not in rrf_hits:
                    rrf_hits[cid] = h
                # [8/15 E-4] accumulate methods (按遍历序, 去重)
                m = h.get("method")
                if m and m not in rrf_methods.get(cid, []):
                    rrf_methods.setdefault(cid, []).append(m)
        ranked = sorted(rrf_score.items(), key=lambda x: -x[1])
        out = []
        for cid, score in ranked[:top_k]:
            h = rrf_hits[cid]
            h["rrf_score"] = score
            # [8/15 E-4] 写入完整 methods 列表 (保留 backward-compat method 字段 = 第一路)
            h["methods"] = rrf_methods.get(cid, [h.get("method")] if h.get("method") else [])
            out.append(h)
        return out

    # === extracted: _log_recall (was L2210-2250) ===
    def _log_recall(self, query: str, results: List[Dict], hops: int, latency_ms: float):
        """[P2+ #3 7/18 patch] 写入 recall_log 审计 (always local time via now() helper).

         feedback loop 数据:
        - results_json 已存 [chunk_id] 数组 (前: 只知道命中哪些 chunk)
        - 新存 recall_details_json: top-K 完整 dict (method, distance/score, importance)
          让 daily_check / analytics 能分析 召回质量 (用什么路召回的, 距离分布)
        """
        from memory import now  # lazy import — avoid circular at module load

        # [8/15 E-4] feedback loop: 每条命中的 methods 列表 (新) + method 单字段 (backward-compat)
        # methods 列表含所有 RRF 命中的 lane (e.g. ["vector", "graph"]), 修复
        # DESIGN §1.2 #4 RRF lane 覆盖问题 — 之前 rrf_hits[cid] = h 覆盖,
        # recall_details_json.method 只记最后遍历 lane, 拖累 E-3 recall_stats.
        detail = [
            {
                "rank": i + 1,
                "chunk_id": r.get("chunk_id"),
                "method": r.get("method"),  # backward-compat: 第一路
                "methods": r.get("methods", [r.get("method")] if r.get("method") else []),
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

    # === extracted: _hit_dict (was L2325-2345) ===
    @staticmethod
    def _hit_dict(row, method: str, **extra) -> Dict:
        """4 路召回统一返回格式 (RRF 融合需要)。

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
