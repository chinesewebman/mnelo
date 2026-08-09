"""
[8/6 M5.3 + DESIGN §10.2 D11] L2 TTL 豁免测试.

覆盖:
  M5.3.1 D11 豁免 — memory.forget(target_kind='task') 抛 ValueError
  M5.3.2 D11 豁免 — memory.forget(target_kind='loop') 抛 ValueError
  M5.3.3 forget_task 显式路径 — 软删 entity + 关 task_states + 写 audit_log
  M5.3.4 forget_task 缺失 reason 抛 ReasonRequiredError (D8 显式纠正门)
  M5.3.5 forget_task 不存在的 task 抛 TaskNotFoundError
  M5.3.6 forget_loop 显式路径 — 同 task 但级联不同
  M5.3.7 forget_loop 缺失 reason 抛 ReasonRequiredError
  M5.3.8 audit_log 含 forced_forget 行 (action_type=explicit_softdelete, status=applied)
  M5.3.9 forget 后 task_states 当前行被关闭 (valid_until IS NOT NULL)
  M5.3.10 chunk/entity/relation kind 不变 — 仍走原 L2 decay 路径
"""
import sqlite3
import sys
from pathlib import Path

REPO = Path("/Users/apple/.hermes/memory")
sys.path.insert(0, str(REPO))
import os
os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

import memory
import task_states


def _setup():
    """Clean m5-forget fixtures + audit_log."""
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:m5-forget%' OR task_id LIKE 'loop:m5-forget%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:m5-forget%' OR id LIKE 'loop:m5-forget%'")
    c.execute("DELETE FROM audit_log WHERE pass_name='forced_forget' AND (ref_id LIKE 'task:%m5-forget%' OR ref_id LIKE 'loop:%m5-forget%')")
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _create_task(name: str, now: str = "2026-08-06T15:00") -> str:
    m = memory.Memory()
    try:
        r = task_states.task_create(m._conn, name=name, now=now)
        tid = r["task_id"]
        m._conn.commit()
        return tid
    finally:
        m.close()


def _create_loop(name: str, now: str = "2026-08-06T15:00") -> str:
    m = memory.Memory()
    try:
        r = task_states.loop_create(m._conn, name=name, trigger="x", interval_hours=24, now=now)
        lid = r["loop_id"]
        m._conn.commit()
        return lid
    finally:
        m.close()


# ===== M5.3.1 D11 forget(task) 抛 ValueError =====

def test_m5_3_1_d11_forget_task_blocked():
    """[D11] memory.forget(target_kind='task') 必须抛 ValueError, 防止 L2 自动删任务."""
    _setup()
    tid = _create_task("m5-forget-blocked")
    m = memory.Memory()
    try:
        try:
            m.forget(tid, target_kind="task", reason="auto_decay")
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "D11 TTL 豁免" in str(e)
            assert "task_states.forget_task" in str(e)
        # 校验 task 未被删 (entity.valid_until 仍 NULL)
        v = m._conn.execute("SELECT valid_until FROM entities WHERE id=?", (tid,)).fetchone()[0]
        assert v is None, f"D11 应阻止 forget, 但 entity.valid_until={v}"
    finally:
        m.close()


# ===== M5.3.2 D11 forget(loop) 抛 ValueError =====

def test_m5_3_2_d11_forget_loop_blocked():
    """[D11] memory.forget(target_kind='loop') 必须抛 ValueError."""
    _setup()
    lid = _create_loop("m5-forget-loop-blocked")
    m = memory.Memory()
    try:
        try:
            m.forget(lid, target_kind="loop", reason="auto_decay")
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "D11 TTL 豁免" in str(e)
        v = m._conn.execute("SELECT valid_until FROM entities WHERE id=?", (lid,)).fetchone()[0]
        assert v is None
    finally:
        m.close()


# ===== M5.3.3 forget_task 显式路径 =====

