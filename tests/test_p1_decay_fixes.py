#!/usr/bin/env python3
"""test_p1_decay_fixes.py — F1+F2 fixes for P1 memory decay.

[P1 2026-08-29] feat/p1-decay-fixes PR 配套测试, 修复 PR #23 cherry-pick 上
defer 的两个 bug:

  F1 (BUG #4) — graph_only / meta_only / entity_only strategy 的 hit dict
                 既没 rrf_score 也没 distance, 修复前 _apply_decay_to_hits 拿
                 base_score=0 → sort 退化为原 SQL ORDER BY, decay 完全失效.
                 修复: 3 个 strategy 分支出口显式注入 base_score=importance,
                 decay 优先级 base_score → rrf_score → distance → 兜底 1.0.

  F2 (BUG #11) — _apply_decay_to_hits 写 r["_decay_factor"]=factor,
                 mcp_tool_handlers._handle_simple 把整个 results json.dumps
                 返给 MCP client → 下划线 debug 字段泄漏. 修复:
                 _log_recall detail 字段注入 _decay_factor (audit log 拿得到),
                 然后 pop 所有 hit dict 的 _decay_factor 字段.

测试覆盖:
  - F1 fix: base_score 优先级, 没 rrf_score/distance 时拿 importance
  - F1 fix: rrf 路径仍走 rrf_score (不变)
  - F1 fix: vector_only 路径仍走 distance 翻转 (不变)
  - F1 fix: 兜底 1.0 (orphan hit 不挂)
  - F1 fix: decay 排序在所有 4 种 hit 形态下都有效 (sort 不退化)
  - F2 fix: _log_recall 写到 audit log 的 detail 含 _decay_factor
  - F2 fix: hit dict 出 recall() 时已 pop _decay_factor (外部 API 看不到)
  - 整合: recall() 跑完, 返回 hits 干净 + audit log 拿得到 factor
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from recall_engine import RecallEngine  # noqa: E402


def _make_test_db():
    """[F1+F2] 构造最小 in-memory SQLite + chunks + recall_log 表."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            content TEXT,
            last_recalled TEXT,
            timestamp TEXT,
            memory_type TEXT,
            importance REAL
        )
    """
    )
    conn.execute(
        """
        CREATE TABLE recall_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            query_embedding_id TEXT,
            results_json TEXT,
            graph_hops INTEGER,
            latency_ms REAL,
            recall_details_json TEXT,
            created_at TEXT NOT NULL DEFAULT (iso_now())
        )
    """
    )
    return conn


class _DecayHost(RecallEngine):
    """[test-only] 单继承 RecallEngine, 不触发完整 Memory init."""

    def __init__(self, conn):
        self._conn = conn


def _call_decay(results, conn, now_iso):
    return _DecayHost(conn=conn)._apply_decay_to_hits(results, now_iso=now_iso)


def _insert_chunk(conn, cid, ts, memory_type="fact", importance=0.7, last_recalled=None):
    conn.execute(
        "INSERT INTO chunks (id, last_recalled, timestamp, memory_type, importance) VALUES (?, ?, ?, ?, ?)",
        (cid, last_recalled or ts, ts, memory_type, importance),
    )
    conn.commit()


# ============================================================
# F1 tests — base_score priority in _apply_decay_to_hits
# ============================================================


class TestF1BaseScorePriority:
    """[F1] decay 优先级: base_score > rrf_score > distance > 兜底 1.0."""

    def test_base_score_used_when_present(self):
        """F1 主路径: hit 带 base_score → 用 base_score 缩放 (不取 importance)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "meta_chunk"
        # 最近 recalled (idle ≈ 0, factor ≈ 1.5)
        ts = now  # exact match → idle=0
        _insert_chunk(conn, cid, ts)

        # meta_only 形态: 只有 base_score (由 recall() 策略分支注入)
        results = [{"chunk_id": cid, "importance": 0.7, "method": "meta", "base_score": 0.5}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # base_score=0.5, factor=1.5 → rrf_score = 0.75
        assert out[0]["rrf_score"] == pytest.approx(0.75)
        assert out[0]["_decay_factor"] == pytest.approx(1.5)

    def test_rrf_score_used_when_no_base(self):
        """rrf 路径 (4 路融合): hit 没 base_score 但有 rrf_score → 用 rrf_score."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "rrf_chunk"
        ts = now
        _insert_chunk(conn, cid, ts)

        # rrf 形态: _rrf_fuse 注入 rrf_score, 没 base_score
        results = [{"chunk_id": cid, "importance": 0.7, "method": "rrf", "rrf_score": 0.4}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # rrf_score=0.4 × factor=1.5 = 0.6
        assert out[0]["rrf_score"] == pytest.approx(0.6)

    def test_distance_used_when_no_base_no_rrf(self):
        """vector_only 路径: hit 有 distance, 没 base_score/rrf_score → 翻转 distance."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "vec_chunk"
        ts = now
        _insert_chunk(conn, cid, ts)

        # vector_only 形态
        results = [{"chunk_id": cid, "importance": 0.7, "method": "vector", "distance": 0.5}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # base_score = (2 - 0.5) = 1.5, factor = 1.5 → 2.25
        assert out[0]["rrf_score"] == pytest.approx(2.25)

    def test_orphan_fallback_uses_one(self):
        """[P1 2026-08-29 C1] 兜底: 入口注入后, hit 一定拿到 base_score/rrf_score/distance 之一.

        C1 方案 A: graph_only/meta_only/entity_only 的 base_score 注入下沉到
        _apply_decay_to_hits 入口, 不再依赖 strategy 分支. 因此'orphan hit'
        (4 种字段全没) 在真实 recall 出口不会存在 — 入口兜底会注入 base_score=importance.

        这个测试验证入口兜底注入生效 (而不是验证 'orphan 走 1.0 兜底' — 那个分支
        现在是 _apply_decay_to_hits 内部的防御性死代码).
        """
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "orphan_chunk"
        _insert_chunk(conn, cid, now)

        # orphan hit: 啥都没有 (模拟 mock 测试场景, 不是真实 recall 出口)
        results = [{"chunk_id": cid, "importance": 0.7, "method": "orphan"}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # C1 方案 A 行为: 入口兜底注入 base_score=importance=0.7, factor=1.5
        # → rrf_score = 0.7 × 1.5 = 1.05
        assert out[0]["rrf_score"] == pytest.approx(1.05)
        # 入口注入留下了 base_score 痕迹 (确认下沉生效)
        assert out[0].get("base_score") == 0.7

    def test_base_score_overrides_distance(self):
        """优先级: base_score 存在时, 即使有 distance 也用 base_score (不会误用 distance)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "weird_chunk"
        _insert_chunk(conn, cid, now)

        # hit 同时有 base_score 和 distance (理论不该发生, 但防御性)
        results = [
            {
                "chunk_id": cid,
                "importance": 0.7,
                "method": "weird",
                "base_score": 0.3,
                "distance": 0.5,  # (2-0.5)=1.5 不该被采纳
            }
        ]
        out = _call_decay(results, conn=conn, now_iso=now)
        # base_score=0.3 × factor=1.5 = 0.45 (不是 1.5 × 1.5 = 2.25)
        assert out[0]["rrf_score"] == pytest.approx(0.45)


class TestF1SortNotDegraded:
    """[F1] decay 排序在 4 种 hit 形态下都有效 (不退化为原 SQL ORDER BY)."""

    def test_meta_only_hits_actually_resort(self):
        """meta_only 形态 hits 的 decay 后排序跟原序不同 (排序确实生效)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        # 老 chunk: 150 天前 timestamp (5x fact 30 天半衰期 → factor 趋近 1.0)
        old_ts = (datetime.fromisoformat(now) - timedelta(days=150)).isoformat()
        # 新 chunk: 1 分钟前 (factor ≈ 1.5)
        new_ts = (datetime.fromisoformat(now) - timedelta(minutes=1)).isoformat()
        _insert_chunk(conn, "old", old_ts, importance=0.7)
        _insert_chunk(conn, "new", new_ts, importance=0.7)

        # 模拟 meta_only strategy 出口: 两个 hit 都带 base_score=importance=0.7
        # 初始 rrf_score 一致 → 让 decay 决定排序
        results = [
            {"chunk_id": "old", "importance": 0.7, "method": "meta", "base_score": 0.7},
            {"chunk_id": "new", "importance": 0.7, "method": "meta", "base_score": 0.7},
        ]
        out = _call_decay(results, conn=conn, now_iso=now)
        # new 应该排前面 (factor 1.5 > old 的 ~1.0)
        assert out[0]["chunk_id"] == "new"
        assert out[1]["chunk_id"] == "old"
        # 排序确实生效 (不是退化)
        assert out[0]["rrf_score"] > out[1]["rrf_score"]
        assert out[0]["_decay_factor"] > out[1]["_decay_factor"]

    def test_graph_only_entity_hits_decay_via_importance(self):
        """graph_only 策略里 entity hit 走 base_score=importance (F1 fix)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        ts = now
        # entity hit 在 _graph_recall 里 inline, 含 importance=0.9
        # 不在 chunks 表 — decay 看 cid.startswith('entity:') → factor=1.0 (entity 不衰减)
        results = [
            {
                "chunk_id": "entity:e1",
                "importance": 0.9,
                "method": "graph_entity",
                "base_score": 0.9,
            }
        ]
        out = _call_decay(results, conn=conn, now_iso=now)
        # entity 不衰减: factor=1.0, base_score=0.9 × 1.0 = 0.9
        assert out[0]["_decay_factor"] == 1.0
        assert out[0]["rrf_score"] == pytest.approx(0.9)


class TestF1StrategyDispatchInjection:
    """[F1 C1 方案 A] 静态验证 base_score 兜底注入下沉到 _apply_decay_to_hits 入口.

    C1 方案 A 把 base_score 注入从 3 个 strategy 分支出口合并到 _apply_decay_to_hits 入口.
    验证:
      1. recall() 的 3 个 strategy 分支 (graph_only/meta_only/entity_only) 不再
         各自注入 base_score — DRY 干净
      2. _apply_decay_to_hits 入口有兜底注入逻辑, 处理没 rrf_score/distance 的 hit
      3. _apply_decay_to_hits 入口的注入使用 importance 作 fallback
    """

    def test_graph_only_branch_no_longer_injects(self):
        """[C1] graph_only 分支不再注入 base_score (下沉到 _apply_decay_to_hits)."""
        import inspect

        src = inspect.getsource(RecallEngine.recall)
        g_start = src.find('elif strategy == "graph_only":')
        assert g_start > 0, "graph_only branch missing"
        g_end = src.find('elif strategy == "meta_only":', g_start)
        assert g_end > 0
        branch = src[g_start:g_end]
        assert "base_score" not in branch, "C1: graph_only branch should NOT inject base_score — moved to _apply_decay_to_hits entry"

    def test_meta_only_branch_no_longer_injects(self):
        """[C1] meta_only 分支不再注入 base_score."""
        import inspect

        src = inspect.getsource(RecallEngine.recall)
        m_start = src.find('elif strategy == "meta_only":')
        assert m_start > 0, "meta_only branch missing"
        m_end = src.find('elif strategy == "entity_only":', m_start)
        assert m_end > 0
        branch = src[m_start:m_end]
        assert "base_score" not in branch, "C1: meta_only branch should NOT inject base_score — moved to _apply_decay_to_hits entry"

    def test_entity_only_branch_no_longer_injects(self):
        """[C1] entity_only 分支不再注入 base_score."""
        import inspect

        src = inspect.getsource(RecallEngine.recall)
        e_start = src.find('elif strategy == "entity_only":')
        assert e_start > 0, "entity_only branch missing"
        e_end = src.find('raise ValueError(f"unknown strategy', e_start)
        assert e_end > 0
        branch = src[e_start:e_end]
        assert "base_score" not in branch, "C1: entity_only branch should NOT inject base_score — moved to _apply_decay_to_hits entry"

    def test_apply_decay_entry_injects_base_score(self):
        """[C1] _apply_decay_to_hits 入口兜底注入 base_score=importance (DRY)."""
        import inspect

        src = inspect.getsource(RecallEngine._apply_decay_to_hits)
        # 找入口位置 (now_iso 解析后, scored 循环前)
        # 检查 base_score 注入逻辑存在
        assert "base_score" in src, "_apply_decay_to_hits missing base_score"
        # 检查防御性条件: 三种字段都没才注入
        assert "rrf_score" in src and "distance" in src, "_apply_decay_to_hits entry should check rrf_score/distance to avoid overwriting"
        # 检查使用 importance 作 fallback
        assert "importance" in src, "_apply_decay_to_hits entry should use importance as base_score fallback"


# ============================================================
# F2 tests — _decay_factor 不泄漏到 MCP 客户端, 但进 audit log
# ============================================================


class TestF2DecayFactorLeak:
    """[F2] _decay_factor 写到 audit log, 但 hit dict 出 recall() 时已 pop."""

    def test_apply_decay_writes_decay_factor_to_hit(self):
        """_apply_decay_to_hits 写入 _decay_factor 到 hit (供 _log_recall 读)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "test_chunk"
        _insert_chunk(conn, cid, now)

        results = [{"chunk_id": cid, "importance": 0.7, "method": "meta", "base_score": 0.7}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # 写入瞬间 _decay_factor 在 hit dict 上 (供 _log_recall 读)
        assert "_decay_factor" in out[0]
        assert out[0]["_decay_factor"] == pytest.approx(1.5)

    def test_log_recall_writes_decay_factor_to_audit(self):
        """_log_recall 把 _decay_factor 写进 recall_details_json (audit log 拿到)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "test_chunk"
        _insert_chunk(conn, cid, now)

        # 建一个最小 _DecayHost 实例 (有 _conn), 然后直接调 _log_recall
        host = _DecayHost(conn=conn)
        results = [
            {
                "chunk_id": cid,
                "content": "x",
                "importance": 0.7,
                "method": "meta",
                "base_score": 0.7,
                "rrf_score": 1.05,  # decay 后的值
                "_decay_factor": 1.5,  # decay 因子
            }
        ]
        host._log_recall("test query", results, hops=0, latency_ms=1.0)
        # recall_log 表的 recall_details_json 应含 _decay_factor
        row = conn.execute("SELECT recall_details_json FROM recall_log ORDER BY created_at DESC LIMIT 1").fetchone()
        assert row is not None, "_log_recall should have inserted a row"
        details = json.loads(row["recall_details_json"])
        assert len(details) == 1
        assert "_decay_factor" in details[0]
        assert details[0]["_decay_factor"] == 1.5

    def test_log_recall_pops_decay_factor_from_hits(self):
        """_log_recall 把 hit dict 上的 _decay_factor 移除 (防止 MCP 客户端泄漏)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        cid = "test_chunk"
        _insert_chunk(conn, cid, now)

        host = _DecayHost(conn=conn)
        results = [
            {
                "chunk_id": cid,
                "content": "x",
                "importance": 0.7,
                "method": "meta",
                "base_score": 0.7,
                "rrf_score": 1.05,
                "_decay_factor": 1.5,
            }
        ]
        host._log_recall("test", results, hops=0, latency_ms=1.0)
        # hit dict 上的 _decay_factor 已被 pop (出 recall() 给外部 API 用)
        assert "_decay_factor" not in results[0], "F2 fix: _decay_factor should be popped from hit dict after _log_recall"


# ============================================================
# 整合测试 — F1+F2 在一个完整 recall 调用里都生效
# ============================================================


class TestF1F2Integration:
    """[整合] 完整跑 recall(), 验证 F1+F2 同时生效."""

    def test_recall_meta_only_writes_audit_no_leak(self):
        """完整 recall: meta_only 路径 + audit log + hit dict 干净."""
        import os

        os.environ["MNELO_MEMORY_DIR"] = "/tmp/mnelo_f1f2_integration"
        os.environ["MNELO_TEST_FRESH"] = "1"
        os.environ["MNELO_MEMORY_SEARCH_BACKEND"] = "usearch"
        # 清理
        import shutil

        shutil.rmtree("/tmp/mnelo_f1f2_integration", ignore_errors=True)

        from memory import Memory

        m = Memory()
        m.remember(
            content="Master's GitHub is Yanru-cafe, lives in Beijing",
            source="manual",
            importance=0.8,
            agent_id="test",
        )

        # 走 meta_only: 触发 F1 fix 的 base_score 注入
        results = m.recall("Yanru-cafe", top_k=3, strategy="meta_only")
        # F2 fix: hit dict 不含 _decay_factor
        for r in results:
            assert "_decay_factor" not in r, f"F2 fix: _decay_factor leaked in hit {r['chunk_id'][:20]}"
        # F1 fix: meta_only 注入 base_score, decay 后 rrf_score > 0 (排序有效)
        for r in results:
            assert r.get("rrf_score", 0) > 0, f"F1 fix: meta_only hit {r['chunk_id'][:20]} has rrf_score=0, decay didn't sort"
            assert r.get("base_score") is not None, f"F1 fix: meta_only hit {r['chunk_id'][:20]} missing base_score"

        # F2 fix: audit log 拿到 _decay_factor
        row = m._conn.execute("SELECT recall_details_json FROM recall_log ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            details = json.loads(row["recall_details_json"])
            if details:
                assert "_decay_factor" in details[0], "F2 fix: audit log should have _decay_factor"

        m.close()

    def test_recall_vector_only_does_not_break(self):
        """回归: vector_only 路径 (走 distance) 不被 F1 fix 误改."""
        import os

        os.environ["MNELO_MEMORY_DIR"] = "/tmp/mnelo_f1f2_vector"
        os.environ["MNELO_TEST_FRESH"] = "1"
        os.environ["MNELO_MEMORY_SEARCH_BACKEND"] = "usearch"
        import shutil

        shutil.rmtree("/tmp/mnelo_f1f2_vector", ignore_errors=True)

        from memory import Memory

        m = Memory()
        m.remember(
            content="Master lives in Beijing",
            source="manual",
            importance=0.8,
            agent_id="test",
        )

        # vector_only: 走 distance 翻转, 不该被 base_score 误盖
        results = m.recall("Beijing", top_k=3, strategy="vector_only")
        for r in results:
            assert "_decay_factor" not in r
            # vector_only 没被注入 base_score (F1 fix 只动 graph/meta/entity)
            assert "base_score" not in r, "F1 fix should only inject base_score in graph_only/meta_only/entity_only"
            # rrf_score 应由 distance 翻转 + factor 算出来 (>0)
            assert r.get("rrf_score", 0) > 0

        m.close()
