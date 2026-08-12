#!/usr/bin/env python3
"""mcp_server.py — mnelo MCP Server facade.

[refactor 2026-08-12] 原 1614 行 monolithic 拆为 5 模块 (跟 memory.py / task_states.py
拆分同模式):

  mcp_server.py             ~80 行  facade — main() + bootstrap + run_* re-export
  mcp_guard.py              ~50 行  _MCP_AVAILABLE flag + heavy mcp/Starlette/uvicorn 导入
  mcp_tool_definitions.py  ~400 行  22 Tool() JSON schemas
  mcp_tool_handlers.py     ~250 行  _TOOL_REGISTRY + _TASK_TOOL_REGISTRY + _CUSTOM_HANDLERS + _handle_*
  mcp_tool_dispatcher.py   ~350 行  _call_tool + _get_mem + _rate_limit_check + server wiring
  mcp_transports.py        ~600 行  stdio/SSE/HTTP/dual + health/metrics

向后兼容: `from mcp_server import ...` 全部仍 work (re-export 全部 public names).

[运行]
    cd LIVE_ROOT && python3 mcp_server.py --transport streamable-http
    (port 走 config: env MNELO_MEMORY_SERVER_PORT > toml [server].port > 8086)
"""

import argparse
import asyncio
import sys

# [refactor 2026-08-12] 显式 import uvicorn so tests can monkeypatch
# `mcp_server.uvicorn.run` (test_mcp_coverage_round4: test_sse_* flow).
import uvicorn as uvicorn  # noqa: F401  re-export (monkeypatch contract)

import mcp_tool_dispatcher as _dispatcher  # for _mem_instance proxy (PEP 562 __getattr__)
from auth import AuthError, load_auth_token
from config import config  # noqa: F401  re-export (test_mcp_handlers_round9 contract)
from mcp_guard import _MCP_AVAILABLE  # noqa: F401  re-export
from mcp_tool_definitions import (
    TOOLS,  # noqa: F401  re-export (22 tool schemas)
    as_tools,  # noqa: F401  re-export
)
from mcp_tool_dispatcher import (  # noqa: F401  re-export
    _RATE_BUCKETS,  # noqa: F401  re-export
    DEFAULT_SSE_HOST,  # noqa: F401  re-export (test_mcp_handlers_round9 contract)
    DEFAULT_SSE_PORT,  # noqa: F401  re-export
    _call_tool,
    _get_mem,
    _rate_limit_check,
    _resolve_server_defaults,
    server,  # the MCP Server instance (or None if _MCP_AVAILABLE)
)
from mcp_tool_handlers import (  # noqa: F401  re-export
    _CUSTOM_HANDLERS,
    _TASK_TOOL_REGISTRY,
    _TOOL_REGISTRY,
    _handle_entity_resolve,
    _handle_list_entities,
    _handle_search_relations,
    _handle_simple,
    _handle_task_simple,
)
from mcp_transports import (  # noqa: F401  re-export
    _build_dual_app,
    _build_sse_app,
    _build_streamable_app,
    _check_port_available,
    _ip_in_tailscale_cgnat,
    _mnelo_health_endpoint,
    _mnelo_metrics_endpoint,
    _validate_loopback_host,
    run_dual,
    run_sse,
    run_stdio,
    run_streamable_http,
)

# [refactor 2026-08-12] PEP 562 module-level __getattr__ proxies module attributes:
#  - _mem_instance → dispatcher singleton (live state for tests)
#  - other names (e.g. list_tools, call_tool) → dispatcher (closure-exposed names
#    defined inside `if _MCP_AVAILABLE:` block)
# Plain `from X import Y` rebinds locally only.


def __getattr__(name):  # noqa: D401  — PEP 562 module __getattr__
    if name == "_mem_instance":
        return _dispatcher._mem_instance
    # Other dispatcher module-level attrs (closures inside if _MCP_AVAILABLE)
    if hasattr(_dispatcher, name):
        return getattr(_dispatcher, name)
    raise AttributeError(f"module 'mcp_server' has no attribute {name!r}")


def main():
    """[refactor 2026-08-12] facade — boot mcp_server via chosen transport."""
    ap = argparse.ArgumentParser(description="mnelo MCP Server")
    ap.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http", "dual"],
    )
    # [Round 2] host/port default 从 config 读 (config.toml [server] 段)
    _cfg_host, _cfg_port = _resolve_server_defaults()
    ap.add_argument("--host", default=_cfg_host)
    ap.add_argument("--port", type=int, default=_cfg_port)
    # [7/19 P0-2] Bearer token 来源 (CLI override; 不传走 env/file 默认)
    ap.add_argument(
        "--auth-token-file",
        default=None,
        help="Path to file containing Bearer token (default: $MNELO_AUTH_TOKEN or ~/.config/mnelo/auth_token)",
    )
    args = ap.parse_args()

    if not _MCP_AVAILABLE:
        sys.stderr.write("FATAL: mcp/Starlette 未装, 跑 `pip install mcp[cli] starlette uvicorn`\n")
        sys.exit(1)

    # [P2-1 优化] MCP server 启动时立即 warm-up Memory (含 Embedder)
    # 实测: 不 warm-up 首次 recall ~760ms (Embedder 1s cold start + 实际工作)
    #        warm-up 后首次 recall ~70ms (model 已在 RAM)
    # 启动慢 1s, 避免首 recall spike 1s
    _get_mem()  # 触发 Memory.__init__() warm-up

    if args.transport == "stdio":
        asyncio.run(run_stdio())
    else:
        # [7/19 P0-2] 提前解析 token (fail-fast before warmup, 避免浪费 Embedder 启动时间)
        token = None
        if args.auth_token_file:
            try:
                token = load_auth_token(explicit_path=args.auth_token_file)
            except AuthError as e:
                sys.stderr.write(f"--auth-token-file load failed: {e}\n")
                sys.exit(2)
        if args.transport == "sse":
            run_sse(host=args.host, port=args.port, auth_token=token)
        elif args.transport == "streamable-http":
            run_streamable_http(host=args.host, port=args.port, auth_token=token)
        else:  # dual
            run_dual(host=args.host, port=args.port, auth_token=token)


if __name__ == "__main__":
    main()
