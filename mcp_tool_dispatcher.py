# === mcp_tool_dispatcher.py ===
# [refactor 2026-08-12] split from mcp_server.py.

#!/usr/bin/env python3
"""
mcp_server.py â mnelo MCP Server

- 7/19 v0.5.0 breaking change: åéå `HERMES_MEMORY_*` â `MNELO_MEMORY_*`, `MNELO_HOME` â `MNELO_HOME`
- æ¥å£: 22 tools â 4 L1 å¥å£ (memory_remember / memory_recall / memory_relate / memory_forget)
       + memory_update / memory_graph_query / memory_stats / memory_entity_resolve /
       memory_list_entities / memory_search_relations / memory_audit_list / memory_audit_undo /
       memory_maintenance / memory_get_digest / memory_task_{create,transition,list,replay} /
       memory_loop_{create,tick,update,list}
- 22 tools, ä¸ mnelo å½å TOOL_REGISTRY + TASK_TOOL_REGISTRY å®éä¸è´ (grep '"name": "memory_' mcp_server.py = 22)
- transports: SSE (/sse) / streamable-http (/mcp, MCP 2025-03-26) / dual â æ¨è streamable-http

[è¿è¡]
    cd LIVE_ROOT && python3 mcp_server.py --transport streamable-http
    (port èµ° config: env MNELO_MEMORY_SERVER_PORT > toml [server].port > 8086)
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import config  # [Round 2] server host/port éç½®
from validation import ValidationError

# è·¯å¾ â [7/21 fix] æå¥æ¬æä»¶æå¨ç®å½ (repo root), ä¸åç¡¬ç¼ç  live è·¯å¾
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("mnelo.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


# [refactor 2026-08-12] åè¿ç¨å Memory å®ä¾ (lock é£é©å½é¶) â æ¨¡åçº§
# (è·¨æ¨¡å import: mcp_transports å¨ endpoint lazy å¼ç¨, dispatcher å¨ _get_mem
#  åé¨ global å¼ç¨).
_mem_instance: Optional[Any] = None


def _get_mem(db_path=None) -> Any:
    """[audit fix 6.1 2026-08-16] singleton Memory w/ optional db_path injection.

    Args:
        db_path: optional injection for test isolation (default: module DB_PATH).
    """
    global _mem_instance
    if _mem_instance is None:
        from memory import DB_PATH as _DB_PATH
        from memory import Memory

        effective_db_path = db_path if db_path is not None else _DB_PATH
        # [audit fix 6.1] fix: 即使 Memory() 抛异常, 也保留为 None 让下次重试
        # (旧版 _get_mem 直接抛 — 单例失败后永远 stuck None, 但表现一致.
        # 新版行为: raise 让 caller 看见 — caller (test fixture) 可决定 retry 还是 skip)
        _mem_instance = Memory(db_path=effective_db_path) if db_path is not None else Memory()
        logger.info(f"mnelo MCP ready (db: {effective_db_path})")
    return _mem_instance


def _reset_mem_for_test() -> None:
    """[audit fix 6.2 2026-08-16] test fixture helper — reset Memory singleton.
    用法: 在 TestMain 或 fixture setup/teardown 调, 让每个 test 拿新 Memory 实例.
    关 _mem_instance 引用 → 下次 _get_mem() 重建 (db_path 注入生效).
    """
    global _mem_instance
    if _mem_instance is not None:
        try:
            _mem_instance.close()
        except Exception:
            pass  # defensive — close may fail if not opened
        _mem_instance = None


def _resolve_server_defaults() -> tuple:
    """ä» config è§£æ SSE host/port é»è®¤å¼. CLI flag ä¼åäº config."""
    try:
        cfg = config  # æ¥èª mcp_server é¡¶é¨ from config import config
        return cfg.server_host, cfg.server_port
    except Exception:
        return DEFAULT_SSE_HOST, DEFAULT_SSE_PORT


# [7/19 P2-3] ç®æ in-memory rate limit (é² runaway loop / æ»¥ç¨)
# key=tool å, value=[window_start_ts, count_in_window]
# [8/9 P1-yanru] 60 â 600 (ç¡¬ç¼ç ) â æå° config.toml (rate_limit.max_per_window).
# é»è®¤ 60/min å¼å®¹æ§è¡ä¸º; å½åçæå¼è§ config.rate_limit_max_per_window.


def _rate_limit_check(tool_name: str) -> None:
    """In-process sliding-window rate limit. è¶éæ ValidationError.

    Threshold from config: config.rate_limit_max_per_window / .rate_limit_window_sec.
    æ¹å®ééå¯ mcp_server è¿ç¨ (config æ¯æ¨¡åçº§åä¾, å¯å¨æ¶å è½½).
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

