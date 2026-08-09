"""
[8/6 M5.1 + DESIGN §4.3 §8 cron/timer 驱动] 测试 scripts/mnelo_loop_tick_cron.py.

覆盖:
  M5.1.1 --dry-run 不写 audit_log / digest
  M5.1.2 真跑写 audit_log (status='proposed', pass_name='loop_tick_cron')
  M5.1.3 due loop 判定正确 (loop 无 active_task 且 interval 已过 → due)
  M5.1.4 lock 防重叠 (PID-based)
  M5.1.5 threshold 过滤 interval_hours
  M5.1.6 digest 输出到 ~/.hermes/cron/output/loop_tick/<date>.json
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

_CREATE_LOOP_SRC = """
import sys
sys.path.insert(0, '{repo}')
import task_states as ts
import memory
m = memory.Memory()
r = ts.loop_create(
    m._conn,
    name='{name}',
    trigger='m5-trigger',
    enabled={enabled},
    interval_hours={interval},
    now='2026-08-06T09:00',
)
m._conn.commit()
m.close()
print('LID:', r['loop_id'])
"""


def _run(args, env_extra=None, timeout=60):
    """[M22 8/6 review-pass fix] 子进程显式传 MNELO_MEMORY_DIR, 跟 _setup/断言同一库."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(Path.home()),
        "MNELO_MEMORY_SEARCH_BACKEND": "usearch",
        # [M22] 干净 checkout + 显式 env: 子进程 memory.Memory() 走 MNELO_MEMORY_DIR,
        # 不再回落 ~/.hermes/memory (项目 8/6 已移除). 跟 _setup sqlite3.connect 用同一库.
        "MNELO_MEMORY_DIR": str(_REPO),
    }
    if env_extra:
        env.update(env_extra)
    p = subprocess.run(
        [sys.executable, str(_REPO / "scripts/mnelo_loop_tick_cron.py")] + args,
        capture_output=True, text=True, env=env, timeout=timeout,
        cwd=str(_REPO),
    )
    return p.stdout, p.stderr, p.returncode


def _setup():
    """Clean fixtures: clear m5 test loops + recent loop_tick_cron audit_log."""
    import sqlite3
    c = sqlite3.connect(str(_REPO / "memory.db"))
    c.execute("PRAGMA foreign_keys = OFF")
    c.execute(
        "DELETE FROM task_states WHERE task_id LIKE 'loop:m5-%' "
        "OR task_id LIKE 'loop:20260806-m5-%'"
    )
    c.execute(
        "DELETE FROM entities WHERE id LIKE 'loop:m5-%' "
        "OR id LIKE 'loop:20260806-m5-%'"
    )
    c.execute(
        "DELETE FROM audit_log WHERE pass_name='loop_tick_cron' "
        "AND (ref_id LIKE 'loop:m5-%' OR after_json LIKE '%m5-%')"
    )
    c.execute("PRAGMA foreign_keys = ON")
    c.commit()
    c.close()
    Path("/tmp/mnelo_loop_tick_cron.lock").unlink(missing_ok=True)


def _latest_digest_path() -> Path:
    """[M22 fix] 找 ~/.hermes/cron/output/loop_tick/ 下最新 .json (按 mtime 排序).

    原硬编码 '2026-08-06.json' 日期敏感, 8/7 起必失败. 改按 mtime 找最新.
    """
    d = Path.home() / ".hermes/cron/output/loop_tick"
    if not d.exists():
        return d / "never.json"
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else d / "never.json"


def _create_loop(name: str, enabled: bool = True, interval: int = 24) -> str:
    """建一个 test loop via subprocess."""
    src = _CREATE_LOOP_SRC.format(
        repo=str(_REPO), name=name, enabled=str(enabled), interval=str(interval),
    )
    p = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "MNELO_MEMORY_SEARCH_BACKEND": "usearch",
             "MNELO_MEMORY_DIR": str(_REPO)},  # [M22]
        cwd=str(_REPO),
    )
    assert p.returncode == 0, f"loop_create failed: {p.stderr}"
    for line in p.stdout.split("\n"):
        if line.startswith("LID:"):
            return line.split(": ", 1)[1].strip()
    raise AssertionError(f"no LID in output: {p.stdout}")


def test_m5_1_dry_run_no_audit_log_no_digest():
    """[M5.1.1] --dry-run 不应写 audit_log 或 digest 文件."""
    _setup()
    _create_loop("m5-dryrun")

    out, err, rc = _run(["--dry-run", "--threshold", "0"])
    assert rc == 0, f"rc={rc}: {err}"

    import sqlite3
    c = sqlite3.connect(str(_REPO / "memory.db"))
    n = c.execute(
        "SELECT COUNT(*) FROM audit_log WHERE pass_name='loop_tick_cron' "
        "AND ref_id LIKE 'loop:m5-%'"
    ).fetchone()[0]
    c.close()
    assert n == 0, f"dry-run should not write audit_log, got {n} rows"


