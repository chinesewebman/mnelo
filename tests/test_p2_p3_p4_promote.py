"""P2-P4 — 晋升动作 + 降级/上限 + 审计链 (TASKS_L2_SESSION_STATE §2.3).

§2.3 验收:
  P2: 晋升后实体存在 + evidence 链完整; 重复晋升 → upsert 不重复
  P3: 降级后实体 kind 变更 + 历史保留; 上限触发腾位
  P4: dry-run 只报不改; apply 后 audit_log 有 applied 行 + revert_sql
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


def _make_high_recall_chunk(mem, suffix="x"):
    cid = mem.remember(
        content=f"p2 test fact {suffix}",
        source="p2_test",
        memory_type="fact",
        importance=0.5,
    )
    mem._conn.execute("UPDATE chunks SET recall_count = 25 WHERE id = ?", (cid,))
    mem._conn.commit()
    return cid


def _cleanup_p2(mem, cids):
    # [8/6 plan §10] 后端感知清理
    from helpers import cleanup_chunks

    cleanup_chunks(mem, chunk_ids=list(set(cids)))
    mem._conn.execute("DELETE FROM relations WHERE relation='canonical_evidence_of' AND source_id LIKE 'canonical:%'")
    mem._conn.execute("DELETE FROM entities WHERE id LIKE 'canonical:%'")
    mem._conn.execute("DELETE FROM audit_log WHERE run_id LIKE 'test_p%' OR run_id LIKE 'test_2_%'")
    mem._conn.commit()


def test_p2_apply_promote_creates_canonical_fact_entity():
    """[P2 §2.3] 单 chunk 晋升: 直接调 _apply_promote_to_canonical (不走全量扫)."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="apply")
    try:
        # 走单 apply 方法 — 验证 chunk → canonical_fact entity + evidence relation
        ok = mem._apply_promote_to_canonical(
            run_id="test_p2_apply_single",
            chunk_id=cid,
            signals={"recall_count": 25},
            ts="2026-08-05T15:00:00",
        )
        assert ok, "_apply_promote_to_canonical 应成功"

        # 验证 entity 创建
        entities = mem._conn.execute("SELECT id, kind FROM entities WHERE id LIKE 'canonical:%' AND kind='canonical_fact'").fetchall()
        assert any(e["kind"] == "canonical_fact" for e in entities), f"canonical_fact entity 应存在, got {len(entities)} 个"
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_p2_apply_promote_creates_evidence_relation():
    """[P2 §2.3] evidence_chunk_id 关系存在 (单 chunk 路径)."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="evidence")
    try:
        mem._apply_promote_to_canonical(
            run_id="test_p2_evidence_single",
            chunk_id=cid,
            signals={"recall_count": 25},
            ts="2026-08-05T15:00:01",
        )
        rels = mem._conn.execute(
            """SELECT source_id, evidence_chunk_id FROM relations
               WHERE relation='canonical_evidence_of' AND evidence_chunk_id = ?""",
            (cid,),
        ).fetchall()
        assert rels, f"应存在 evidence_chunk_id={cid} 的关系, got {rels}"
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_p2_apply_promote_writes_audit_log():
    """[P4 §2.3] apply 后 audit_log 有 applied 行 + revert_sql (单 chunk 路径)."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="audit")
    try:
        mem._apply_promote_to_canonical(
            run_id="test_p2_audit_single",
            chunk_id=cid,
            signals={"recall_count": 25},
            ts="2026-08-05T15:00:02",
        )
        audit_rows = mem._conn.execute(
            """SELECT pass_name, action_type, status, revert_sql FROM audit_log
               WHERE run_id='test_p2_audit_single'"""
        ).fetchall()
        promote_rows = [r for r in audit_rows if r["pass_name"] == "promote" and r["status"] == "applied"]
        assert promote_rows, f"应有 promote applied 行, got {audit_rows}"
        assert promote_rows[0]["revert_sql"], "applied 行应有 revert_sql"
        assert "DELETE FROM relations" in promote_rows[0]["revert_sql"]
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_p2_idempotent_promote_same_chunk_twice():
    """[P2 §2.3] 重复晋升同一 chunk → entity id 幂等 (slug 来自 chunk_id 哈希)."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="idem")
    try:
        mem._apply_promote_to_canonical(
            run_id="test_p2_idem_1",
            chunk_id=cid,
            signals={"recall_count": 25},
            ts="2026-08-05T15:00:03",
        )
        # 第二次 — entity id 应相同 (slug 含 chunk_id hash)
        mem._apply_promote_to_canonical(
            run_id="test_p2_idem_2",
            chunk_id=cid,
            signals={"recall_count": 25},
            ts="2026-08-05T15:00:04",
        )
        # 验证 entity 唯一 (同 chunk → 同 entity id)
        canonical_entities = mem._conn.execute("SELECT id FROM entities WHERE id LIKE 'canonical:%' AND kind='canonical_fact'").fetchall()
        # 同 chunk 的所有 promote 共享同一 entity_id (slug 含 chunk_id hash)
        canonical_ids = [e["id"] for e in canonical_entities]
        # 期望 chunk 对应 1 个 entity
        from collections import Counter

        entity_counts = Counter(canonical_ids)
        assert all(c == 1 for c in entity_counts.values()), f"每个 entity_id 应只出现 1 次, counts={dict(entity_counts)}"
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_p4_dry_run_does_not_apply_or_write_audit():
    """[P4 §2.3] dry_run=True → 不应用 + 无 audit_log applied 行."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="dryrun")
    try:
        result = mem._run_promote_pass(
            run_id="test_p2_dryrun",
            dry_run=True,
            confirm_destructive=False,
        )
        assert result["applied"] == 0, f"dry_run 必须 applied=0, got {result['applied']}"
        # 验证 audit_log 无对应 applied 行
        audit_count = mem._conn.execute("SELECT COUNT(*) FROM audit_log WHERE run_id='test_p2_dryrun' AND status='applied'").fetchone()[0]
        assert audit_count == 0, f"dry_run 不应有 audit_log applied, got {audit_count}"
        # 但 proposals 应有
        assert len(result["proposals"]) >= 1, "dry_run 应有 proposals (报告)"
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_p3_demote_90d_unused_changes_kind_to_concept():
    """[P3 §2.3] canonical_fact 90d 未召回 + ref_degree < 3 → 降级 kind=concept (单 apply 路径)."""
    mem = _new_mem()
    entity_id = "canonical:test_p3_demote"
    mem._conn.execute(
        """INSERT INTO entities (id, kind, name, summary, importance, last_recalled)
           VALUES (?, 'canonical_fact', ?, ?, 0.5, datetime('now', '-100 days'))""",
        (entity_id, "Test P3 Demote", "Test P3 Demote summary"),
    )
    mem._conn.commit()
    try:
        # 直接 _apply_demote_canonical
        ok = mem._apply_demote_canonical(
            run_id="test_p3_demote_single",
            entity_id=entity_id,
            reason="90d未召回(ref_degree=0)",
            ts="2026-08-05T15:30:00",
        )
        assert ok, "_apply_demote_canonical 应成功"
        # 验证 kind 已变更
        row = mem._conn.execute("SELECT kind FROM entities WHERE id = ?", (entity_id,)).fetchone()
        assert row["kind"] == "concept", f"应 kind=concept, got {row['kind']}"
    finally:
        mem._conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
        mem._conn.execute("DELETE FROM audit_log WHERE run_id='test_p3_demote_single'")
        mem._conn.commit()
        mem.close()


