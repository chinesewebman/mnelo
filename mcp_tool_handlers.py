# === mcp_tool_handlers.py ===
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
import sqlite3
import sys
from pathlib import Path
from typing import Dict

import memory as memory_module  # [refactor 2026-08-12] memory reference for handler helpers
from config import config  # [Round 2] server host/port 配置
from task_states import TaskLoopError
from validation import ValidationError

# 路径 — [7/21 fix] 插入本文件所在目录 (repo root), 不再硬编码 live 路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("mnelo.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


# === [8/6 v0.2 M3] task/loop 状态机 MCP tools (DESIGN §5.1) ===
# 8 新工具: task_create / task_transition / task_list / task_replay /
#           loop_create / loop_update / loop_tick / loop_list
#
# 走 task_states.py 模块函数 (直接 sqlite3 conn), 不走 Memory class.
# 委托 hook: _handle_task_simple() 跟 _handle_simple() 同形, 只是从模块拿函数.

_TASK_TOOL_REGISTRY = {
    # name -> (task_states attr, id_field for response wrapping)
    # 8 个 tool per DESIGN §5.1; Step 8-11 分批 ship.
    "memory_task_create": ("task_create", "task_id"),
    "memory_task_transition": ("transition", None),
    "memory_task_list": ("list_tasks", None),
    "memory_task_replay": ("replay_task", None),
    "memory_loop_create": ("loop_create", "loop_id"),
    "memory_loop_update": ("loop_update", "loop_id"),
    "memory_loop_list": ("list_loops", None),
    "memory_loop_tick": ("loop_tick", None),
}


def _handle_task_simple(mem, name: str, args: Dict) -> str:
    """[8/6 M3 + 8/6 review RF8 高] Dispatch task/loop tool to task_states module function.

    事务包裹: 用 try/except/rollback 路径包裹所有 task_states.* 调用.
    失败抛 TaskLoopError / IntegrityError / ProgrammingError 时显式 rollback,
    防 RF3 双 spawn UPDATE 0 行抛错后, 前面 INSERT 的 entity + 状态窗变成孤儿行.

    调用方走 `with mem._conn:` 自动事务; 但因为 Memory class 的 conn 默认在
    autocommit (sqlite3 默认), 这里显式手动 BEGIN/COMMIT/ROLLBACK 包裹.

    Returns:
        result JSON. 失败时返回 {"error": ..., "type": ...} 而不 raise.
    """
    import task_states as _ts

    attr_name, _id_field = _TASK_TOOL_REGISTRY[name]
    func = getattr(_ts, attr_name)

    # [RF8 8/6 + RF16 8/6 review-pass] 错误契约统一:
    #   - 领域错 (TaskLoopError + 子类): 保 message (含 field/code, Claude 可解析决策)
    #   - 底层错 (sqlite/IntegrityError/OperationalError): 只返类型名 (防泄露内部
    #     路径/SQL 细节), logger.exception 留 traceback 给运营
    try:
        result = func(mem._conn, **args)
        mem._conn.commit()
    except TaskLoopError as e:
        mem._conn.rollback()
        logger.warning(f"_handle_task_simple {name} 领域错: {type(e).__name__}: {e.message}")
        return json.dumps(
            {
                "error": e.message,
                "code": e.code,
                "field": getattr(e, "field", None),
                "type": type(e).__name__,
                "tool": name,
            },
            ensure_ascii=False,
        )
    except Exception as e:
        mem._conn.rollback()
        # 底层错 (sqlite / IntegrityError / OperationalError): 不暴露原始 message
        # 同 _call_tool 外层对待其他工具的契约.
        logger.exception(f"_handle_task_simple {name} 底层错 rolled back")
        return json.dumps(
            {"error": type(e).__name__, "type": type(e).__name__, "tool": name},
            ensure_ascii=False,
        )

    return json.dumps(result, ensure_ascii=False, default=str)


# === Tool dispatch ===
#
# P0 审计: 之前 _call_tool 是 80 行 if/elif 链 (10 个分支, 8 个简单委托 + 2 个自定义)
# 现在抽 TOOL_REGISTRY: 简单委托走通用 wrapper, 自定义逻辑走 _custom_handlers.
# 减 ~50 行, 加 ~5 行.

