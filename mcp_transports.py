# === mcp_transports.py ===
# [refactor 2026-08-12] split from mcp_server.py.

# [refactor 2026-08-12] cross-module: _get_mem + _mem_instance live in dispatcher.
# Lazy import to avoid circular: dispatcher imports server wiring here, transports
# imports dispatcher. dispatcher → handlers (one direction), transports → dispatcher
# (one direction). No cycle.

import asyncio
import ipaddress
import logging
import socket
import sys
from pathlib import Path
from typing import Optional

from auth import AuthError, load_auth_token, verify_bearer
from config import config  # [Round 2] server host/port 配置
from mcp_guard import (
    _MCP_AVAILABLE,  # noqa: F401
    Mount,  # noqa: F401
    Route,  # noqa: F401
    SseServerTransport,  # noqa: F401
    Starlette,  # noqa: F401
    StreamableHTTPSessionManager,  # noqa: F401
    asynccontextmanager,  # noqa: F401
    stdio_server,  # noqa: F401
    uvicorn,  # noqa: F401
)
from mcp_tool_dispatcher import _get_mem, _mem_instance, _resolve_server_defaults, server  # noqa: F401  cross-module

# 路径 — [7/21 fix] 插入本文件所在目录 (repo root), 不再硬编码 live 路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("mnelo.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