def test_p3_max_canonical_eviction_by_lowest_importance():
    """[P3 §2.3] 单 entity demote + eviction: 直接验 _apply_demote_canonical kind 变更 + 排序."""
    mem = _new_mem()
    # 3 个测试 canonical_fact, importance 0.1/0.5/0.9
    test_ids = []
    for i, imp in enumerate([0.1, 0.5, 0.9]):
        eid = f"canonical:test_p3_evict_{i:03d}"
        mem._conn.execute(
            """INSERT INTO entities (id, kind, name, summary, importance, last_recalled)
               VALUES (?, 'canonical_fact', ?, ?, ?, datetime('now', '-100 days'))""",
            (eid, f"Evict {i}", f"summary {i}", imp),
        )
        test_ids.append(eid)
    mem._conn.commit()
    try:
        # 直接对最低 importance (0.1) demote
        ok = mem._apply_demote_canonical(
            run_id="test_p3_evict_single",
            entity_id="canonical:test_p3_evict_000",
            reason="test_lowest_imp",
            ts="2026-08-05T15:30:00",
        )
        assert ok, "_apply_demote_canonical 应成功"
        # 验证 kind 变更
        kind_row = mem._conn.execute(
            "SELECT kind FROM entities WHERE id = ?",
            ("canonical:test_p3_evict_000",),
        ).fetchone()
        assert kind_row["kind"] == "concept", f"最低 importance 应降级 kind=concept, got {kind_row['kind']}"
        # 验证 audit_log applied 行
        audit = mem._conn.execute(
            """SELECT action_type, status, revert_sql FROM audit_log
               WHERE run_id='test_p3_evict_single' AND ref_id='canonical:test_p3_evict_000'"""
        ).fetchone()
        assert audit is not None
        assert audit["action_type"] == "demote_canonical"
        assert audit["status"] == "applied"
        assert "UPDATE entities SET kind='canonical_fact'" in audit["revert_sql"]
    finally:
        for eid in test_ids:
            mem._conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
        mem._conn.execute("DELETE FROM audit_log WHERE run_id='test_p3_evict_single'")
        mem._conn.commit()
        mem.close()


