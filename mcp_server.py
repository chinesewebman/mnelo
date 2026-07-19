#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp_server.py — mnelo MCP Server (替代 Mnemosyne MCP)

- 主人口中 7/18 拍板 A+C 方案: 写 mnelo (前身 hermes-memory) mcp + 杀 Mnemosyne MCP
- 7/19 v0.5.0 breaking change: 变量名 `HERMES_MEMORY_*` → `MNELO_MEMORY_*`, `MNELO_HOME` → `MNELO_HOME`
- 接口: memory_remember / memory_recall / memory_relate / memory_forget
       / memory_update / memory_graph_query / memory_stats
- 7 工具, 与 hermes-memory v1.0 6 API + 1 个 stats 完美对齐
- SSE transport on 127.0.0.1:8086 (与 Mnemosyne 同端口, 无缝替换)

[运行]
    cd LIVE_ROOT && python3 mcp_server.py --transport sse
    (port 走 config: env MNELO_MEMORY_SERVER_PORT > toml [server].port > 8086)
"""
import sys
import os
import json
import sqlite3
import asyncio
import socket
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from validation import ValidationError
from auth import load_auth_token, verify_bearer, AuthError
from config import config  # [Round 2] server host/port 配置

# 路径
sys.path.insert(0, '/Users/apple/.hermes/memory')

logger = logging.getLogger('mnelo.mcp')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '[%(asctime)s] %(name)s %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)

# Guarded import
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.server.sse import SseServerTransport
    from mcp.types import TextContent, Tool
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.responses import Response
    import uvicorn
    _MCP_AVAILABLE = True
except ImportError as e:
    _MCP_AVAILABLE = False
    logger.warning(f'MCP/Starlette not fully available: {e}')

# [P0 审计] 复用 memory.now() / memory._with_row_factory, 删 _dt_now 重复
import memory as memory_module

# : 单进程单 Memory 实例 ( lock 风险归零)
_mem_instance: Optional[Any] = None


def _get_mem() -> Any:
    """单例 Memory."""
    global _mem_instance
    if _mem_instance is None:
        from memory import Memory
        _mem_instance = Memory()
        logger.info(f'hermes-memory MCP ready (db: {Path("/Users/apple/.hermes/memory/memory.db")})')
    return _mem_instance


# === 工具 schema (7 个 MCP tools) ===

TOOLS = [
    {
        'name': 'memory_remember',
        'description': '写入一条 chunk + 实体 + 关系到 hermes-memory. 返回 chunk_id.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string', 'description': '正文内容 (必填)'},
                'source': {'type': 'string', 'description': '来源 (master:0029, trinity_daily:part1, etc.)', 'default': 'manual'},
                'importance': {'type': 'number', 'description': '0.0-1.0, 默认 0.5', 'default': 0.5},
                'entities': {'type': 'array', 'description': '[{id, kind, name, summary?, aliases?, properties?}]'},
                'relations': {'type': 'array', 'description': '[{source_id, target_id, relation, weight?, properties?, valid_from?, valid_until?, evidence_chunk_id?}]'},
                'tags': {'type': 'array', 'description': '["finance", "weng-resonance"]'},
                'session_id': {'type': 'string', 'default': 'default'},
                'timestamp': {'type': 'string', 'description': 'ISO 8601, None=now'},
            },
            'required': ['content'],
        },
    },
    {
        'name': 'memory_recall',
        'description': '3 路召回 (向量 + 图遍历 + 元数据) + RRF 融合.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '查询文本 (必填)'},
                'top_k': {'type': 'integer', 'default': 5},
                'graph_hops': {'type': 'integer', 'default': 2},
                'filters': {'type': 'object', 'description': '{kind, source, tag, time_range}'},
                'strategy': {'type': 'string', 'enum': ['rrf', 'vector_only', 'graph_only', 'meta_only', 'entity_only'], 'default': 'rrf'},
                'asof': {'type': 'string', 'description': 'ISO 8601 时间切片, None=now'},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'memory_relate',
        'description': '新建一条关系.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'source_id': {'type': 'string'},
                'target_id': {'type': 'string'},
                'relation': {'type': 'string'},
                'weight': {'type': 'number', 'default': 1.0},
                'valid_from': {'type': 'string'},
                'valid_until': {'type': 'string'},
                'evidence_chunk_id': {'type': 'string'},
                'properties': {'type': 'object'},
            },
            'required': ['source_id', 'target_id', 'relation'],
        },
    },
    {
        'name': 'memory_forget',
        'description': '软删除 entity/chunk/relation (valid_until = now). 触发器自动级联.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'target_id': {'type': 'string'},
                'target_kind': {'type': 'string', 'enum': ['chunk', 'entity', 'relation'], 'default': 'chunk'},
                'reason': {'type': 'string', 'default': 'outdated'},
                'cascade': {'type': 'boolean', 'default': True},
            },
            'required': ['target_id'],
        },
    },
    {
        'name': 'memory_update',
        'description': '"更新": 创建新 chunk + 老 chunk superseded_by. 不覆盖.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'old_id': {'type': 'string'},
                'reason': {'type': 'string', 'default': 'updated'},
                'new_content': {'type': 'string'},
                'new_properties': {'type': 'object'},
                'new_importance': {'type': 'number'},
            },
            'required': ['old_id'],
        },
    },
    {
        'name': 'memory_graph_query',
        'description': '图遍历: start_node 起 max_hops 跳内的子图.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'start_node': {'type': 'string'},
                'max_hops': {'type': 'integer', 'default': 3},
                'edge_types': {'type': 'array', 'description': 'list of relation names, None=all'},
                'asof': {'type': 'string'},
            },
            'required': ['start_node'],
        },
    },
    {
        'name': 'memory_stats',
        'description': '统计: entities/chunks/relations/vectors/recall_log 数量.',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    # === [v1.1] 新增 3 个工具 ===
    {
        'name': 'memory_entity_resolve',
        'description': '实体消歧: 找疑似重复 entity (alias/name 相似度).',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'threshold': {'type': 'number', 'description': '相似度阈值 0.0-1.0, 默认 0.85', 'default': 0.85},
                'kind': {'type': 'string', 'description': 'kind 过滤 (stock/concept/person/...), None=全部'},
            },
        },
    },
    {
        'name': 'memory_list_entities',
        'description': '列实体, 按 kind/importance 过滤.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'kind': {'type': 'string'},
                'min_importance': {'type': 'number', 'default': 0.0},
                'limit': {'type': 'integer', 'default': 50},
            },
        },
    },
    {
        'name': 'memory_search_relations',
        'description': '按 relation 类型搜索关系.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'relation': {'type': 'string', 'description': 'relation 名 (_关注_于 / 翁氏_共振_BUY_于)'},
                'asof': {'type': 'string'},
                'limit': {'type': 'integer', 'default': 100},
            },
        },
    },
]


# === Tool dispatch ===
#
# P0 审计: 之前 _call_tool 是 80 行 if/elif 链 (10 个分支, 8 个简单委托 + 2 个自定义)
# 现在抽 TOOL_REGISTRY: 简单委托走通用 wrapper, 自定义逻辑走 _custom_handlers.
# 减 ~50 行, 加 ~5 行.

# [Round 1 quality audit] 抽常量避免 magic numbers 散落
# [Round 2] DEFAULT 从 config.server_host/port 读, 仍保留常量作 fallback
DEFAULT_SSE_HOST = '127.0.0.1'      # P2-1: loopback-only fallback
DEFAULT_SSE_PORT = 8086             # SSE 默认端口 fallback (config 优先)


def _resolve_server_defaults() -> tuple:
    """从 config 解析 SSE host/port 默认值. CLI flag 优先于 config."""
    try:
        cfg = config  # 来自 mcp_server 顶部 from config import config
        return cfg.server_host, cfg.server_port
    except Exception:
        return DEFAULT_SSE_HOST, DEFAULT_SSE_PORT

# [7/19 P2-3] 简易 in-memory rate limit (防 runaway loop / 滥用)
# key=tool 名, value=[window_start_ts, count_in_window]
_RATE_LIMIT_WINDOW_SEC = 60
_RATE_LIMIT_MAX_REQS = 60  # 每分钟每 tool 最多 60 次 (实测 recall ~50ms, 足够人用)


def _rate_limit_check(tool_name: str) -> None:
    """In-process sliding-window rate limit. 超限抛 ValidationError."""
    import time as _time
    now_ts = _time.time()
    bucket = _RATE_BUCKETS.get(tool_name)
    if bucket is None or now_ts - bucket[0] > _RATE_LIMIT_WINDOW_SEC:
        _RATE_BUCKETS[tool_name] = [now_ts, 1]
        return
    bucket[1] += 1
    if bucket[1] > _RATE_LIMIT_MAX_REQS:
        raise ValidationError(
            tool_name,
            f'rate limit: {_RATE_LIMIT_MAX_REQS} reqs / {_RATE_LIMIT_WINDOW_SEC}s exceeded'
        )


_RATE_BUCKETS: Dict[str, list] = {}

_TOOL_REGISTRY = {
    # name -> (mem method attr, response id field name or None)
    'memory_remember': ('remember', 'chunk_id'),
    'memory_recall': ('recall', None),
    'memory_relate': ('relate', 'relation_id'),
    'memory_forget': ('forget', None),
    'memory_update': ('update', 'new_chunk_id'),
    'memory_graph_query': ('graph_query', None),
    'memory_stats': ('stats', None),
}


def _handle_simple(mem, name: str, args: Dict) -> str:
    """Generic dispatcher: call Memory.<method>(**args), wrap result in JSON.

    P0 审计: 之前 8 个 if/elif 分支都是同一模式 `result = mem.xxx(**args); json.dumps(...)`,
    现在统一 wrapper. id_field 为 None 时直接序列化 result; 否则 wrap 成 `{id_field: result, status: 'ok'}`.
    """
    attr_name, id_field = _TOOL_REGISTRY[name]
    result = getattr(mem, attr_name)(**args)
    if id_field:
        return json.dumps({id_field: result, 'status': 'ok'}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


def _handle_entity_resolve(mem, args: Dict) -> str:
    """[v1.1] Find duplicate entity candidates via entity_resolve module.

    Args:
        args.threshold: similarity threshold [0.0, 1.0], default 0.85
        args.kind: filter by entity kind (optional)

    Returns:
        {'candidates': [{'a', 'b', 'score', 'reason'}], 'count': N}
    """
    from entity_resolve import find_duplicate_candidates
    # [Round 3 fix] 加 max_pairs=500 cap 防 live DB hang
    # (5K entities → 12.5M O(N²) pairs, 数十秒 difflib 计算)
    with memory_module._with_row_factory(mem._conn, sqlite3.Row):
        candidates = find_duplicate_candidates(
            mem._conn,
            threshold=args.get('threshold', 0.85),
            kind=args.get('kind'),
            max_pairs=args.get('max_pairs', 500),
        )
    out = [{'a': a, 'b': b, 'score': s, 'reason': r}
           for a, b, s, r in candidates]
    return json.dumps({'candidates': out, 'count': len(out)}, ensure_ascii=False)


def _handle_list_entities(mem, args: Dict) -> str:
    """[v1.1] List entities filtered by kind/min_importance, ordered by importance DESC.

    Args:
        args.kind: filter by entity kind (e.g. 'stock', 'identity_fact')
        args.min_importance: minimum importance threshold [0.0, 1.0]
        args.limit: max results, default 50

    Returns:
        {'entities': [{'id', 'kind', 'name', 'summary', 'importance'}], 'count': N}
    """
    sql = 'SELECT id, kind, name, summary, importance FROM entities WHERE valid_until IS NULL'
    params = []
    if args.get('kind'):
        sql += ' AND kind = ?'
        params.append(args['kind'])
    if args.get('min_importance'):
        sql += ' AND importance >= ?'
        params.append(args['min_importance'])
    sql += ' ORDER BY importance DESC LIMIT ?'
    params.append(args.get('limit', 50))
    rows = mem._conn.execute(sql, params).fetchall()
    entities = [{'id': r[0], 'kind': r[1], 'name': r[2],
                 'summary': r[3], 'importance': r[4]} for r in rows]
    return json.dumps({'entities': entities, 'count': len(entities)}, ensure_ascii=False)


def _handle_search_relations(mem, args: Dict) -> str:
    """[v1.1] Search relations by relation type, with time-as-of filter.

    Args:
        args.relation (required): relation type string (e.g. 'owns', 'references')
        args.asof: ISO 8601 timestamp, default = now()
        args.limit: max results, default 100

    Returns:
        {'relations': [{'id', 'source_id', 'target_id', 'relation', 'weight', 'valid_from', 'valid_until'}], 'count': N}
    """
    asof = args.get('asof') or memory_module.now()
    sql = '''
        SELECT id, source_id, target_id, relation, weight, valid_from, valid_until
        FROM relations
        WHERE relation = ?
          AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY weight DESC, valid_from DESC
        LIMIT ?
    '''
    rows = mem._conn.execute(sql, (
        args['relation'], asof, asof, args.get('limit', 100)
    )).fetchall()
    relations = [{'id': r[0], 'source_id': r[1], 'target_id': r[2],
                  'relation': r[3], 'weight': r[4],
                  'valid_from': r[5], 'valid_until': r[6]}
                 for r in rows]
    return json.dumps({'relations': relations, 'count': len(relations)}, ensure_ascii=False)


# Custom handlers — 不走 TOOL_REGISTRY, 因有特殊 SQL 或依赖
_CUSTOM_HANDLERS = {
    'memory_entity_resolve': _handle_entity_resolve,
    'memory_list_entities': _handle_list_entities,
    'memory_search_relations': _handle_search_relations,
}


def _call_tool(name: str, args: Dict) -> str:
    """统一处理 10 个工具调用, 返回 JSON 字符串.

    [7/19 P1-3] except 返回 type name + 简短 reason, 不带原始 str(e)
    (避免泄露内部路径 / SQL 错误细节 / stack hint 给 MCP client).
    logger.exception 仍保留全 traceback 给 operator (操作员查 ~/.hermes/logs/).
    """
    mem = _get_mem()
    # [7/19 P2-3] rate limit 在 dispatch 前, 防 owner infinite loop 拖死 MCP server
    try:
        _rate_limit_check(name)
    except ValidationError as ve:
        logger.warning(f'call_tool {name} rate-limited')
        return json.dumps({'error': str(ve), 'tool': name, 'type': 'rate_limit'},
                          ensure_ascii=False)
    try:
        if name in _TOOL_REGISTRY:
            return _handle_simple(mem, name, args)
        if name in _CUSTOM_HANDLERS:
            return _CUSTOM_HANDLERS[name](mem, args)
        return json.dumps({'error': f'unknown tool: {name}'}, ensure_ascii=False)
    except ValidationError as ve:
        # validation 错误是 user-facing 的, message 安全 (不带原始 input)
        logger.warning(f'call_tool {name} validation: {ve.field}: {ve.reason}')
        return json.dumps({'error': str(ve), 'tool': name, 'type': 'validation'},
                          ensure_ascii=False)
    except Exception as e:
        logger.exception(f'call_tool {name} failed')
        # 只返 type name (e.g. "ValueError", "sqlite3.OperationalError"), 不带 str(e)
        return json.dumps({
            'error': type(e).__name__,
            'tool': name,
            'type': 'internal',
            # 'detail' 字段只在调试模式 (MNELO_MEMORY_DEBUG=1) 暴露
            'detail': str(e) if os.environ.get('MNELO_MEMORY_DEBUG') == '1' else None,
        }, ensure_ascii=False)


# === MCP server ===

if _MCP_AVAILABLE:
    server = Server('hermes-memory')

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return [Tool(**t) for t in TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict) -> List[TextContent]:
        result_json = _call_tool(name, arguments)
        return [TextContent(type='text', text=result_json)]


# === 启动入口 ===

async def run_stdio() -> None:
    """: 主路径 stdio transport (与 MCP 客户端对接)."""
    if not _MCP_AVAILABLE:
        raise RuntimeError('MCP libraries not available')
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _validate_loopback_host(host: str) -> None:
    """[P2-1] host 白名单 — 只接 loopback, 拒绝 0.0.0.0 / LAN IP.

    Raises:
        ValueError: host 不在 loopback 范围
    """
    if host != '127.0.0.1' and host != 'localhost' and not host.startswith('127.'):
        raise ValueError(
            f'--host {host!r} not allowed. mnelo SSE is loopback-only for security. '
            f'Pass 127.0.0.1 or localhost. For LAN access, '
            f'use SSH tunnel or VPN instead.'
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


def _build_sse_app(auth_token: str) -> 'Starlette':
    """[P0-2] 构建 SSE Starlette app: routes + Bearer auth middleware.

    Args:
        auth_token: 已加载的 Bearer token (不能为空)
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class _BearerAuthMiddleware(BaseHTTPMiddleware):
        """校验 Authorization: Bearer <token> header. 用 hmac.compare_digest 防 timing attack."""
        async def dispatch(self, request, call_next):
            # /health 路径不需 auth (允许健康检查 / 监控)
            if request.url.path == '/health':
                return await call_next(request)
            # SSE + messages 路由都需 Bearer token
            auth_header = request.headers.get('authorization', '')
            if not verify_bearer(auth_header, auth_token):
                logger.warning(
                    f'rejected {request.method} {request.url.path} from '
                    f'{request.client.host if request.client else "?"} - invalid/missing token'
                )
                return JSONResponse(
                    {'error': 'unauthorized', 'detail': 'Bearer token required'},
                    status_code=401,
                    headers={'WWW-Authenticate': 'Bearer realm="mnelo-mcp"'},
                )
            return await call_next(request)

    sse = SseServerTransport('/messages/')

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    app = Starlette(
        routes=[
            Route('/sse', endpoint=handle_sse),
            Mount('/messages/', app=sse.handle_post_message),
        ]
    )
    app.add_middleware(_BearerAuthMiddleware)
    return app