async def run_stdio() -> None:
    """: 主路径 stdio transport (与 MCP 客户端对接).

    [refactor 2026-08-12] 通过 sys.modules['mcp_server'].stdio_server 读取, 这样
    测试用 `monkeypatch.setattr('mcp_server.stdio_server', mock)` 能拦截
    (直接读 module global 会绕过 monkeypatch — refactor 后需要走 facade).
    """
    import sys

    if not _MCP_AVAILABLE:
        raise RuntimeError("MCP libraries not available")
    _stdio_server = sys.modules["mcp_server"].stdio_server
    async with _stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def _mnelo_health_endpoint(request):
    """Public liveness/readiness endpoint with compact hygiene signals.

    [8/7] 提到模块级, 让 _build_sse_app 和 _build_streamable_app 共享
    (原 _build_sse_app 内 closure 依赖全部模块级, 无副作用).
    """
    from starlette.responses import JSONResponse

    target = _mem_instance
    try:
        if target is None:
            target = _get_mem()
        stats = target.stats()
        hygiene = stats.get("hygiene", {})
        backlog = int(hygiene.get("purge_backlog", 0))
        floor_count = int(hygiene.get("decay_floor_chunks", 0))
        backlog_limit = config.health_purge_backlog_threshold
        floor_limit = config.health_floor_chunks_threshold
        status = "degraded" if backlog > backlog_limit or floor_count > floor_limit else "ok"
        recommendations = []
        if status == "degraded":
            reasons = []
            if backlog > backlog_limit:
                reasons.append(f"purge backlog {backlog} > {backlog_limit}")
            if floor_count > floor_limit:
                reasons.append(f"floor chunks {floor_count} > {floor_limit}")
            recommendations = [
                {
                    "tool": "memory_maintenance",
                    "safe": True,
                    "reason": "; ".join(reasons),
                    "args": {
                        "passes": ["hygiene"],
                        "dry_run": True,
                        "confirm_destructive": False,
                    },
                },
                {
                    "tool": "memory_audit_list",
                    "safe": True,
                    "reason": "review recent hygiene proposals before destructive runs",
                    "args": {"pass_name": "hygiene", "limit": 20},
                },
            ]
        # [8/6 E 路线] PII advisory 24h count (audit_log, pass_name=pii_audit).
        # 不 block, 仅 surface 提醒调用方. 阈值高于 0 → 推荐用户自检.
        pii_24h = target._conn.execute(  # noqa: SLF001 (intentional private access for /health)
            "SELECT COUNT(*) FROM audit_log WHERE pass_name='pii_audit' AND created_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        pii_recommendation = None
        if pii_24h > 0:
            pii_recommendation = {
                "tool": "memory_audit_list",
                "safe": True,
                "reason": (
                    f"{pii_24h} chunks in the last 24h matched advisory PII patterns (credit card / email / cn mobile / id card / secret token); mnelo does NOT redact or refuse — caller decides."
                ),
                "args": {"pass_name": "pii_audit", "limit": 50},
            }

        return JSONResponse(
            {
                "status": status,
                "hygiene": {
                    "purge_backlog": backlog,
                    "importance_below_floor": floor_count,
                    "freshness": hygiene.get("freshness"),
                },
                "pii_warnings_last_24h": pii_24h,
                "recommendations": recommendations + ([pii_recommendation] if pii_recommendation else []),
            }
        )
    except Exception:
        logger.exception("health check failed")
        return JSONResponse(
            {
                "status": "degraded",
                "hygiene": {
                    "purge_backlog": None,
                    "importance_below_floor": None,
                    "freshness": None,
                },
                "recommendations": [],
            },
            status_code=503,
        )


async def _mnelo_metrics_endpoint(request):
    """[7/19 v0.5.3] /metrics endpoint (Prometheus text format).

    [8/7] 提到模块级, 共享给 SSE + streamable 双 transport.
    Bypasses Bearer auth (like /health in RUNBOOK spec). Refreshes DB
    stats with TTL caching so scrape doesn't hammer SQLite.
    """
    from starlette.responses import PlainTextResponse

    from metrics import get_registry

    reg = get_registry()
    # Refresh DB gauges (TTL=10s inside registry)
    try:
        target = _mem_instance
        if target is None:
            from memory import Memory as _Memory

            target = _Memory()
        reg.refresh_db_stats(target)
    except Exception:
        pass
    body = reg.render()
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


def _ip_in_tailscale_cgnat(ip_str: str) -> bool:
    """[8/8 Tailscale multi-agent] 检测 IP 是否在 Tailscale CGNAT 100.64.0.0/10.

    Tailscale 给 mesh 设备分配 100.64.0.0/10 (实际可见 100.64.0.0 - 100.127.255.255).
    这段是私网 + 跨 WAN mesh, 主人 8/8 拍板作为 multi-agent 远程通道.

    Returns:
        bool: True if IP in 100.64.0.0/10, False otherwise (含无效 IP).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Tailscale CGNAT = 100.64.0.0/10
    return ip in ipaddress.ip_network("100.64.0.0/10")


def _validate_loopback_host(host: str) -> None:
    """[8/8 Tailscale multi-agent] host 白名单 — 接受 loopback + Tailscale CGNAT.

    [8/8 决策] 主人拍板 mnelo 改成 multi-agent 远程调用. P2-1 单机 loopback
    限制解除, 接受:
      - 127.0.0.0/8 (loopback, 单机本地)
      - 100.64.0.0/10 (Tailscale CGNAT, 跨 vps mesh)
      - localhost (DNS alias)

    拒绝 (LAN/公网/IPv6 攻击面):
      - 192.168.x.x / 10.x.x.x / 172.16-31.x.x (LAN)
      - 公网 IP (8.8.8.8 等)
      - IPv6 (Tailscale multi-agent 现阶段只走 IPv4)

    单独路径 (不验证):
      - 0.0.0.0 / :: (bind 任意, 启动后用 ipfilter 限来源)

    [8/9 review B6] host=0.0.0.0 / :: 走任意 bind 路径, 网络层只有 Bearer
    token 单点认证. 加 stderr warn 明示: 没 ipfilter = Bearer 是唯一防线.
    主人需要可在 [server] config.toml 加 ipfilter_cidrs (CIDR list) 限制
    来源 IP. 当前实现: warn only, 不强制, 不破坏 backward compat.

    Raises:
        ValueError: host 不在白名单
    """
    # bind 任意地址 — 单独路径, 启动后由 ipfilter / OS firewall 限制来源
    if host in ("0.0.0.0", "::"):
        # [8/9 review B6] 明示风险: 无 ipfilter, Bearer 是唯一防线.
        print(
            f"[WARN] bind '{host}' = 任意接口. 当前仅 Bearer token 鉴权.",
            file=sys.stderr,
        )
        print(
            "[WARN]   建议: config.toml [server] 加 ipfilter_cidrs 限制来源 IP,",
            file=sys.stderr,
        )
        print(
            "[WARN]         例: ipfilter_cidrs = ['100.64.0.0/10'] (Tailscale CGNAT only)",
            file=sys.stderr,
        )
        return
    # localhost alias
    if host == "localhost":
        return
    # IPv4 loopback (127.0.0.0/8)
    if host.startswith("127."):
        try:
            ip = ipaddress.ip_address(host)
            if ip in ipaddress.ip_network("127.0.0.0/8"):
                return
        except ValueError:
            pass
    # Tailscale CGNAT
    if _ip_in_tailscale_cgnat(host):
        return
    raise ValueError(
        f"--host {host!r} not allowed. mnelo host must be loopback (127.0.0.0/8) "
        f"or Tailscale CGNAT (100.64.0.0/10). For LAN/public access, "
        f"add Tailscale to the agent machine and use the Tailscale IP."
    )


def _check_port_available(host: str, port: int) -> bool:
    """[P2-2] 启动前试 bind 端口. 返回 True if free, False if in use.

    Raises:
        OSError: 非 port-in-use 的其他 socket error (向上传播)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        sock.close()
        return False


def _build_sse_app(auth_token: str) -> "Starlette":
    """[P0-2] 构建 SSE Starlette app: routes + Bearer auth middleware.

    Args:
        auth_token: 已加载的 Bearer token (不能为空)
    """
    from starlette.responses import JSONResponse

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        from starlette.responses import JSONResponse, Response

        # 不再用 BaseHTTPMiddleware (SSE 直发与其中间件 body_stream 不兼容),
        # /sse 与 /messages/ 的 Bearer 鉴权各自独立实现, 这里自己校验.
        auth_header = request.headers.get("authorization", "")
        if not verify_bearer(auth_header, auth_token):
            logger.warning(f"rejected GET /sse from {request.client.host if request.client else '?'} - invalid/missing token")
            return JSONResponse(
                {"error": "unauthorized", "detail": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnelo-mcp"'},
            )

        async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        # mcp SDK sse.py docstring: handle_sse 必须返回 Response, 否则 client 断开时
        # starlette 调 None(...) 报 "TypeError: 'NoneType' object is not callable"
        return Response()

    async def handle_health(request):
        """[8/7] 委托到模块级 _mnelo_health_endpoint (SSE + streamable 共享)."""
        return await _mnelo_health_endpoint(request)

    async def handle_metrics(request):
        """[8/7] 委托到模块级 _mnelo_metrics_endpoint."""
        return await _mnelo_metrics_endpoint(request)

    async def handle_messages(scope, receive, send):
        """/messages/ 的 Bearer 鉴权 ASGI 包装.

        BaseHTTPMiddleware 包 SSE 端点会在断开时 body_stream 断言崩 (SSE 响应绕过
        中间件 send_stream), 所以这里用纯 ASGI 写鉴权, 不经过 BaseHTTPMiddleware.
        """
        auth_header = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                auth_header = v.decode("latin-1")
                break
        if not verify_bearer(auth_header, auth_token):
            client = scope.get("client")
            logger.warning(f"rejected {scope.get('method', '?')} {scope.get('path', '?')} from {client[0] if client else '?'} - invalid/missing token")
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnelo-mcp"'},
            )
            await response(scope, receive, send)
            return
        await sse.handle_post_message(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/health", endpoint=handle_health),
            Route("/metrics", endpoint=handle_metrics),  # [7/19 v0.5.3] Prometheus
            Mount("/messages/", app=handle_messages),
        ]
    )
    return app