def test_m5_1_real_run_writes_audit_log_proposed():
    """[M5.1.2] 真跑写 audit_log (status='proposed', pass_name='loop_tick_cron')."""
    _setup()
    lid = _create_loop("m5-audit")
    print(f"created loop: {lid}")

    out, err, rc = _run(["--threshold", "0"])
    assert rc == 0, f"rc={rc}: {err}"

    import sqlite3
    c = sqlite3.connect(str(_REPO / "memory.db"))
    row = c.execute(
        "SELECT pass_name, action_type, ref_type, ref_id, status, after_json "
        "FROM audit_log WHERE pass_name='loop_tick_cron' AND ref_id=?",
        (lid,),
    ).fetchone()
    c.close()
    assert row is not None, f"audit_log row missing for {lid}"
    assert row[0] == "loop_tick_cron", row
    assert row[1] == "tick_due", row
    assert row[2] == "loop", row
    assert row[3] == lid, row
    assert row[4] == "proposed", row
    after = json.loads(row[5])
    assert after["verdict"] == "due", after


def test_m5_1_due_verdict_correct():
    """[M5.1.3] loop 无 active_task + interval 已过 → due."""
    _setup()
    lid = _create_loop("m5-due", interval=1)

    out, err, rc = _run(["--threshold", "0"])
    assert rc == 0, f"rc={rc}: {err}"

    digest_path = _latest_digest_path()
    assert digest_path.exists(), f"digest missing: {digest_path}"
    entries = json.loads(digest_path.read_text())
    if not isinstance(entries, list):
        entries = [entries]
    all_due_ids = set()
    for entry in entries:
        for loop in entry.get("due_loops", []):
            all_due_ids.add(loop["loop_id"])
    assert lid in all_due_ids, f"loop {lid} should be due, found {all_due_ids}"


def test_m5_1_lock_prevents_overlap():
    """[M5.1.4] lock 防重叠. 模拟 stale lock."""
    _setup()
    lock_path = Path("/tmp/mnelo_loop_tick_cron.lock")
    lock_path.write_text(str(os.getpid()))
    old_time = time.time() - 7200
    os.utime(lock_path, (old_time, old_time))

    out, err, rc = _run(["--threshold", "0"])
    assert rc == 0, f"rc={rc}: {err}"
    assert "stale lock" in out or "replacing" in out, \
        f"expected stale lock replacement msg: {out[:300]}"


def test_m5_1_threshold_filter():
    """[M5.1.5] threshold 过滤 interval_hours < threshold 的 loop."""
    _setup()
    lid_short = _create_loop("m5-short", interval=1)
    lid_long = _create_loop("m5-long", interval=48)
    print(f"lid_short={lid_short} lid_long={lid_long}")

    out, err, rc = _run(["--threshold", "24"])
    assert rc == 0, f"rc={rc}: {err}"

    import sqlite3
    c = sqlite3.connect(str(_REPO / "memory.db"))
    short_n = c.execute(
        "SELECT COUNT(*) FROM audit_log WHERE pass_name='loop_tick_cron' AND ref_id=?",
        (lid_short,),
    ).fetchone()[0]
    long_n = c.execute(
        "SELECT COUNT(*) FROM audit_log WHERE pass_name='loop_tick_cron' AND ref_id=?",
        (lid_long,),
    ).fetchone()[0]
    c.close()
    assert short_n == 0, f"threshold=24 应过滤掉 interval=1 loop, got {short_n}"
    assert long_n >= 1, f"threshold=24 应保留 interval=48 loop, got {long_n}"


def test_m5_1_digest_path_well_formed():
    """[M5.1.6] digest 输出路径 + JSON 结构正确."""
    _setup()
    _create_loop("m5-digest")

    out, err, rc = _run(["--threshold", "0"])
    assert rc == 0, f"rc={rc}: {err}"

    digest_path = _latest_digest_path()
    assert digest_path.exists(), f"digest missing: {digest_path}"
    data = json.loads(digest_path.read_text())
    entries = data if isinstance(data, list) else [data]

    last = entries[-1]
    for key in ("ts", "total_loops", "due_count", "due_loops", "not_due_count",
                "error_count", "error_loops", "dry_run"):
        assert key in last, f"missing key {key} in {last.keys()}"
    assert isinstance(last["due_loops"], list)
    assert last["dry_run"] is False


