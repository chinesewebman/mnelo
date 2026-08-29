"""[8/6 M35 review-pass a8e7dca] 2 发现整改测试.

覆盖:
  M35.1 [API 边界一致] forget_task/loop reason 非 str 类型 (e.g. int) 抛
       InvalidReasonTypeError, 不再 AttributeError. M34 给 limit 加了
       isinstance 守卫, M33 reason 校验未加, 风格不一.
  M35.2 [digest 一致性] render_digest_block4 dormant loop 段用 task 段同长度公式
       + name 60 char cap. 旧 sum(len(s)+1) 多算 1, 长 dormant loop name
       越界 2000 chars.
"""

import sys
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import task_states
import memory


def _setup():
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute("DELETE FROM task_states WHERE task_id LIKE 'task:%m35-%' OR task_id LIKE 'loop:%m35-%'")
    c.execute("DELETE FROM entities WHERE id LIKE 'task:%m35-%' OR id LIKE 'loop:%m35-%'")
    c.execute("DELETE FROM audit_log WHERE ref_id LIKE 'task:%m35-%' OR ref_id LIKE 'loop:%m35-%'")
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


def _create_loop(name: str, enabled: bool = False) -> str:
    c = sqlite3.connect(str(memory.DB_PATH))
    r = task_states.loop_create(c, name=name, trigger="x", enabled=enabled, now="2026-08-06T09:00")
    lid = r["loop_id"]
    c.commit()
    c.close()
    return lid


# ===== M35.1 tests =====


def test_m35_1_forget_task_reason_int_raises_tasklooperror():
    """[M35.1a] forget_task reason=12345 (int) 抛 InvalidReasonTypeError, 不 AttributeError."""
    _setup()
    tid = _create_task("m35-reason-int")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_task(c, tid, reason=12345)
            raise AssertionError("应抛 TaskLoopError")
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidReasonTypeError", f"got {e.code}"
            assert "int" in str(e), f"应含类型名 int, got: {e}"
        except AttributeError as e:
            raise AssertionError(f"应 TaskLoopError, got AttributeError: {e}")
    finally:
        c.close()


def test_m35_1b_forget_task_reason_none_raises_tasklooperror():
    """[M35.1b] reason=None 抛 InvalidReasonTypeError (None 不算 str)."""
    _setup()
    tid = _create_task("m35-reason-none")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_task(c, tid, reason=None)
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidReasonTypeError"
    finally:
        c.close()


def test_m35_1c_forget_task_reason_list_raises_tasklooperror():
    """[M35.1c] reason=['x'] (list) 抛 InvalidReasonTypeError."""
    _setup()
    tid = _create_task("m35-reason-list")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_task(c, tid, reason=["x"])
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidReasonTypeError"
            assert "list" in str(e)
    finally:
        c.close()


def test_m35_1d_forget_loop_reason_int_raises_tasklooperror():
    """[M35.1d] forget_loop reason=42 抛 InvalidReasonTypeError."""
    _setup()
    lid = _create_loop("m35-loop-reason-int")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.forget_loop(c, lid, reason=42)
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidReasonTypeError"
            assert "int" in str(e)
    finally:
        c.close()


# ===== M35.2 tests =====


def test_m35_2_digest_block4_dormant_loop_name_truncated_to_60():
    """[M35.2a] 长 dormant loop name (>60 char) 截断到 60 + '...' 后缀."""
    _setup()
    long_loop_name = "m35-loop-dormant-truncate-" + "X" * 80  # 100+ chars
    _create_loop(long_loop_name, enabled=False)
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        active = task_states.list_active_tasks_and_loops(c, now="2026-08-06T15:00")
        text_lines, _ = task_states.render_digest_block4(active)
        joined = "\n".join(text_lines)
        # 1. 总长 ≤2000 chars
        assert len(joined) <= 2000, f"joined={len(joined)} 应 ≤2000"
        # 2. 含截断的 loop name (60 char cap)
        # 截断后 name = loop_name[:57] + "..." = "m35-loop-dormant-truncate-XXXXXXXXX..."
        truncated_marker = "m35-loop-dormant-truncate-XX..."
        # 检查 '...' 后缀 出现
        # find any line that has the long name truncated
        found_truncated = any("..." in line for line in text_lines if "m35-loop-dormant" in line)
        assert found_truncated, f"应有 truncated loop, got lines: {text_lines}"
    finally:
        c.close()


def test_m35_2b_digest_block4_dormant_loop_length_consistent_with_task_segment():
    """[M35.2b] 大量 dormant loop 触发截断, 末尾 '...', 总长 ≤2000 chars.

    task 段已用 sum(len(s))+n-1 公式, dormant loop 段现在也用同公式.
    """
    _setup()
    # 建 50 个长名 dormant loop
    c = sqlite3.connect(str(memory.DB_PATH))
    for i in range(50):
        long_name = f"m35-dormant-{i:03d}-" + "X" * 70
        c.execute(
            "INSERT OR IGNORE INTO entities (id, kind, name, properties_json, valid_until, created_at, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (f"loop:20260806-m35-dormant-{i:03d}", "loop", long_name, '{"enabled": false}', "2026-08-06T09:00", "2026-08-06T09:00"),
        )
        c.execute(
            "INSERT OR IGNORE INTO task_states (task_id, state, valid_from, valid_until, reason, evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, NULL, ?)",
            (f"loop:20260806-m35-dormant-{i:03d}", "dormant", "2026-08-06T09:00", "loop_create", "2026-08-06T09:00"),
        )
    c.commit()

    try:
        active = task_states.list_active_tasks_and_loops(c, now="2026-08-06T15:00")
        text_lines, _ = task_states.render_digest_block4(active)
        joined = "\n".join(text_lines)
        assert len(joined) <= 2000, f"joined={len(joined)} 应 ≤2000 (M35.2 fix)"
        # 末尾应 '...' 截断
        assert joined.endswith("..."), f"应被截断, got tail: ...{joined[-50:]}"
    finally:
        c.close()


def test_m35_2c_forget_task_invalid_type_then_valid_str_works():
    """[M35.2c] (顺便) type 校验后给合法 str 应成功 - 不污染 task."""
    _setup()
    tid = _create_task("m35-after-type-err")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        # 第一次 type 错
        try:
            task_states.forget_task(c, tid, reason=12345)
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidReasonTypeError"
        # task 应还在
        row = c.execute("SELECT 1 FROM entities WHERE id=? AND valid_until IS NULL", (tid,)).fetchone()
        assert row is not None, "type 错不应软删 task"
        # 第二次 str 成功
        result = task_states.forget_task(c, tid, reason="valid_reason_after_type_error")
        assert result["task_id"] == tid
    finally:
        c.close()