# [8/15 E-3] Default tool visibility flags â é»è®¤ä»æ´é² core+audit+advanced.
# mcp_server.py å¯å¨æ¶æ ¹æ® --audit-tools/--l2-tools/--all-tools è¦ç.
# [P1 #83 fix] æ¬ç±»èµæºä»å¨ mcp_server ä¸»è·¯å¾è®¾ç½®; æ¬å° import ä¸æ´é² l2/admin.
_DEFAULT_TOOL_VIS_FLAGS = {
    "audit_tools": False,
    "l2_tools": False,
    "all_tools": False,
}
_TOOL_VIS_FLAGS = _DEFAULT_TOOL_VIS_FLAGS  # å¯è¢« mcp_server å¨æè¦ç

_TOOL_REGISTRY = {
    # name -> (mem method attr, response id field name or None)
    "memory_remember": ("remember", "chunk_id"),
    "memory_recall": ("recall", None),
    "memory_relate": ("relate", "relation_id"),
    # === [8/15 E-A] åé´ Mem0 get_all ===
    # èµ° _handle_simple: args={kind, relation, user_id, limit, offset, include_superseded}
    # èµ° **kwargs éä¼ , æ¹æ³ç­¾åæé»è®¤å¼ååº.
    "memory_get_all": ("get_all", None),
    "memory_forget": ("forget", None),
    "memory_update": ("update", "new_chunk_id"),
    "memory_graph_query": ("graph_query", None),
    "memory_stats": ("stats", None),
    # === [8/15 E-3] Recall quality analytics (DESIGN Â§1.2 #6) ===
    # èµ° _handle_simple: args={days, group_by} èµ° **kwargs éä¼ , æ¹æ³ç­¾åæé»è®¤å¼ååº.
    "memory_recall_stats": ("recall_stats", None),
    # === [Â§1.2 #5 P1 #92 fix] Æ¯è¦å± raw-SQL Éæ°æ ===
    # Èp memory.list_entities / memory.search_relations (P1 #92 Éæ°æ Â· ä¸å _CUSTOM_HANDLERS raw SQL)
    "memory_list_entities": ("list_entities", None),
    "memory_search_relations": ("search_relations", None),
    # === [H-1 8/4] DESIGN Â§5.7 (3 L1 å¥å£ + 1 stats æ´å) ===
    "memory_audit_list": ("list_audit", None),  # ä¸èµ° _handle_simple (ææä¸¾è¿æ»¤)
    "memory_audit_undo": ("audit_undo", None),
    "memory_maintenance": ("run_maintenance", None),  # ä¸èµ° _handle_simple (passes åè¡¨)
    # === [S1 8/5] TASKS_L2_SESSION_STATE Â§1.3A ===
    "memory_get_digest": ("get_digest", None),  # ç®åå§æ â _handle_simple ç´æ¥èµ°
}


