#!/usr/bin/env python3
"""mcp_guard.py — _MCP_AVAILABLE flag + guarded imports."""

import logging

logger = logging.getLogger("mnelo.mcp")

# Guarded imports — mcp/Starlette/uvicorn are heavy
_MCP_AVAILABLE = False
try:
    from contextlib import asynccontextmanager  # noqa: F401  re-export

    import uvicorn  # noqa: F401  re-export
    from mcp.server import Server  # noqa: F401  re-export
    from mcp.server.sse import SseServerTransport  # noqa: F401  re-export
    from mcp.server.stdio import stdio_server  # noqa: F401  re-export
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: F401  re-export
    from mcp.types import (  # noqa: F401  re-export
        ReadResourceRequestParams,
        Resource,
        TextContent,
        Tool,
    )

    # mcp 1.27+ removed AnyUrl from mcp.types (moved to pydantic).
    # Re-export from pydantic so downstream `from mcp_guard import AnyUrl` works.
    # [v0.81.7 P1-2 review fix v2] pydantic missing 时 raise 让外层 except 接管
    # 设 _MCP_AVAILABLE=False. 之前在 try 块内 set False 然后被 line 37 _MCP_AVAILABLE=True
    # 覆盖 — owner bias 漏看的 bug. 用 raise ImportError 是唯一可靠信号.
    try:
        from pydantic import AnyUrl  # noqa: F401  re-export
    except ImportError as e:
        logger.error(f"pydantic missing — AnyUrl re-export unavailable, mnelo MCP disabled: {e}")
        raise
    from starlette.applications import Starlette  # noqa: F401  re-export
    from starlette.routing import Mount, Route  # noqa: F401  re-export

    _MCP_AVAILABLE = True
except ImportError as e:
    _MCP_AVAILABLE = False
    logger.warning(f"MCP/Starlette not fully available: {e}")
