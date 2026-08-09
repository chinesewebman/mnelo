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

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from auth import AuthError, load_auth_token, verify_bearer
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

# Guarded import
try:
    from contextlib import asynccontextmanager

    import uvicorn
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
    from mcp.server.stdio import stdio_server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.types import (
        AnyUrl,
        ListResourcesRequest,
        ReadResourceRequest,
        ReadResourceRequestParams,
        Resource,
        TextContent,
        Tool,
    )
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    _MCP_AVAILABLE = True
except ImportError as e:
    _MCP_AVAILABLE = False
    logger.warning(f"MCP/Starlette not fully available: {e}")

# [P0 审计] 复用 memory.now() / memory._with_row_factory, 删 _dt_now 重复
import memory as memory_module

# : 单进程单 Memory 实例 ( lock 风险归零)
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


# === 工具 schema (7 个 MCP tools) ===

TOOLS = [
    {
        "name": "memory_remember",
        "description": "写入一条 chunk + 实体 + 关系到 mnelo. 返回 chunk_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "正文内容 (必填)"},
                "source": {
                    "type": "string",
                    "description": "来源 (master:0029, trinity_daily:part1, etc.)",
                    "default": "manual",
                },
                "importance": {"type": "number", "description": "0.0-1.0, 默认 0.5", "default": 0.5},
                "memory_type": {
                    "type": "string",
                    "description": (
                        "[P0 §3.0] fact / preference / episode / decision / procedure / ephemeral. [P1a E4 8/4] 默认 None 触发 P1a 规则自动分类; 显式传值永远尊重 (None=未指定, 触发分类器)."
                    ),
                    "default": None,
                    "enum": ["fact", "preference", "episode", "decision", "procedure", "ephemeral", None],
                },
                "entities": {"type": "array", "description": "[{id, kind, name, summary?, aliases?, properties?}]"},
                "relations": {
                    "type": "array",
                    "description": ("[{source_id, target_id, relation, weight?, properties?, valid_from?, valid_until?, evidence_chunk_id?}]"),
                },
                "tags": {"type": "array", "description": '["finance", "weng-resonance"]'},
                "session_id": {"type": "string", "default": "default"},
                "timestamp": {"type": "string", "description": "ISO 8601, None=now"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_recall",
        "description": ("4 路召回 (向量 + 图遍历 + 元数据 + 实体) + RRF 融合."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询文本 (必填)"},
                "top_k": {"type": "integer", "default": 5},
                "graph_hops": {"type": "integer", "default": 2},
                "filters": {
                    "type": "object",
                    "description": "{kind, source, tag, time_range, type} — type = 记忆类型 (fact/preference/episode/decision/procedure/ephemeral)",
                },
                "strategy": {
                    "type": "string",
                    "enum": ["rrf", "vector_only", "graph_only", "meta_only", "entity_only"],
                    "default": "rrf",
                },
                "asof": {"type": "string", "description": "ISO 8601 时间切片, None=now"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_relate",
        "description": "新建一条关系.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {"type": "string"},
                "target_id": {"type": "string"},
                "relation": {"type": "string"},
                "weight": {"type": "number", "default": 1.0},
                "valid_from": {"type": "string"},
                "valid_until": {"type": "string"},
                "evidence_chunk_id": {"type": "string"},
                "properties": {"type": "object"},
            },
            "required": ["source_id", "target_id", "relation"],
        },
    },
    {
        "name": "memory_forget",
        "description": "软删除 entity/chunk/relation (valid_until = now). 触发器自动级联.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "target_kind": {"type": "string", "enum": ["chunk", "entity", "relation"], "default": "chunk"},
                "reason": {"type": "string", "default": "outdated"},
                "cascade": {"type": "boolean", "default": True},
            },
            "required": ["target_id"],
        },
    },
    {
        "name": "memory_update",
        "description": '"更新": 创建新 chunk + 老 chunk superseded_by. 不覆盖.',
        "inputSchema": {
            "type": "object",
            "properties": {
                "old_id": {"type": "string"},
                "reason": {"type": "string", "default": "updated"},
                "new_content": {"type": "string"},
                "new_properties": {"type": "object"},
                "new_importance": {"type": "number"},
            },
            "required": ["old_id"],
        },
    },
    {
        "name": "memory_graph_query",
        "description": "图遍历: start_node 起 max_hops 跳内的子图.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_node": {"type": "string"},
                "max_hops": {"type": "integer", "default": 3},
                "edge_types": {"type": "array", "description": "list of relation names, None=all"},
                "asof": {"type": "string"},
            },
            "required": ["start_node"],
        },
    },
    {
        "name": "memory_stats",
        "description": "统计: entities/chunks/relations/vectors/recall_log 数量. [H-1 §6.5] 加 hygiene 子键 (decay/TTL/purge/audit 报告).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # === [v1.1] 新增 3 个工具 ===
    {
        "name": "memory_entity_resolve",
        "description": "实体消歧: 找疑似重复 entity (alias/name 相似度).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "相似度阈值 0.0-1.0, 默认 0.85", "default": 0.85},
                "kind": {"type": "string", "description": "kind 过滤 (stock/concept/person/...), None=全部"},
            },
        },
    },
    {
        "name": "memory_list_entities",
        "description": "列实体, 按 kind/importance 过滤.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "min_importance": {"type": "number", "default": 0.0},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "memory_search_relations",
        "description": "按 relation 类型搜索关系.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "relation": {"type": "string", "description": "relation 名 (_关注_于 / 翁氏_共振_BUY_于)"},
                "asof": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    # === [H-1 8/4] DESIGN §5.7 L2 主动层入口 + audit_log 查询 (§6.5 工具收敛) ===
    {
        "name": "memory_audit_list",
        "description": "[H-1 §5.7] 查 audit_log 提案历史. 主人 §5.9.1 状态机: proposed / applied / reverted / skipped.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "过滤特定 run"},
                "pass_name": {"type": "string", "description": "过滤特定 pass"},
                "status": {
                    "type": "string",
                    "enum": ["proposed", "applied", "reverted", "skipped"],
                },
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
        },
    },
    {
        "name": "memory_audit_undo",
        "description": "Undo one applied audit record; executes its stored multi-statement revert_sql.",
        "inputSchema": {"type": "object", "properties": {"audit_id": {"type": "integer"}}, "required": ["audit_id"]},
    },
    {
        "name": "memory_maintenance",
        "description": "[H-1 §5.7] L2 主动层入口. dry_run 默认 true; l2.enabled=1 才生效. [H-3 §5.9.2] confirm_destructive=True 允许 TTL 真删 (默认 false 安全).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "passes": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["hygiene", "promote"]},
                    "default": ["hygiene"],
                },
                "dry_run": {"type": "boolean", "default": True},
                "confirm_destructive": {
                    "type": "boolean",
                    "description": "[§5.9.2] TTL/Purge 实际需显式 True (默认 false 安全). decay_importance 不需.",
                    "default": False,
                },
            },
        },
    },
    # === [S1 8/5] TASKS_L2_SESSION_STATE §1.3A: 常驻摘要 MCP 工具 ===
    # 任一 MCP 客户端可用, 跟 G7 resources/list+read 是同一个通用层薄包装.
    {
        "name": "memory_get_digest",
        "description": (
            "[S1 8/5] 常驻记忆摘要 (DESIGN §4.5 + 可逆压缩 v0.13 + TASKS_L2_DIGEST §1.1). "
            "缺省/省略 ref → 摘要压缩视图 (content + line_refs); 传 ref=<行号> → 展开该行源 chunk. "
            "非法 ref → {error: ...}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "行号 (缺省/省略 = 摘要; 传字符串 = 展开该行源 chunk)",
                },
            },
        },
    },
    # === [8/6 v0.2 M3] task/loop 状态机 — 8 工具 §5.1 (Step 7: task_create) ===
    {
        "name": "memory_task_create",
        "description": "建 task entity + open 状态窗. loop_id 必填时检查 loop 已启用且无 active_task_id (防双 spawn). 返回 task_id + current_state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "任务名 (必填, e.g. '采购耗材')"},
                "loop_id": {"type": "string", "description": "父 loop; None = 独立一次性任务"},
                "owner_id": {"type": "string", "description": "责任人 entity id (默认 person:yanru)"},
                "priority": {"type": "integer", "description": "0-5, default 3"},
                "summary": {"type": "string", "description": "可选摘要"},
                "evidence_chunk_id": {"type": "string", "description": "触发此任务的 chunk FK"},
                "now": {"type": "string", "description": "timestamp 覆盖 (T 分隔)"},
            },
            "required": ["name"],
        },
    },
    # === [8/6 M3 Step 8] memory_task_transition ===
    {
        "name": "memory_task_transition",
        "description": (
            "CAS 关旧窗 + 开新窗 (DESIGN §4.2). task_id 必填, to_state/reason 必填, "
            "evidence_chunk_id 可选但若提供须存在. force=True 绕过转移图 (D8 纠正门). "
            "终端 (cancelled) 拒收. 并发 / 重复提交报 NotCurrentStateError."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "task entity id (必填)"},
                "to_state": {"type": "string", "description": "目标状态 (task: open/in_progress/waiting/blocked/done/cancelled; loop: running/dormant/paused)"},
                "reason": {"type": "string", "description": "语义摘要 (必填, 含 actor 痕迹)"},
                "evidence_chunk_id": {"type": "string", "description": "支撑转移的 chunk FK (推荐必填, 创建可空)"},
                "force": {"type": "boolean", "description": "D8 纠正门: 绕过转移图 (要求 reason 已填)", "default": False},
                "now": {"type": "string", "description": "timestamp 覆盖 (T 分隔)"},
            },
            "required": ["task_id", "to_state", "reason"],
        },
    },
    # === [8/6 M3 Step 9] memory_task_list ===
    {
        "name": "memory_task_list",
        "description": (
            "列出活跃任务 (DESIGN §5.1). 默认 state 过滤 active "
            "(state NOT IN done/cancelled/dormant/paused). "
            "state 参数当前状态精确过滤 (state=in_progress). loop_id 过滤父 loop. "
            "asof 时间切片. stale_days 算窗口年龄."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "description": "当前状态精确过滤 (None=活跃)"},  # 利用默认即 active
                "loop_id": {"type": "string", "description": "父 loop 过滤"},
                "asof": {"type": "string", "description": "时间切片 (isof 8601)"},
                "stale_days": {"type": "boolean", "description": "算 valid_from 到当前的天数", "default": False},
                "limit": {"type": "integer", "description": "max rows", "default": 50},
            },
            "required": [],
        },
    },
    # === [8/6 M3 Step 9] memory_task_replay ===
    {
        "name": "memory_task_replay",
        "description": "Replay task 状态窗历史 (DESIGN §5.1). asof 默认 = 当前. 返回 {task_id, current_state, window_count, windows: [{state, valid_from, valid_until, reason, evidence_chunk_id}]}",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "task / loop entity id (必填)"},
                "asof": {"type": "string", "description": "回放时间点 (默认 = 当前)"},
            },
            "required": ["task_id"],
        },
    },
    # === [8/6 M3 Step 10] memory_loop_create ===
    {
        "name": "memory_loop_create",
        "description": "建 loop entity (DESIGN §5.1). enabled=False 写 dormant 状态窗, True 等首个 tick. 返回 {loop_id, enabled, interval_hours}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "loop 名称 (必填)"},
                "trigger": {"type": "string", "description": "触发条件 (必填, e.g. '库存低于阈值')"},
                "interval_hours": {"type": "integer", "description": "轮转间隔 (默认 24)", "default": 24},
                "enabled": {"type": "boolean", "description": "默认 True", "default": True},
                "priority": {"type": "integer", "description": "0-5", "default": 3},
                "owner_id": {"type": "string", "description": "责任人 entity id"},
                "now": {"type": "string", "description": "timestamp 覆盖"},
            },
            "required": ["name", "trigger"],
        },
    },
    # === [8/6 M3 Step 11] memory_loop_tick ===
    {
        "name": "memory_loop_tick",
        "description": "机械算 loop tick verdict (DESIGN §4.3). 4 verdict: dormant / waiting / due / not_due. 不写 task_states, tick 判定不落行 (DESIGN §4.3 strict).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "loop_id": {"type": "string", "description": "loop entity id (必填)"},
                "now": {"type": "string", "description": "timestamp 覆盖 (默认 = 当前)"},
            },
            "required": ["loop_id"],
        },
    },
    # === [8/6 M3 Step 12] memory_loop_update ===
    {
        "name": "memory_loop_update",
        "description": (
            "改 loop properties (DESIGN §5.1). enabled/trigger/interval_hours/priority/owner_id "
            "任一可改; None 字段不动. enabled 切换落 dormant/running 状态窗. "
            "不动 active_task_id (那是 task_create 的领域)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "loop_id": {"type": "string", "description": "loop entity id (必填)"},
                "enabled": {"type": "boolean", "description": "True=启动, False=停用 (写 dormant)"},
                "trigger": {"type": "string", "description": "新触发条件"},
                "interval_hours": {"type": "integer", "description": "新轮转间隔 (小时)"},
                "priority": {"type": "integer", "description": "新优先级 0-5"},
                "owner_id": {"type": "string", "description": "新责任人 entity id"},
                "now": {"type": "string", "description": "timestamp 覆盖"},
            },
            "required": ["loop_id"],
        },
    },
    # === [8/6 M3 Step 12] memory_loop_list ===
    {
        "name": "memory_loop_list",
        "description": "列 loop entities + 当前状态 (DESIGN §5.1). enabled_only=True 仅启用的; state= 精确过滤 (running/dormant/paused); asof 时间切片.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "enabled_only": {"type": "boolean", "description": "仅 enabled=True", "default": False},
                "state": {"type": "string", "description": "当前状态精确过滤"},
                "asof": {"type": "string", "description": "时间切片 (默认 = 当前)"},
                "limit": {"type": "integer", "description": "max rows", "default": 50},
            },
            "required": [],
        },
    },
]


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


async def run_stdio() -> None:
    """: 主路径 stdio transport (与 MCP 客户端对接)."""
    if not _MCP_AVAILABLE:
        raise RuntimeError("MCP libraries not available")
    async with stdio_server() as (read_stream, write_stream):
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
    """
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
            auth_token = load_auth_token()
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