# [Round 1 quality audit] 抽常量避免 magic numbers 散落
# [Round 2] DEFAULT 从 config.server_host/port 读, 仍保留常量作 fallback
DEFAULT_SSE_HOST = "127.0.0.1"  # P2-1: loopback-only fallback
DEFAULT_SSE_PORT = 8086  # SSE 默认端口 fallback (config 优先)


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


def _handle_simple(mem, name: str, args: Dict) -> str:
    """Generic dispatcher: call Memory.<method>(**args), wrap result in JSON.

    P0 审计: 之前 8 个 if/elif 分支都是同一模式 `result = mem.xxx(**args); json.dumps(...)`,
    现在统一 wrapper. id_field 为 None 时直接序列化 result; 否则 wrap 成 `{id_field: result, status: 'ok'}`.
    """
    attr_name, id_field = _TOOL_REGISTRY[name]
    result = getattr(mem, attr_name)(**args)
    if id_field:
        return json.dumps({id_field: result, "status": "ok"}, ensure_ascii=False)
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
            threshold=args.get("threshold", 0.85),
            kind=args.get("kind"),
            max_pairs=args.get("max_pairs", 500),
        )
    out = [{"a": a, "b": b, "score": s, "reason": r} for a, b, s, r in candidates]
    return json.dumps({"candidates": out, "count": len(out)}, ensure_ascii=False)


def _handle_list_entities(mem, args: Dict) -> str:
    """[v1.1] List entities filtered by kind/min_importance, ordered by importance DESC.

    Args:
        args.kind: filter by entity kind (e.g. 'stock', 'identity_fact')
        args.min_importance: minimum importance threshold [0.0, 1.0]
        args.limit: max results, default 50

    Returns:
        {'entities': [{'id', 'kind', 'name', 'summary', 'importance'}], 'count': N}
    """
    sql = "SELECT id, kind, name, summary, importance FROM entities WHERE valid_until IS NULL"
    params = []
    if args.get("kind"):
        sql += " AND kind = ?"
        params.append(args["kind"])
    if args.get("min_importance"):
        sql += " AND importance >= ?"
        params.append(args["min_importance"])
    sql += " ORDER BY importance DESC LIMIT ?"
    params.append(args.get("limit", 50))
    rows = mem._conn.execute(sql, params).fetchall()
    entities = [{"id": r[0], "kind": r[1], "name": r[2], "summary": r[3], "importance": r[4]} for r in rows]
    return json.dumps({"entities": entities, "count": len(entities)}, ensure_ascii=False)


def _handle_search_relations(mem, args: Dict) -> str:
    """[v1.1] Search relations by relation type, with time-as-of filter.

    Args:
        args.relation (required): relation type string (e.g. 'owns', 'references')
        args.asof: ISO 8601 timestamp, default = now()
        args.limit: max results, default 100

    Returns:
        {'relations': [{'id', 'source_id', 'target_id', 'relation', 'weight', 'valid_from', 'valid_until'}], 'count': N}
    """
    asof = args.get("asof") or memory_module.now()
    sql = """
        SELECT id, source_id, target_id, relation, weight, valid_from, valid_until
        FROM relations
        WHERE relation = ?
          AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
        ORDER BY weight DESC, valid_from DESC
        LIMIT ?
    """
    rows = mem._conn.execute(sql, (args["relation"], asof, asof, args.get("limit", 100))).fetchall()
    relations = [
        {
            "id": r[0],
            "source_id": r[1],
            "target_id": r[2],
            "relation": r[3],
            "weight": r[4],
            "valid_from": r[5],
            "valid_until": r[6],
        }
        for r in rows
    ]
    return json.dumps({"relations": relations, "count": len(relations)}, ensure_ascii=False)


# Custom handlers — 不走 TOOL_REGISTRY, 因有特殊 SQL 或依赖
_CUSTOM_HANDLERS = {
    "memory_entity_resolve": _handle_entity_resolve,
    "memory_list_entities": _handle_list_entities,
    "memory_search_relations": _handle_search_relations,
}
