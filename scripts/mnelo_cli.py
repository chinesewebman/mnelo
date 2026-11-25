#!/usr/bin/env python3
"""mnelo — CLI wrapper for mnelo MCP tools.

[2026-08-07燕如 v0.1]
[2026-08-29 fix] 自适应 venv: wrapper 自动找 repo 内的 .venv/bin/python3, 不用绑
                 死 ~/projects/mnelo/.venv 路径 (8/29 笔记: mnelo 项目固定在家目录,
                 但路径写死违反 "不破坏 future 部署" 原则). install.sh 复制到
                 LIVE_ROOT/scripts/ 后, wrapper 同样能找到 LIVE_ROOT/.venv/.

Usage:
    mnelo [--endpoint URL] [--token-file PATH | --token T] <command> [args]

Commands:
    ls                          list all 22 tools (name + description)
    show <name>                 show full schema for one tool
    call <name> --json '{...}'  invoke a tool with raw JSON args
    health                      GET /health (hygiene + PII warnings)
    metrics                     GET /metrics (Prometheus text)
    tunnel up|down|status       manage ssh tunnel to remote macbook

Examples:
    mnelo ls
    mnelo show memory_recall
    mnelo call memory_stats
    mnelo call memory_recall --json '{"query":"今天","top_k":3}'
    mnelo --remote apple@100.83.50.99 call memory_stats

Auth:
    Token resolution order: --token > $MNELO_AUTH_TOKEN > --token-file (default ~/.config/mnelo/token)

Remote mode:
    --remote USER@HOST spawns ssh -L 18086:127.0.0.1:8086 and connects to localhost:18086
"""

import argparse
import json as json_lib
import os
import subprocess
import sys
import time

# [2026-08-29 fix] 自适应 venv / repo 路径 — wrapper 找自己所在的 mnelo repo, 不绑死 ~/projects/mnelo/.
# 安装位置两条路径都支持:
#   1. 开发模式: scripts/mnelo_cli.py 在 ~/projects/mnelo/scripts/ 里跑
#   2. 生产模式: install.sh 复制到 ~/.hermes/memory/scripts/ (LIVE_ROOT/scripts/)
# 两种模式下 .venv 都在 repo 根的兄弟目录, api/ 同理.
_THIS = os.path.dirname(os.path.abspath(__file__))
# wrapper 在 scripts/ 下, repo 根 = ../; LIVE_ROOT 时 wrapper 在 scripts/ 下, LIVE_ROOT = ../.
# [2026-08-29 fix] realpath 解析 symlink — wrapper 装到 ~/.local/bin/mnelo 时是 symlink,
# __file__ 拿到的是 symlink 路径 (/Users/apple/.local/bin/mnelo), dirname 就是 ~/.local/bin,
# 找不到 mnelo repo. 用 realpath 拿真路径 = /Users/apple/projects/mnelo/scripts/mnelo_cli.py.
_REAL_FILE = os.path.realpath(__file__)
_REAL_THIS = os.path.dirname(_REAL_FILE)
_REAL_REPO_ROOT = os.path.dirname(_REAL_THIS)
_VENV_PY = os.path.join(_REAL_REPO_ROOT, ".venv", "bin", "python3")
if os.path.isfile(_VENV_PY) and sys.executable != _VENV_PY:
    # wrapper 当前 python 不是 mnelo venv 的 python — re-exec 自己进 venv,
    # 避免找不到 mcp 包. 单次 forward, 没有 infinite loop 风险 (因为 sys.executable 检查).
    os.execv(_VENV_PY, [_VENV_PY] + sys.argv)
# 让 wrapper 自身可被 import (自身 mnelo_client 等模块, 如果有)
sys.path.insert(0, _REAL_THIS)
# repo 内 api/ 目录放 mnelo_client.py — 优先 import 这个 (新 client 支持 streamable-http)
_repo_api = os.path.join(_REAL_REPO_ROOT, "api")
if os.path.isdir(_repo_api):
    sys.path.insert(0, _repo_api)
