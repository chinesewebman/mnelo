"""
[8/6 M4 digest 集成] 测试 task/loop 未闭环块 (DESIGN §4.4).

覆盖:
  task_states.list_active_tasks_and_loops — 列活跃 task + dormant loop,
       算 age_days / is_stale, counts.
  task_states.render_digest_block4 — 渲染成 digest 行.
  memory.py _build_digest — 走完路径, 校验 block4 出现在 content + line_refs.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# [8/9 P1 follow-up] 强制 m4 用 fresh DB 子目录, 隔离大盒 ~/.hermes/memory 8000+ chunk
# 残留 (m5_2/m30/m34 跨 test 的 stale 任务). CI env MNELO_MEMORY_DIR 可能跟主人
# ~/.hermes/memory 不一样但仍有 stale — 始终覆盖, 让 m4 隔离.
os.environ["MNELO_MEMORY_DIR"] = tempfile.mkdtemp(prefix="m4_test_")
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
ts_mod = _load("task_states")


def _setup():
    """Clean fixtures using a fresh Memory instance."""
    mem = mem_mod.Memory()
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'task:m4-%' "
            "OR task_id LIKE 'loop:m4-%' "
            "OR task_id LIKE 'task:tlm4-%' "
            "OR task_id LIKE 'loop:tlm4-%' "
            "OR task_id LIKE 'task:%m4-%' "
            "OR task_id LIKE 'loop:%m4-%' "
            "OR task_id LIKE 'task:20260801-m4-%' "
            "OR task_id LIKE 'task:tlm2-repeat%' "
            "OR task_id LIKE 'task:rf%' "
            "OR task_id LIKE 'loop:rf%' "
            "OR task_id LIKE 'task:20260806-rf%' "
            "OR task_id LIKE 'task:20260806-first%' OR task_id LIKE 'task:20260806-replay%' "
            "OR task_id LIKE 'task:20260806-second%' "
            "OR task_id LIKE 'task:20260806-cli%' "
            "OR task_id LIKE 'task:tlm12-%' "
            "OR task_id LIKE 'task:%m5-%' "
            "OR task_id LIKE 'task:%m28-%' "
            "OR task_id LIKE 'task:%m29-%' "
            "OR task_id LIKE 'task:%m30-%' "
            "OR task_id LIKE 'task:%m32-%' "
            "OR task_id LIKE 'task:%m33-%' "
            "OR task_id LIKE 'task:%m34-%' "
            "OR task_id LIKE 'task:%m35-%' "
            "OR task_id LIKE 'task:%m36-%' "
            "OR task_id LIKE 'task:%e2e-%'"
        )
        mem._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'task:m4-%' "
            "OR id LIKE 'loop:m4-%' "
            "OR id LIKE 'task:tlm4-%' "
            "OR id LIKE 'loop:tlm4-%' "
            "OR id LIKE 'task:%m4-%' "
            "OR id LIKE 'loop:%m4-%' "
            "OR id LIKE 'task:20260801-m4-%' "
            "OR id LIKE 'task:tlm2-repeat%' "
            "OR id LIKE 'task:rf%' "
            "OR id LIKE 'loop:rf%' "
            "OR id LIKE 'task:20260806-rf%' "
            "OR id LIKE 'task:20260806-first%' OR id LIKE 'task:20260806-replay%' "
            "OR id LIKE 'task:20260806-second%' "
            "OR id LIKE 'task:20260806-cli%' "
            "OR id LIKE 'task:tlm12-%' OR id LIKE 'task:step14-%' OR id LIKE 'loop:step14-%' OR id LIKE 'task:20260806-step14-%' OR id LIKE 'loop:20260806-step14-%' "
            "OR id LIKE 'task:%m5-%' OR id LIKE 'task:%m28-%' OR id LIKE 'task:%m29-%' OR id LIKE 'task:%m30-%' OR id LIKE 'task:%m32-%' OR id LIKE 'task:%m33-%' OR id LIKE 'task:%m34-%' OR id LIKE 'task:%m35-%' OR id LIKE 'task:%m36-%' OR id LIKE 'task:%e2e-%'"
        )
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()
    mem.close()


def test_list_active_tasks_and_loops_basic():
    """建 2 active task + 1 done + 1 dormant loop → active_tasks + dormant_loops."""
    _setup()
    m = mem_mod.Memory()
    try:
        # active task 1
        ts_mod.task_create(m._conn, name="m4-active-1", now="2026-08-06T09:00")
        # active task 2
        r2 = ts_mod.task_create(m._conn, name="m4-active-2", now="2026-08-06T09:05")
        tid2 = r2["task_id"]
        ts_mod.transition(
            m._conn,
            task_id=tid2,
            to_state="in_progress",
            reason="start",
            now="2026-08-06T10:00",
        )
        # done task (排除)
        r3 = ts_mod.task_create(m._conn, name="m4-done", now="2026-08-06T09:10")
        tid3 = r3["task_id"]
        ts_mod.transition(
            m._conn,
            task_id=tid3,
            to_state="in_progress",
            reason="work",
            now="2026-08-06T10:00",
        )
        ts_mod.transition(
            m._conn,
            task_id=tid3,
            to_state="done",
            reason="finish",
            now="2026-08-06T11:00",
        )
        # dormant loop
        ts_mod.loop_create(
            m._conn,
            name="m4-dormant",
            trigger="x",
            enabled=False,
            now="2026-08-06T09:00",
        )
        # enabled loop (排除)
        ts_mod.loop_create(
            m._conn,
            name="m4-running",
            trigger="y",
            enabled=True,
            now="2026-08-06T09:00",
        )
        m._conn.commit()

        now = "2026-08-07T09:00"  # 1 天后
        result = ts_mod.list_active_tasks_and_loops(m._conn, now=now)

        active_ids = [t["task_id"] for t in result["active_tasks"]]
        dormant_names = [loop["name"] for loop in result["dormant_loops"]]

        assert result["counts"]["active_tasks"] == 2, result["counts"]
        assert result["counts"]["dormant_loops"] == 1, result["counts"]
        assert "m4-dormant" in dormant_names
        assert "m4-running" not in dormant_names
        assert any("m4-active-1" in n for n in active_ids), active_ids
    finally:
        m.close()


def test_list_active_tasks_and_loops_stale_flag():
    """stale_days_threshold=3, 5 天前的 active task 应 is_stale=True."""
    _setup()
    m = mem_mod.Memory()
    try:
        ts_mod.task_create(m._conn, name="m4-stale", now="2026-08-01T09:00")
        m._conn.commit()

        # 5 天后
        now = "2026-08-06T09:00"
        result = ts_mod.list_active_tasks_and_loops(m._conn, now=now, stale_days_threshold=3)
        stale_tasks = [t for t in result["active_tasks"] if t["is_stale"]]
        assert len(stale_tasks) == 1, f"expected 1 stale, got {len(stale_tasks)}"
        assert stale_tasks[0]["age_days"] >= 5.0
        assert result["counts"]["stale_tasks"] == 1
    finally:
        m.close()


def test_list_active_excludes_done_cancelled():
    """done / cancelled task 不应出现在 active_tasks."""
    _setup()
    m = mem_mod.Memory()
    try:
        r1 = ts_mod.task_create(m._conn, name="m4-done-x", now="2026-08-06T09:00")
        tid1 = r1["task_id"]
        ts_mod.transition(m._conn, task_id=tid1, to_state="done", reason="x", now="2026-08-06T10:00")

        r2 = ts_mod.task_create(m._conn, name="m4-cancel", now="2026-08-06T09:01")
        tid2 = r2["task_id"]
        ts_mod.transition(m._conn, task_id=tid2, to_state="cancelled", reason="y", now="2026-08-06T10:00")
        m._conn.commit()

        result = ts_mod.list_active_tasks_and_loops(m._conn, now="2026-08-06T11:00")
        names = [t["name"] for t in result["active_tasks"]]
        assert "m4-done-x" not in names
        assert "m4-cancel" not in names
        assert result["counts"]["active_tasks"] == 0
    finally:
        m.close()


def test_render_digest_block4_basic():
    """render 输出含 '未闭环 task' 标头 + task 行 + line_refs."""
    _setup()
    m = mem_mod.Memory()
    try:
        ts_mod.task_create(m._conn, name="m4-render", now="2026-08-06T09:00")
        ts_mod.loop_create(
            m._conn,
            name="m4-render-loop",
            trigger="x",
            enabled=False,
            now="2026-08-06T09:00",
        )
        m._conn.commit()

        active_block = ts_mod.list_active_tasks_and_loops(
            m._conn,
            now="2026-08-06T10:00",
        )
        text_lines, refs = ts_mod.render_digest_block4(active_block)

        # 含标头
        assert any("未闭环 task" in line for line in text_lines)
        assert any("m4-render" in line for line in text_lines)
        assert any("dormant loop" in line for line in text_lines)
        assert any("m4-render-loop" in line for line in text_lines)
        # refs 非空
        assert len(refs) > 0
        # 至少 1 个 task ref
        task_refs = [v for v in refs.values() if v and "task:" in str(v)]
        assert len(task_refs) >= 1
    finally:
        m.close()


def test_build_digest_includes_block4():
    """memory._build_digest 走完整路径, content 含 '未闭环 task'."""
    _setup()
    m = mem_mod.Memory()
    try:
        ts_mod.task_create(m._conn, name="m4-digest-active", now="2026-08-06T09:00")
        ts_mod.loop_create(
            m._conn,
            name="m4-digest-dormant",
            trigger="x",
            enabled=False,
            now="2026-08-06T09:00",
        )
        m._conn.commit()

        # 走 _build_digest (不走缓存层, 避免 digest_dirty)
        text, refs, truncated = m._build_digest()

        assert "未闭环 task" in text, f"block4 missing in digest: {text[:500]}"
        assert "m4-digest-active" in text
        assert "dormant loop" in text
        assert "m4-digest-dormant" in text
        # refs 应含 task + loop ref
        all_ref_ids = [rid for ref_list in refs.values() for rid in ref_list]
        assert any("task:m4-" in r or "task:20260806-m4-" in r for r in all_ref_ids)
        assert any("loop:m4-" in r for r in all_ref_ids)
    finally:
        m.close()


def test_list_active_excludes_soft_deleted_loop():
    """valid_until != NULL loop 不出现."""
    _setup()
    m = mem_mod.Memory()
    try:
        # 建 enabled=True loop, 然后 transition 终端 cancel 模拟软删
        r = ts_mod.loop_create(
            m._conn,
            name="m4-soft",
            trigger="x",
            enabled=True,
            now="2026-08-06T09:00",
        )
        lid = r["loop_id"]
        ts_mod.loop_update(m._conn, loop_id=lid, enabled=False, now="2026-08-06T10:00")
        # 用 transition 关 entity (entity 软删路径)
        m._conn.execute(
            "UPDATE entities SET valid_until = '2026-08-06T11:00' WHERE id = ?",
            (lid,),
        )
        m._conn.commit()

        result = ts_mod.list_active_tasks_and_loops(m._conn, now="2026-08-06T12:00")
        dormant_names = [loop["name"] for loop in result["dormant_loops"]]
        assert "m4-soft" not in dormant_names
    finally:
        m.close()
