"""
[8/6 v0.2 M1 schema] Tests for task_states + state_transitions migration.

DESIGN §11 测试计划 M1 — schema:
- test_task_states_table_created          # 全新 + 迁移双写
- test_ux_task_current_state_rejects_double_open   # 不变量 1 (partial UNIQUE)
- test_state_transitions_seeded_defaults           # seed 矩阵存在
- test_task_loop_kind_in_entities                   # kind 校验 (task/loop 是合法 kind)

实际: stop MCP first (单写锁) — zvec 跟 test 抢锁会失败.
"""

import os
import sys
import sqlite3
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import importlib.util as _ilu


def _load(name: str):
    spec = _ilu.spec_from_file_location(name, _REPO / f"{name}.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("config")
_load("embedder")
_load("search_index")
_load("validation")
mem_mod = _load("memory")


def test_task_states_table_created():
    """[M1] Memory 实例化后, task_states + state_transitions 都已建 (双写一致)."""
    m = mem_mod.Memory()
    try:
        # task_states 列存在
        cols = {r[1] for r in m._conn.execute("PRAGMA table_info(task_states)").fetchall()}
        assert "task_id" in cols, "task_states.task_id column missing"
        assert "state" in cols, "task_states.state column missing"
        assert "valid_from" in cols, "task_states.valid_from missing"
        assert "valid_until" in cols, "task_states.valid_until missing"
        assert "reason" in cols, "task_states.reason missing"
        assert "evidence_chunk_id" in cols, "task_states.evidence_chunk_id missing"
        assert "created_at" in cols, "task_states.created_at missing"

        # state_transitions 列存在
        cols2 = {r[1] for r in m._conn.execute("PRAGMA table_info(state_transitions)").fetchall()}
        assert "scope" in cols2, "state_transitions.scope missing"
        assert "from_state" in cols2, "state_transitions.from_state missing"
        assert "to_state" in cols2, "state_transitions.to_state missing"

        # 不变量索引 — sqlite_master 索引有 name (col 0) + sql (col 1).
        # partial UNIQUE 索引 'ux_task_current_state' 也走这条路.
        indexes = {row[0] for row in m._conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")}
        assert "ux_task_current_state" in indexes, f"ux_task_current_state partial UNIQUE missing. Got indexes: {sorted(indexes)}"
        assert "idx_task_states_open" in indexes, "idx_task_states_open partial index missing"
        assert "idx_task_states_task_valid" in indexes, "idx_task_states_task_valid missing"

        # schema_version 已 bump
        version = m._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert version is not None and version[0] == "1.1", f"schema_version should be '1.1', got {version}"
    finally:
        m.close()


def _setup_task_test_fixture(m):
    """Clean up our test data before each test that uses task:* or loop:* prefixes.

    Other test files (test_memory.py, test_search_index.py) don't touch task tables,
    so this prefix-cleanup is safe. Idempotent. We DELETE entities first (cascades
    via FK to task_states for our rows), then task_states for any orphans.

    Cleans:
    - task:test-* / loop:test-* (test ids)
    - task:20260806-restock-1 / loop:consumables-stock (PII-test residual from prior session)
    """
    # entities has FKs from relations (source_id/target_id). Disable FK enforcement
    # for the whole fixture cleanup (other tests may have left dangling relations).
    m._conn.execute("PRAGMA foreign_keys = OFF")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:test-%' OR task_id LIKE 'loop:test-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id IN ('task:20260806-restock-1', 'loop:consumables-stock')")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm2-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm3-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm7-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-m3-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm7-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-m3-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE '%m3-probe%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:耗材-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm5-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm5-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:rf%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:rf%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-rf%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-rf%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-t1%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-t1%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm9-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm10-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm11-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-t9-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-t10-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-t11-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm9-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm10-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm11-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-t9-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-t10-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-t11-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-first%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-replay%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-second%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-active%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:消耗品%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:暂挂%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE '%d31f3997%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:7b526973%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:3c8e2f1a%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm12m-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-t12m%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm12-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:rf15-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:rf15-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-rf15-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:20260806-rf15-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id = 'task:nonexistent-rf15'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:cli-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-cli-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:m4-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:m4-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm4-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'loop:tlm4-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-m4-%'")
    m._conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:tlm12-%'")
    # entities has FKs from relations (source_id/target_id). Disable FK enforcement
    # just for the DELETE (other tests may have left dangling relations).
    m._conn.execute(
        "DELETE FROM entities WHERE id LIKE 'task:test-%' "
        "OR id LIKE 'loop:test-%' "
        "OR id IN ('task:20260806-restock-1', 'loop:consumables-stock') "
        "OR id LIKE 'task:tlm2-%' "
        "OR id LIKE 'task:tlm3-%' "
        "OR id LIKE 'task:tlm7-%' "
        "OR id LIKE 'task:tlm8-%' "
        "OR id LIKE 'task:20260806-m3-%' "
        "OR id LIKE 'task:20260806-t8-%' "
        "OR id LIKE 'loop:tlm3-%' "
        "OR id LIKE 'loop:tlm7-%' "
        "OR id LIKE 'loop:tlm8-%' "
        "OR id LIKE 'loop:20260806-m3-%' "
        "OR id LIKE 'loop:20260806-t8-%' "
        "OR id LIKE 'task:tlm5-%' "
        "OR id LIKE 'loop:tlm5-%' "
        "OR id LIKE '%m3-probe%' "
        "OR id LIKE '%t8-%' "
        "OR id LIKE 'loop:耗材-%' OR id LIKE 'task:rf%' OR id LIKE 'loop:rf%' OR id LIKE 'task:20260806-rf%' OR id LIKE 'loop:20260806-rf%' OR id LIKE '%rf%-%' OR id LIKE 'task:20260806-t1%' OR id LIKE 'task:tlm9-%' OR id LIKE 'task:tlm10-%' OR id LIKE 'task:tlm11-%' OR id LIKE 'task:20260806-t9-%' OR id LIKE 'task:20260806-t10-%' OR id LIKE 'task:20260806-t11-%' OR id LIKE 'loop:tlm9-%' OR id LIKE 'loop:tlm10-%' OR id LIKE 'loop:tlm11-%' OR id LIKE 'loop:20260806-t9-%' OR id LIKE 'loop:20260806-t10-%' OR id LIKE 'loop:20260806-t11-%' OR id LIKE 'task:20260806-first%' OR id LIKE 'task:20260806-replay%' OR id LIKE 'task:20260806-second%' OR id LIKE 'task:20260806-active%' OR id LIKE 'loop:消耗品%' OR id LIKE 'loop:暂挂%' OR id LIKE '%d31f3997%' OR id LIKE 'loop:7b526973%' OR id LIKE 'loop:3c8e2f1a%' OR id LIKE 'loop:tlm12m-%' OR id LIKE 'loop:20260806-t12m-%' OR id LIKE 'loop:tlm12-%' OR id LIKE 'loop:cli-%' OR id LIKE 'task:20260806-cli-%' OR id LIKE 'task:m4-%' OR id LIKE 'loop:m4-%' OR id LIKE 'task:tlm4-%' OR id LIKE 'loop:tlm4-%' OR id LIKE 'task:20260806-m4-%' OR id LIKE 'loop:20260806-t1%' "
    )
    m._conn.execute("PRAGMA foreign_keys = ON")
    m._conn.commit()


def test_ux_task_current_state_rejects_double_open():
    """[M1 不变量 1] 同一 task 同时最多 1 个当前状态行 (partial UNIQUE)."""
    m = mem_mod.Memory()
    try:
        _setup_task_test_fixture(m)
        # 先建一个 task entity (kind=task) — task_states.task_id FK 到 entities.id
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            ("task:test-1", "task", "test task"),
        )
        # 第一个当前窗: ok
        m._conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
            ("task:test-1", "open", "2026-08-06T10:00", "2026-08-06T10:00"),
        )
        # 第二个当前窗 (同 task, valid_until IS NULL): 应该被 ux_task_current_state 拒
        try:
            m._conn.execute(
                "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
                ("task:test-1", "in_progress", "2026-08-06T10:01", "2026-08-06T10:01"),
            )
        except sqlite3.IntegrityError as e:
            assert "ux_task_current_state" in str(e) or "UNIQUE" in str(e), f"IntegrityError message should reference the constraint, got: {e}"
        else:
            raise AssertionError("Second open window for same task should be rejected by partial UNIQUE")
    finally:
        m.close()