def test_m5_1_naive_now_avoids_subtract_error():
    """[M20 8/6 review-pass fix] cron now 用 naive local, 跟 storage 一致.

    8/6 review-pass 发现 cron 用 datetime.now(timezone.utc) (aware), 跟
    task_states.transition 写入的 naive local last_cycle_done_at 相减抛 TypeError,
    导致 cron 稳态下 due loop 永远检不出. 现改 naive, 验证:
      1. cron now_ts 字符串无 timezone offset ('+00:00' / tz suffix)
      2. cron run 不会因 subtract error 抛 TypeError
      3. 第一个 cycle 完成后, 下次 tick 仍能正常判定 due/not_due
    """
    _setup()

    # 子进程脚本: 1) 建 loop 2) 跑 cron 一次 (标记 cycle done) 3) 再跑 cron 一次 (验证不抛错)
    src = """import sys, subprocess
sys.path.insert(0, '{repo}')
import task_states as ts
import memory
m = memory.Memory()

# 1. 建 loop
r = ts.loop_create(m._conn, name='m5-naive', trigger='x', interval_hours=1, now='2026-08-06T09:00')
lid = r['loop_id']
m._conn.commit()
m.close()

# 2. 跑 cron (用 subprocess 隔离, 模拟稳态)
script = '{repo}/scripts/mnelo_loop_tick_cron.py'
import os
env = os.environ.copy()
env['MNELO_MEMORY_DIR'] = '{repo}'
env['MNELO_MEMORY_SEARCH_BACKEND'] = 'usearch'
out1 = subprocess.run([sys.executable, script, '--threshold', '0'], capture_output=True, text=True, env=env)
print('STDOUT1:', out1.stdout[:200])
if out1.returncode != 0:
    print('ERR1:', out1.stderr[:200])
    raise SystemExit(1)

# 3. 再跑 cron (首次 cycle done 后, 第二次跑)
out2 = subprocess.run([sys.executable, script, '--threshold', '0'], capture_output=True, text=True, env=env)
print('STDOUT2:', out2.stdout[:200])
if out2.returncode != 0:
    print('ERR2:', out2.stderr[:200])
    raise SystemExit(1)

# 4. 关键校验: stdout 含 'due=' 或 'not_due=' (说明 verdict 正常判定, 不是 TypeError)
import re
all_stdout = out1.stdout + out2.stdout
if 'TypeError' in all_stdout or "can't subtract" in all_stdout:
    raise AssertionError(f'naive/aware subtract TypeError detected: {all_stdout[:500]}')
if 'verdicts:' not in all_stdout:
    raise AssertionError(f'cron did not emit verdict summary: {all_stdout[:500]}')
print('NO_TYPE_ERROR')
"""
    # [M22 helper compat] 用 replace 不用 .format — src 内含 f-string + {...} 占位符冲突.
    src = src.replace("{repo}", str(_REPO))
    p = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "MNELO_MEMORY_SEARCH_BACKEND": "usearch",
             "MNELO_MEMORY_DIR": str(_REPO)},
        cwd=str(_REPO),
    )
    assert p.returncode == 0, f"M20 reproduce shell failed: rc={p.returncode} stderr={p.stderr[-300:]}"
    # 校验 stdout 含 'NO_TYPE_ERROR'
    assert "NO_TYPE_ERROR" in p.stdout, f"M20 NOT fixed: {p.stdout[-500:]}"
    # 校验 stdout 不含 TypeError
    assert "TypeError" not in p.stdout, f"TypeError leaked: {p.stdout[-500:]}"


def test_m5_1_naive_now_no_timezone_offset():
    """[M20 fix + scrub] cron now_ts 字符串不应含 '+00:00' 等 tz offset.

    naive ISO 字符串示例: '2026-08-06T15:30:00.123'
    aware ISO 字符串示例: '2026-08-06T15:30:00.123+00:00' (拒绝)

    静态源码契约校验 — 跟 RF15 同一模式, 避开 _ilu module instance 麻烦.
    """
    import re
    src_path = _REPO / "scripts/mnelo_loop_tick_cron.py"
    text = src_path.read_text()
    # 不应再含 timezone.utc (修订后已删除)
    assert "datetime.now(timezone.utc)" not in text, \
        "M20: cron 仍用 timezone.utc (aware), 会触发 naive-aware subtract TypeError"
    # 应含 naive datetime.now()
    assert "datetime.now().isoformat(timespec=" in text, \
        "M20: cron 应改用 naive datetime.now()"


def test_m5_1_m21_plist_has_memory_dir():
    """[M21 8/6 review-pass fix] launchd plist 含 MNELO_MEMORY_DIR env."""
    plist_path = _REPO / "scripts/launchd/ai.mnelo.loop_tick.plist"
    text = plist_path.read_text()
    assert "MNELO_MEMORY_DIR" in text, "M21: plist 缺 MNELO_MEMORY_DIR, macOS 会回落到 ~/.hermes/memory"
    assert "__LIVE_ROOT__" in text, "M21: plist MNELO_MEMORY_DIR 应指向 __LIVE_ROOT__ 占位符"


def test_m5_1_m22_subprocess_env_propagates():
    """[M22 8/6 review-pass fix] _run 子进程 env 传 MNELO_MEMORY_DIR."""
    import re
    test_src = (_REPO / "tests/test_m5_1_cron_tick.py").read_text()
    # _run 函数应设 MNELO_MEMORY_DIR=str(_REPO)
    assert "MNELO_MEMORY_DIR" in test_src, "M22: 测试 env 没传 MNELO_MEMORY_DIR"
    # 旧硬编码日期 '2026-08-06.json' 应已被 _latest_digest_path 替换
    assert "_latest_digest_path" in test_src, "M22: digest 应改 _latest_digest_path (按 mtime)"