# Fallback: 老副本 (向后兼容 — 老部署升级时可能还有这个)
_legacy = os.path.expanduser("~/.local/lib/mnelo-cli")
if _legacy not in sys.path:
    sys.path.append(_legacy)
try:
    from mnelo_client import MneloClient  # noqa: E402
except ImportError as e:
    sys.stderr.write(f"FATAL: cannot import mnelo_client: {e}\n")
    sys.stderr.write("Source paths tried:\n")
    sys.stderr.write(f"  {_repo_api} (repo api/)\n")
    sys.stderr.write(f"  {_legacy} (legacy ~/.local/lib/mnelo-cli/)\n")
    sys.stderr.write(f"  {sys.path[:3]} (Python sys.path head)\n")
    sys.exit(2)


# [2026-08-29 fix] mnelo_client 切换 streamable-http 后删了这些工具函数.
# CLI wrapper 自身保留本地副本, 避免在 client 里堆兼容代码.
class MneloError(RuntimeError):
    """CLI 内部错误类型 — 与老 mnelo_client.MneloError 等价."""

    pass


def read_token(path: str = "~/.config/mnelo/token") -> str:
    """读 token 文件 — 老 mnelo_client.read_token 的 CLI 本地副本."""
    from pathlib import Path

    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise MneloError(f"token file not found: {p}")
    text = p.read_text().strip()
    if not text:
        raise MneloError(f"token file empty: {p}")
    return text


def cmd_ls(cli: MneloClient, args: argparse.Namespace):
    """列出所有 tool."""
    tools = cli.list_tools()
    if args.json:
        print(json_lib.dumps(tools, indent=2, ensure_ascii=False))
        return
    print(f"{'NAME':<35} {'DESCRIPTION'}")
    print("-" * 90)
    for t in tools:
        print(f"{t['name']:<35} {t.get('description', '')[:70]}")
    print(f"\n  total: {len(tools)} tools")


def cmd_show(cli: MneloClient, args: argparse.Namespace):
    """显示单 tool 的 schema."""
    tools = cli.list_tools()
    target = next((t for t in tools if t["name"] == args.tool_name), None)
    if not target:
        all_names = [t["name"] for t in tools]
        print(f"ERROR: unknown tool '{args.tool_name}'", file=sys.stderr)
        print(f"  available: {', '.join(all_names)}", file=sys.stderr)
        sys.exit(2)
    print(json_lib.dumps(target, indent=2, ensure_ascii=False))


def cmd_call(cli: MneloClient, args: argparse.Namespace):
    """调用一个 tool (args 来自 --json 或 stdin 或 --key value 重复)."""
    if args.json:
        try:
            tool_args = json_lib.loads(args.json)
        except json_lib.JSONDecodeError as e:
            print(f"ERROR: --json invalid: {e}", file=sys.stderr)
            sys.exit(2)
    elif args.stdin:
        tool_args = json_lib.loads(sys.stdin.read())
    else:
        # Build from --key value pairs
        tool_args = {}
        i = 0
        while i < len(args.kwargs):
            key = args.kwargs[i]
            if not key.startswith("--"):
                print(f"ERROR: expected --key, got '{key}'", file=sys.stderr)
                sys.exit(2)
            key = key[2:]
            if i + 1 >= len(args.kwargs):
                print(f"ERROR: --{key} missing value", file=sys.stderr)
                sys.exit(2)
            value = args.kwargs[i + 1]
            # 尝试解析 JSON literal (e.g. 123, true, null, "string")
            try:
                value = json_lib.loads(value)
            except (json_lib.JSONDecodeError, TypeError):
                pass  # keep as string
            tool_args[key] = value
            i += 2
    result = cli.call_tool(args.tool_name, tool_args)
    if args.raw:
        # 透传 mnelo 原始 JSON（双 JSON 编码的）
        print(json_lib.dumps(result, indent=2, ensure_ascii=False))
    else:
        # Pretty print, JSON if possible
        if isinstance(result, (dict, list)):
            print(json_lib.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result)


