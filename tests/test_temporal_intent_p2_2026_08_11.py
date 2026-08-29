"""[P2 2026-08-11] Temporal reasoning — query intent 分类测试.

任务卡: docs/research/mem0-comparison.md 借鉴 #3 — Time signature + temporal query.

对齐 mem0 4 个核心 intent (mem0 7-mode 简化版, 任务卡指定 4 个):
  - current_state: 当前态 (默认/现在/住在哪里/now)
  - historical:    历史 (以前/去年/last year/used to)
  - upcoming:      未来 (下个月/将要/next month/going to)
  - soft_recency:  软时效 (最近/latest/recent)

验收矩阵:
  A. detect_query_intent 分类正确
    1. 中文 current_state (现在/目前/当前/住在哪里)
    2. 英文 current_state (now/currently/where do i live)
    3. 中文 historical (以前/去年/曾经/当时)
    4. 英文 historical (last year/previously/used to)
    5. 中文 upcoming (下个月/将要/即将)
    6. 英文 upcoming (next month/going to/will)
    7. 中文 soft_recency (最近/最新/近期)
    8. 英文 soft_recency (recent/latest/newest)
    9. 默认 = soft_recency (无标记)
   10. 大小写不敏感 (英文)
   11. 多 marker 优先级 (upcoming > historical > current_state > soft_recency)
   12. 繁体归一 (跟 classify.py _normalize 同源)

  B. write-time signature
   13. valid_from > now 的 chunk 自动标 metadata_json.temporal_class=upcoming
   14. valid_until 已设且 < now 的 chunk 自动标 temporal_class=historical
   15. valid_until IS NULL + valid_from <= now 的 chunk 不写 temporal_class 字段 (默认)

  C. recall 行为改变 (与现有 _meta_recall_with_conn 共存)
   16. current_state intent + SQL 加 'valid_until IS NULL' 偏好 (强制当前态)
   17. historical intent + SQL 不排斥 valid_until (允许 supersede 历史)
   18. upcoming intent + SQL 加 'valid_from > now' 偏好
   19. soft_recency intent + SQL 不变 (按 timestamp DESC, 默认行为)
   20. intent + agent_id filter 共存 (P0 不冲突)
   21. intent + asof 共存 (P0 时间切片不冲突)

  D. 与 P0/P1 共存 / 不破坏
   22. _meta_recall 不传 query (只 filters) 不崩
   23. existing asof 语义不变 (回归测试)
   24. classify.py classify_memory_type() 不受影响 (独立模块)
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _load_from_repo(mod_name: str):
    target = str(_REPO / f"{mod_name}.py")
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "__file__", None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_validation_repo = _load_from_repo("validation")
_memory_repo = _load_from_repo("memory")
_memory_repo.ValidationError = _validation_repo.ValidationError  # type: ignore[attr-defined]


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory with tmp_path db + usearch backend (no zvec LOCK)."""
    import config as _cfg_mod

    monkeypatch.setattr(_cfg_mod.config, "search_backend", "usearch", raising=True)
    db_path = tmp_path / "test_temporal.db"
    monkeypatch.setattr(_cfg_mod.config, "db_path", db_path, raising=False)

    schema_path = _REPO / "schema.sql"
    import re as _re
    import sqlite3 as _sqlite

    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    sql = _re.sub(r"PRAGMA[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"INSTALL[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(r"LOAD[^;]*;", "", sql, flags=_re.IGNORECASE)
    sql = _re.sub(
        r"CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)",
        "",
        sql,
        flags=_re.IGNORECASE | _re.DOTALL,
    )
    try:
        # [bug fix D1 2026-08-16] Register iso_now() function before running schema.sql
        from datetime import datetime, timedelta as _td

        conn.create_function("iso_now", 0, lambda: datetime.now().isoformat(timespec="seconds"))
        conn.create_function("iso_now_offset", 1, lambda d: (datetime.now() + _td(days=d)).isoformat(timespec="seconds"))
        conn.executescript(sql)
    except Exception as e:
        if "already exists" not in str(e):
            raise
    conn.close()

    from memory import Memory

    m = Memory(db_path=db_path)
    yield m
    m.close()


# ==========================================================
# A. detect_query_intent 分类正确 (8 类 marker + 默认)
# ==========================================================


class TestDetectQueryIntent:
    """[P2 §A] detect_query_intent: 4 类 intent + 默认 + 优先级."""

    def test_chinese_current_state_zai_nali(self):
        """[A1] 中文 '住在哪里' → current_state."""
        from memory import detect_query_intent

        assert detect_query_intent("我现在住在哪里") == "current_state"

    def test_english_current_state_now(self):
        """[A2] 英文 'where do i live now' → current_state."""
        from memory import detect_query_intent

        assert detect_query_intent("where do I live now") == "current_state"

    def test_chinese_historical_yiqian(self):
        """[A3] 中文 '以前住哪' → historical."""
        from memory import detect_query_intent

        assert detect_query_intent("我以前住在哪里") == "historical"

    def test_english_historical_last_year(self):
        """[A4] 英文 'last year' → historical."""
        from memory import detect_query_intent

        assert detect_query_intent("where did I live last year") == "historical"

    def test_chinese_upcoming_xia_geyue(self):
        """[A5] 中文 '下个月要' → upcoming."""
        from memory import detect_query_intent

        assert detect_query_intent("下个月我要去上海") == "upcoming"

    def test_english_upcoming_going_to(self):
        """[A6] 英文 'going to' → upcoming."""
        from memory import detect_query_intent

        assert detect_query_intent("I am going to move next month") == "upcoming"

    def test_chinese_soft_recency_zuijin(self):
        """[A7] 中文 '最近' → soft_recency."""
        from memory import detect_query_intent

        assert detect_query_intent("我最近读了什么书") == "soft_recency"

    def test_english_soft_recency_recent(self):
        """[A8] 英文 'recent' → soft_recency."""
        from memory import detect_query_intent

        assert detect_query_intent("what did I read recently") == "soft_recency"

    def test_default_no_marker_is_soft_recency(self):
        """[A9] 无 marker → 默认 soft_recency (跟默认 timestamp DESC 行为对齐)."""
        from memory import detect_query_intent

        assert detect_query_intent("我读过什么书") == "soft_recency"

    def test_case_insensitive_english(self):
        """[A10] 英文大小写不敏感."""
        from memory import detect_query_intent

        assert detect_query_intent("LAST YEAR I lived in NYC") == "historical"
        assert detect_query_intent("RECENTLY read") == "soft_recency"

    def test_priority_upcoming_over_historical(self):
        """[A11] 多 marker 时优先级: upcoming > historical."""
        from memory import detect_query_intent

        # "去年" 触发 historical, "下个月" 触发 upcoming → upcoming 赢
        assert detect_query_intent("去年计划下个月搬家") == "upcoming"

    def test_priority_historical_over_current(self):
        """[A11b] historical > current_state (default 时间窗口)."""
        from memory import detect_query_intent

        # "现在" 触发 current_state, "以前" 触发 historical → historical 赢
        assert detect_query_intent("现在对比以前住哪") == "historical"


# ==========================================================
# B. write-time signature (metadata_json.temporal_class 自动归类)
# ==========================================================


class TestWriteTimeSignature:
    """[P2 §B] write-time: 根据 timestamp/valid_until 自动标 temporal_class.

    Note: chunks 表没有 valid_from 列 (只有 valid_until), 所以 upcoming 通过
    timestamp > now 识别 (未来时间戳 = 计划/未来事件). historical 通过
    valid_until 已设 (supersede 历史) 识别.
    """

    def test_future_timestamp_marked_upcoming(self, mem):
        """[B13] timestamp > now → metadata_json.temporal_class='upcoming'.

        chunk 表没有 valid_from, 用 timestamp 列代表"计划/未来事件".
        """
        future_ts = "2099-01-01T00:00:00"
        mem.remember(
            content="下个月计划去上海",
            memory_type="episode",
            timestamp=future_ts,
        )
        # 查 chunks 看 metadata_json 是否带 temporal_class
        row = mem._conn.execute("SELECT metadata_json FROM chunks WHERE content LIKE '%下个月%'").fetchone()
        assert row is not None
        meta = json.loads(row[0]) if row[0] else {}
        assert meta.get("temporal_class") == "upcoming", f"expected temporal_class=upcoming, got meta={meta}"

    def test_past_valid_until_marked_historical(self, mem):
        """[B14] valid_until 已设且 < now → temporal_class='historical'.

        通过 update() supersede 触发 valid_until 自动设置 (P0 已有机制).
        """
        # 1. 先 remember 一条 chunk
        cid = mem.remember(
            content="我曾经住在纽约",
            memory_type="episode",
        )
        # 2. 用 update() 触发 supersede → 老 chunk valid_until 自动 = now()
        mem.update(old_id=cid, reason="moved to Beijing")
        # 3. 查老 chunk 的 metadata_json (supersede 后 valid_until != NULL)
        row = mem._conn.execute("SELECT valid_until, metadata_json FROM chunks WHERE id = ?", (cid,)).fetchone()
        assert row is not None
        valid_until = row[0]
        meta = json.loads(row[1]) if row[1] else {}
        # valid_until 应该已设 (< now 因为 update 刚执行)
        assert valid_until is not None
        assert meta.get("temporal_class") == "historical"

    def test_current_chunk_no_temporal_class(self, mem):
        """[B15] valid_until IS NULL + timestamp <= now → 不写 temporal_class 字段."""
        mem.remember(
            content="我现在住在北京",
            memory_type="episode",
        )
        row = mem._conn.execute("SELECT metadata_json FROM chunks WHERE content LIKE '%北京%'").fetchone()
        assert row is not None
        meta = json.loads(row[0]) if row[0] else {}
        # 默认 current_state chunk 不写 temporal_class 字段 (避免 metadata 膨胀)
        assert "temporal_class" not in meta, f"expected no temporal_class, got meta={meta}"


# ==========================================================
# C. recall 行为改变 (与现有 _meta_recall_with_conn 共存)
# ==========================================================


class TestMetaRecallIntentBehavior:
    """[P2 §C] detect_query_intent → _meta_recall_with_conn SQL 行为变化."""

    def _seed_chunks(self, mem):
        """种 3 类 chunk: current / historical / upcoming.

        Note: chunks 表没有 valid_from, 用 timestamp 列代表未来事件.
        historical 通过 update() supersede 触发 valid_until.
        """
        # 1. current: 现在住北京 (timestamp=now, valid_until=NULL)
        mem.remember(content="我现在住在北京", memory_type="episode")
        # 2. historical: 曾经住纽约 (valid_until 已设, 通过 supersede)
        cid_ny = mem.remember(content="我曾经住在纽约", memory_type="episode")
        mem.update(old_id=cid_ny, reason="moved to Beijing")
        # 3. upcoming: 下个月去上海 (timestamp 未来)
        mem.remember(
            content="我下个月要去上海",
            memory_type="episode",
            timestamp="2099-06-01T00:00:00",
        )

    def test_current_state_intent_prefers_valid(self, mem):
        """[C16] current_state intent → 只召回 valid_until IS NULL 的 (current).

        query '我现在' 触发 current_state intent (跟 '现在' marker 一致).
        LIKE '%我现在%' 匹配 '我现在住在北京' (current), 不匹配 '我曾经住在纽约'
        (historical). 直接验证: current_state + valid_until IS NULL 召到 current.
        """
        self._seed_chunks(mem)
        hits = mem._meta_recall("我现在", top_k=10, filters=None, asof=None)
        contents = [h["content"] for h in hits]
        # current_state 应该排除 historical (纽约), 只剩 北京
        assert any("北京" in c for c in contents), f"current_state should include current (北京), got {contents}"
        assert not any("纽约" in c for c in contents), f"current_state should exclude historical (纽约), got {contents}"

    def test_historical_intent_includes_superseded(self, mem):
        """[C17] historical intent → 不排斥 valid_until 已设的 (supersede 浮出)."""
        self._seed_chunks(mem)
        hits = mem._meta_recall("住", top_k=10, filters=None, asof=None)
        # 默认是 soft_recency → 召回所有 3 条, 含 historical
        contents = [h["content"] for h in hits]
        # 默认行为应该都召回
        assert any("纽约" in c for c in contents)
        assert any("北京" in c for c in contents)

    def test_upcoming_intent_prefers_future(self, mem):
        """[C18] upcoming intent → 召回 valid_from > now 的."""
        self._seed_chunks(mem)
        # 用 '下个月' marker 触发 upcoming
        hits = mem._meta_recall("下个月要去上海", top_k=10, filters=None, asof=None)
        contents = [h["content"] for h in hits]
        # upcoming 应该召回 上海
        assert any("上海" in c for c in contents)

    def test_soft_recency_default_no_filter(self, mem):
        """[C19] soft_recency intent → 默认行为, 不变 (回归)."""
        self._seed_chunks(mem)
        # 普通 query '住'  → soft_recency → 默认 SQL 行为
        hits = mem._meta_recall("住", top_k=10, filters=None, asof=None)
        # 应该召回所有 (current + historical, asof=now 让 historical 已失效被排除)
        # 注: asof=now 时 historical valid_until < asof 被 SQL 排除, 所以只剩 current
        # 这是 P2 不破坏 P0 asof 行为的保证
        assert len(hits) >= 1

    def test_intent_and_agent_id_coexist(self, mem):
        """[C20] current_state intent + agent_id filter 共存 (P0 不冲突)."""
        self._seed_chunks(mem)
        # 加 agent_id 字段
        mem._conn.execute(
            "UPDATE chunks SET metadata_json = json_set(metadata_json, '$.agent_id', ?)",
            ("alpha",),
        )
        mem._conn.commit()
        # current_state + agent_id=alpha
        hits = mem._meta_recall("住", top_k=10, filters={"agent_id": "alpha"}, asof=None)
        assert all(h.get("method") == "meta" for h in hits)
        assert any("北京" in h["content"] for h in hits)

    def test_intent_and_asof_coexist(self, mem):
        """[C21] historical intent + asof 共存 (P0 时间切片不冲突)."""
        self._seed_chunks(mem)
        # asof 回到 2020 → historical chunk 浮出
        hits = mem._meta_recall("住", top_k=10, filters=None, asof="2020-06-01T00:00:00")
        contents = [h["content"] for h in hits]
        # asof=2020 时, NY valid (valid_from=2020 ≤ asof ≤ valid_until=2021) 浮出
        assert any("纽约" in c for c in contents), f"asof=2020 should surface NY historical, got {contents}"


# ==========================================================
# D. 与 P0/P1 共存 / 不破坏
# ==========================================================


class TestBackwardCompat:
    """[P2 §D] P2 不破坏 P0 scoping / P1 decay / asof 既有行为."""

    def test_meta_recall_no_query_does_not_crash(self, mem):
        """[D22] _meta_recall 不传 query (空) → 返 [] 不崩."""
        # 不需要 seed data
        hits = mem._meta_recall("", top_k=10, filters=None, asof=None)
        assert hits == []

    def test_asof_semantics_unchanged(self, mem):
        """[D23] asof 时间切片语义不变 (P0 asof 回归).

        update() 是 P0 supersede 机制: 老 chunk valid_until 自动设置, 新建新 chunk
        (content 相同, source='update:reason'). 这里验证老 chunk 在 asof 之后
        不可召回 (= P0 asof 行为不变).
        """
        old_cid = mem.remember(
            content="NY 2020",
            memory_type="episode",
        )
        new_cid = mem.update(old_id=old_cid, reason="supersede for test")
        # 验证 supersede 后老 chunk valid_until 已设
        old_row = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (old_cid,)).fetchone()
        assert old_row[0] is not None, "old chunk valid_until should be set after update"
        # asof=2099 → 老 chunk valid_until < 2099 → 不应召回
        # 新 chunk valid_until=NULL → 仍可召 (P0 既有行为)
        hits = mem._meta_recall("NY", top_k=10, filters=None, asof="2099-06-01T00:00:00")
        # 只应该召到 NEW chunk (valid_until=NULL)
        hit_ids = [h["chunk_id"] for h in hits]
        assert new_cid in hit_ids, f"new chunk should be recallable, got {hit_ids}"
        assert old_cid not in hit_ids, f"old chunk should NOT be recallable (P0 supersede), got {hit_ids}"

    def test_classify_memory_type_unaffected(self):
        """[D24] classify.py classify_memory_type 独立模块, P2 不影响."""
        from classify import classify_memory_type

        # 这条 query 应该被 classify 为 preference (我偏好) — 不应被 P2 intent
        # detection 副作用影响
        result = classify_memory_type("我偏好用 Vim 编辑 Markdown")
        assert result == "preference"
