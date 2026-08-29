"""H4 — purge_candidate 生成 (非破坏, dry-run 报告).

设计文档 TASKS_L2_HYGIENE §3 H4:
- 衰减到 floor 且 TTL 过期的项 → 写入报告 `purge_candidates` 列表 (不自动删)
- 真正物理删除: confirm_destructive=True → 走 purged_queue (30 天延迟)
- 验收: dry-run 只报告不删; confirm 后入 purged_queue 而非直接 DELETE

测试用 live DB + 严格 cleanup, 跟 tests/test_digest.py 同策略.
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


def _cleanup(mem, ids, purged_target_ids=()):
    # [8/6 plan §10] 后端感知清理 (helper 先 _index.remove 再 DELETE chunks)
    from helpers import cleanup_chunks

    cleanup_chunks(mem, chunk_ids=list(set(ids)))
    # 清掉测试产生的 audit_log 行 (按 content/source 锚定)
    for cid in purged_target_ids:
        mem._conn.execute("DELETE FROM purged_queue WHERE target_id = ?", (cid,))
    mem._conn.execute("DELETE FROM audit_log WHERE after_json LIKE '%\"H4-test-old-ephemeral\"%'")
    mem._conn.commit()


def _insert_old_ephemeral(mem):
    cid = mem.remember(
        content="H4-test-old-ephemeral",
        source="h4_test",
        memory_type="ephemeral",
        importance=0.5,
    )
    # 强制 timestamp 是 30 天前 → 触发 TTL 7d 过期
    mem._conn.execute(
        "UPDATE chunks SET timestamp = datetime('now', '-30 days') WHERE id = ?",
        (cid,),
    )
    mem._conn.commit()
    return cid


def test_h4_dry_run_emits_purge_candidates_list():
    mem = _new_mem()
    cid = _insert_old_ephemeral(mem)
    try:
        result = mem.run_maintenance(passes=["hygiene"], dry_run=True, confirm_destructive=False)
        assert "purge_candidates" in result, "顶层结果必须含 purge_candidates 聚合字段"
        assert isinstance(result["purge_candidates"], list)
        assert any(c.get("ref_id") == cid for c in result["purge_candidates"]), f"TTL 过期 ephemeral {cid} 应进入 purge_candidates 报告"
    finally:
        _cleanup(mem, [cid], [cid])
        mem.close()


def test_h4_dry_run_does_not_physically_delete():
    mem = _new_mem()
    cid = _insert_old_ephemeral(mem)
    try:
        before = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid,)).fetchone()
        assert before["valid_until"] is None
        mem.run_maintenance(passes=["hygiene"], dry_run=True, confirm_destructive=False)
        after = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid,)).fetchone()
        assert after["valid_until"] is None, "dry-run 不能改 valid_until"
        queue_rows = mem._conn.execute("SELECT COUNT(*) FROM purged_queue WHERE target_id = ?", (cid,)).fetchone()[0]
        assert queue_rows == 0, "dry-run 不能入 purged_queue"
    finally:
        _cleanup(mem, [cid], [cid])
        mem.close()


def test_h4_stats_expose_purge_candidate_count():
    mem = _new_mem()
    # 30 天前的 ephemeral (TTL 7d) — 更接近实际场景 (95% 的真 purge 是 ephemeral)
    cid = _insert_old_ephemeral(mem)
    try:
        s = mem.stats()["hygiene"]
        assert "purge_candidates" in s, "stats()['hygiene'] 必须含 purge_candidates"
        assert s["purge_candidates"] >= 1, f"30d ephemeral 应被计入, got {s['purge_candidates']}"
    finally:
        _cleanup(mem, [cid], [cid])
        mem.close()


def test_h4_confirm_destructive_enqueues_purged_queue():
    mem = _new_mem()
    cid = _insert_old_ephemeral(mem)
    try:
        mem.run_maintenance(passes=["hygiene"], dry_run=False, confirm_destructive=True)
        rows = mem._conn.execute(
            "SELECT target_id, target_kind, done FROM purged_queue WHERE target_id = ?",
            (cid,),
        ).fetchall()
        assert len(rows) == 1, f"confirm 后应入 purged_queue, got {len(rows)} rows"
        assert rows[0]["done"] == 0
    finally:
        _cleanup(mem, [cid], [cid])
        mem.close()
