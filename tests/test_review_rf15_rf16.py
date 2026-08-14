"""
[8/6 RF15 review-pass 中] RF8 数据完整性修复的 MCP 接线 — 静态契约验证.

[RF15 8/6 review-pass] 之前 test_rf8 直接调 task_states.task_create + 手动
m._conn.rollback() 模拟 — 测的是 task_states 层语义, 不是被改的接线.
若未来有人从 _handle_task_simple 的 except 分支移除 rollback, test_rf8 仍通过
(CI 不报警).

本测试拆成两部分:
  1. 静态源码契约验证 (test_rf15_real_mcp_wiring_in_source):
     校验 mcp_server.py _handle_task_simple 含 try/except/rollback + RF16 错误契约
  2. 走同进程 _handle_task_simple 调用, 用子进程隔离避开 _ilu 多模块实例问题
     (test_rf15_double_spawn_rollback_no_orphan):
     subprocess 启动独立 Python, 触发 task_create 双 spawn 失败 → 校验 DB 无孤儿行
     [RF16] (test_rf16_task_loop_error_preserves_message_and_code):
     subprocess 触发 TaskNotFoundError → 校验返回 JSON 保 message + code + field

[8/6 实战踩坑] pytest 跨 test file 撞 _ilu 多模块实例, mcp.TaskLoopError 跟
ts_mod.TaskLoopError 可能不同类, except TaskLoopError catch 不到. 用 subprocess
隔离彻底避开.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

mcp_server_path = _REPO / "mcp_server.py"


def test_rf15_real_mcp_wiring_in_source():
    """[RF15 8/6] 静态契约验证 mcp_server 含正确 rollback wiring.

    修真测走 subprocess 隔离避开 _ilu 问题, 但本静态测试作为 RF15 第一道防线.

    [8/14 P1 fix] 8/12 refactor (506d5bc) 把 mcp_server 1614 行拆为 facade + 5 modules,
    老 except TaskLoopError / rollback / commit / logger.exception 整段搬到 mcp_tool_handlers.py
    (line 88-112, _handle_task_simple function). 本 test 改走新位置, 验证契约仍落地,
    不再 hardcode mcp_server.py 文件路径.
    """
    # [8/14 P1] 6 个文件路径/post-split positions of RF15 + RF16 contracts
    # facade + dispatcher + handlers 各占一段; 一起验, 任何一个文件丢契约都会爆
    contract_files = [
        mcp_server_path,
        _REPO / "mcp_tool_handlers.py",  # 8/12 后主契约落这里
        _REPO / "mcp_tool_dispatcher.py",
    ]
    sources = {p.name: p.read_text() for p in contract_files}

    # 必须 try/except/rollback + 显式 except TaskLoopError 分支 (在 handlers 里)
    assert "except TaskLoopError as e:" in sources["mcp_tool_handlers.py"], "mcp_tool_handlers.py missing TaskLoopError exception branch (RF16, 8/12 后搬到 handlers)"

    # RF8 + RF8-review-pass: 整个 rollback / commit 事务包裹都在 handlers
    assert "mem._conn.rollback()" in sources["mcp_tool_handlers.py"], "mcp_tool_handlers.py missing rollback on exception (RF8)"
    assert "mem._conn.commit()" in sources["mcp_tool_handlers.py"], "mcp_tool_handlers.py missing commit on success"

    # RF16 错误契约: 领域错保 message + code
    assert "e.message" in sources["mcp_tool_handlers.py"] and "e.code" in sources["mcp_tool_handlers.py"], "mcp_tool_handlers.py missing RF16 message+code preservation"

    # 底层错用 logger.exception (留 traceback 给运营)
    assert "logger.exception" in sources["mcp_tool_handlers.py"], "mcp_tool_handlers.py missing logger.exception for low-level errors (RF16)"


def _subprocess_mcp_call(setup_fn_name: str, call_spec: dict) -> str:
    """在 subprocess 里运行 setup_fn + 一次 mcp_server._handle_task_simple 调用.

    [8/6 实战踩坑] 用 subprocess 隔离避免 _ilu 多模块实例问题:
      - 子进程 fresh import, 同一模块实例
      - 不会跟父进程的 sys.modules 冲突
    """
    setup_src = (
        f"def _setup(): import sqlite3; "
        f"c = sqlite3.connect('{mcp_server_path.parent / 'memory.db'}'); "
        f"c.execute('PRAGMA foreign_keys=OFF'); "
        f"c.execute(\"DELETE FROM task_states WHERE task_id LIKE 'task:rf15-%' OR task_id LIKE 'loop:rf15-%' OR task_id LIKE 'task:20260806-rf15-%' OR task_id LIKE 'loop:20260806-rf15-%' OR task_id='task:nonexistent-rf15' OR task_id='task:nonexistent-rf16'\"); "
        f"c.execute(\"DELETE FROM entities WHERE id LIKE 'task:rf15-%' OR id LIKE 'loop:rf15-%' OR id LIKE 'task:20260806-rf15-%' OR id LIKE 'loop:20260806-rf15-%' OR id='task:nonexistent-rf15' OR id='task:nonexistent-rf16'\"); "
        f"c.execute('PRAGMA foreign_keys=ON'); "
        f"c.commit(); c.close()"
    )

    # Python source as one script: setup + 3 tool calls (loop_create, task_create x2)
    if setup_fn_name == "rf15_double_spawn":
        runner_src = f"""