def cmd_health(cli: MneloClient, args: argparse.Namespace):
    """GET /health."""
    h = cli.health()
    print(json_lib.dumps(h, indent=2, ensure_ascii=False))


def cmd_metrics(cli: MneloClient, args: argparse.Namespace):
    """GET /metrics."""
    print(cli.metrics())


# ===== tunnel helpers =====

TUNNEL_DEFAULT_PORT = 18086
TUNNEL_PID_FILE = "/tmp/mnelo-tunnel.pid"


def tunnel_up(remote: str, port: int):
    """Start ssh tunnel as background process. Writes pid file."""
    pid_file = TUNNEL_PID_FILE
    if os.path.exists(pid_file):
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # check alive
            print(f"tunnel already up (pid={pid})")
            return
        except (OSError, ValueError):
            os.remove(pid_file)
    cmd = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"{port}:127.0.0.1:8086",
        remote,
    ]
    log = open("/tmp/mnelo-tunnel.log", "ab")
    proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    # wait for port
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            import urllib.request

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    with open(pid_file, "w") as f:
                        f.write(str(proc.pid))
                    print(f"tunnel up: {remote}:8086 → localhost:{port} (pid={proc.pid})")
                    return
        except Exception:
            if proc.poll() is not None:
                print(f"ERROR: ssh tunnel died. log: /tmp/mnelo-tunnel.log", file=sys.stderr)
                sys.exit(1)
            time.sleep(0.2)
    print(f"ERROR: tunnel not up in 10s", file=sys.stderr)
    proc.terminate()
    sys.exit(1)


def tunnel_down():
    pid_file = TUNNEL_PID_FILE
    if not os.path.exists(pid_file):
        print("tunnel not running (no pid file)")
        return
    with open(pid_file) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"tunnel killed (pid={pid})")
    except OSError as e:
        print(f"ERROR: kill {pid}: {e}")
    os.remove(pid_file)


def tunnel_status(port: int):
    pid_file = TUNNEL_PID_FILE
    if not os.path.exists(pid_file):
        print("tunnel not running")
        sys.exit(1)
    with open(pid_file) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)
        # check port responds
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            print(f"tunnel alive (pid={pid}, port={port}, status={r.status})")
    except (OSError, urllib.error.URLError) as e:
        print(f"tunnel dead (pid={pid}): {e}")
        sys.exit(1)


def cmd_tunnel(args: argparse.Namespace):
    if args.action == "up":
        tunnel_up(args.remote, args.port)
    elif args.action == "down":
        tunnel_down()
    elif args.action == "status":
        tunnel_status(args.port)


