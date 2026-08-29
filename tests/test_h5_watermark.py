"""H5 — watermark + 原子性 (DESIGN §5.9.2 + TASKS_L2_HYGIENE §3 H5).

设计验收:
- meta.l2.last_run.hygiene 推进: pass 内全部 proposal 处理完才更新; 异常中止不推进
- 每 proposal 一事务; 失败标 skipped 继续; 返回 {applied, skipped, failed}
- 跑两次 → 第二次无副作用 (幂等)
- 中途人为异常 → watermark 不推进, 失败项下次重试

测试用 live DB + cleanup (同 test_h4_purge_candidate.py 策略).
"""

from datetime import datetime
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


def _cleanup(mem, ids, source="h5_test"):
    # [8/6 plan §10] 后端感知清理 (helper 先 _index.remove 再 DELETE chunks)
    from helpers import cleanup_chunks

    cleanup_chunks(mem, chunk_ids=list(set(ids)))
    # 顺手清掉 source 全量的孤儿 (跨测试类积累)
    cleanup_chunks(mem, source=source)
    mem._conn.execute(
        "DELETE FROM audit_log WHERE after_json LIKE ?",
        (f'%"{source}-%',),
    )
    mem._conn.execute(
        "DELETE FROM purged_queue WHERE target_id IN (SELECT id FROM chunks WHERE source = ?)",
        (source,),
    )
    mem._conn.commit()


def _insert_old_ephemeral(mem, suffix="a"):
    cid = mem.remember(
        content=f"H5-test-{suffix}",
        source="h5_test",
        memory_type="ephemeral",
        importance=0.5,
    )
    mem._conn.execute(
        "UPDATE chunks SET timestamp = datetime('now', '-30 days') WHERE id = ?",
        (cid,),
    )
    mem._conn.commit()
    return cid


def test_h5_watermark_advances_on_clean_run():
    import time as _t

    mem = _new_mem()
    cid = _insert_old_ephemeral(mem, "wm")
    try:
        before = mem._l2_get("l2.last_run.hygiene")
        # 确保时间戳能前进 (run_maintenance 用 ms 精度)
        _t.sleep(1.05)
        result = mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=True,
        )
        after = mem._l2_get("l2.last_run.hygiene")
        assert result["applied"] >= 1, f"应有 applied > 0, got {result['applied']}"
        assert result["failed"] == 0, f"无失败时 failed 必须 = 0, got {result['failed']}"
        assert "hygiene" in result["watermark_updated"], "成功 pass 应推进 watermark"
        assert after is not None and after != before, f"watermark 必须改变: {before} → {after}"
    finally:
        _cleanup(mem, [cid])
        mem.close()


def test_h5_watermark_holds_on_failure():
    """[H5 验收] 中途人为异常 → watermark 不推进."""
    mem = _new_mem()
    cid = _insert_old_ephemeral(mem, "fail")

    # 在 apply 路径注入 RuntimeError — 用 monkeypatch
    original_apply = mem._apply_ttl_soft_delete

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure for H5 test")

    mem._apply_ttl_soft_delete = boom
    try:
        before = mem._l2_get("l2.last_run.hygiene")
        try:
            mem.run_maintenance(
                passes=["hygiene"],
                dry_run=False,
                confirm_destructive=True,
            )
        except RuntimeError:
            pass  # expected
        after = mem._l2_get("l2.last_run.hygiene")
        assert after == before, f"异常中止后 watermark 不能推进: {before} → {after}"
    finally:
        mem._apply_ttl_soft_delete = original_apply
        _cleanup(mem, [cid])
        mem.close()


def test_h5_idempotency_second_run_no_side_effects():
    """[H5 验收] 跑两次 → 第二次无副作用 (applied=0, watermark 不变)."""
    mem = _new_mem()
    cids = [_insert_old_ephemeral(mem, "idem1"), _insert_old_ephemeral(mem, "idem2")]
    try:
        first = mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=True,
        )
        first_applied = first["applied"]
        assert first_applied >= 1, f"first run 必须 applied >= 1, got {first_applied}"
        first_wm = mem._l2_get("l2.last_run.hygiene")

        second = mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=True,
        )
        assert second["applied"] == 0, f"second run 已被 soft-delete 的 chunk 不能被再次 applied, got {second['applied']}"
        second_wm = mem._l2_get("l2.last_run.hygiene")
        # watermark 可以推 (idempotent 软写), 但 applied 必须 0
        assert second_wm == first_wm, f"无 applied 时 watermark 必须不变, got {first_wm} → {second_wm}"
    finally:
        _cleanup(mem, cids)
        mem.close()


def test_h5_apply_failure_rolls_back_half_state():
    """[H5 P0] apply 抛异常时已 UPDATE 的 chunks.valid_until + purged_queue 行都必须 rollback."""
    mem = _new_mem()
    cid = _insert_old_ephemeral(mem, "rollback")
    try:
        original_apply = mem._apply_ttl_soft_delete

        def half_then_fail(*args, **kwargs):
            # _apply_ttl_soft_delete 走真实路径, 但在 audit_log applied INSERT 前 raise
            # (模拟 audit_log 写失败 — PK 冲突/磁盘满).
            run_id, chunk_id, mtype, before, after, revert_sql, ts = args
            mem._exec_clean(
                "UPDATE chunks SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
                (ts, chunk_id),
            )
            from datetime import timedelta as _td

            mem._exec_clean(
                "INSERT INTO purged_queue (target_id, target_kind, purged_at, done) VALUES (?, 'chunk', ?, 0)",
                (chunk_id, (datetime.now() + _td(days=30)).strftime("%Y-%m-%dT%H:%M:%S")),
            )
            raise RuntimeError("simulated audit_log INSERT failure")

        mem._apply_ttl_soft_delete = half_then_fail
        result = mem.run_maintenance(
            passes=["hygiene"],
            dry_run=False,
            confirm_destructive=True,
        )
        # [P0 验收] rollback 必须发生
        valid_until = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid,)).fetchone()["valid_until"]
        assert valid_until is None, f"P0: valid_until 必须被 rollback, got {valid_until}"
        queue_rows = mem._conn.execute("SELECT COUNT(*) FROM purged_queue WHERE target_id = ?", (cid,)).fetchone()[0]
        assert queue_rows == 0, f"P0: purged_queue 必须被 rollback, got {queue_rows}"
        assert result["failed"] == 1, "该 proposal 必须计入 failed"
    finally:
        mem._apply_ttl_soft_delete = original_apply
        _cleanup(mem, [cid])
        mem.close()