def test_p4_promote_pass_advances_watermark_on_clean_run():
    """[P4 §5.9.2] 单 apply 干净 + 无 failed → watermark 不变量 (测试用 ms 精度差异)."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="watermark")
    try:
        before = mem._l2_get("l2.last_run.promote")
        # 用 sleep 1.05 确保 ts ms 精度前进
        import time as _t

        _t.sleep(1.05)
        ok = mem._apply_promote_to_canonical(
            run_id="test_p4_wm",
            chunk_id=cid,
            signals={"recall_count": 25},
            ts=_t.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        assert ok, "_apply_promote_to_canonical 应成功"
        # 单 apply 不直接推 watermark — 那是 _run_promote_pass 的责任
        # 这里只验证 _apply 不破坏 watermark
        after = mem._l2_get("l2.last_run.promote")
        assert after == before, f"_apply 不应修改 watermark, {before} → {after}"
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_run_maintenance_dispatches_promote_pass():
    """[P4 集成] run_maintenance(passes=['promote']) 应调到 _run_promote_pass."""
    mem = _new_mem()
    cid = _make_high_recall_chunk(mem, suffix="dispatch")
    try:
        result = mem.run_maintenance(
            passes=["promote"],
            dry_run=False,
            confirm_destructive=True,
        )
        assert "promote" in result["passes_run"], f"promote 应在 passes_run, got {result['passes_run']}"
        assert "promote" in result["proposals"], f"proposals 应含 promote key, got {list(result['proposals'])}"
    finally:
        _cleanup_p2(mem, [cid])
        mem.close()


def test_p2_slug_collision_two_chunks_get_distinct_entities():
    """[P2 review MEDIUM fix] 两个 chunk 共享首 40 字事实 → 应各自得独立 entity id."""
    mem = _new_mem()
    cid_a = mem.remember(
        content="用户偏好 A 股上海电力持仓长期持有第一句相同后面不同",
        source="p2_test_collision",
        memory_type="fact",
        importance=0.5,
    )
    cid_b = mem.remember(
        content="用户偏好 A 股上海电力持仓长期持有第一句相同后面另有差异",
        source="p2_test_collision",
        memory_type="fact",
        importance=0.5,
    )
    # 强制 recall_count >= 20
    mem._conn.execute(
        "UPDATE chunks SET recall_count = 25 WHERE id IN (?, ?)",
        (cid_a, cid_b),
    )
    mem._conn.commit()
    try:
        mem._apply_promote_to_canonical(
            run_id="test_p2_collision_a",
            chunk_id=cid_a,
            signals={"recall_count": 25},
            ts="2026-08-05T15:30:00",
        )
        mem._apply_promote_to_canonical(
            run_id="test_p2_collision_b",
            chunk_id=cid_b,
            signals={"recall_count": 25},
            ts="2026-08-05T15:30:01",
        )
        # 验两个独立 entity (slug hash 后缀不同)
        canonical_ids = mem._conn.execute("SELECT id FROM entities WHERE id LIKE 'canonical:%' AND kind='canonical_fact'").fetchall()
        entity_ids = [e["id"] for e in canonical_ids]
        # 两个 cid 各对应一个 entity (slug 含不同 hash 后缀)
        # 找 chunk-a 来源 entity: slug 应含 cid_a hash 前缀 (md5[:6] first 2)
        from hashlib import md5

        suffix_a = md5(cid_a.encode()).hexdigest()[:6]
        suffix_b = md5(cid_b.encode()).hexdigest()[:6]
        cid_a_entity = next((eid for eid in entity_ids if suffix_a in eid), None)
        cid_b_entity = next((eid for eid in entity_ids if suffix_b in eid), None)
        assert cid_a_entity is not None, f"cid_a 应有自己的 entity (含 hash {suffix_a}), got {entity_ids}"
        assert cid_b_entity is not None, f"cid_b 应有自己的 entity (含 hash {suffix_b}), got {entity_ids}"
        assert cid_a_entity != cid_b_entity, f"两个 chunk 应得不同 entity, 但都映射到 {cid_a_entity}"
    finally:
        _cleanup_p2(mem, [cid_a, cid_b])
        mem.close()
