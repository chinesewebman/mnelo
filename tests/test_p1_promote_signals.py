"""P1 — 晋升信号扫描 (TASKS_L2_SESSION_STATE §2.3 P1).

设计 §2.1 表: chunk 满足任一强信号 → promote 候选
  - recall_count ≥ 20
  - evidence_chunk_id 被引用度 ≥ 10
  - 长期 (90d) importance ≥ 0.8

候选按信号强度排序.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import memory as memory_mod  # noqa: E402


def _new_mem():
    mem = memory_mod.Memory()
    mem._l2_set("l2.enabled", "1")
    return mem


def _make_high_recall_chunk(mem, source="p1_test", suffix="a", target_recall=25):
    cid = mem.remember(
        content=f"p1 test fact {suffix}",
        source=source,
        memory_type="fact",
        importance=0.5,
    )
    mem._conn.execute(
        "UPDATE chunks SET recall_count = ? WHERE id = ?",
        (target_recall, cid),
    )
    mem._conn.commit()
    return cid


def _make_high_ref_chunk(mem, source="p1_test", suffix="b", target_refs=12):
    cid = mem.remember(
        content=f"p1 test fact {suffix}",
        source=source,
        memory_type="fact",
        importance=0.5,
    )
    entity_id = f"p1_test_entity_{suffix}"
    mem._conn.execute(
        "INSERT OR IGNORE INTO entities (id, kind, name, summary, importance) VALUES (?, 'concept', ?, ?, 0.5)",
        (entity_id, f"P1 Tester {suffix}", f"ref target {suffix}"),
    )
    # relations.id 是 INTEGER AUTOINCREMENT — 用 hash 转 int
    import hashlib as _hl

    for i in range(target_refs):
        rel_id_src = f"{entity_id}|{cid}|{i}"
        rel_id = int.from_bytes(_hl.md5(rel_id_src.encode()).digest()[:4], "big", signed=False) % (2**31)
        mem._conn.execute(
            """INSERT OR IGNORE INTO relations
                 (id, source_id, target_id, relation, weight, valid_from, evidence_chunk_id)
               VALUES (?, ?, ?, 'ref_target', 1.0, ?, ?)""",
            (rel_id, entity_id, entity_id, "2026-01-01T00:00:00", cid),
        )
    mem._conn.commit()
    return cid


def _make_long_high_imp_chunk(mem, source="p1_test", suffix="c"):
    cid = mem.remember(
        content=f"p1 test fact {suffix}",
        source=source,
        memory_type="fact",
        importance=0.9,
    )
    mem._conn.execute(
        "UPDATE chunks SET timestamp = datetime('now', '-100 days') WHERE id = ?",
        (cid,),
    )
    mem._conn.commit()
    return cid


def _cleanup_p1(mem, cids):
    # [8/6 plan §10] 后端感知清理 (helper 先 _index.remove 再 DELETE chunks)
    from helpers import cleanup_chunks

    cleanup_chunks(mem, chunk_ids=list(set(cids)))
    mem._conn.execute("DELETE FROM relations WHERE id LIKE 'rel_c_%' OR id LIKE 'rel_b_%' OR id LIKE 'rel_a_%'")
    mem._conn.execute("DELETE FROM entities WHERE id LIKE 'p1_test_entity_%'")
    mem._conn.commit()


def test_p1_scan_returns_candidate_for_high_recall_count():
    """[P1 §2.3] recall_count ≥20 → 候选."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="high_recall")
    try:
        result = mem._run_promote_pass(run_id="test_p1_recall")
        candidates = result["candidates"]
        assert any(c["chunk_id"] == cid for c in candidates), f"recall_count=25 应入选, got {candidates}"
    finally:
        _cleanup_p1(mem, [cid])
        mem.close()


def test_p1_scan_returns_candidate_for_high_ref_degree():
    """[P1 §2.3] ref_degree ≥10 → 候选."""
    mem = _new_mem()
    cid = _make_high_ref_chunk(mem, suffix="high_ref")
    try:
        result = mem._run_promote_pass(run_id="test_p1_ref")
        candidates = result["candidates"]
        assert any(c["chunk_id"] == cid for c in candidates), f"ref_degree=12 应入选, got {candidates}"
    finally:
        _cleanup_p1(mem, [cid])
        mem.close()


def test_p1_scan_returns_candidate_for_long_high_importance():
    """[P1 §2.3] 长期 (90d) + importance ≥0.8 → 候选."""
    mem = _new_mem()
    cid = _make_long_high_imp_chunk(mem, suffix="long_imp")
    try:
        result = mem._run_promote_pass(run_id="test_p1_long_imp")
        candidates = result["candidates"]
        assert any(c["chunk_id"] == cid for c in candidates), f"长期 importance=0.9 应入选, got {candidates}"
    finally:
        _cleanup_p1(mem, [cid])
        mem.close()


def test_p1_scan_excludes_non_fact_chunks():
    """[P1 §2.1] preference/episode/decision 不晋升 — 只晋升 fact."""
    mem = _new_mem()
    cid_pref = mem.remember(
        content="p1 test preference",
        source="p1_test",
        memory_type="preference",
        importance=0.9,
    )
    mem._conn.execute(
        "UPDATE chunks SET recall_count = 25, timestamp = datetime('now', '-100 days') WHERE id = ?",
        (cid_pref,),
    )
    mem._conn.commit()
    try:
        result = mem._run_promote_pass(run_id="test_p1_preference")
        candidates = result["candidates"]
        assert not any(c["chunk_id"] == cid_pref for c in candidates), f"preference 不应晋升, got {candidates}"
    finally:
        _cleanup_p1(mem, [cid_pref])
        mem.close()


def test_p1_scan_empty_returns_valid_shape():
    """[P1 §2.3] 无候选或候选空 → result 仍含 candidates: list 不抛."""
    mem = _new_mem()
    try:
        result = mem._run_promote_pass(run_id="test_p1_empty")
        assert "candidates" in result
        assert isinstance(result["candidates"], list)
    finally:
        mem.close()


def test_p1_candidate_includes_signal_breakdown():
    """[P1 §2.3] 候选项应含 signals dict 供审计日志记录 (recall_count / ref_degree / long_imp)."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="breakdown")
    try:
        result = mem._run_promote_pass(run_id="test_p1_breakdown")
        candidate = next(
            (c for c in result["candidates"] if c["chunk_id"] == cid),
            None,
        )
        assert candidate is not None, "测试 chunk 应入选"
        assert "signals" in candidate, f"候选必须含 signals 字段, got {candidate}"
        assert isinstance(candidate["signals"], dict)
        signals = candidate["signals"]
        assert "recall_count" in signals, f"signals 应含 recall_count, got {signals}"
        assert signals["recall_count"] >= 20
    finally:
        _cleanup_p1(mem, [cid])
        mem.close()