# ===== main =====


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mnelo",
        description="MCP-SSE client for mnelo memory server.",
    )
    p.add_argument("--endpoint", default=None, help="mnelo endpoint (default http://127.0.0.1:8086/mcp — streamable-http transport)")
    p.add_argument("--token", default=None, help="Bearer token (else $MNELO_AUTH_TOKEN or --token-file)")
    p.add_argument("--token-file", default="~/.config/mnelo/token", help="Path to token file (default ~/.config/mnelo/token)")
    p.add_argument("--remote", default=None, help="USER@HOST for auto ssh tunnel mode (uses --ssh-port)")
    p.add_argument("--ssh-port", type=int, default=8086, help="Remote mnelo port (default 8086)")
    p.add_argument("--timeout", type=float, default=30.0, help="Request timeout seconds (default 30)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ls = sub.add_parser("ls", help="List all 22 tools")
    p_ls.add_argument("--json", action="store_true", help="Full JSON output")

    p_show = sub.add_parser("show", help="Show schema for one tool")
    p_show.add_argument("tool_name")

    p_call = sub.add_parser("call", help="Call a tool")
    p_call.add_argument("tool_name")
    p_call.add_argument("--json", default=None, help="JSON args string (else --key value pairs)")
    p_call.add_argument("--stdin", action="store_true", help="Read JSON args from stdin")
    p_call.add_argument("kwargs", nargs="*", help="--key value pairs (alternative to --json)")
    p_call.add_argument("--raw", action="store_true", help="Raw JSON output (skip pretty-print)")

    sub.add_parser("health", help="GET /health")
    sub.add_parser("metrics", help="GET /metrics (Prometheus)")

    p_tun = sub.add_parser("tunnel", help="Manage ssh tunnel")
    p_tun_sub = p_tun.add_subparsers(dest="action", required=True)
    p_tun_up = p_tun_sub.add_parser("up", help="Start ssh tunnel")
    p_tun_up.add_argument("remote", help="USER@HOST (e.g. apple@100.83.50.99)")
    p_tun_up.add_argument("--port", type=int, default=TUNNEL_DEFAULT_PORT, help=f"Local port (default {TUNNEL_DEFAULT_PORT})")
    p_tun_down = p_tun_sub.add_parser("down", help="Stop ssh tunnel")
    p_tun_status = p_tun_sub.add_parser("status", help="Check tunnel status")
    p_tun_status.add_argument("--port", type=int, default=TUNNEL_DEFAULT_PORT)

    return p


def main():
    args = build_parser().parse_args()

    # Tunnel subcommand 不需要 mnelo client
    if args.cmd == "tunnel":
        cmd_tunnel(args)
        return

    # Resolve token
    token = args.token
    if not token and not args.remote:
        try:
            token = read_token(args.token_file)
        except MneloError:
            # try env var
            token = os.environ.get("MNELO_AUTH_TOKEN")
    if not token:
        print("ERROR: no token. Pass --token, --token-file, or set $MNELO_AUTH_TOKEN", file=sys.stderr)
        sys.exit(2)

    # Resolve endpoint
    # [2026-08-29 fix] 默认 endpoint 必须含 /mcp 路径 — 新 client (streamable-http) 按
    # URL 是否含 /sse 判 transport, 不含 /mcp 时直接打 http://host:port/ → 404.
    # 老 client 用 DEFAULT_SSE_HOST/PORT, 拼出无路径 URL (== SSE endpoint), 跟新 client 不兼容.
    if args.remote:
        # [2026-08-29] --remote 模式 (ssh -L tunnel) 暂未适配新 client — 主人本地 server 模式足够.
        # 老 MneloClient.from_remote 在新 client 里已移除. 如果主人以后需要, 走 wrapper 自带的 tunnel up.
        print("ERROR: --remote mode not yet ported to streamable-http client.", file=sys.stderr)
        print("       Use 'mnelo tunnel up USER@HOST' first, then connect to localhost:18086 manually.", file=sys.stderr)
        sys.exit(1)
    else:
        # 默认走 streamable-http + /mcp (与 mnelo server 当前 transport 一致).
        default_endpoint = f"http://{os.environ.get('MNELO_HOST', '127.0.0.1')}:8086/mcp"
        endpoint = args.endpoint or default_endpoint
        # [2026-08-29 fix] 新 client 签名: __init__(sse_url=None, url=None, transport=None, timeout=30.0, auth_token=None)
        # wrapper 老调用 MneloClient(endpoint, token, timeout=...) 把 token 当成 url — 必须走 kwarg.
        cli = MneloClient(url=endpoint, auth_token=token, timeout=args.timeout)

    # Dispatch
    # [2026-08-29 fix] 新 client (streamable-http) 每次 _call() 自带短连接, 无需 with 块.
    # 老 SSE client 需要 with cli: 维持长连接, 已废弃.
    try:
        if args.cmd == "ls":
            cmd_ls(cli, args)
        elif args.cmd == "show":
            cmd_show(cli, args)
        elif args.cmd == "call":
            cmd_call(cli, args)
        elif args.cmd == "health":
            cmd_health(cli, args)
        elif args.cmd == "metrics":
            cmd_metrics(cli, args)
    except MneloError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n(interrupted)", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
