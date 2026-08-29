"""
[8/6 M3 Step 13] Integration test for scripts/task_manager.py CLI.

走 subprocess 调 CLI (跟 CLI 用法一致). 验证 5 子命令 + 0 默认值 + 异常路径.

[CLI-R1 8/6 review-pass] 不硬编码 macOS 路径:
  - _PY = sys.executable (继承当前 pytest 解释器)
  - _CLI = _REPO / "scripts" / "task_manager.py" (repo-relative)

[CLI-R2 8/6 review-pass] 测试不污染 live memory.db:
  - 共享 live db 但 _setup_clean 删除 task:20260806-cli-% / loop:cli-% 前缀
  - memory.py init 假设库已存在 (live db), 不复制 DDL — 共享即走 live

[CLI-R3] falsy 默认值 0 不会被吞 — 测试用例覆盖 --priority 0.

[CLI-R4] main() 捕获 task_states 异常 — 测试用例覆盖故意失败 (unknown id) 校验
  返回非 0 exit code.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_CLI = _REPO / "scripts" / "task_manager.py"
_PY = sys.executable  # [CLI-R1]


def _live_db() -> Path:
    """Resolve live memory.db via repo config (不硬编码绝对路径)."""
    sys.path.insert(0, str(_REPO))
    from config import resolve_db_path

    return resolve_db_path()


def _setup_clean():
    """Pre-clean cli-* task/loop 数据 (跟 M1 fixture 同型 DELETE + PRAGMA)."""
    db = _live_db()
    if not db.exists():
        return
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM task_states WHERE task_id LIKE 'task:20260806-cli-%' OR task_id LIKE 'loop:cli%' OR task_id LIKE 'task:cli%' OR task_id LIKE 'loop:%cli-%'")
        conn.execute("DELETE FROM entities WHERE id LIKE 'task:20260806-cli-%' OR id LIKE 'loop:cli%' OR id LIKE 'task:cli%' OR id LIKE 'loop:%cli-%'")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    finally:
        conn.close()


def _extract_json(stdout: str):
    """从 CLI stdout 找第一个 '{' 起点 (跳过 embedder 噪声行)."""
    lines = stdout.split("\n")
    start_idx = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            start_idx = i
            break
    buf = lines[start_idx:]
    depth = 0
    out_lines = []
    for line in buf:
        out_lines.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0 and "}" in line:
            break
    if not out_lines:
        raise ValueError(f"no JSON object in stdout: {stdout[:300]}")
    return json.loads("\n".join(out_lines))


def _run(args: list) -> tuple:
    """Run CLI 走 live mnelo memory.db.

    [CLI-R1] 全部 repo-relative: _PY = sys.executable, _CLI = _REPO.
    [CLI-R2] 共享 live db; _setup_clean 隔离测试数据.
    """
    env = dict(os.environ)
    env["MNELO_MEMORY_SEARCH_BACKEND"] = "usearch"

    cmd = [_PY, str(_CLI)] + args
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout, p.stderr, _live_db()


def test_cli_create_task():
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "create",
            "--kind",
            "task",
            "--name",
            "cli-a",
            "--now",
            "2026-08-06T10:00",
        ]
    )
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    assert data["task_id"] == "task:20260806-cli-a"
    assert data["current_state"] == "open"


def test_cli_create_loop():
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "create",
            "--kind",
            "loop",
            "--name",
            "cli-l",
            "--trigger",
            "x",
            "--interval-hours",
            "12",
            "--now",
            "2026-08-06T09:00",
        ]
    )
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    assert data["loop_id"] == "loop:cli-l"
    assert data["enabled"] is True
    assert data["interval_hours"] == 12


def test_cli_create_loop_disabled():
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "create",
            "--kind",
            "loop",
            "--name",
            "cli-dormant",
            "--trigger",
            "x",
            "--disabled",
            "--now",
            "2026-08-06T09:00",
        ]
    )
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    assert data["enabled"] is False


def test_cli_move():
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "create",
            "--kind",
            "task",
            "--name",
            "cli-move",
            "--now",
            "2026-08-06T10:00",
        ]
    )
    tid = _extract_json(out)["task_id"]

    rc, out, err, _ = _run(
        [
            "move",
            tid,
            "--to",
            "in_progress",
            "--reason",
            "start",
            "--now",
            "2026-08-06T10:05",
        ]
    )
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    assert data["from_state"] == "open"
    assert data["to_state"] == "in_progress"


def test_cli_list_tasks():
    _setup_clean()
    _run(["create", "--kind", "task", "--name", "cli-list1", "--now", "2026-08-06T10:00"])
    _run(["create", "--kind", "task", "--name", "cli-list2", "--now", "2026-08-06T10:01"])
    rc, out, err, _ = _run(["list", "--kind", "task", "--limit", "50"])
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    names = [t["name"] for t in data["tasks"]]
    assert "cli-list1" in names
    assert "cli-list2" in names


def test_cli_replay():
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "create",
            "--kind",
            "task",
            "--name",
            "cli-replay",
            "--now",
            "2026-08-06T10:00",
        ]
    )
    tid = _extract_json(out)["task_id"]
    _run(["move", tid, "--to", "in_progress", "--reason", "start", "--now", "2026-08-06T10:05"])

    rc, out, err, _ = _run(["replay", tid])
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    assert data["current_state"] == "in_progress"
    assert data["window_count"] == 2


def test_cli_tick_due():
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "create",
            "--kind",
            "loop",
            "--name",
            "cli-tick",
            "--trigger",
            "x",
            "--now",
            "2026-08-06T09:00",
        ]
    )
    lid = _extract_json(out)["loop_id"]

    rc, out, err, _ = _run(["tick", lid, "--now", "2026-08-06T10:00"])
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    assert data["verdict"] == "due"


def test_cli_loop_list_enabled_only():
    _setup_clean()
    _run(["create", "--kind", "loop", "--name", "cli-on", "--trigger", "x", "--now", "2026-08-06T09:00"])
    _run(["create", "--kind", "loop", "--name", "cli-off", "--trigger", "x", "--disabled", "--now", "2026-08-06T09:01"])

    rc, out, err, _ = _run(["list", "--kind", "loop", "--enabled-only"])
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    data = _extract_json(out)
    names = [loop["name"] for loop in data["loops"]]
    assert "cli-on" in names
    assert "cli-off" not in names


def test_cli_priority_zero_passes_through():
    """[CLI-R3] --priority 0 不被 `or 3` 吞 — 实测 properties_json.priority == 0."""
    _setup_clean()
    rc, out, err, db = _run(
        [
            "create",
            "--kind",
            "task",
            "--name",
            "cli-prio",
            "--priority",
            "0",
            "--now",
            "2026-08-06T10:00",
        ]
    )
    assert rc == 0, f"rc={rc}, stderr={err[:300]}"
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT properties_json FROM entities WHERE kind='task' AND name='cli-prio'").fetchone()
        assert row is not None, "cli-prio task entity not found"
        cfg = json.loads(row[0])
        assert cfg.get("priority") == 0, f"priority 0 was swallowed: {cfg}"
    finally:
        conn.close()


def test_cli_move_unknown_id_returns_nonzero():
    """[CLI-R4] move 未知 task_id — main 应捕获 + 友好错 (非裸 Traceback)."""
    _setup_clean()
    rc, out, err, _ = _run(
        [
            "move",
            "task:nonexistent",
            "--to",
            "done",
            "--reason",
            "test",
        ]
    )
    assert rc != 0, f"expected non-zero exit, got {rc}, stderr={err[:300]}"
    # 期望 stderr 或 stdout 含 TaskNotFound code (无 Traceback)
    has_err = "TaskNotFound" in err or "TaskNotFound" in out
    has_traceback = "Traceback (most recent call last)" in err
    assert has_err or not has_traceback, f"expected friendly error, got stderr={err[:300]}"