def _build_streamable_app(auth_token: str) -> "Starlette":
    """[8/7] Streamable HTTP transport (MCP 2025-03-26 spec).

    与 SSE 共享同一端口 (路径不同):
      - /sse + /messages/  → SSE 客户端 (hermes gateway 现状)
      - /mcp               → streamable-http (Claude Desktop / Cursor / 任意 agent)
      - /health + /metrics → 公用

    设计决策 (为啥走 stateless=True):
    - 多 agent 同时调 /mcp 不需要维护 session 状态, 每次 request fresh transport
    - 没有 "client 离开后 session 泄漏" 问题
    - server.run stateless=True 走 anyio task_group 起短任务,
      对短查询 (memory_recall / memory_remember) 开销 < 50ms
    - stateful 模式会留下 orphan session, 不适合此场景

    [为啥用 Route 不是 Mount]
    Mount("/mcp") 默认会 307 redirect → /mcp/ (trailing slash), 而 MCP Python client
    (mcp/client/streamable_http.py line 341) 默认 follow_redirects=False, 会收到 307
    当作 error 处理. Route("/mcp", endpoint=...) exact-match, 不走 redirect.

    [Bearer auth] endpoint 内部读 request.headers (跟 SSE /messages/ 同样的写法).
    """
    from starlette.responses import JSONResponse

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,  # SSE-style streaming (客户端可监听 server push)
        stateless=True,  # 多客户端无 session 冲突
    )

    class _MCPASGI:
        """[8/7] /mcp ASGI callable instance.

        Starlette Route 区分 endpoint 类型:
          - function/method → wrap 成 request_response(func) (期望返回 Response)
          - class instance (callable) → 当 ASGI app 直接调 __call__(scope, recv, send)
        我们要走 ASGI 路径 (manager.handle_request 自己通过 send 写响应),
        所以用 class instance, 不要 async def function.
        """

        def __init__(self):
            self.manager = manager
            self.auth_token = auth_token

        async def __call__(self, scope, receive, send):
            auth_header = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    auth_header = v.decode("latin-1")
                    break
            if not verify_bearer(auth_header, self.auth_token):
                client = scope.get("client")
                logger.warning(f"rejected {scope.get('method', '?')} /mcp from {client[0] if client else '?'} - invalid/missing token")
                response = JSONResponse(
                    {"error": "unauthorized", "detail": "Bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mnelo-mcp-streamable"'},
                )
                await response(scope, receive, send)
                return
            await self.manager.handle_request(scope, receive, send)

    mcp_asgi = _MCPASGI()

    @asynccontextmanager
    async def lifespan(_app):
        """启动 streamable_http task_group, 客户端请求来时 manager 才 spawn server.run task."""
        async with manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=mcp_asgi, methods=["GET", "POST", "DELETE"]),
            Route("/health", endpoint=_mnelo_health_endpoint),
            Route("/metrics", endpoint=_mnelo_metrics_endpoint),
        ],
        lifespan=lifespan,
    )
    return app


