#!/usr/bin/env python3
"""
[8/6 M5.1 + DESIGN §4.3 §8 cron/timer 驱动] mnelo_loop_tick_cron.py

DESIGN §8 推进机制: cron/timer 驱动 (二期). loop interval 到期 → 唤醒 agent
去 tick. 这里实现 M5.1 第一步: 纯 cron wrapper — 走 MCP `memory_loop_list`
扫所有 enabled loop, 对每个 due 的 loop, 打印 tick hint (给人/agent 看).

注意: mnelo 绝不自主转移任务 (DESIGN §10.1 D5). 这个脚本只发现 due loop +
写一条 audit_log Proposal (D9 stuck_task 同模式), 让 agent 主动评估后执行
spawn (memory_task_create). 不直接调 task_create.

[契约]
  启动方式 (cron): launchd 每 30 分钟跑一次 (下面 plist)
  输出: 三种目标
    1. console log (launchd 重定向到 logs/mnelo.loop_tick.log)
    2. audit_log 表 (pass_name='loop_tick_cron', status='info' 或 'due_found')
    3. digest 候选 (写到 ~/.hermes/cron/output/loop_tick/<date>.json 给人/agent 看)
  退出: 0=正常 / 1=异常

[实现选择]
  同进程 memory.Memory() + task_states.* 直连 SQLite. 不起 MCP server 端口.
  隔离靠 SQLite WAL (single writer) + busy_timeout=30s, 跟其他 cron (backup 等)
  互不阻塞. 跟 subprocess MCP 隔离 (多进程端口) 不是同一回事, cron 不需要.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 找 LIVE_ROOT
os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")
_LIVE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_LIVE_ROOT))

# 自动 lock path 避免 cron 多次重叠
_LOCK_PATH = Path("/tmp/mnelo_loop_tick_cron.lock")


def _log(msg: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] mnelo_loop_tick_cron: {msg}", flush=True)


def _check_lock() -> bool:
    """[8/6 M5.1] 防 cron 重叠. 30 分钟 cron 偶尔跟上次运行重叠 (前次还没退出),
    用 flock-style lock. PID-based + timeout 兜底 (前次超过 60 分钟视为僵尸).
    """
    if not _LOCK_PATH.exists():
        _LOCK_PATH.write_text(str(os.getpid()))
        return True
    try:
        old_pid = int(_LOCK_PATH.read_text().strip() or "0")
    except ValueError:
        old_pid = 0
    # 检查 PID 是否还活
    try:
        os.kill(old_pid, 0)
        alive = True
    except OSError:
        alive = False
    if not alive:
        _log(f"stale lock from pid={old_pid}, replacing")
        _LOCK_PATH.write_text(str(os.getpid()))
        return True
    # PID 还活: 检查 lock mtime
    age = time.time() - _LOCK_PATH.stat().st_mtime
    if age > 3600:  # 1 小时 = 僵尸
        _log(f"stale lock age={age:.0f}s from pid={old_pid}, replacing")
        _LOCK_PATH.write_text(str(os.getpid()))
        return True
    _log(f"lock held by pid={old_pid} age={age:.0f}s, skipping")
    return False


def _release_lock() -> None:
    try:
        if _LOCK_PATH.exists():
            _LOCK_PATH.unlink()
    except OSError:
        pass


def _mcp_call(tool: str, args: dict) -> dict:
    """走 mnelo_echo CLI 调 MCP, 返回 JSON. 比 subprocess 起 MCP server 简单.

    mnelo_echo 已有 mcp_memory 子命令, 但为 cron wrapper 轻量化, 走 direct
    memory.Memory + task_states.* 函数 — 同进程, 不需 MCP server 端口.
    """
    import memory

    mem = memory.Memory()
    try:
        if tool == "memory_loop_list":
            import task_states

            enabled_only = args.get("enabled_only", True)
            # list_loops 已返回 {loops: [...], count, truncated}, 直接透传
            return task_states.list_loops(mem._conn, enabled_only=enabled_only)
        elif tool == "memory_loop_tick":
            import task_states

            result = task_states.loop_tick(
                mem._conn,
                loop_id=args["loop_id"],
                now=args.get("now"),
            )
            return result
        else:
            return {"error": f"unsupported tool: {tool}"}
    finally:
        mem.close()


def main():
    parser = argparse.ArgumentParser(description="mnelo cron loop tick wrapper")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="[8/6] 演练模式: 扫 due loops 但不写 audit_log / digest",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=0,
        help="[8/6] 仅 tick interval_hours > threshold 的 loop (默认 0 = 全部)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.home() / ".hermes/cron/output/loop_tick",
        help="[8/6] digest 输出目录",
    )
    args = parser.parse_args()

    if not _check_lock():
        return 1

    try:
        return _run(args)
    finally:
        _release_lock()


def _run(args) -> int:
    _log(f"start (dry_run={args.dry_run}, threshold={args.threshold})")

    # 1. 扫所有 enabled loop
    list_result = _mcp_call("memory_loop_list", {"enabled_only": True})
    if "error" in list_result:
        _log(f"loop_list failed: {list_result['error']}")
        return 1

    loops = list_result.get("loops", [])
    _log(f"found {len(loops)} enabled loops")

    # 2. 过滤 threshold
    if args.threshold > 0:
        loops = [l for l in loops if (l.get("interval_hours") or 24) >= args.threshold]
        _log(f"after threshold filter: {len(loops)} loops")

    # 3. 对每个 loop 跑 tick, 收集 due
    due_loops = []
    not_due_loops = []
    error_loops = []
    # [M20 8/6 review-pass fix] 用 naive local ISO 字符串, 跟 task_states._default_now()
    # 写入格式一致. 原用 timezone.utc (aware) 会让 loop_tick 内部 naive-aware
    # datetime 相减抛 TypeError, 导致 cron 稳态下检不出 due loop.
    now_ts = datetime.now().isoformat(timespec="milliseconds")

    for loop in loops:
        loop_id = loop.get("loop_id")
        if not loop_id:
            error_loops.append({"loop_id": "?", "error": "missing loop_id"})
            continue
        try:
            tick = _mcp_call("memory_loop_tick", {"loop_id": loop_id, "now": now_ts})
            verdict = tick.get("verdict", "unknown")
            entry = {
                "loop_id": loop_id,
                "name": loop.get("name"),
                "trigger": loop.get("trigger"),
                "verdict": verdict,
                "active_task_id": tick.get("active_task_id"),
                "last_cycle_done_at": tick.get("last_cycle_done_at"),
            }
            if verdict == "due":
                due_loops.append(entry)
            elif verdict in ("not_due", "waiting", "dormant"):
                not_due_loops.append(entry)
            else:
                error_loops.append({**entry, "error": f"unknown verdict: {verdict}"})
        except Exception as e:
            error_loops.append({"loop_id": loop_id, "error": str(e)[:120]})

    _log(f"verdicts: due={len(due_loops)} not_due={len(not_due_loops)} error={len(error_loops)}")

    # 4. 写 digest 候选 + audit_log
    summary = {
        "ts": now_ts,
        "total_loops": len(loops),
        "due_count": len(due_loops),
        "due_loops": due_loops,
        "not_due_count": len(not_due_loops),
        "error_count": len(error_loops),
        "error_loops": error_loops,
        "dry_run": args.dry_run,
    }

    if not args.dry_run:
        # 4a. 写 digest 候选
        args.output_dir.mkdir(parents=True, exist_ok=True)
        # [M20 fix] naive local 日期, 跟 storage 一致, 避免 cron 跨日不一致
        today = datetime.now().strftime("%Y-%m-%d")
        out_path = args.output_dir / f"{today}.json"
        # 累加同日多 tick
        existing = []
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
                if not isinstance(existing, list):
                    existing = [existing]
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(summary)
        out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
        _log(f"digest written: {out_path}")

        # 4b. 写 audit_log (mnelo 一等公民)
        if due_loops:
            _write_audit_log(summary)

    # 5. console 报告 (供 launchd log 看)
    if due_loops:
        _log(f"!! {len(due_loops)} due loops need agent evaluation:")
        for entry in due_loops:
            _log(f"   - {entry['name']} ({entry['loop_id']}): trigger={entry['trigger']}")
    else:
        _log("no due loops")

    return 0


def _write_audit_log(summary: dict) -> None:
    """[8/6 M5.1] 写 audit_log pass_name='loop_tick_cron'.

    Proposal 模式 (DESIGN §4.4): pass_name='loop_tick_cron', status='due_found'.
    Agent / 用户 评估后用 memory_apply_proposal() 走 applied.
    """
    import memory
    import uuid

    run_id = f"loop_tick_cron-{summary['ts']}-{uuid.uuid4().hex[:8]}"
    try:
        mem = memory.Memory()
        try:
            for entry in summary["due_loops"]:
                # [8/6 M5.1 + DESIGN §4.4 Proposal 模式] 单 loop 一条 audit_log,
                # 用 before_json=NULL / after_json=proposal JSON 表达 due 状态.
                # status='proposed' 跟现有 schema 一致 — agent 评估后用 status='applied'.
                mem._conn.execute(
                    """
                    INSERT INTO audit_log (
                        run_id, pass_name, action_type, ref_type, ref_id,
                        before_json, after_json, confidence, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        "loop_tick_cron",
                        "tick_due",  # action_type: 跟现有 enum 兼容
                        "loop",
                        entry["loop_id"],
                        None,  # before_json: 无前置
                        json.dumps(entry, ensure_ascii=False),  # after_json: due proposal
                        1.0,  # confidence: cron 是机械判断
                        "proposed",  # status: 待 agent 评估后 applied
                        summary["ts"],
                    ),
                )
            mem._conn.commit()
            _log(f"audit_log written: run_id={run_id} count={len(summary['due_loops'])}")
        finally:
            mem.close()
    except Exception as e:
        _log(f"audit_log write failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
