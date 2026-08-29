#!/usr/bin/env python3
"""test_p1_memory_decay.py — P1 memory decay (recall-time recency scaling) unit tests.

[cherry-pick 2026-08-29] 从 082a7f8 dirty 移植; 同时给 cherry-pick 到 82ce284 之后的
RecallEngine._apply_decay_to_hits 加 unit test 覆盖.

覆盖:
  - _recency_decay_factor 公式 (idle=0 → 1.5, idle=∞ → 1.0, half_life=inf → 1.5)
  - 不同 memory_type 半衰期 (ephemeral 24h / episode 336h / fact 720h / preference 2160h)
  - _apply_decay_to_hits 实体 hit 不衰减
  - _apply_decay_to_hits chunk hit 重排 (新写入上浮, 老 chunk 下沉)
  - _apply_decay_to_hits 边界 (空 results, now_iso 解析失败, chunk 不存在)

设计:
  - 不依赖完整 Memory() init — 直接构造一个最小 fake 对象 (单继承 _FakeMem + RecallEngine).
  - sqlite3 真实 DB (in-memory), 插入 chunks 后调 decay, 检查返回排序跟 _decay_factor.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 把项目根加进 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from recall_engine import RecallEngine  # noqa: E402


def _make_test_db():
    """构造最小 in-memory SQLite + chunks 表 — 只 decay 用到的字段."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE chunks (
            id TEXT PRIMARY KEY,
            content TEXT,
            last_recalled TEXT,
            timestamp TEXT,
            memory_type TEXT
        )
    """
    )
    return conn


class _DecayHost(RecallEngine):
    """[test-only] 只继承 RecallEngine, 把 _conn 设到 in-memory test DB."""

    def __init__(self, conn):
        self._conn = conn


def _call_decay(results, conn, now_iso):
    """便捷: 构造 _DecayHost 并调 _apply_decay_to_hits."""
    return _DecayHost(conn=conn)._apply_decay_to_hits(results, now_iso=now_iso)


class TestRecencyDecayFactor:
    """_recency_decay_factor 公式静态测试 — 不依赖 DB."""

    def test_idle_zero_is_ceiling(self):
        """idle=0 → factor = 1.5 (浮顶 — 刚 recall 不衰减)."""
        f = RecallEngine._recency_decay_factor
        for mt in ["ephemeral", "episode", "fact", "preference", "decision"]:
            assert f(0.0, mt) == pytest.approx(1.5)

    def test_procedure_is_inf_half_life_always_15(self):
        """procedure (半衰期 inf) → 不衰减, factor 恒为 1.5."""
        f = RecallEngine._recency_decay_factor
        for idle in [0, 1, 100, 1_000_000]:
            assert f(idle, "procedure") == 1.5

    def test_idle_large_decays_to_baseline(self):
        """idle >> half_life → factor → 1.0 (基线)."""
        f = RecallEngine._recency_decay_factor
        # ephemeral half_life = 24h, idle = 240h = 10x → exp(-10) ≈ 4.5e-5
        factor = f(240.0, "ephemeral")
        assert 1.0 <= factor < 1.001
        # fact half_life = 720h, idle = 7200h = 10x
        factor = f(7200.0, "fact")
        assert 1.0 <= factor < 1.001

    def test_idle_negative_clamped_to_zero(self):
        """clock skew: idle < 0 → clamp 到 0, factor = 1.5."""
        f = RecallEngine._recency_decay_factor
        assert f(-100.0, "fact") == 1.5

    def test_unknown_memory_type_uses_default(self):
        """未知 / None memory_type → 用 _DEFAULT_DECAY_HALF_LIFE_HOURS = 168h."""
        f = RecallEngine._recency_decay_factor
        # 168h 默认 half_life, idle=168 → factor = 1 + 0.5*exp(-1) ≈ 1.184
        import math

        expected = 1.0 + 0.5 * math.exp(-1)
        assert f(168.0, "unknown_type") == pytest.approx(expected)
        # None 等同于 "unknown_type"
        assert f(168.0, None) == pytest.approx(expected)

    def test_ephemeral_decays_fast(self):
        """ephemeral 24h 半衰期 — 1 天没用就明显下沉."""
        import math

        f = RecallEngine._recency_decay_factor
        # 1 half-life 后 → factor = 1 + 0.5*exp(-1)
        assert f(24.0, "ephemeral") == pytest.approx(1.0 + 0.5 * math.exp(-1), rel=1e-3)
        # 4 half-life (4 天) → factor ≈ 1.009
        assert f(96.0, "ephemeral") == pytest.approx(1.0 + 0.5 * math.exp(-4), rel=1e-3)

    def test_preference_decays_slow(self):
        """preference 2160h (90d) 半衰期 — 几周 idle 影响小."""
        import math

        f = RecallEngine._recency_decay_factor
        # 14 天 idle → idle/half_life ≈ 0.0648 → exp(-0.0648) ≈ 0.937 → factor ≈ 1.469
        expected = 1.0 + 0.5 * math.exp(-14 * 24 / 2160.0)
        assert f(14 * 24, "preference") == pytest.approx(expected, rel=1e-3)


class TestApplyDecayToHits:
    """_apply_decay_to_hits 集成 — 真实 SQLite + chunks 表."""

    def test_empty_results_returns_empty(self):
        """results 空 → 返回 []."""
        out = _call_decay([], conn=_make_test_db(), now_iso="2026-08-29T12:00:00")
        assert out == []

    def test_invalid_now_iso_returns_unchanged(self):
        """now_iso 解析失败 → 不衰减, 保持原序."""
        results = [{"chunk_id": "c1", "rrf_score": 0.5}]
        out = _call_decay(results, conn=_make_test_db(), now_iso="not-a-date")
        assert out == results
        # rrf_score 没被改 (没衰减也没乘 factor)
        assert out[0]["rrf_score"] == 0.5

    def test_entity_hit_no_decay(self):
        """entity:<id> — 无 timestamp 概念, 不衰减 (factor=1.0)."""
        conn = _make_test_db()
        results = [{"chunk_id": "entity:e1", "rrf_score": 0.8}]
        out = _call_decay(results, conn=conn, now_iso="2026-08-29T12:00:00")
        assert out[0]["_decay_factor"] == 1.0
        assert out[0]["rrf_score"] == 0.8  # 没乘 factor

    def test_chunk_not_found_no_decay(self):
        """chunk_id 在 DB 里查不到 (RRF 残留) → factor=1.0."""
        conn = _make_test_db()
        results = [{"chunk_id": "ghost", "rrf_score": 0.5}]
        out = _call_decay(results, conn=conn, now_iso="2026-08-29T12:00:00")
        assert out[0]["_decay_factor"] == 1.0

    def test_recent_chunk_rises_above_old_chunk(self):
        """新 recalled chunk 上浮, 老 chunk 下沉."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        # recent: 1 分钟前 recall → factor 接近 1.5
        recent_ts = (datetime.fromisoformat(now) - timedelta(minutes=1)).isoformat()
        # old: 150 天前 recall (5x fact 30 天半衰期) → factor 趋近 1.0
        old_ts = (datetime.fromisoformat(now) - timedelta(days=150)).isoformat()
        conn.execute(
            "INSERT INTO chunks (id, last_recalled, timestamp, memory_type) VALUES (?, ?, ?, ?)",
            ("recent", recent_ts, recent_ts, "fact"),
        )
        conn.execute(
            "INSERT INTO chunks (id, last_recalled, timestamp, memory_type) VALUES (?, ?, ?, ?)",
            ("old", old_ts, old_ts, "fact"),
        )
        conn.commit()

        # 初始 rrf_score 一致 → 让 decay 决定排序
        results = [
            {"chunk_id": "old", "rrf_score": 0.5},
            {"chunk_id": "recent", "rrf_score": 0.5},
        ]
        out = _call_decay(results, conn=conn, now_iso=now)
        # recent 应该排前面
        assert out[0]["chunk_id"] == "recent"
        assert out[1]["chunk_id"] == "old"
        # recent factor 接近 1.5
        assert out[0]["_decay_factor"] > 1.4
        # old factor 接近 1.0
        assert out[1]["_decay_factor"] < 1.05

    def test_last_recalled_fallback_to_timestamp(self):
        """last_recalled 为 NULL → fallback 到 timestamp (旧数据兼容)."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        # 只设 timestamp, 不设 last_recalled (旧数据)
        old_ts = (datetime.fromisoformat(now) - timedelta(days=150)).isoformat()
        conn.execute(
            "INSERT INTO chunks (id, last_recalled, timestamp, memory_type) VALUES (?, NULL, ?, ?)",
            ("old_fallback", old_ts, "fact"),
        )
        conn.commit()

        results = [{"chunk_id": "old_fallback", "rrf_score": 0.5}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # 应该走 timestamp fallback → factor 接近 1.0 (150 天 = 5x fact 30 天半衰期)
        assert out[0]["_decay_factor"] < 1.05

    def test_vector_only_distance_to_score(self):
        """vector_only 路: rrf_score 缺, distance 有 → 翻转成 score 再乘 factor."""
        conn = _make_test_db()
        now = "2026-08-29T12:00:00"
        # 最近 recalled, fact 半衰期
        recent_ts = (datetime.fromisoformat(now) - timedelta(minutes=1)).isoformat()
        conn.execute(
            "INSERT INTO chunks (id, last_recalled, timestamp, memory_type) VALUES (?, ?, ?, ?)",
            ("vec_chunk", recent_ts, recent_ts, "fact"),
        )
        conn.commit()

        # 只有 distance, 没 rrf_score (vector_only 路)
        results = [{"chunk_id": "vec_chunk", "distance": 0.5}]
        out = _call_decay(results, conn=conn, now_iso=now)
        # base_score = max(0, 2 - 0.5) = 1.5, 然后乘 factor ≈ 1.5 → ≈ 2.25
        # 字段 _decay_factor 应该接近 1.5
        assert out[0]["_decay_factor"] > 1.4
        # rrf_score 应该是 base_score * factor
        assert out[0]["rrf_score"] > 2.0


class TestRecallIntegration:
    """[cherry-pick contract] 静态检查 decay 跟 RecallEngine.recall() 集成的调用点 — 不开 MCP server."""

    def test_recall_calls_decay_before_log_recall(self):
        """decay 调用必须在 _log_recall 之前 (排序权重在 audit 落盘前确定)."""
        import inspect

        src = inspect.getsource(RecallEngine.recall)
        decay_pos = src.find("_apply_decay_to_hits(")
        log_pos = src.find("_log_recall(")
        assert decay_pos > 0, "decay call site missing in recall()"
        assert log_pos > 0, "_log_recall call site missing in recall()"
        assert decay_pos < log_pos, "decay must be called before _log_recall"

    def test_decay_constants_on_recall_engine(self):
        """decay constants 必须挂在 RecallEngine 上 (而不是 Memory — 避免循环 import)."""
        hl = RecallEngine._MEMORY_TYPE_DECAY_HALF_LIFE_HOURS
        for mt in ["ephemeral", "episode", "fact", "preference", "decision", "procedure"]:
            assert mt in hl, f"missing half-life for {mt}"
        assert hl["procedure"] == float("inf")
        assert RecallEngine._DEFAULT_DECAY_HALF_LIFE_HOURS == 168.0
        assert hasattr(RecallEngine, "_recency_decay_factor")
        assert hasattr(RecallEngine, "_apply_decay_to_hits")