def _call_tool(name: str, args: Dict) -> str:
    """ç»ä¸å¤ç 10 ä¸ªå·¥å·è°ç¨, è¿å JSON å­ç¬¦ä¸².

    [7/19 P1-3] except è¿å type name + ç®ç­ reason, ä¸å¸¦åå§ str(e)
    (é¿åæ³é²åé¨è·¯å¾ / SQL éè¯¯ç»è / stack hint ç» MCP client).
    logger.exception ä»ä¿çå¨ traceback ç» operator (æä½åæ¥ ~/.hermes/logs/).
    """
    # [7/19 P2-3] rate limit å¨ dispatch å, é² owner infinite loop ææ­» MCP server
    try:
        _rate_limit_check(name)
    except ValidationError as ve:
        logger.warning(f"call_tool {name} rate-limited")
        return json.dumps({"error": str(ve), "tool": name, "type": "rate_limit"}, ensure_ascii=False)

    # [8/15 E-3 audit fix Plan A3] hidden tool friendly error.
    # éè tool (é»è®¤éè l2/admin) ä»å¯éå¼ call â è¿ informative error ä¸è§£éæç¤º.
    # ä½äº _get_mem() ä¹å â hidden tool ä¸åº trigger Memory init (é¿å zvec LOCK ç­èµæºæµªè´¹).
    # è¿ä¸æ¯å®å¨å¢é â æ¯ âä½ å¯è½ä¸ç¥éè¿ä¸ª tool è¢«éèäºâ æç¤ºã
    # å®å¨å¡éä»ç± owner æ§å¶ (flags ä¸å¯ç¨ â éå¼ call ä¾ç¶æ¥é, ä¸æ¯ silent).
    flags = _TOOL_VIS_FLAGS
    if is_tool_hidden(name, flags):
        tier = get_tool_tier(name)
        # å¨ audit-tools æªå¼æ¶, l2_tools flag æ æ³è§£é admin tools
        unlock_flag = "l2" if tier == "l2" else ("audit" if tier == "admin" else None)
        hint = f"--{unlock_flag}-tools" if unlock_flag else "N/A"
        logger.warning(f"call_tool {name} (tier={tier}) hidden by default. Hint: {hint}")
        return json.dumps(
            {
                "error": f"Tool '{name}' is hidden by default (tier={tier}).",
                "tool": name,
                "type": "hidden_tool",
                "hint": f"Restart mcp_server with --{unlock_flag}-tools flag, or use --all-tools for full debugging.",
                "tier": tier,
            },
            ensure_ascii=False,
        )

    mem = _get_mem()
    try:
        if name in _TOOL_REGISTRY:
            return _handle_simple(mem, name, args)
        if name in _CUSTOM_HANDLERS:
            return _CUSTOM_HANDLERS[name](mem, args)
        if name in _TASK_TOOL_REGISTRY:
            return _handle_task_simple(mem, name, args)
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)
    except ValidationError as ve:
        # validation éè¯¯æ¯ user-facing ç, message å®å¨ (ä¸å¸¦åå§ input)
        logger.warning(f"call_tool {name} validation: {ve.field}: {ve.reason}")
        return json.dumps({"error": str(ve), "tool": name, "type": "validation"}, ensure_ascii=False)
    except Exception as e:
        logger.exception(f"call_tool {name} failed")
        # åªè¿ type name (e.g. "ValueError", "sqlite3.OperationalError"), ä¸å¸¦ str(e)
        return json.dumps(
            {
                "error": type(e).__name__,
                "tool": name,
                "type": "internal",
                # 'detail' å­æ®µåªå¨è°è¯æ¨¡å¼ (MNELO_MEMORY_DEBUG=1) æ´é²
                "detail": str(e) if os.environ.get("MNELO_MEMORY_DEBUG") == "1" else None,
            },
            ensure_ascii=False,
        )


# === MCP server ===

# mcp 2.x 新 SDK types — 不在 mcp_guard re-export 里, 直接 import
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    TextResourceContents,
)

from mcp_guard import (
    _MCP_AVAILABLE,
    Resource,
    Server,
    TextContent,
    Tool,
)

# [refactor 2026-08-12] cross-module: handlers + definitions
from mcp_tool_definitions import (  # noqa: F401  cross-module
    TOOL_METADATA,
    TOOLS,
    get_exposed_tools,
    get_tool_tier,
    is_tool_hidden,
)
from mcp_tool_handlers import (  # noqa: F401  cross-module
    _CUSTOM_HANDLERS,
    _TASK_TOOL_REGISTRY,
    _handle_simple,
    _handle_task_simple,
)

DEFAULT_SSE_HOST = "127.0.0.1"  # P2-1: loopback-only fallback
DEFAULT_SSE_PORT = 8086  # SSE é»è®¤ç«¯å£ fallback (config ä¼å)

