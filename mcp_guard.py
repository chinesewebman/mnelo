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
        AnyUrl,
        ListResourcesRequest,
        ReadResourceRequest,
        ReadResourceRequestParams,
        Resource,
        TextContent,
        Tool,
    )
    from starlette.applications import Starlette  # noqa: F401  re-export
    from starlette.routing import Mount, Route  # noqa: F401  re-export

    _MCP_AVAILABLE = True
except ImportError as e:
    logger.warning(f"MCP/Starlette not fully available: {e}")