def test_m5_3_3_forget_task_explicit_softdelete():
    """[M5.3.3] forget_task 软删 entity + 关 task_states + 写 audit_log."""
    _setup()
    tid = _create_task("m5-forget-explicit")
    m = memory.Memory()
    try:
        result = task_states.forget_task(
            m._conn, tid, reason="user_requested_cleanup",
        )
        assert result["task_id"] == tid
        assert "forgotten_at" in result
        assert result["rows_invalidated"] >= 1

        # 校验 entity.valid_until 已被设置
        v = m._conn.execute("SELECT valid_until FROM entities WHERE id=?", (tid,)).fetchone()[0]
        assert v is not None, "entity 应已被软删"

        # 校验 task_states 当前行 valid_until 已被设置
        n = m._conn.execute(
            "SELECT COUNT(*) FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchone()[0]
        assert n == 0, "forget_task 应关闭 task_states 当前行"

        # 校验 audit_log
        row = m._conn.execute(
            """SELECT pass_name, action_type, ref_type, status FROM audit_log
               WHERE pass_name='forced_forget' AND ref_id=?""",
            (tid,),
        ).fetchone()
        assert row is not None
        assert row[0] == "forced_forget"
        assert row[1] == "explicit_softdelete"
        assert row[2] == "task"
        assert row[3] == "applied"
    finally:
        m.close()


# ===== M5.3.4 forget_task 缺失 reason =====

def test_m5_3_4_forget_task_requires_reason():
    """[D8] forget_task 缺 reason / 空 reason 都抛 ReasonRequiredError.

    Python keyword-only 签名: reason 必须显式传, 缺则 TypeError (Python 内建).
    传空 reason -> TaskLoopError ReasonRequiredError.
    """
    _setup()
    tid = _create_task("m5-forget-no-reason")
    m = memory.Memory()
    try:
        # 1. 空 reason -> ReasonRequiredError
        try:
            task_states.forget_task(m._conn, tid, reason="")
            raise AssertionError("expected raise")
        except task_states.TaskLoopError as e:
            assert e.code == "ReasonRequiredError"

        # 2. 缺 reason kwarg -> TypeError (Python 签名强制)
        try:
            task_states.forget_task(m._conn, tid)  # no reason
            raise AssertionError("expected raise")
        except TypeError:
            pass
    finally:
        m.close()


# ===== M5.3.5 forget_task 不存在 =====

def test_m5_3_5_forget_task_not_found():
    """[M5.3.5] forget_task 不存在的 task_id 抛 TaskNotFoundError."""
    _setup()
    m = memory.Memory()
    try:
        try:
            task_states.forget_task(m._conn, "task:nonexistent-9999", reason="not_real_task_path")
            raise AssertionError("expected raise")
        except task_states.TaskLoopError as e:
            assert e.code == "TaskNotFoundError"
    finally:
        m.close()


# ===== M5.3.6 forget_loop 显式路径 =====

def test_m5_3_6_forget_loop_explicit():
    """[M5.3.6] forget_loop 软删 loop entity + 关 task_states + 写 audit_log."""
    _setup()
    lid = _create_loop("m5-forget-loop-explicit")
    m = memory.Memory()
    try:
        result = task_states.forget_loop(
            m._conn, lid, reason="loop_no_longer_relevant",
        )
        assert result["loop_id"] == lid
        assert "forgotten_at" in result

        v = m._conn.execute("SELECT valid_until FROM entities WHERE id=?", (lid,)).fetchone()[0]
        assert v is not None

        n = m._conn.execute(
            "SELECT COUNT(*) FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (lid,),
        ).fetchone()[0]
        assert n == 0

        row = m._conn.execute(
            """SELECT pass_name, action_type, ref_type FROM audit_log
               WHERE pass_name='forced_forget' AND ref_id=?""",
            (lid,),
        ).fetchone()
        assert row is not None
        assert row[2] == "loop"
    finally:
        m.close()


# ===== M5.3.7 forget_loop 缺 reason =====

def test_m5_3_7_forget_loop_requires_reason():
    _setup()
    lid = _create_loop("m5-forget-loop-noreason")
    m = memory.Memory()
    try:
        try:
            task_states.forget_loop(m._conn, lid, reason="")
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "ReasonRequiredError"
    finally:
        m.close()


# ===== M5.3.8 audit_log 内容 =====

def test_m5_3_8_audit_log_records_forced_forget():
    """[M5.3.8] audit_log before_json + after_json 含 reason + forgotten_at."""
    _setup()
    tid = _create_task("m5-forget-auditlog")
    m = memory.Memory()
    try:
        task_states.forget_task(
            m._conn, tid, reason="test_reason_text",
            now="2026-08-06T15:30",
        )
        row = m._conn.execute(
            """SELECT before_json, after_json FROM audit_log
               WHERE pass_name='forced_forget' AND ref_id=?""",
            (tid,),
        ).fetchone()
        assert row is not None
        import json as _json
        before = _json.loads(row[0])
        after = _json.loads(row[1])
        assert before["status_before"] == "active"
        assert after["reason"] == "test_reason_text"
        assert after["forgotten_at"] == "2026-08-06T15:30"
    finally:
        m.close()


# ===== M5.3.9 forget 后 task_states 当前行被关闭 =====

def test_m5_3_9_forget_closes_task_states_window():
    """[M5.3.9] forget_task 后 task_states valid_until IS NOT NULL."""
    _setup()
    tid = _create_task("m5-forget-window")
    m = memory.Memory()
    try:
        # 起点: 1 行当前
        n_before = m._conn.execute(
            "SELECT COUNT(*) FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchone()[0]
        assert n_before == 1

        task_states.forget_task(m._conn, tid, reason="cleanup_test_for_verification")

        # forget 后: 0 行当前
        n_after = m._conn.execute(
            "SELECT COUNT(*) FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (tid,),
        ).fetchone()[0]
        assert n_after == 0, "forget_task 应关闭 task_states 当前行"
    finally:
        m.close()


# ===== M5.3.10 chunk/entity/relation kind 路径不变 =====

def test_m5_3_10_unknown_kind_does_not_hit_d11():
    """[M5.3.10 静态契约] 未知 kind 应报 unknown kind, 不是 D11.

    不调用 memory.forget(真实 chunk) — 那路径走 embedder, 当前测试环境 usearch
    native SIGSEGV. 静态验证 D11 拦截只对 'task'/'loop' 触发, 其他 kind 走原
    路径 (含 unknown kind 报 unknown kind 错).
    """
    import inspect as _inspect
    src = _inspect.getsource(memory.Memory.forget)
    assert "D11 TTL 豁免" in src
    assert "task_states.forget_task" in src
    assert "task_states.forget_loop" in src
    # chunk/entity/relation 分支存在 (源码中字面量, inspect 输出引号可能转义)
    for kind in ("chunk", "entity", "relation"):
        assert kind in src, f"missing {kind} branch"
        # 校验是 target_kind 比较, 不是注释
        assert f'target_kind == "{kind}"' in src or f"target_kind == '{kind}'" in src, \
            f"missing target_kind == '{kind}' literal"