def run_streamable_http(host: Optional[str] = None, port: Optional[int] = None, auth_token: Optional[str] = None) -> None:
    """[8/7] Streamable HTTP transport 入口 (跟 run_sse 镜像).

    [8/7 P1] host 只接受 loopback (跟 run_sse 同安全策略).
    [8/7 P2] Bearer token 复用 run_sse 加载逻辑 (load_auth_token()).
    [8/7 P3] port 默认从 config.server_port 读 (跟 SSE 共用 8086).
    [8/14 P1 fix] 内部用 module-attribute call (getattr(auth, 'load_auth_token')())
    走 facade forwarding — 这样 `monkeypatch.setattr('mcp_server.load_auth_token', mock)`
    (或 `monkeypatch.setattr(auth, 'load_auth_token', mock)`) 都能拦截.
    """
    import auth as _auth_mod

    if host is None or port is None:
        cfg_host, cfg_port = _resolve_server_defaults()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port

    if not _MCP_AVAILABLE:
        raise RuntimeError("MCP/Starlette not available")

    if auth_token is None:
        try:
            auth_token = _auth_mod.load_auth_token()
        except AuthError as e:
            logger.error(f"streamable_http transport requires auth token: {e}")
            raise
    logger.info("streamable_http auth: Bearer token loaded (length=%d chars)", len(auth_token))

    _validate_loopback_host(host)
    if not _check_port_available(host, port):
        logger.warning(f"port {port} already in use on {host}; exiting cleanly")
        return

    app = _build_streamable_app(auth_token)
    logger.info(f"mnelo MCP streamable-http listening on http://{host}:{port}/mcp (Bearer auth ON)")
    uvicorn.run(app, host=host, port=port, log_level="info")


