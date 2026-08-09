#!/usr/bin/env python3
"""
[8/7 L2 调度 P1] run_hygiene.py — launchd daily 03:00 wrapper for L2 hygiene pass.

设计:
- 调用 Memory.run_maintenance(passes=['hygiene'], dry_run=True) 跑 audit-only hygiene
- 写入 ~/.hermes/memory/logs/hygiene-cron.log (StandardOutPath 路由)
- 退出 0=正常 / 1=异常 (launchd MailOnFailure 用)
- dry_run 默认 True — 主人人工 review audit_log 后再 destructive
- 复用 Memory class + MNELO_HOME env (跟 mcp_server.py 一致)

[风险/边界]
- 同进程 SQLite 写 — busy_timeout=30s 跟 mcp_server.py 不冲突 (WAL reader OK)
- 启动时长 ~1-3s (memory.Memory() init 走 fastembed warmup)
- 失败 retry: launchd StartCalendarInterval 不自带, 失败靠主人下次手动跑
"""

import sys
import json
import os
from pathlib import Path

# [跟 mcp_server.py / mnelo_loop_tick_cron.py 一致] MNELO_HOME 路由 DB/config
_HOME = Path(os.environ.get("MNELO_HOME") or Path.home() / ".hermes")
sys.path.insert(0, str(_HOME / "memory"))

# [8/7 P1] 强制 usearch backend — hygiene 不需要 search index, 用 zvec 会跟
# mcp_server 抢 zvec collection LOCK 抛 RuntimeError ("Can't lock read-write collection").
# usearch 是纯内存, 无外部 lock, 跟 mcp_server 同 db 走 WAL reader 不冲突.
os.environ.setdefault("MNELO_MEMORY_SEARCH_BACKEND", "usearch")

from memory import Memory  # noqa: E402

# [8/7 SOP-fix] 把所有 logger (含 mnelo namespace) 全部 redirect 到 stderr, 让 stdout 只剩 JSON result
# (cron 场景 StandardOutPath 写 log, 但 SOP 脚本要 stdout 是纯 JSON 才能 json.load 解析)
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    stream=sys.stderr,
    force=True,  # [8/7 SOP-fix] 强制覆盖 root logger 已 attach 到 stdout 的 handler
)
# [8/7 SOP-fix] 强制覆盖所有 child logger (mnelo / hermes 等) 已有的 handler — basicConfig 只重置 root
_err_handler = logging.StreamHandler(sys.stderr)
_err_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
for _name in ("mnelo", "hermes", ""):
    _lg = logging.getLogger(_name)
    _lg.handlers = [_err_handler]
    _lg.propagate = False


def main() -> int:
    # [8/7 SOP-fix] stdout 必须只含 JSON — wrapper 内部 embedder.print 会污染 stdout
    # 在 Memory() init 之前 redirect stdout 到 stderr, init 完再恢复
    _real_stdout = sys.stdout
    sys.stdout = sys.stderr
    db_path = _HOME / "memory" / "memory.db"
    mem = Memory(db_path=db_path)
    sys.stdout = _real_stdout

    result = mem.run_maintenance(
        passes=["hygiene"],
        dry_run=True,  # [P1 默认 safe] 主人 review audit 后再 destructive
        confirm_destructive=False,  # 永不在 cron 自动 destructive
    )

    # [8/7 SOP-fix] stdout 必须只含 JSON — wrapper 内部 embedder.print 也会污染 stdout
    # init 阶段已 redirect, 这里直接 write 不再 print
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    # [exit code 契约]
    if result.get("status") == "disabled":
        print("⚠ L2 未启用 (l2.enabled=False). plist 仍会跑, 只是 noop.", file=sys.stderr)
        return 0
    if result.get("failed", 0) > 0:
        print(f"✗ {result['failed']} pass 失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