def run_sse(host: Optional[str] = None, port: Optional[int] = None,
            auth_token: Optional[str] = None) -> None:
    """: SSE transport (与 launchd 兼容).

    [7/19 P2-1] host 只接受 loopback (127.x / ::1 / localhost), 拒绝 0.0.0.0 / LAN IP
    防止误传把整个 LAN 暴露出去 (本地任何端口暴露都是 P0 风险)

    [7/19 P0-2] Bearer token auth:
    - auth_token 显式传入 → 用 (CLI --auth-token-file 模式)
    - 没传 → 调 load_auth_token() 从 env/file 读
    - 都没 → fail-fast

    [Round 2] host/port 不传 → 从 config.server_host/server_port 读 (config.toml [server] 段)
    """
    # 1. resolve host/port from config if not provided
    if host is None or port is None:
        cfg_host, cfg_port = _resolve_server_defaults()
        host = host if host is not None else cfg_host
        port = port if port is not None else cfg_port

    if not _MCP_AVAILABLE:
        raise RuntimeError('MCP/Starlette not available')

    # 2. Bearer token 加载 (fail-fast)
    if auth_token is None:
        try:
            auth_token = load_auth_token()
        except AuthError as e:
            logger.error(f'SSE transport requires auth token: {e}')
            raise
    logger.info('SSE auth: Bearer token loaded (length=%d chars)', len(auth_token))

    # 3. validate + port pre-check
    _validate_loopback_host(host)
    if not _check_port_available(host, port):
        logger.warning(f'port {port} already in use on {host}; exiting cleanly')
        return  # 让 launchd KeepAlive 自然接管

    # 4. build app + run
    app = _build_sse_app(auth_token)
    logger.info(f'mnelo MCP SSE listening on http://{host}:{port}/sse (Bearer auth ON)')
    uvicorn.run(app, host=host, port=port, log_level='info')


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--transport', default='stdio', choices=['stdio', 'sse'])
    # [Round 2] host/port default 从 config 读 (config.toml [server] 段)
    _cfg_host, _cfg_port = _resolve_server_defaults()
    ap.add_argument('--host', default=_cfg_host)
    ap.add_argument('--port', type=int, default=_cfg_port)
    # [7/19 P0-2] Bearer token 来源 (CLI override; 不传走 env/file 默认)
    ap.add_argument(
        '--auth-token-file', default=None,
        help='Path to file containing Bearer token (default: $MNEOLO_AUTH_TOKEN '
             'or ~/.config/mnelo/auth_token)'
    )
    args = ap.parse_args()

    if not _MCP_AVAILABLE:
        logger.error('MCP libraries missing. Install: pip install mcp[cli] starlette uvicorn')
        sys.exit(1)

    # [P2-1 优化] MCP server 启动时立即 warm-up Memory (含 Embedder)
    # 实测: 不 warm-up 首次 recall ~760ms (Embedder 1s cold start + 实际工作)
    #        warm-up 后首次 recall ~70ms (model 已在 RAM)
    # 启动慢 1s, 避免首 recall spike 1s
    logger.info('[P2-1] Pre-warming Memory + Embedder at MCP server startup...')
    _get_mem()  # 触发 Memory.__init__() warm-up

    if args.transport == 'stdio':
        asyncio.run(run_stdio())
    else:
        # [7/19 P0-2] 提前解析 token (fail-fast before warmup, 避免浪费 Embedder 启动时间)
        token = None
        if args.auth_token_file:
            try:
                token = load_auth_token(explicit_path=args.auth_token_file)
            except AuthError as e:
                logger.error(f'--auth-token-file load failed: {e}')
                sys.exit(2)
        run_sse(host=args.host, port=args.port, auth_token=token)


if __name__ == '__main__':
    main()