def test_state_transitions_seeded_defaults():
    """[M1 §3.2] 默认转移矩阵 seed 已落 (15 条 default scope)."""
    m = mem_mod.Memory()
    try:
        _setup_task_test_fixture(m)
        rows = m._conn.execute("SELECT scope, from_state, to_state FROM state_transitions WHERE scope='default' ORDER BY from_state, to_state").fetchall()
        seen = {(r[0], r[1], r[2]) for r in rows}

        # 15 条默认 (DESIGN §3.2):
        expected = {
            ("default", "open", "in_progress"),
            ("default", "open", "done"),
            ("default", "open", "cancelled"),
            ("default", "in_progress", "waiting"),
            ("default", "in_progress", "blocked"),
            ("default", "in_progress", "done"),
            ("default", "in_progress", "cancelled"),
            ("default", "waiting", "in_progress"),
            ("default", "waiting", "done"),
            ("default", "waiting", "cancelled"),
            ("default", "blocked", "in_progress"),
            ("default", "blocked", "waiting"),
            ("default", "blocked", "done"),
            ("default", "blocked", "cancelled"),
            ("default", "done", "open"),  # reopen 逃生门
        }
        missing = expected - seen
        assert not missing, f"Missing default transitions: {missing}"

        # cancelled 是 terminal — 不应出现在 from_state 列
        cancelled_transitions = [r for r in rows if r[1] == "cancelled"]
        assert cancelled_transitions == [], f"cancelled is terminal; should not be a from_state. Got: {cancelled_transitions}"
    finally:
        m.close()


