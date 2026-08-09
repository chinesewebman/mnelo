"""[8/6 M36] transition() API 边界守卫测试.

覆盖:
  M36.1 reason 必填 (always, not just force=True) - 跟 docstring 契约一致
  M36.2 reason 非 str 类型 (int/None/list) 抛 ReasonRequiredError
  M36.3 task_id / to_state 非 str 抛 InvalidInputError
  M36.4 evidence_chunk_id 非 str 抛 InvalidInputError
  M36.5 完整成功链路 (reason='valid' + valid transition)
"""
import sys
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import task_states
import memory


def _assert_isolated_db():
    """[M36 整改] 隔离守卫 — 拒绝在含真实记忆 chunk 的库上运行.

    防 MNELO_MEMORY_DIR 未指到临时库时误连 live 记忆库并污染 (测试会写
    task_states/entities/audit_log). 既有隔离模式:
      MNELO_MEMORY_DIR=$(mktemp -d) && python scripts/init_db.py && \
      pytest tests/test_m36_transition_guards.py

    [8/10 主人验证报告 fix] 之前误假设 init_db seed 1 个 'manual' chunk,
    实际 init_db.py 只 CREATE TABLE + 验证, 不 seed chunk. 旧 guard
    n_total==0 拒绝 = 误杀 init_db 后隔离库 (空). 改成:
      - 验证必要表存在 (task_states / entities / chunks) — init_db 必建
      - 仍拒绝 n_total > 50 明确 live 库
      - 仍拒绝 n_live > 5 (大量异源 source)
      - n_total == 0 放行 (init_db 后的空库, 测试自己 _create_task + transition)
    """
    db = Path(memory.DB_PATH).resolve()
    if not db.exists():
        raise SystemExit(f"[test_m36] DB 不存在: {db} — 请先 scripts/init_db.py 初始化隔离库")
    conn = sqlite3.connect(str(db))
    # [8/10 fix] 验证 init_db 必要表存在, 而不是靠 chunk 数推断
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('task_states','entities','chunks')"
    )
    tables = {r[0] for r in cur.fetchall()}
    conn.close()
    required = {"task_states", "entities", "chunks"}
    missing = required - tables
    if missing:
        raise SystemExit(
            f"[test_m36] DB {db} 缺 schema 表: {sorted(missing)}. "
            "请先 scripts/init_db.py 初始化隔离库"
        )
    # [8/10 fix] 仍走 n_total + n_live 阈值 (防连到真 live 库)
    conn = sqlite3.connect(str(db))
    n_total = conn.execute("SELECT COUNT(*) FROM chunks WHERE valid_until IS NULL").fetchone()[0]
    n_live = conn.execute(
        "SELECT COUNT(*) FROM chunks "
        "WHERE (source IS NULL OR source NOT IN ('manual', 'init', 'test', 'audit')) "
        "AND valid_until IS NULL"
    ).fetchone()[0]
    conn.close()
    if n_total > 50 or n_live > 5:
        raise SystemExit(
            f"[test_m36] 拒绝运行: {db} 含 {n_total} 个 chunk ({n_live} 非种子, 疑似 live 库). "
            "请用隔离临时库: MNELO_MEMORY_DIR=$(mktemp -d) && python scripts/init_db.py"
        )


_assert_isolated_db()


def _setup():
    c = sqlite3.connect(str(memory.DB_PATH))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute(
        "DELETE FROM task_states WHERE task_id LIKE 'task:%m36-%' OR task_id LIKE 'loop:%m36-%'"
    )
    c.execute(
        "DELETE FROM entities WHERE id LIKE 'task:%m36-%' OR id LIKE 'loop:%m36-%'"
    )
    c.execute(
        "DELETE FROM audit_log WHERE ref_id LIKE 'task:%m36-%' OR ref_id LIKE 'loop:%m36-%'"
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


# ===== M36.1 reason required always =====

def test_m36_1_transition_empty_reason_without_force_rejected():
    """[M36.1] force=False 时 reason='' 也抛 ReasonRequiredError (docstring 契约).

    旧 'if force and not reason' 只在 force=True 校验, docstring 说
    reason: required 行为不一. M36 统一 always required.
    """
    _setup()
    tid = _create_task("m36-empty-reason")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state="in_progress", reason="", force=False
            )
            raise AssertionError("应抛 ReasonRequiredError (reason='' + force=False)")
        except task_states.ReasonRequiredError:
            pass  # 期望
    finally:
        c.close()