if _MCP_AVAILABLE:
    # MCP echo — visual marker (🌳) so agent + 主人 can distinguish mnelo
    # operations from Hermes `memory` tool (🧠). Set MNELO_ECHO=0 to disable.
    _ECHO = "🌳"
    _ECHO_LABEL = "mnelo"

    _DIGEST_URI = "memory://session/digest"

    # ===== mcp 2.x SDK: handlers 通过 Server(name, on_*) 注册 =====
    # handler 签名: async def(ctx, params) -> ResultType

    async def _list_tools_handler(ctx, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(**t) for t in TOOLS])

    async def _list_resources_handler(ctx, params: PaginatedRequestParams | None) -> ListResourcesResult:
        if not config.digest_inject_on_initialize:
            return ListResourcesResult(resources=[])
        return ListResourcesResult(
            resources=[
                Resource(
                    uri=_DIGEST_URI,  # str (mcp 2.x Resource.uri 是 str, 不再 AnyUrl)
                    name="Session digest",
                    description="Currently cached 常驻摘要 (memory_get_digest, ref=None).",
                    mime_type="text/plain",
                )
            ]
        )

    async def _read_resource_handler(ctx, params: ReadResourceRequestParams) -> ReadResourceResult:
        uri_str = str(params.uri)
        if uri_str != _DIGEST_URI:
            raise ValueError(f"unknown resource uri: {params.uri}")
        if not config.digest_inject_on_initialize:
            raise ValueError(f"resource disabled: {params.uri}")
        try:
            target = _get_mem()
            digest = target.get_digest()
        except Exception:
            logger.exception("digest resource read failed")
            text = ""
        else:
            if not digest.get("enabled"):
                text = ""
            else:
                text = digest.get("content", "") or ""
        return ReadResourceResult(contents=[TextResourceContents(uri=str(params.uri), text=text, mime_type="text/plain")])

    async def _call_tool_handler(ctx, params: CallToolRequestParams) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        result_json = _call_tool(name, arguments)
        echo = _build_echo(name, arguments, result_json)
        blocks: List[TextContent] = []
        if echo:
            blocks.append(TextContent(type="text", text=echo))
        blocks.append(TextContent(type="text", text=result_json))
        # [v0.81.7 P1-1 review fix] MCP spec compliance: signal tool errors via is_error=True.
        # _call_tool returns {"error": ..., "type": "validation|rate_limit|hidden_tool|internal"}
        # on failure; without this parse, client treats error as success and parses
        # chunk_id out of error JSON (silent failure).
        is_error = False
        try:
            data = json.loads(result_json)
            if isinstance(data, dict) and "error" in data:
                is_error = True
        except Exception:
            pass
        return CallToolResult(content=blocks, is_error=is_error)

    server = Server(
        "mnelo",
        on_list_tools=_list_tools_handler,
        on_call_tool=_call_tool_handler,
        on_list_resources=_list_resources_handler,
        on_read_resource=_read_resource_handler,
    )

    # ===== Test helpers (tests/test_initialize_inject.py 直接 asyncio.run 调) =====

    async def _list_resources():
        return await _list_resources_handler(None, None)

    async def _read_resource(uri):
        # mcp 2.x ReadResourceRequestParams.uri 是 str, 不再 AnyUrl
        uri_str = str(uri) if not isinstance(uri, str) else uri
        return await _read_resource_handler(None, ReadResourceRequestParams(uri=uri_str))

    # [dead code removed 2026-08-29 #21] 22 _echo_* helpers + _ECHO_BUILDERS dict that
    # the 8/16 audit fix added but never wired into _build_echo. The if-chain below is
    # the actual active code path. Per-tool format strings are unique enough that
    # dictionary dispatch wouldn't compress further. If a future refactor wants to
    # consolidate, see PR #21 follow-up — but the if-chain is checked by baseline diff.
    def _build_echo(name: str, args: Dict, result_json: str) -> str:
        """Render a one-line ð³ summary from tool name + args + result.

        Design: parse the JSON the handler returned (cheap, since handler just
        json.dump'd it), extract the most useful single fact, and emit a fixed-
        width line. Errors get ð³ too (with the type field) so the prefix is
        consistent regardless of success/failure.

        Format: ð³ mnelo {verb} {key_fact}
        Examples:
          ð³ mnelo    +chunk_20260720_xxx  (importance=0.7)
          ð³ mnelo    ~5 hits  "query"  (top=vector rrf=0.0164)
          ð³ mnelo    -chunk:chunk_xxx  (1 edge purged)
          ð³ mnelo    stats: chunks=4156 entities=4394 vectors=4105
        """
        if os.environ.get("MNELO_ECHO") == "0":
            return ""
        try:
            data = json.loads(result_json)
        except Exception:
            return f"{_ECHO} {_ECHO_LABEL}    {name} (unparseable response)"

        # Error responses: show type, no decorative wrapper
        if isinstance(data, dict) and "error" in data:
            err_type = data.get("type", "error")
            return f"{_ECHO} {_ECHO_LABEL}    â{err_type}: {name}"

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
            return f"{_ECHO} {_ECHO_LABEL}    â»{new_cid}  (supersedes {old})"
        if name == "memory_relate":
            # handler returns {relation_id, status}
            src = args.get("source_id", "?")
            tgt = args.get("target_id", "?")
            rel = args.get("relation", "?")
            return f"{_ECHO} {_ECHO_LABEL}    â¶{src}â{tgt}  ({rel})"
        if name == "memory_graph_query":
            # handler returns {nodes: [...], edges: [...], asof}
            nodes = data.get("nodes", []) if isinstance(data, dict) else []
            edges = data.get("edges", []) if isinstance(data, dict) else []
            start = args.get("start_node", "?")
            return f"{_ECHO} {_ECHO_LABEL}    â{start}  ({len(nodes)} nodes, {len(edges)} edges)"
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
            return f"{_ECHO} {_ECHO_LABEL}    â¡{len(cands)} dup candidates  (threshold={thresh})"
        if name == "memory_list_entities":
            # handler returns list of entities
            ents = data if isinstance(data, list) else (data.get("entities", []) if isinstance(data, dict) else [])
            kind = args.get("kind", "all")
            return f"{_ECHO} {_ECHO_LABEL}    â{len(ents)} entities  (kind={kind})"
        if name == "memory_search_relations":
            # handler returns {relations: [...], count: N}
            rels = data.get("relations", []) if isinstance(data, dict) else []
            rel_type = args.get("relation", "all")
            return f"{_ECHO} {_ECHO_LABEL}    â¢{len(rels)} relations  (type={rel_type})"

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
            return f"{_ECHO} {_ECHO_LABEL}    {task_id}  {from_state}â{to_state}  (win={wid})"

        if name == "memory_task_list":
            # handler returns {tasks: [...], count, truncated}
            tasks = data.get("tasks", []) if isinstance(data, dict) else []
            truncated = data.get("truncated", False) if isinstance(data, dict) else False
            return f"{_ECHO} {_ECHO_LABEL}    â{len(tasks)} tasks  (truncated={truncated})"

        if name == "memory_task_replay":
            # handler returns {task_id, current_state, window_count, windows: [...]}
            wc = data.get("window_count", "?") if isinstance(data, dict) else "?"
            cs = data.get("current_state", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    â»{data.get('task_id', '?') if isinstance(data, dict) else '?'}  ({wc} windows, current={cs})"

        if name == "memory_loop_create":
            # handler returns {loop_id, enabled, interval_hours}
            lid = data.get("loop_id", "?") if isinstance(data, dict) else "?"
            ih = data.get("interval_hours", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    â{lid}  (interval={ih}h)"

        if name == "memory_loop_tick":
            # handler returns {loop_id, verdict, ...}
            verdict = data.get("verdict", "?") if isinstance(data, dict) else "?"
            return f"{_ECHO} {_ECHO_LABEL}    â±{data.get('loop_id', '?') if isinstance(data, dict) else '?'}  verdict={verdict}"

        if name == "memory_loop_update":
            # handler returns {loop_id, changed: {...}, enabled, interval_hours}
            lid = data.get("loop_id", "?") if isinstance(data, dict) else "?"
            changed = data.get("changed", {}) if isinstance(data, dict) else {}
            return f"{_ECHO} {_ECHO_LABEL}    â{lid}  changed={list(changed.keys())}"

        if name == "memory_loop_list":
            # handler returns {loops: [...], count, truncated}
            loops = data.get("loops", []) if isinstance(data, dict) else []
            return f"{_ECHO} {_ECHO_LABEL}    â{len(loops)} loops"

        # Fallback for unknown shape
        return f"{_ECHO} {_ECHO_LABEL}    {name}  (ok)"

    # [v0.81.7 P1-3 review fix] Old @server.call_tool() decorator removed in 89a0ac7;
    # replaced by on_call_tool=_call_tool_handler (mcp SDK 2.x Server constructor API).


# === å¯å¨å¥å£ ===