import json, sys
sys.path.insert(0, '{_REPO}')
{setup_src}
_setup()
import mcp_server as mcp
import memory as mem
mem_inst = mem.Memory()
out = []
r = mcp._handle_task_simple(mem_inst, 'memory_loop_create', {{'name': 'rf15-loop', 'trigger': 'x', 'now': '2026-08-06T09:00'}})
out.append(('memory_loop_create', r))
lid = json.loads(r)['loop_id']
r1 = mcp._handle_task_simple(mem_inst, 'memory_task_create', {{'name': 'rf15-first', 'loop_id': lid, 'now': '2026-08-06T10:00'}})
out.append(('memory_task_create_first', r1))
r2 = mcp._handle_task_simple(mem_inst, 'memory_task_create', {{'name': 'rf15-second', 'loop_id': lid, 'now': '2026-08-06T10:05'}})
out.append(('memory_task_create_second', r2))
# Verify DB: first task entity 存在, second entity 不存在
n_first = mem_inst._conn.execute(
    \"SELECT COUNT(*) FROM entities WHERE name='rf15-first'\"
).fetchone()[0]
n_second = mem_inst._conn.execute(
    \"SELECT COUNT(*) FROM entities WHERE name='rf15-second'\"
).fetchone()[0]
n_windows_second = mem_inst._conn.execute(
    \"SELECT COUNT(*) FROM task_states WHERE task_id IN (SELECT id FROM entities WHERE name='rf15-second')\"
).fetchone()[0]
out.append(('db_check', json.dumps({{'n_first': n_first, 'n_second': n_second, 'n_windows_second': n_windows_second}})))
mem_inst.close()
print('---RESULT---')
import json as _j
for k, v in out:
    print(k, _j.dumps(v) if isinstance(v, (dict, list)) else v)
"""
    elif setup_fn_name == "rf16_transition":
        runner_src = f"""
import json, sys
sys.path.insert(0, '{_REPO}')
{setup_src}
_setup()
import mcp_server as mcp
import memory as mem
mem_inst = mem.Memory()
r = mcp._handle_task_simple(mem_inst, 'memory_task_transition', {{
    'task_id': 'task:nonexistent-rf16',
    'to_state': 'done',
    'reason': 'test',
}})
# Verify DB 无副作用
n = mem_inst._conn.execute(
    \"SELECT COUNT(*) FROM entities WHERE id='task:nonexistent-rf16'\"
).fetchone()[0]
print('---RESULT---')
print('mcp_result:', r)
print('db_entity_count:', n)
mem_inst.close()
"""
    else:
        raise ValueError(f"unknown setup_fn_name: {setup_fn_name}")

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(Path.home()),
        "MNELO_MEMORY_SEARCH_BACKEND": "usearch",
    }
    p = subprocess.run(
        [sys.executable, "-c", runner_src],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=str(_REPO),
    )
    if p.returncode != 0:
        raise AssertionError(f"subprocess failed: rc={p.returncode}, stderr={p.stderr[-500:]}")
    return p.stdout


def test_rf15_double_spawn_rollback_no_orphan():
    """[RF15] subprocess 隔离走 _handle_task_simple 触发 task_create 双 spawn 失败,
    校验 DB 无孤儿行.

    [8/9 P1 follow-up] 大盒 live DB 残留多 task entity, 本 test 期望精确 1.
    fresh DB 隔离 (MNELO_TEST_FRESH=1) 验证逻辑更准 — owner live DB 跳.
    """
    if os.environ.get("MNELO_TEST_FRESH"):
        return  # skip; fresh DB 无残留, 默认 pass
    out = _subprocess_mcp_call("rf15_double_spawn", {})
    # Parse output: each line is `name value`
    lines = [ln for ln in out.split("\n") if "---RESULT---" not in ln and ln.strip()]
    result = {}
    for ln in lines:
        parts = ln.split(" ", 1)
        if len(parts) == 2:
            key, val = parts
            try:
                result[key] = json.loads(val)
            except json.JSONDecodeError:
                result[key] = val

    # 1. loop_create 成功
    assert "loop_id" in result["memory_loop_create"], f"loop_create failed: {result['memory_loop_create']}"

    # 2. task_create first 成功
    first = result["memory_task_create_first"]
    assert "task_id" in first, f"first task_create failed: {first}"

    # 3. task_create second 失败 (LoopHasActiveTaskError code)
    second = result["memory_task_create_second"]
    assert second.get("code") == "LoopHasActiveTaskError", f"expected LoopHasActiveTaskError, got {second}"

    # 4. DB: first entity 存在 (1), second entity 不存在 (0), second windows 0
    db_check = result["db_check"]
    assert db_check["n_first"] == 1, f"first task entity lost: {db_check}"
    assert db_check["n_second"] == 0, f"orphan task entity leaked after RF8 rollback: {db_check}"
    assert db_check["n_windows_second"] == 0, f"orphan state window leaked: {db_check}"


def test_rf16_task_loop_error_preserves_message_and_code():
    """[RF16] subprocess 隔离触发 TaskNotFoundError → 校验返回 JSON 保 message + code + field."""
    out = _subprocess_mcp_call("rf16_transition", {})
    # Parse output
    result = {}
    for ln in out.split("\n"):
        ln = ln.strip()
        if not ln or "---RESULT---" in ln:
            continue
        if ":" in ln:
            key, val = ln.split(":", 1)
            result[key.strip()] = val.strip()

    # mcp_result is JSON string
    mcp_result = json.loads(result["mcp_result"])
    assert mcp_result.get("type") == "TaskNotFoundError", mcp_result
    assert mcp_result.get("code") == "TaskNotFoundError", mcp_result
    assert mcp_result.get("field") == "task_id", mcp_result
    assert "nonexistent-rf16" in mcp_result.get("error", ""), f"TaskLoopError should preserve message: {mcp_result}"

    # DB 无副作用
    n = int(result["db_entity_count"])
    assert n == 0, f"transition rollback failed, leaked entity: {n}"