def test_task_loop_kind_in_entities():
    """[M1 §2.1] entities 表接受 kind='task' 和 kind='loop' (跟 DESIGN §2.1 命名一致)."""
    m = mem_mod.Memory()
    try:
        _setup_task_test_fixture(m)
        # task kind (task_id 命名: task:YYYYMMDD-<slug>)
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            ("task:20260806-restock-1", "task", "采购耗材"),
        )
        # loop kind (命名: loop:<slug>)
        m._conn.execute(
            "INSERT INTO entities (id, kind, name, properties_json) VALUES (?, ?, ?, ?)",
            (
                "loop:consumables-stock",
                "loop",
                "耗材库存监控",
                '{"trigger":"库存低于阈值","interval_hours":24,"enabled":true}',
            ),
        )
        # task_states 接受 task_id 引用这两个 entity (evidence_chunk_id 实际创建可空)
        m._conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
            ("task:20260806-restock-1", "open", "2026-08-06T10:00", "2026-08-06T10:00"),
        )
        m._conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
            ("loop:consumables-stock", "running", "2026-08-06T10:00", "2026-08-06T10:00"),
        )
        # 校验回读 — 使用 sqlite3.Row 兼容, 转 tuple
        rows = [tuple(r) for r in m._conn.execute("SELECT task_id, state FROM task_states ORDER BY id")]
        assert rows == [
            ("task:20260806-restock-1", "open"),
            ("loop:consumables-stock", "running"),
        ]
    finally:
        m.close()


def test_check_constraint_rejects_unknown_state():
    """[M1 不变量 2] state 词汇集 CHECK 约束生效 (task 6 / loop 3 = 9 个)."""
    m = mem_mod.Memory()
    try:
        _setup_task_test_fixture(m)
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            ("task:test-2", "task", "test"),
        )
        try:
            m._conn.execute(
                "INSERT INTO task_states (task_id, state, valid_from, created_at) VALUES (?, ?, ?, ?)",
                ("task:test-2", "flying", "2026-08-06T10:00", "2026-08-06T10:00"),
            )
        except sqlite3.IntegrityError as e:
            assert "CHECK" in str(e), f"IntegrityError should reference CHECK constraint, got: {e}"
        else:
            raise AssertionError("Unknown state 'flying' should be rejected by CHECK")
    finally:
        m.close()


def test_asof_replay_query_returns_windows():
    """[M1 §3.1] asof 回放查询: 取某 task 在某时点的状态窗."""
    m = mem_mod.Memory()
    try:
        _setup_task_test_fixture(m)
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            ("task:test-3", "task", "test"),
        )
        # 3 个状态窗: open → in_progress → waiting (evidence_chunk_id 为 NULL — 实际创建可空, §3.1)
        windows = [
            ("open", "2026-08-06T10:00", "2026-08-06T10:30", None),
            ("in_progress", "2026-08-06T10:30", "2026-08-06T11:00", None),
            ("waiting", "2026-08-06T11:00", None, None),
        ]
        for state, vf, vu, ev in windows:
            m._conn.execute(
                "INSERT INTO task_states (task_id, state, valid_from, valid_until, evidence_chunk_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("task:test-3", state, vf, vu, ev, vf),
            )

        # 当前状态 (valid_until IS NULL)
        cur = m._conn.execute("SELECT state FROM task_states WHERE task_id='task:test-3' AND valid_until IS NULL").fetchone()
        assert cur[0] == "waiting"

        # asof 10:15 时点: 应该是 in_progress
        asof = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id='task:test-3' AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?) ORDER BY valid_from DESC LIMIT 1",
            ("2026-08-06T10:15", "2026-08-06T10:15"),
        ).fetchone()
        assert asof[0] == "open", f"at 10:15 task should be in 'open', got {asof[0]}"

        # asof 11:30: waiting (valid_from 11:00, valid_until NULL)
        asof2 = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id='task:test-3' AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?) ORDER BY valid_from DESC LIMIT 1",
            ("2026-08-06T11:30", "2026-08-06T11:30"),
        ).fetchone()
        assert asof2[0] == "waiting"
    finally:
        m.close()
