#!/usr/bin/env python3
"""digest_mixin.py — DigestMixin: 4 digest methods split from memory.py.

[refactor 2026-08-12] Memory 类 3853 行拆分 (PR #11 benchmarks 子包同样先例).
DigestMixin: _mark_digest_dirty / _rebuild_digest / get_digest / _build_digest.

Design intent:
- Digest 是独立的 L2 自维护子系统 (DESIGN §5.2 P4 衍生), 独立 mixin.
- 跨 mixin 依赖 (`self._exec_clean`, `self._conn`) 运行时通过 MRO 解析.

依赖 (runtime via self.xxx):
- self._conn (MemoryCore 提供)
- self._exec_clean (AuditMixin 提供, 跨 mixin)
- self._build_digest / self._rebuild_digest (本类内)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import config

if TYPE_CHECKING:
    pass  # forward refs only — avoid circular import at runtime

logger = logging.getLogger("mnelo")


class DigestMixin:
    """[refactor 2026-08-12] Digest 子系统 mixin.

    Methods (4):
        _mark_digest_dirty:  写后设 dirty 标志 (remember/update 调用)
        _rebuild_digest:     重建 digest content
        get_digest:          MCP 工具入口
        _build_digest:       实际构建逻辑
    """

    def _mark_digest_dirty(self) -> None:
        """[G3 8/4] TASKS_L2_DIGEST §3.3 — dirty 追踪, set meta.digest_dirty=1."""
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('digest_dirty', '1')")
        self._conn.commit()

    def _rebuild_digest(self) -> Optional[str]:
        """[G4 8/4] TASKS_L2_DIGEST §3.4 — digest chunk 生命周期 + 双时态."""
        cfg = config.config
        if not cfg.digest_enabled:
            return None
        text, line_refs, truncated = self._build_digest()
        if not text:
            return None
        from memory import generate_id, now  # lazy import — avoid circular at module load

        ts = now()
        new_id = generate_id("chunk") + "_digest"
        metadata = json.dumps(
            {
                "digest": True,
                "line_refs": line_refs,
                "truncated": truncated,
                "built_at": ts,
            },
            ensure_ascii=False,
        )
        try:
            old_row = self._conn.execute("SELECT value FROM meta WHERE key='digest_chunk_id'").fetchone()
            old_id = old_row["value"] if old_row else None
            self._exec_clean(
                """INSERT INTO chunks
                       (id, content, source, session_id, timestamp, importance,
                        memory_type, metadata_json, valid_until)
                   VALUES (?, ?, 'digest', NULL, ?, 1.0, 'fact', ?, NULL)""",
                (new_id, text, ts, metadata),
            )
            # [2026-08-29 fix] digest path bypasses memory_remember, so self._index.add
            # never gets called → digest chunks accumulate as orphan chunks with no vectors.
            # Fix: embed + add to search index immediately after the INSERT. Same call
            # memory_remember would make (memory_core.py:572). Keep digest in sync with
            # the vector lane so recall actually surfaces recent digest content.
            try:
                from embedder import embed_bytes

                v_bytes = embed_bytes(text)
                self._index.add(
                    new_id,
                    v_bytes,
                    conn=self._conn,
                    content=text,
                    memory_type="fact",
                    source="digest",
                )
            except Exception as idx_exc:
                # Don't fail digest on index error — the chunk is already in DB.
                # Surface as warning so /tmp/yanru-reply-style triage can spot drift.
                logger.warning(f"[rebuild_digest] vector index add failed for {new_id}: {idx_exc}")
            if old_id:
                # [2026-08-29 fix] Mirror the same fix for the OLD digest: mark
                # superseded + drop its vector so chunk.active stays in sync with vectors.
                try:
                    self._index.remove(old_id, conn=self._conn)
                except Exception as rm_exc:
                    logger.warning(f"[rebuild_digest] vector index remove failed for {old_id}: {rm_exc}")
            if old_id:
                cur_meta = self._exec_clean("SELECT metadata_json FROM chunks WHERE id = ?", (old_id,)).fetchone()
                m: Dict[str, Any] = {}
                if cur_meta and cur_meta["metadata_json"]:
                    try:
                        m = json.loads(cur_meta["metadata_json"])
                    except Exception:
                        m = {}
                m["superseded_by"] = new_id
                m["superseded_at"] = ts
                self._exec_clean(
                    "UPDATE chunks SET metadata_json = ?, valid_until = ? WHERE id = ?",
                    (json.dumps(m, ensure_ascii=False), ts, old_id),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('digest_chunk_id', ?)",
                (new_id,),
            )
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('digest_dirty', '0')")
            self._conn.commit()
            return new_id
        except Exception as e:
            logger.warning(f"[rebuild_digest] 实际 错误: {e}")
            return None

    def get_digest(self, ref: Optional[str] = None) -> Dict[str, Any]:
        """[G5 8/4] TASKS_L2_DIGEST §3.5 — 双模式."""
        cfg = config.config
        if not cfg.digest_enabled:
            return {"enabled": False, "content": "", "line_refs": {}, "truncated": False, "built_at": None}
        meta_dirty = self._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()
        is_dirty = meta_dirty and meta_dirty["value"] == "1"
        chunk_id_row = self._conn.execute("SELECT value FROM meta WHERE key='digest_chunk_id'").fetchone()
        chunk_id = chunk_id_row["value"] if chunk_id_row else None
        if is_dirty or not chunk_id:
            chunk_id = self._rebuild_digest()
        if not chunk_id:
            return {"enabled": True, "content": "", "line_refs": {}, "truncated": False, "built_at": None}
        row = self._exec_clean("SELECT content, metadata_json FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return {"enabled": True, "content": "", "line_refs": {}, "truncated": False, "built_at": None}
        meta_obj: Dict[str, Any] = {}
        if row["metadata_json"]:
            try:
                meta_obj = json.loads(row["metadata_json"])
            except Exception:
                meta_obj = {}
        line_refs = meta_obj.get("line_refs", {})
        truncated = meta_obj.get("truncated", False)
        built_at = meta_obj.get("built_at")
        if ref is None:
            return {
                "enabled": True,
                "content": row["content"],
                "chunk_id": chunk_id,
                "line_refs": line_refs,
                "truncated": truncated,
                "built_at": built_at,
            }
        ref_ids = line_refs.get(str(ref))
        if not ref_ids:
            return {"error": f"ref {ref} not found", "chunk_id": chunk_id}
        source_chunks = []
        for rid in ref_ids:
            entity_row = self._exec_clean(
                "SELECT id, name, summary, kind, importance FROM entities WHERE id = ?",
                (rid,),
            ).fetchone()
            if entity_row:
                source_chunks.append(
                    {
                        "type": "entity",
                        "id": entity_row["id"],
                        "name": entity_row["name"],
                        "summary": entity_row["summary"],
                        "importance": entity_row["importance"],
                    }
                )
            else:
                ck_row = self._exec_clean(
                    "SELECT id, content, memory_type, importance, timestamp FROM chunks WHERE id = ?",
                    (rid,),
                ).fetchone()
                if ck_row:
                    source_chunks.append(
                        {
                            "type": "chunk",
                            "id": ck_row["id"],
                            "content": ck_row["content"],
                            "memory_type": ck_row["memory_type"],
                            "importance": ck_row["importance"],
                            "timestamp": ck_row["timestamp"],
                        }
                    )
        return {
            "enabled": True,
            "ref": ref,
            "chunk_id": chunk_id,
            "source_chunks": source_chunks,
        }

    def _build_digest(self) -> Tuple[str, Dict[str, List[str]], bool]:
        """[G2 8/4] TASKS_L2_DIGEST §3.2 — 三块 + line_refs.

        纯规则, 无 LLM (§0 v0.2 拍板: deterministic).
        Returns:
            (text, line_refs, truncated)
        """
        cfg = config.config
        max_chars = cfg.digest_max_chars
        recent_window_days = cfg.digest_recent_window_days
        importance_threshold = cfg.digest_importance_threshold

        block1_lines = []
        block1_refs = {}
        n = 0
        try:
            for f in self._exec_clean(
                """SELECT id, name, summary, importance FROM entities
                   WHERE kind='identity_fact' AND valid_until IS NULL
                   ORDER BY importance DESC LIMIT 50"""
            ).fetchall():
                n += 1
                val = f["summary"] or f["name"]
                block1_lines.append(f"身份: {val}")
                block1_refs[str(n)] = [f["id"]]
        except Exception as e:
            logger.debug(f"[build_digest] identity 块 1 实际 错误: {e}")

        block2_lines = []
        block2_chunk_ids = []
        cutoff = (datetime.now() - timedelta(days=recent_window_days)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            for c in self._exec_clean(
                """SELECT id, content, memory_type FROM chunks
                   WHERE valid_until IS NULL
                     AND memory_type IN ('decision', 'episode')
                     AND importance >= ?
                     AND timestamp >= ?
                   ORDER BY importance DESC LIMIT 20""",
                (importance_threshold, cutoff),
            ).fetchall():
                head = (c["content"] or "").split("\n")[0][:50]
                block2_lines.append(f"{c['memory_type']}: {head}")
                block2_chunk_ids.append(c["id"])
        except Exception as e:
            logger.debug(f"[build_digest] chunks 块 2 实际 错误: {e}")

        block3_lines = []
        block3_chunk_ids = []
        try:
            for s in self._exec_clean(
                """SELECT id, content FROM chunks
                   WHERE valid_until IS NULL AND source != 'digest'
                   ORDER BY timestamp DESC LIMIT 5"""
            ).fetchall():
                head = (s["content"] or "").split("\n")[0][:50]
                block3_lines.append(f"近期: {head}")
                block3_chunk_ids.append(s["id"])
        except Exception as e:
            logger.debug(f"[build_digest] chunks 块 3 实际 错误: {e}")

        # [8/6 M4 digest 集成 §4.4] block4 — 未闭环 task + dormant loop
        # 用 task_states.list_active_tasks_and_loops + render_digest_block4.
        from task_states import list_active_tasks_and_loops as _ts_list_active
        from task_states import render_digest_block4 as _ts_render_b4

        try:
            active_block = _ts_list_active(self._conn, now=None, stale_days_threshold=7, limit=50)
            block4_lines, block4_refs = _ts_render_b4(active_block)
        except Exception as e:
            logger.debug(f"[build_digest] block4 (active loops) 实际 错误: {e}")
            block4_lines = []
            block4_refs = {}

        all_lines = block1_lines + block2_lines + block3_lines + block4_lines
        full_text = "\n".join(all_lines)
        truncated = len(full_text) > max_chars
        if truncated:
            full_text = full_text[:max_chars]

        all_refs: Dict[str, List[str]] = dict(block1_refs)
        cur_n = len(block1_lines)
        for cid in block2_chunk_ids:
            cur_n += 1
            all_refs[str(cur_n)] = [cid]
        for cid in block3_chunk_ids:
            cur_n += 1
            all_refs[str(cur_n)] = [cid]
        # block4 refs 拼接
        for _k, v in block4_refs.items():
            cur_n += 1
            all_refs[str(cur_n)] = v

        return full_text, all_refs, truncated
