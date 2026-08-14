# === mcp_tool_dispatcher.py ===
# [refactor 2026-08-12] split from mcp_server.py.

#!/usr/bin/env python3
"""
mcp_server.py — mnelo MCP Server

- 7/19 v0.5.0 breaking change: 变量名 `HERMES_MEMORY_*` → `MNELO_MEMORY_*`, `MNELO_HOME` → `MNELO_HOME`
- 接口: 22 tools — 4 L1 入口 (memory_remember / memory_recall / memory_relate / memory_forget)
       + memory_update / memory_graph_query / memory_stats / memory_entity_resolve /
       memory_list_entities / memory_search_relations / memory_audit_list / memory_audit_undo /
       memory_maintenance / memory_get_digest / memory_task_{create,transition,list,replay} /
       memory_loop_{create,tick,update,list}
- 22 tools, 与 mnelo 当前 TOOL_REGISTRY + TASK_TOOL_REGISTRY 实际一致 (grep '"name": "memory_' mcp_server.py = 22)
- transports: SSE (/sse) / streamable-http (/mcp, MCP 2025-03-26) / dual — 推荐 streamable-http

[运行]
    cd LIVE_ROOT && python3 mcp_server.py --transport streamable-http
    (port 走 config: env MNELO_MEMORY_SERVER_PORT > toml [server].port > 8086)
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import config  # [Round 2] server host/port 配置
from validation import ValidationError

# 路径 — [7/21 fix] 插入本文件所在目录 (repo root), 不再硬编码 live 路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("mnelo.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


# [refactor 2026-08-12] 单进程单 Memory 实例 (lock 风险归零) — 模块级
# (跨模块 import: mcp_transports 在 endpoint lazy 引用, dispatcher 在 _get_mem
#  内部 global 引用).
_mem_instance: Optional[Any] = None


def _get_mem() -> Any:
    """单例 Memory."""
    global _mem_instance
    if _mem_instance is None:
        from memory import DB_PATH as _DB_PATH
        from memory import Memory

        _mem_instance = Memory()
        logger.info(f"mnelo MCP ready (db: {_DB_PATH})")
    return _mem_instance


def _resolve_server_defaults() -> tuple:
    """从 config 解析 SSE host/port 默认值. CLI flag 优先于 config."""
    try:
        cfg = config  # 来自 mcp_server 顶部 from config import config
        return cfg.server_host, cfg.server_port
    except Exception:
        return DEFAULT_SSE_HOST, DEFAULT_SSE_PORT


# [7/19 P2-3] 简易 in-memory rate limit (防 runaway loop / 滥用)
# key=tool 名, value=[window_start_ts, count_in_window]
# [8/9 P1-yanru] 60 → 600 (硬编码) → 提到 config.toml (rate_limit.max_per_window).
# 默认 60/min 兼容旧行为; 当前生效值见 config.rate_limit_max_per_window.


def _rate_limit_check(tool_name: str) -> None:
    """In-process sliding-window rate limit. 超限抛 ValidationError.

    Threshold from config: config.rate_limit_max_per_window / .rate_limit_window_sec.
    改完需重启 mcp_server 进程 (config 是模块级单例, 启动时加载).
    """
    import time as _time

    max_reqs = config.rate_limit_max_per_window
    window_sec = config.rate_limit_window_sec

    now_ts = _time.time()
    bucket = _RATE_BUCKETS.get(tool_name)
    if bucket is None or now_ts - bucket[0] > window_sec:
        _RATE_BUCKETS[tool_name] = [now_ts, 1]
        return
    bucket[1] += 1
    if bucket[1] > max_reqs:
        raise ValidationError(tool_name, f"rate limit: {max_reqs} reqs / {window_sec}s exceeded")


_RATE_BUCKETS: Dict[str, list] = {}

_TOOL_REGISTRY = {
    # name -> (mem method attr, response id field name or None)
    "memory_remember": ("remember", "chunk_id"),
    "memory_recall": ("recall", None),
    "memory_relate": ("relate", "relation_id"),
    "memory_forget": ("forget", None),
    "memory_update": ("update", "new_chunk_id"),
    "memory_graph_query": ("graph_query", None),
    "memory_stats": ("stats", None),
    # === [8/15 E-3] Recall quality analytics (DESIGN §1.2 #6) ===
    # 走 _handle_simple: args={days, group_by} 走 **kwargs 透传, 方法签名有默认值兜底.
    "memory_recall_stats": ("recall_stats", None),
    # === [H-1 8/4] DESIGN §5.7 (3 L1 入口 + 1 stats 整合) ===
    "memory_audit_list": ("list_audit", None),  # 不走 _handle_simple (有枚举过滤)
    "memory_audit_undo": ("audit_undo", None),
    "memory_maintenance": ("run_maintenance", None),  # 不走 _handle_simple (passes 列表)
    # === [S1 8/5] TASKS_L2_SESSION_STATE §1.3A ===
    "memory_get_digest": ("get_digest", None),  # 简单委托 — _handle_simple 直接走
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
        logger.warning(f"call_tool {name} rate-limited")
        return json.dumps({"error": str(ve), "tool": name, "type": "rate_limit"}, ensure_ascii=False)
    try:
        if name in _TOOL_REGISTRY:
            return _handle_simple(mem, name, args)
        if name in _CUSTOM_HANDLERS:
            return _CUSTOM_HANDLERS[name](mem, args)
        if name in _TASK_TOOL_REGISTRY:
            return _handle_task_simple(mem, name, args)
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    except ValidationError as ve:
        # validation 错误是 user-facing 的, message 安全 (不带原始 input)
        logger.warning(f"call_tool {name} validation: {ve.field}: {ve.reason}")
        return json.dumps({"error": str(ve), "tool": name, "type": "validation"}, ensure_ascii=False)
    except Exception as e:
        logger.exception(f"call_tool {name} failed")
        # 只返 type name (e.g. "ValueError", "sqlite3.OperationalError"), 不带 str(e)
        return json.dumps(
            {
                "error": type(e).__name__,
                "tool": name,
                "type": "internal",
                # 'detail' 字段只在调试模式 (MNELO_MEMORY_DEBUG=1) 暴露
                "detail": str(e) if os.environ.get("MNELO_MEMORY_DEBUG") == "1" else None,
            },
            ensure_ascii=False,
        )


# === MCP server ===

from mcp_guard import (
    _MCP_AVAILABLE,
    AnyUrl,
    ListResourcesRequest,
    ReadResourceRequest,
    ReadResourceRequestParams,
    Resource,
    Server,
    TextContent,
    Tool,
)  # guarded imports

# [refactor 2026-08-12] cross-module: handlers + definitions
from mcp_tool_definitions import TOOLS  # noqa: F401  cross-module
from mcp_tool_handlers import (
    _CUSTOM_HANDLERS,
    _TASK_TOOL_REGISTRY,
    _handle_simple,
    _handle_task_simple,
)

DEFAULT_SSE_HOST = "127.0.0.1"  # P2-1: loopback-only fallback
DEFAULT_SSE_PORT = 8086  # SSE 默认端口 fallback (config 优先)

if _MCP_AVAILABLE:
    server = Server("mnelo")

    # MCP echo — visual marker (🌳) so agent + 主人 can distinguish mnelo
    # operations from Hermes `memory` tool (🧠). Set MNELO_ECHO=0 to disable.
    _ECHO = "🌳"
    _ECHO_LABEL = "mnelo"

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return [Tool(**t) for t in TOOLS]

    _DIGEST_URI = "memory://session/digest"

    @server.list_resources()
    async def list_resources() -> List[Resource]:
        if not config.digest_inject_on_initialize:
            return []
        return [
            Resource(
                uri=AnyUrl(_DIGEST_URI),
                name="Session digest",
                description="Currently cached 常驻摘要 (memory_get_digest, ref=None).",
                mimeType="text/plain",
            )
        ]

    @server.read_resource()
    async def read_resource(uri: AnyUrl) -> str:
        if str(uri) != _DIGEST_URI:
            raise ValueError(f"unknown resource uri: {uri}")
        if not config.digest_inject_on_initialize:
            raise ValueError(f"resource disabled: {uri}")
        try:
            target = _get_mem()
            digest = target.get_digest()
        except Exception:
            logger.exception("digest resource read failed")
            return ""
        if not digest.get("enabled"):
            return ""
        return digest.get("content", "") or ""

    # [mcp v1.26 SDK bug] server.read_resource() decorator expects either
    # str/bytes (deprecated) or Iterable[ReadResourceContents]. The SDK then
    # reads `content_item.content`, which the public TextResourceContents class
    # does not expose (its field is `text`). Returning a list of MCP types
    # would hit this gap; we fall back to the deprecated str form, which still
    # works and is what mcp currently tolerates. The accompanying
    # DeprecationWarning is suppressed at test time via
    # `tests/conftest.py::pytest_configure`.

    def _list_resources():
        return server.request_handlers[ListResourcesRequest](ListResourcesRequest())

    def _read_resource(uri):
        req = ReadResourceRequest(params=ReadResourceRequestParams(uri=AnyUrl(uri)))
        return server.request_handlers[ReadResourceRequest](req)

    def _build_echo(name: str, args: Dict, result_json: str) -> str:
        """Render a one-line 🌳 summary from tool name + args + result.

        Design: parse the JSON the handler returned (cheap, since handler just
        json.dump'd it), extract the most useful single fact, and emit a fixed-
        width line. Errors get 🌳 too (with the type field) so the prefix is
        consistent regardless of success/failure.

        Format: 🌳 mnelo {verb} {key_fact}
        Examples:
          🌳 mnelo    +chunk_20260720_xxx  (importance=0.7)
          🌳 mnelo    ~5 hits  "query"  (top=vector rrf=0.0164)
          🌳 mnelo    -chunk:chunk_xxx  (1 edge purged)
          🌳 mnelo    stats: chunks=4156 entities=4394 vectors=4105
        """
        if os.environ.get("MNELO_ECHO") == "0":
            return ""
        try:
            data = json.loads(result_json)
        except Exception:
            # handler didn't return JSON (shouldn't happen, but be safe)
            return f"{_ECHO} {_ECHO_LABEL}    {name} (unparseable response)"

        # Error responses: show type, no decorative wrapper
        if isinstance(data, dict) and "error" in data:
            err_type = data.get("type", "error")
            return f"{_ECHO} {_ECHO_LABEL}    ✗{err_type}: {name}"

        # Per-tool compact echoes
        if name == "memory_remember":
            # handler returns {chunk_id, status}
            cid = data.get("chunk_id", "?") if isinstance(data, dict) else "?"
            imp = args.get("importance", "?")
            return f"{_ECHO} {_ECHO_LABEL}    +{cid}  (importance={imp})"
        if name == "memory_recall":
            # handler returns a list of {chunk_id, content, method, rrf_score, ...}
            hits = data if isinstance(data, list) else []
            query = args.get("query", "")[:30]
            if hits:
                top = hits[0]
                method = top.get("method", "?") if isinstance(top, dict) else "?"
                rrf = top.get("rrf_score", "?")
                try:
                    rrf = f"{float(rrf):.4f}"
                except (TypeError, ValueError):
                    pass
                return f'{_ECHO} {_ECHO_LABEL}    ~{len(hits)} hits  "{query}"  (top={method} rrf={rrf})'
            return f'{_ECHO} {_ECHO_LABEL}    ~0 hits  "{query}"'
        if name == "memory_forget":
            # handler returns {edges_invalidated, queued_purge}
            target = args.get("target_id", "?")
            kind = args.get("target_kind", "chunk")
            purged = data.get("queued_purge", 1) if isinstance(data, dict) else 1
            return f"{_ECHO} {_ECHO_LABEL}    -{kind}:{target}  ({purged} queued)"
        if name == "memory_update":
            # handler returns {new_chunk_id, status}
            new_cid = data.get("new_chunk_id", "?") if isinstance(data, dict) else "?"
            old = args.get("old_id", "?")
            return f"{_ECHO} {_ECHO_LABEL}    ↻{new_cid}  (supersedes {old})"
        if name == "memory_relate":
            # handler returns {relation_id, status}
            src = args.get("source_id", "?")
            tgt = args.get("target_id", "?")
            rel = args.get("relation", "?")
            return f"{_ECHO} {_ECHO_LABEL}    ⟶{src}→{tgt}  ({rel})"
        if name == "memory_graph_query":
            # handler returns {nodes: [...], edges: [...], asof}
            nodes = data.get("nodes", []) if isinstance(data, dict) else []
            edges = data.get("edges", []) if isinstance(data, dict) else []
            start = args.get("start_node", "?")
            return f"{_ECHO} {_ECHO_LABEL}    ⌘{start}  ({len(nodes)} nodes, {len(edges)} edges)"
        if name == "memory_stats":
            # Compact: chunks=N entities=N vectors=N
            chunks = data.get("chunks", {}).get("active", "?") if isinstance(data.get("chunks"), dict) else "?"
            ents = data.get("entities", {}).get("active", "?") if isinstance(data.get("entities"), dict) else "?"
            vecs = data.get("vectors", "?")
            return f"{_ECHO} {_ECHO_LABEL}    stats: chunks={chunks} entities={ents} vectors={vecs}"
        if name == "memory_entity_resolve":
            # handler returns {candidates: [...], count: N}
            cands = data.get("candidates", []) if isinstance(data, dict) else []
            thresh = args.get("threshold", 0.85)
            return f"{_ECHO} {_ECHO_LABEL}    ≡{len(cands)} dup candidates  (threshold={thresh})"
        if name == "memory_list_entities":
            # handler returns list of entities
            ents = data if isinstance(data, list) else (data.get("entities", []) if isinstance(data, dict) else [])
            kind = args.get("kind", "all")
            return f"{_ECHO} {_ECHO_LABEL}    ⊃{len(ents)} entities  (kind={kind})"
        if name == "memory_search_relations":
            # handler returns {relations: [...], count: N}
            rels = data.get("relations", []) if isinstance(data, dict) else []
            rel_type = args.get("relation", "all")
            return f"{_ECHO} {_ECHO_LABEL}    ⇢{len(rels)} relations  (type={rel_type})"

        if name == "memory_task_create":
            # handler returns {task_id, current_state, status}
            task_id = data.get("task_id", "?") if isinstance(data, dict) else "?"
            state = data.get("current_state", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    +{task_id}  (state={state})"

        if name == "memory_task_transition":
            # handler returns {task_id, from_state, to_state, valid_from, window_id}
            task_id = data.get("task_id", "?") if isinstance(data, dict) else "?"
            from_state = data.get("from_state", "?") if isinstance(data, dict) else "?"
            to_state = data.get("to_state", "?") if isinstance(data, dict) else "?"
            wid = data.get("window_id", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    {task_id}  {from_state}→{to_state}  (win={wid})"

        if name == "memory_task_list":
            # handler returns {tasks: [...], count, truncated}
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
            truncated = data.get("truncated", False) if isinstance(data, dict) else False
            return f"{_ECHO} {_ECHO_LABEL}    ⊃{len(tasks)} tasks  (truncated={truncated})"

        if name == "memory_task_replay":
            # handler returns {task_id, current_state, window_count, windows: [...]}
            wc = data.get("window_count", "?") if isinstance(data, dict) else "?"
            cs = data.get("current_state", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    ↻{data.get('task_id', '?') if isinstance(data, dict) else '?'}  ({wc} windows, current={cs})"

        if name == "memory_loop_create":
            # handler returns {loop_id, enabled, interval_hours}
            lid = data.get("loop_id", "?") if isinstance(data, dict) else "?"
            ih = data.get("interval_hours", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    ⊕{lid}  (interval={ih}h)"

        if name == "memory_loop_tick":
            # handler returns {loop_id, verdict, ...}
            verdict = data.get("verdict", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    ⏱{data.get('loop_id', '?') if isinstance(data, dict) else '?'}  verdict={verdict}"

        if name == "memory_loop_update":
            # handler returns {loop_id, changed: {...}, enabled, interval_hours}
            lid = data.get("loop_id", "?") if isinstance(data, dict) else "?"
            changed = data.get("changed", {}) if isinstance(data, dict) else {}
            return f"{_ECHO} {_ECHO_LABEL}    ✎{lid}  changed={list(changed.keys())}"

        if name == "memory_loop_list":
            # handler returns {loops: [...], count, truncated}
            loops = data.get("loops", []) if isinstance(data, dict) else []
            return f"{_ECHO} {_ECHO_LABEL}    ⊃{len(loops)} loops"

        # Fallback for unknown shape
        return f"{_ECHO} {_ECHO_LABEL}    {name}  (ok)"

    @server.call_tool()
    async def call_tool(name: str, arguments: Dict) -> List[TextContent]:
        result_json = _call_tool(name, arguments)
        echo = _build_echo(name, arguments or {}, result_json)
        blocks: List[TextContent] = []
        if echo:
            blocks.append(TextContent(type="text", text=echo))
        blocks.append(TextContent(type="text", text=result_json))
        return blocks


# === 启动入口 ===
