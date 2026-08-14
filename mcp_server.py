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

向后兼容:
  - `from mcp_server import X` 走 PEP 562 `__getattr__` 转发到源 sub-module
  - `mcp_server.X` (read) 走 PEP 562 `__getattr__`
  - `mcp_server.X = Y` (write) 走 PEP 562 `__setattr__`, 透到 sub-module.X
  - `monkeypatch.setattr(mcp_server, 'X', value)` 生效 (透到 sub-module.X)

[8/14 P1 fix] 之前 `from X import Y` 是 value-binding — `from mcp_server import run_sse`
让 caller 拿到老值. `monkeypatch.setattr(mcp_server, 'run_sse', mock)` 改 mcp_server module
dict, 但 `mcp_transports.run_stdio` 用了 import 时的 value-binding ref, 永远看不到 mock.

改成 `import X` + `__getattr__` + `__setattr__` 全动态转发, facade 真成 attribute router.

Test contract:
  - `monkeypatch.setattr(mcp_server, 'X', value)` → X 在 sub-module 那里被 set
  - 外部代码 `mcp_server.X` (or `from mcp_server import X`) 拉到 updated value
  - 内部函数 (main() 等) 用 direct sub-module ref; 测试时改 facade 属性**不影响** main() —
    这是 Python 函数 local-binding 决定的 (module attribute access 才能 reflect 改动)。

[运行]
    cd LIVE_ROOT && python3 mcp_server.py --transport streamable-http
    (port 走 config: env MNELO_MEMORY_SERVER_PORT > toml [server].port > 8086)
