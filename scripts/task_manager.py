#!/usr/bin/env python3
"""
task_manager.py — mnelo task/loop 状态机 CLI (DESIGN §5.1).

[8/6 M3 Step 13] task/loop CLI 薄封装. 跟 mn.py 同一 pattern (Claude Code agent
Bash 驱动). 底层走 task_states.py 模块函数 (不绕 MCP, 直连 SQLite).

子命令:
  create    创建 task 或 loop
  move      transition task/loop (CAS)
  list      列出 task 或 loop (活跃过滤)
  replay    replay task/loop 历史
  tick      算 loop verdict

例:
  task_manager.py create task --name "采购耗材" --loop loop:abc
  task_manager.py move <task_id> --to in_progress --reason "start"
  task_manager.py list tasks [--state open] [--loop loop:abc]
  task_manager.py replay <task_id> [--asof 2026-08-06T10:00]
  task_manager.py tick <loop_id>

为简化 / Claude Code 友好, 所有子命令走相同 subparser 路径, --kind task|loop
决定调哪个 task_states 函数.
"""

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from memory import Memory  # noqa: E402
import task_states as ts  # noqa: E402


def cmd_create(args, mem):
    if args.kind == "task":
        r = ts.task_create(
            mem._conn,
            name=args.name,
            loop_id=args.loop,
            priority=args.priority if args.priority is not None else 3,
            now=args.now,
        )
    elif args.kind == "loop":
        r = ts.loop_create(
            mem._conn,
            name=args.name,
            trigger=args.trigger,
            interval_hours=args.interval_hours if args.interval_hours is not None else 24,
            enabled=not args.disabled,
            priority=args.priority if args.priority is not None else 3,
            now=args.now,
        )
    else:
        raise SystemExit(f"unknown --kind {args.kind}")
    mem._conn.commit()
    print(json.dumps(r, ensure_ascii=False, default=str, indent=2))


def cmd_move(args, mem):
    if not args.task_id:
        raise SystemExit("move requires task_id (positional)")
    r = ts.transition(
        mem._conn,
        task_id=args.task_id,
        to_state=args.to,
        reason=args.reason or "(no reason)",
        evidence_chunk_id=args.evidence,
        force=args.force,
        now=args.now,
    )
    mem._conn.commit()
    print(json.dumps(r, ensure_ascii=False, default=str, indent=2))


def cmd_list(args, mem):
    if args.kind == "task":
        r = ts.list_tasks(
            mem._conn,
            state=args.state,
            loop_id=args.loop,
            asof=args.asof,
            limit=args.limit if args.limit is not None else 50,
        )
    elif args.kind == "loop":
        r = ts.list_loops(
            mem._conn,
            enabled_only=args.enabled_only,
            state=args.state,
            asof=args.asof,
            limit=args.limit if args.limit is not None else 50,
        )
    else:
        raise SystemExit(f"unknown --kind {args.kind}")
    print(json.dumps(r, ensure_ascii=False, default=str, indent=2))


def cmd_replay(args, mem):
    if not args.task_id:
        raise SystemExit("replay requires task_id (positional)")
    r = ts.replay_task(
        mem._conn,
        task_id=args.task_id,
        asof=args.asof,
    )
    print(json.dumps(r, ensure_ascii=False, default=str, indent=2))


def cmd_tick(args, mem):
    if not args.loop_id:
        raise SystemExit("tick requires loop_id (positional)")
    r = ts.loop_tick(
        mem._conn,
        loop_id=args.loop_id,
        now=args.now,
    )
    print(json.dumps(r, ensure_ascii=False, default=str, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="mnelo task/loop 状态机 CLI (DESIGN §5.1)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # create
    p_create = sub.add_parser("create", help="建 task 或 loop")
    p_create.add_argument("--kind", choices=["task", "loop"], required=True)
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--loop", help="父 loop (仅 task)")
    p_create.add_argument("--trigger", help="loop 触发条件")
    p_create.add_argument("--interval-hours", type=int)
    p_create.add_argument("--disabled", action="store_true", help="loop 默认 disabled")
    p_create.add_argument("--priority", type=int)
    p_create.add_argument("--now")
    p_create.set_defaults(func=cmd_create)

    # move
    p_move = sub.add_parser("move", help="transition task/loop")
    p_move.add_argument("task_id")
    p_move.add_argument("--to", required=True, choices=["open", "in_progress", "waiting", "blocked", "done", "cancelled", "running", "dormant", "paused"])
    p_move.add_argument("--reason")
    p_move.add_argument("--evidence")
    p_move.add_argument("--force", action="store_true")
    p_move.add_argument("--now")
    p_move.set_defaults(func=cmd_move)

    # list
    p_list = sub.add_parser("list", help="列 task 或 loop")
    p_list.add_argument("--kind", choices=["task", "loop"], required=True)
    p_list.add_argument("--state")
    p_list.add_argument("--loop", help="父 loop (仅 task)")
    p_list.add_argument("--enabled-only", action="store_true")
    p_list.add_argument("--asof")
    p_list.add_argument("--limit", type=int)
    p_list.set_defaults(func=cmd_list)

    # replay
    p_replay = sub.add_parser("replay", help="replay task 状态历史")
    p_replay.add_argument("task_id")
    p_replay.add_argument("--asof")
    p_replay.set_defaults(func=cmd_replay)

    # tick
    p_tick = sub.add_parser("tick", help="loop tick verdict")
    p_tick.add_argument("loop_id")
    p_tick.add_argument("--now")
    p_tick.set_defaults(func=cmd_tick)

    args = parser.parse_args()
    mem = Memory()
    try:
        try:
            args.func(args, mem)
        except TaskLoopError as e:
            # [CLI-R4 8/6 review-pass] 友好错误输出, non-zero 退出.
            # 不裸 Traceback; Claude Code / 终端用户都能解析.
            print(f"[{e.code}] {e.message}", file=sys.stderr)
            sys.exit(1)
    finally:
        mem.close()


if __name__ == "__main__":
    main()