def test_m36_1b_transition_whitespace_only_reason_rejected():
    """[M36.1b] reason='   ' (纯空白) 抛 ReasonRequiredError (post-strip)."""
    _setup()
    tid = _create_task("m36-whitespace-reason")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state="in_progress", reason="   ", force=False
            )
            raise AssertionError()
        except task_states.ReasonRequiredError:
            pass
    finally:
        c.close()


# ===== M36.2 reason type guard =====

def test_m36_2_transition_reason_int_rejected():
    """[M36.2a] reason=12345 (int) 抛 ReasonRequiredError (含类型名)."""
    _setup()
    tid = _create_task("m36-int-reason")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state="in_progress", reason=12345, force=False
            )
            raise AssertionError()
        except task_states.ReasonRequiredError as e:
            assert "int" in str(e), f"应含 'int', got: {e}"
    finally:
        c.close()


def test_m36_2b_transition_reason_none_rejected():
    """[M36.2b] reason=None 抛 ReasonRequiredError."""
    _setup()
    tid = _create_task("m36-none-reason")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state="in_progress", reason=None, force=False
            )
            raise AssertionError()
        except task_states.ReasonRequiredError:
            pass
    finally:
        c.close()


def test_m36_2c_transition_reason_list_rejected():
    """[M36.2c] reason=['x'] (list) 抛 ReasonRequiredError."""
    _setup()
    tid = _create_task("m36-list-reason")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state="in_progress", reason=["x"], force=False
            )
            raise AssertionError()
        except task_states.ReasonRequiredError as e:
            assert "list" in str(e)
    finally:
        c.close()


# ===== M36.3 task_id / to_state guards =====

def test_m36_3a_transition_task_id_int_rejected():
    """[M36.3a] task_id=12345 (int) 抛 InvalidInputError."""
    _setup()
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=12345, to_state="in_progress", reason="test_reason"
            )
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidInputError", f"got {e.code}"
            assert "task_id" in str(e), f"应指 task_id 字段: {e}"
    finally:
        c.close()


def test_m36_3b_transition_to_state_int_rejected():
    """[M36.3b] to_state=99 (int) 抛 InvalidInputError."""
    _setup()
    tid = _create_task("m36-int-state")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state=99, reason="test_reason"
            )
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidInputError", f"got {e.code}"
            assert "to_state" in str(e)
    finally:
        c.close()


# ===== M36.4 evidence_chunk_id guard =====

def test_m36_4_transition_evidence_int_rejected():
    """[M36.4] evidence_chunk_id=99999 (int) 抛 InvalidInputError."""
    _setup()
    tid = _create_task("m36-evidence-int")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        try:
            task_states.transition(
                c, task_id=tid, to_state="in_progress",
                reason="test_reason", evidence_chunk_id=99999,
            )
            raise AssertionError()
        except task_states.TaskLoopError as e:
            assert e.code == "InvalidInputError", f"got {e.code}"
            assert "evidence_chunk_id" in str(e)
    finally:
        c.close()


def test_m36_4b_transition_evidence_none_allowed():
    """[M36.4 正向] evidence_chunk_id=None 通过."""
    _setup()
    tid = _create_task("m36-evidence-none")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        result = task_states.transition(
            c, task_id=tid, to_state="in_progress",
            reason="test_reason", evidence_chunk_id=None,
        )
        assert result["to_state"] == "in_progress"
    finally:
        c.close()


# ===== M36.5 happy path =====

def test_m36_5_transition_valid_reason_succeeds():
    """[M36.5 正向] 完整成功链路 - reason='valid' + valid transition 成功."""
    _setup()
    tid = _create_task("m36-happy-path")
    c = sqlite3.connect(str(memory.DB_PATH))
    try:
        result = task_states.transition(
            c, task_id=tid, to_state="in_progress", reason="valid_reason_audit"
        )
        assert result["from_state"] == "open"
        assert result["to_state"] == "in_progress"
    finally:
        c.close()