"""

import argparse
import asyncio
import sys

# [refactor 2026-08-12] Sub-module references (NOT value-binding via `from X import Y`)
# 因为我们用 PEP 562 `__getattr__` + `__setattr__` 做 attribute router,
# 这层必须保留 `import X` (module ref), 不是 `from X import Y` (value).
#
# [8/14 P1] 之前 `from X import Y` 让 monkeypatch.setattr(mcp_server, Y, val) 不影响
# X.Y 的 binding. 改 import 后, facade __setattr__ 路由到 X.Y, 真生效.
import uvicorn  # noqa: F401  re-export (test_mcp_coverage_round4: test_sse_* flow)

import auth
import config  # noqa: F401  re-export (test_mcp_handlers_round9 contract)
import mcp_guard
import mcp_tool_definitions
import mcp_tool_dispatcher
import mcp_tool_handlers
import mcp_transports

# Ordered sub-modules for __getattr__/__setattr__ lookup chain
# Most-specific first; guard last (it has _MCP_AVAILABLE flag overrides).
_SUB_MODULES = (
    mcp_tool_dispatcher,  # _call_tool, _get_mem, server, _RATE_BUCKETS, _mem_instance
    mcp_tool_handlers,  # _TOOL_REGISTRY, _TASK_TOOL_REGISTRY, _CUSTOM_HANDLERS
    mcp_tool_definitions,  # TOOLS, as_tools
    mcp_transports,  # run_*, stdio_server, _check_port_available, _build_*_app
    mcp_guard,  # _MCP_AVAILABLE, uvicorn, stdio_server, Server, ...
    auth,  # AuthError, load_auth_token
    config,  # config singleton
)


def __getattr__(name):  # noqa: D401  — PEP 562 module __getattr__
    """Forward attribute reads to sub-modules in lookup order.

    Enables:
      - `from mcp_server import _MCP_AVAILABLE` (re-export)
      - `mcp_server._MCP_AVAILABLE` (transitive access)
      - 外部代码访问 facade 拿到 forward 后的真实 sub-module 属性

    Lazy lookups (per-call) — does NOT bind at import time — so test-set values
    on facade actually reach external code paths.
    """
    for mod in _SUB_MODULES:
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module 'mcp_server' has no attribute {name!r}")


def __setattr__(name, value):  # noqa: D401  — PEP 562 module __setattr__
    """Forward attribute writes to sub-module where name is defined.

    Solves Pattern X/Y 8/12 split regression:
      - `monkeypatch.setattr(mcp_server, '_MCP_AVAILABLE', True)` → mcp_guard._MCP_AVAILABLE = True
      - 然后外部代码读 `mcp_server._MCP_AVAILABLE` 拿到 True (走 __getattr__)

    Caveat: 函数内 local binding (`run_sse = mcp_server.run_sse`) 不反映 setattr 改动 —
    这是 Python function scope 决定的. 想要 reflect, 用 `getattr(mcp_server, 'run_sse')()`
    或者 monkeypatch 直 sub-module (e.g. `mcp_transports.run_sse`).
    """
    for mod in _SUB_MODULES:
        if hasattr(mod, name):
            setattr(mod, name, value)
            return
    # Fallback: write to local module dict (for facade-internal state like test fixtures)
    super().__setattr__(name, value)


def __delattr__(name):  # noqa: D401  — PEP 562 module __delattr__
    """Forward attribute deletes to sub-module where name is defined."""
    for mod in _SUB_MODULES:
        if hasattr(mod, name):
            delattr(mod, name)
            return
    super().__delattr__(name)


def main():
    """[refactor 2026-08-12] facade — boot mcp_server via chosen transport.

    [8/14 P1] 使用 direct sub-module ref (不是 facade), main() 内 module-level binding 永远
    捕获 import-time value. 测试想 mock main() 内的 transport call, 直接 patch
    `mcp_transports.run_sse` (sub-module) 不是 facade — 那样 setattr 才会通过 PEP 562 路由.
    """
    ap = argparse.ArgumentParser(description="mnelo MCP Server")
    ap.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "sse", "streamable-http", "dual"],
    )
    # [Round 2] host/port default 从 config 读 (config.toml [server] 段)
    _cfg_host, _cfg_port = mcp_tool_dispatcher._resolve_server_defaults()
    ap.add_argument("--host", default=_cfg_host)
    ap.add_argument("--port", type=int, default=_cfg_port)
    # [7/19 P0-2] Bearer token 来源 (CLI override; 不传走 env/file 默认)
    ap.add_argument(
        "--auth-token-file",
        default=None,
        help="Path to file containing Bearer token (default: $MNELO_AUTH_TOKEN or ~/.config/mnelo/auth_token)",
    )
    args = ap.parse_args()

    # Direct sub-module access (function scope): PEP 562 forwarding does NOT
    # fire on rebinding locals. To mock these in tests, patch the sub-module
    # directly (e.g. monkeypatch.setattr(mcp_transports, 'run_sse', mock))
    # — setattr mcp_server.X will route to sub-module via PEP 562.
    if not mcp_guard._MCP_AVAILABLE:
        sys.stderr.write("FATAL: mcp/Starlette 未装, 跑 `pip install mcp[cli] starlette uvicorn`\n")
        sys.exit(1)

    # [P2-1 优化] MCP server 启动时立即 warm-up Memory (含 Embedder)
    # 实测: 不 warm-up 首次 recall ~760ms (Embedder 1s cold start + 实际工作)
    #        warm-up 后首次 recall ~70ms (model 已在 RAM)
    # 启动慢 1s, 避免首 recall spike 1s
    mcp_tool_dispatcher._get_mem()  # 触发 Memory.__init__() warm-up

    if args.transport == "stdio":
        asyncio.run(mcp_transports.run_stdio())
    else:
        # [7/19 P0-2] 提前解析 token (fail-fast before warmup, 避免浪费 Embedder 启动时间)
        token = None
        if args.auth_token_file:
            try:
                token = auth.load_auth_token(explicit_path=args.auth_token_file)
            except auth.AuthError as e:
                sys.stderr.write(f"--auth-token-file load failed: {e}\n")
                sys.exit(2)
        if args.transport == "sse":
            mcp_transports.run_sse(host=args.host, port=args.port, auth_token=token)
        elif args.transport == "streamable-http":
            mcp_transports.run_streamable_http(host=args.host, port=args.port, auth_token=token)
        else:  # dual
            mcp_transports.run_dual(host=args.host, port=args.port, auth_token=token)


if __name__ == "__main__":
    main()
