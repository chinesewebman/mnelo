# === mcp_tool_definitions.py ===
# [refactor 2026-08-12] split from mcp_server.py (1614 行 → 5 模块).
# 22 MCP Tool() JSON schema definitions (memory_remember/recall/relate/forget + update/
# graph_query/stats + audit_list/audit_undo/maintenance/get_digest + entity_resolve/
# list_entities/search_relations + task_create/transition/list/replay + loop_create/
# tick/update/list). Convert via as_tools() helper for server.list_tools() registration.

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

import logging
import sys
from pathlib import Path

# 路径 — [7/21 fix] 插入本文件所在目录 (repo root), 不再硬编码 live 路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

logger = logging.getLogger("mnelo.mcp")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


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
                # [P0 2026-08-11] scoping IDs — 借鉴 Mem0 scoping IDs.
                # 写入侧: 这 3 字段 merge 进 chunks.metadata_json (JSON K-V),
                # 不覆盖现有 'tags' 键. None = 未指定, 不写入 (旧数据兼容).
                # 空串是显式选择 ('no scoping'), 保留.
                "agent_id": {"type": "string", "description": "[P0 scoping] agent 标识 (e.g. 'main', 'tiancanbian'). 召回时按 agent_id 过滤."},
                "user_id": {"type": "string", "description": "[P0 scoping] 用户标识 (e.g. 'owner_ling')."},
                "run_id": {"type": "string", "description": "[P0 scoping] 单次 run 标识 (e.g. session UUID)."},
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
                    "description": (
                        "{kind, source, tag, time_range, type, agent_id, user_id, run_id}"
                        " — type = 记忆类型 (fact/preference/episode/decision/procedure/ephemeral);"
                        " [P0 2026-08-11] agent_id/user_id/run_id = metadata_json 字段过滤 (旧数据无对应键保留, 不误过滤)."
                    ),
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
    # === [8/15 E-3] Recall quality analytics ===
    {
        "name": "memory_recall_stats",
        "description": "[E-3] 召回质量分析 (DESIGN §1.2 #6): 各 method (vector/graph/meta/entity) 命中率/avg rank/score, latency p50/p95/p99, 空窗率, 按日聚合. 让主人看清召回现状决定优化方向.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "时间窗口 (近 N 天), 默认 30. None/0 = 全部.",
                    "default": 30,
                },
                "group_by": {
                    "type": "string",
                    "enum": ["method", "day"],
                    "description": "聚合维度 — method (各路召回分布) / day (按日序列). 当前实现 method=默认行为, day=返回 by_day 数组.",
                    "default": "method",
                },
            },
        },
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


def as_tools():
    """Convert raw TOOLS dicts to mcp.types.Tool instances."""
    from mcp_guard import Tool  # noqa: F401  lazy import

    return [Tool(**t) for t in TOOLS]
