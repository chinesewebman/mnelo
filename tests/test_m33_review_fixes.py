"""
[8/6 M33 多角度持续优化] 3 fix tests.

覆盖:
  M33.1 [race] forget_task/forget_loop 并发安全 - BEGIN IMMEDIATE 事务
       双线程同时 forget 同 task 应只 1 成功, audit_log 只 1 行
  M33.2 [validation] forget_task/forget_loop reason 强校验 - strip +
       non-empty + min length 5
  M33.3 [whitespace] reason='   ' (纯空白) 应抛 ReasonRequiredError
"""
import sys
import sqlite3
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import task_states
import memory

FAILED_PREFIXES = (
    'task:%m33%', 'loop:%m33%'
)


def _setup():
    """Clean m33 fixture prefix."""
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute(
        "DELETE FROM task_states WHERE task_id LIKE 'task:%m33-%' OR task_id LIKE 'loop:%m33-%'"
    )
    c.execute(
        "DELETE FROM entities WHERE id LIKE 'task:%m33-%' OR id LIKE 'loop:%m33-%'"
    )
    c.execute(
        "DELETE FROM audit_log WHERE ref_id LIKE 'task:%m33-%' OR ref_id LIKE 'loop:%m33-%'"
    )
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()


def _create_task(name: str) -> str:
    c = sqlite3.connect(str(memory.DB_PATH))
    r = task_states.task_create(c, name=name, now="2026-08-06T09:00")
    tid = r["task_id"]
    c.commit()
    c.close()
    return tid


def _create_loop(name: str) -> str:
    c = sqlite3.connect(str(memory.DB_PATH))
    r = task_states.loop_create(c, name=name, trigger="x", enabled=True, now="2026-08-06T09:00")
    lid = r["loop_id"]
    c.commit()
    c.close()
    return lid


# ===== M33.2 [validation] tests =====

def test_m33_2_forget_task_reason_required_rejects_empty():
    """[M33.2a] forget_task reason='' 抛 ReasonRequiredError."""
    _setup()
    tid = _create_task("m33-reason-empty")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_task(c, tid, reason="")
            raise AssertionError("应抛 TaskLoopError")
        except task_states.TaskLoopError as e:
            assert e.code == "ReasonRequiredError", f"got {e.code}"
    finally:
        c.close()


def test_m33_2_forget_task_reason_rejects_whitespace_only():
    """[M33.2b] reason='   ' (纯空白) 抛 ReasonRequiredError (strip + check)."""
    _setup()
    tid = _create_task("m33-reason-whitespace")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_task(c, tid, reason="   ")
            raise AssertionError("纯空白 reason 应抛错")
        except task_states.TaskLoopError as e:
            assert e.code == "ReasonRequiredError", f"got {e.code}"
    finally:
        c.close()


def test_m33_3_forget_task_reason_too_short():
    """[M33.3] reason 长度 < 5 抛 ReasonTooShortError (审计可读)."""
    _setup()
    tid = _create_task("m33-reason-short")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_task(c, tid, reason="abc")
            raise AssertionError("长度 3 应抛错")
        except task_states.TaskLoopError as e:
            assert e.code == "ReasonTooShortError", f"got {e.code}"
    finally:
        c.close()


def test_m33_3b_forget_loop_reason_too_short():
    """[M33.3] forget_loop 同校验."""
    _setup()
    lid = _create_loop("m33-loop-short")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_loop(c, lid, reason="x")
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "ReasonTooShortError"
    finally:
        c.close()


def test_m33_3c_forget_task_reason_min_length_5_ok():
    """[M33.3 正向] reason='valid_reason_5' 长度>=5 应成功."""
    _setup()
    tid = _create_task("m33-reason-ok")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        result = task_states.forget_task(
            c, tid, reason="valid_reason_for_audit_log", now="2026-08-06T15:00"
        )
        assert result["task_id"] == tid
        # after_json.reason 应存 stripped 值
        row = c.execute(
            """SELECT after_json FROM audit_log
               WHERE pass_name='forced_forget' AND ref_id=? ORDER BY id DESC LIMIT 1""",
            (tid,),
        ).fetchone()
        import json as _json
        after = _json.loads(row[0])
        assert after["reason"] == "valid_reason_for_audit_log"
    finally:
        c.close()


# ===== M33.1 [race] concurrent tests =====

def test_m33_1_concurrent_forget_task_only_one_succeeds():
    """[M33.1] 真并发 (双连接) forget 同 task_id, 应只 1 成功 + 1 抛 TaskAlreadyForgotten.

    SQLite3 Connection 是 thread-local - 子线程必须自己 connect.
    旧实现 check + UPDATE 非原子 - 两 agent 都过 check + 双 UPDATE, audit_log
    重复污染. 修复: BEGIN IMMEDIATE 序列化第二个拿锁者, 见 row[0] IS NOT NULL.
    """
    _setup()
    tid = _create_task("m33-race-forget")

    results = {"a": None, "b": None}
    errors = {"a": None, "b": None}

    def worker(name: str):
        conn = sqlite3.connect(str(memory.DB_PATH), timeout=30)
        try:
            time.sleep(0.01)
            r = task_states.forget_task(
                conn, tid, reason=f"concurrent_forget_from_{name}",
            )
            results[name] = r
        except task_states.TaskLoopError as e:
            errors[name] = e.code
        finally:
            conn.close()

    ta = threading.Thread(target=worker, args=("a",))
    tb = threading.Thread(target=worker, args=("b",))
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    # 期望: 1 成功 + 1 抛 TaskAlreadyForgotten
    ok_count = sum(1 for k in ("a", "b") if results[k] is not None)
    err_count = sum(1 for k in ("a", "b") if errors[k] is not None)
    assert ok_count == 1, f"应 1 成功, got {ok_count} ({results}, {errors})"
    assert err_count == 1, f"应 1 抛错, got {err_count}"
    assert errors["a"] == "TaskAlreadyForgotten" or errors["b"] == "TaskAlreadyForgotten"

    # 校验 audit_log 只 1 行 forced_forget/explicit_softdelete/applied
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        n = c.execute(
            """SELECT COUNT(*) FROM audit_log
               WHERE pass_name='forced_forget' AND action_type='explicit_softdelete'
                 AND ref_id=? AND status='applied'""",
            (tid,),
        ).fetchone()[0]
        assert n == 1, f"应 1 行 audit_log, got {n}"
    finally:
        c.close()


def test_m33_1b_concurrent_forget_loop_only_one_succeeds():
    """[M33.1] forget_loop 并发安全 — 同 task 版."""
    _setup()
    lid = _create_loop("m33-race-forget-loop")

    results = {"a": None, "b": None}
    errors = {"a": None, "b": None}

    def worker(name: str):
        conn = sqlite3.connect(str(memory.DB_PATH), timeout=30)
        try:
            time.sleep(0.01)
            r = task_states.forget_loop(
                conn, lid, reason=f"concurrent_forget_loop_from_{name}",
            )
            results[name] = r
        except task_states.TaskLoopError as e:
            errors[name] = e.code
        finally:
            conn.close()

    ta = threading.Thread(target=worker, args=("a",))
    tb = threading.Thread(target=worker, args=("b",))
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    ok_count = sum(1 for k in ("a", "b") if results[k] is not None)
    err_count = sum(1 for k in ("a", "b") if errors[k] is not None)
    assert ok_count == 1, f"应 1 成功, got {ok_count} ({results}, {errors})"
    assert err_count == 1, f"应 1 抛错, got {err_count}"
    assert errors["a"] == "LoopAlreadyForgotten" or errors["b"] == "LoopAlreadyForgotten"