def _build_dual_app(auth_token: str) -> "Starlette":
    """[8/7] Dual transport: SSE + streamable-http 同进程同端口.

    为什么需要 dual:
      - hermes gateway 现状配的是 SSE (/sse + /messages/) → 不能破
      - 新 agent (Claude Desktop / Cursor / 助手 直接调) → 需 streamable-http
      - 单 process 两个 transport 共享同一端口 8086, paths 分流, launchd plist 不动

    Routes:
      - GET  /sse         → SSE handshake (hermes gateway)
      - POST /messages/   → SSE message channel (hermes gateway)
      - *    /mcp         → streamable-http (新 agent / 可选)
      - GET  /health      → hygiene JSON
      - GET  /metrics     → Prometheus
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response

    sse = SseServerTransport("/messages/")

    streamable_mgr = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=True,
    )

    async def sse_endpoint(request: Request) -> Response:
        """/sse Route endpoint — 原有 handle_sse 的 Route 版本 (auth + sse.connect)."""
        auth_header = request.headers.get("authorization", "")
        if not verify_bearer(auth_header, auth_token):
            client = request.client
            logger.warning(f"rejected GET /sse from {client.host if client else '?'} - invalid/missing token")
            return JSONResponse(
                {"error": "unauthorized", "detail": "Bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mnelo-mcp"'},
            )
        async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
        return Response()

    class _MessagesASGI:
        """/messages/ ASGI handler (Bearer auth + sse.handle_post_message)."""

        def __init__(self):
            self.sse = sse
            self.auth_token = auth_token

        async def __call__(self, scope, receive, send):
            auth_header = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    auth_header = v.decode("latin-1")
                    break
            if not verify_bearer(auth_header, self.auth_token):
                client = scope.get("client")
                logger.warning(f"rejected {scope.get('method', '?')} {scope.get('path', '?')} from {client[0] if client else '?'} - invalid/missing token")
                response = JSONResponse(
                    {"error": "unauthorized", "detail": "Bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mnelo-mcp"'},
                )
                await response(scope, receive, send)
                return
            await self.sse.handle_post_message(scope, receive, send)

    class _MCPASGI:
        """/mcp ASGI handler (Bearer auth + streamable_mgr.handle_request)."""

        def __init__(self):
            self.manager = streamable_mgr
            self.auth_token = auth_token

        async def __call__(self, scope, receive, send):
            auth_header = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    auth_header = v.decode("latin-1")
                    break
            if not verify_bearer(auth_header, self.auth_token):
                client = scope.get("client")
                logger.warning(f"rejected {scope.get('method', '?')} /mcp from {client[0] if client else '?'} - invalid/missing token")
                response = JSONResponse(
                    {"error": "unauthorized", "detail": "Bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mnelo-mcp-streamable"'},
                )
                await response(scope, receive, send)
                return
            await self.manager.handle_request(scope, receive, send)

    messages_asgi = _MessagesASGI()
    mcp_asgi = _MCPASGI()

    @asynccontextmanager
    async def lifespan(_app):
        """启动 streamable_http task_group. SSE 不需要 lifespan (per-request connect_sse)."""
        async with streamable_mgr.run():
            yield

    app = Starlette(
        routes=[
            Route("/sse", endpoint=sse_endpoint),
            Route("/mcp", endpoint=mcp_asgi, methods=["GET", "POST", "DELETE"]),
            Mount("/messages/", app=messages_asgi),
            Route("/health", endpoint=_mnelo_health_endpoint),
            Route("/metrics", endpoint=_mnelo_metrics_endpoint),
        ],
        lifespan=lifespan,
    )
    return app


def run_dual(host: Optional[str] = None, port: Optional[int] = None, auth_token: Optional[str] = None) -> None:
    """[8/7] Dual transport 入口 (SSE + streamable-http 同进程同端口).

    与 run_sse / run_streamable_http 镜像, 但构造 dual app 同时挂 SSE + streamable.
    Launchd plist 仅需改 --transport dual, host/port/Bearer 不动.
    """
    if host is None or port is None:
        cfg_host, cfg_port = _resolve_server_defaults()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port

    if not _MCP_AVAILABLE:
        raise RuntimeError("MCP/Starlette not available")

    if auth_token is None:
        try:
            auth_token = load_auth_token()
        except AuthError as e:
            logger.error(f"dual transport requires auth token: {e}")
            raise
    logger.info("dual auth: Bearer token loaded (length=%d chars)", len(auth_token))

    _validate_loopback_host(host)
    if not _check_port_available(host, port):
        logger.warning(f"port {port} already in use on {host}; exiting cleanly")
        return

    app = _build_dual_app(auth_token)
    logger.info(f"mnelo MCP DUAL listening on http://{host}:{port} (SSE=/sse+/messages/, streamable-http=/mcp, /health, /metrics; Bearer auth ON)")
    uvicorn.run(app, host=host, port=port, log_level="info")


def run_sse(host: Optional[str] = None, port: Optional[int] = None, auth_token: Optional[str] = None) -> None:
    """: SSE transport (与 launchd 兼容).

    [7/19 P2-1] host 只接受 loopback (127.x / ::1 / localhost), 拒绝 0.0.0.0 / LAN IP
    防止误传把整个 LAN 暴露出去 (本地任何端口暴露都是 P0 风险)

    [7/19 P0-2] Bearer token auth:
    - auth_token 显式传入 → 用 (CLI --auth-token-file 模式)
    - 没传 → 调 load_auth_token() 从 env/file 读
    - 都没 → fail-fast

    [Round 2] host/port 不传 → 从 config.server_host/server_port 读 (config.toml [server] 段)

    [8/14 P1 fix] 内部用 `_auth_mod.load_auth_token()` 走 facade forwarding —
    `monkeypatch.setattr('mcp_server.load_auth_token', mock)` 或 setattr auth module 都生效.
    """
    import auth as _auth_mod

    # 1. resolve host/port from config if not provided
    if host is None or port is None:
        cfg_host, cfg_port = _resolve_server_defaults()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port

    if not _MCP_AVAILABLE:
        raise RuntimeError("MCP/Starlette not available")

    # 2. Bearer token 加载 (fail-fast)
    if auth_token is None:
        try:
            auth_token = _auth_mod.load_auth_token()
        except AuthError as e:
            logger.error(f"SSE transport requires auth token: {e}")
            raise
    logger.info("SSE auth: Bearer token loaded (length=%d chars)", len(auth_token))

    # 3. validate + port pre-check
    _validate_loopback_host(host)
    if not _check_port_available(host, port):
        logger.warning(f"port {port} already in use on {host}; exiting cleanly")
        return  # 让 launchd KeepAlive 自然接管

    # 4. build app + run
    app = _build_sse_app(auth_token)
    logger.info(f"mnelo MCP SSE listening on http://{host}:{port}/sse (Bearer auth ON)")
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http", "dual"])
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
        logger.error("MCP libraries missing. Install: pip install mcp[cli] starlette uvicorn")
        sys.exit(1)

    # [P2-1 优化] MCP server 启动时立即 warm-up Memory (含 Embedder)
    # 实测: 不 warm-up 首次 recall ~760ms (Embedder 1s cold start + 实际工作)
    #        warm-up 后首次 recall ~70ms (model 已在 RAM)
    # 启动慢 1s, 避免首 recall spike 1s
    logger.info("[P2-1] Pre-warming Memory + Embedder at MCP server startup...")
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
                logger.error(f"--auth-token-file load failed: {e}")
                sys.exit(2)
        if args.transport == "sse":
            run_sse(host=args.host, port=args.port, auth_token=token)
        elif args.transport == "streamable-http":
            run_streamable_http(host=args.host, port=args.port, auth_token=token)
        else:  # dual
            run_dual(host=args.host, port=args.port, auth_token=token)


if __name__ == "__main__":
    main()
