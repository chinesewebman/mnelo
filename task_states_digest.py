# === task_states_digest.py — digest block rendering ===
# [refactor 2026-08-12] 拆分自 task_states.py — 见 task_states_core.py 顶部注释.
# digest_mixin.py 直接 import 这两个函数 (不在 facade re-export 失败路径上).
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("mnelo.task_states")

# [refactor 2026-08-12] 跨模块依赖从 task_states_core 取 — 见 task_states_loop.py 同款注释.
from task_states_core import (  # noqa: E402
    DIGEST_BLOCK4_MAX_CHARS,
    _default_now,
)


def list_active_tasks_and_loops(
    conn: Any,
    *,
    now: Optional[str] = None,
    stale_days_threshold: Optional[int] = None,  # [8/9 P1-yanru] 默认从 config.task_stale_days_threshold 读
    limit: int = 50,
) -> Dict[str, Any]:
    """[8/6 M4 digest 集成] 列出未闭环 task + dormant loop (DESIGN §4.4).

    目的: digest「未闭环」块; 给 LLM / Claude Code 上下文恢复时, 知道哪些 task / loop
    在自身生命周期里未结束, 应该被关注.

    返回结构:
        {
            "active_tasks": [
                {
                    "task_id": str, "name": str, "state": str,
                    "loop_id": Optional[str], "state_valid_from": str,
                    "age_days": float,
                    "is_stale": bool,
                    "last_reason": Optional[str],
                },
            ],
            "dormant_loops": [
                {
                    "loop_id": str, "name": str, "interval_hours": int,
                    "current_state": str,
                    "age_days": float,
                    "last_cycle_done_at": Optional[str],
                    "trigger": str,
                },
            ],
            "counts": {"active_tasks": N, "dormant_loops": N, "stale_tasks": N},
            "truncated": bool,
        }

    Args:
        conn: open sqlite3.Connection.
        now: 时间参考 (默认 = 当前).
        stale_days_threshold: 算 stale (默认从 config.task_stale_days_threshold 读, 当前 7 天).
        active 超阈值天数没 transition → stale.
        limit: 各自分组上限 (默认 50).
    """
    from datetime import datetime as _dt

    # [8/9 P1-yanru] stale_days_threshold fallback — None 时从 config 读
    if stale_days_threshold is None:
        from config import config as _cfg

        stale_days_threshold = _cfg.task_stale_days_threshold

    now_ts = now or _default_now()
    try:
        ref_now = _dt.fromisoformat(now_ts)
    except (ValueError, TypeError):
        ref_now = _dt.fromisoformat(_default_now())

    # 1. active tasks (state NOT IN done/cancelled, valid_until IS NULL)
    rows = conn.execute(
        """SELECT t.task_id, t.state, t.valid_from, t.reason, t.evidence_chunk_id,
                  e.name, e.properties_json
           FROM task_states t
           JOIN entities e ON e.id = t.task_id
           WHERE t.valid_until IS NULL
             AND t.state NOT IN ('done', 'cancelled')
             AND e.kind = 'task'
           ORDER BY t.valid_from ASC LIMIT ?""",
        (limit,),
    ).fetchall()

    active_tasks = []
    stale_count = 0
    for r in rows:
        try:
            props = json.loads(r[5]) if r[5] else {}
        except (TypeError, ValueError):
            props = {}
        try:
            vf = _dt.fromisoformat(r[2])
            age_days = (ref_now - vf).total_seconds() / 86400.0
        except (ValueError, TypeError):
            age_days = 0.0
        is_stale = age_days >= stale_days_threshold
        if is_stale:
            stale_count += 1
        active_tasks.append(
            {
                "task_id": r[0],
                "name": r[5] if r[5] else None,
                "state": r[1],
                "state_valid_from": r[2],
                "loop_id": props.get("loop_id"),
                "age_days": round(age_days, 2),
                "is_stale": is_stale,
                "last_reason": r[3],
            }
        )

    # 2. dormant loops (enabled=False, 当前状态=dormant, valid_until IS NULL)
    rows2 = conn.execute(
        """SELECT e.id, e.name, e.properties_json, t.state, t.valid_from
           FROM entities e
           LEFT JOIN task_states t ON t.task_id = e.id AND t.valid_until IS NULL
           WHERE e.kind = 'loop' AND e.valid_until IS NULL
           ORDER BY e.updated_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    dormant_loops = []
    for r in rows2:
        try:
            props = json.loads(r[2]) if r[2] else {}
        except (TypeError, ValueError):
            props = {}
        enabled = props.get("enabled", True)
        # 仅 dormant (enabled=False 或 current_state=dormant)
        if enabled and r[3] != "dormant":
            continue
        try:
            vf = _dt.fromisoformat(r[4]) if r[4] else _dt.fromisoformat("1970-01-01T00:00:00")
            age_days = (ref_now - vf).total_seconds() / 86400.0
        except (ValueError, TypeError):
            age_days = 0.0
        dormant_loops.append(
            {
                "loop_id": r[0],
                "name": r[1],
                "interval_hours": props.get("interval_hours"),
                "current_state": r[3] or "dormant",
                "age_days": round(age_days, 2),
                "last_cycle_done_at": props.get("last_cycle_done_at"),
                "trigger": props.get("trigger"),
            }
        )

    truncated = len(rows) == limit or len(rows2) == limit
    return {
        "active_tasks": active_tasks,
        "dormant_loops": dormant_loops,
        "counts": {
            "active_tasks": len(active_tasks),
            "dormant_loops": len(dormant_loops),
            "stale_tasks": stale_count,
        },
        "truncated": truncated,
    }


def render_digest_block4(active_block: Dict[str, Any]) -> Tuple[str, Dict[str, List[str]]]:
    """[8/6 M4 + M30 + M32.3] 把 list_active_tasks_and_loops 结果渲染成 digest 行.

    返回:
        (text_lines, line_refs) - 跟 _build_digest 其他 block 同型.

    M30 fix: 输出总长 <=2000 字符 (DIGEST_BLOCK4_MAX_CHARS). 超出截断 + "..."
    后缀, 避免 digest 撑爆 agent context window.

    M32.3 fix: 用显式 truncated flag 判断是否已截断, 不要从末行 endswith("...")
    反推 - 任务名以 "..." 结尾会误判已截断, 静默丢失 dormant loop 块.
    """
    text_lines: List[str] = []
    refs: Dict[str, List[str]] = {}
    n = 0
    truncated = False  # [M32.3 fix] 显式标记

    n += 1
    text_lines.append(f"未闭环 task ({active_block['counts']['active_tasks']}):")
    refs[str(n)] = []

    for t in active_block["active_tasks"]:
        if truncated:
            # 已截断, 剩余 task 不渲染 (block 4 容量用尽)
            break
        n += 1
        stale_mark = " ⚠stale" if t["is_stale"] else ""
        # [M32.3 fix] 截断长 task name 到 60 chars (防单行撑爆). 完整 name 在
        # memory.list_tasks 可见, digest 只给 hook 摘要. 加 "..." 标记.
        name = t["name"]
        if len(name) > 60:
            name = name[:57] + "..."
        line = f"  - [{t['state']}] {name} (age={t['age_days']}d{stale_mark})"
        text_lines.append(line)
        refs[str(n)] = [t["task_id"]]
        # 累计长度检测 + 截断. 真 joined 长 = sum(len(s)) + n - 1.
        # 加 "..." (4 chars: 3 + 1 separator) 触发.
        current = sum(len(s) for s in text_lines) + len(text_lines) - 1
        # 下次循环 append 1 line (含 sep) → current + len(line) + 1.
        # 阈值: current + 4 + 80 > 2000 → 保守触发 (80 char 是 truncate 后 name 行最大).
        if current + 4 + 80 > DIGEST_BLOCK4_MAX_CHARS:
            text_lines.append("...")
            truncated = True
            break

    if active_block["counts"]["dormant_loops"] and not truncated:
        n += 1
        text_lines.append(f"dormant loop ({active_block['counts']['dormant_loops']}):")
        refs[str(n)] = []
        for loop in active_block["dormant_loops"]:
            if truncated:
                break
            n += 1
            # [M35.2 fix] 截断长 loop name 到 60 chars (跟 task 段对称)
            loop_name = loop["name"]
            if len(loop_name) > 60:
                loop_name = loop_name[:57] + "..."
            line = f"  - {loop_name} (interval={loop.get('interval_hours')}h)"
            text_lines.append(line)
            refs[str(n)] = [loop["loop_id"]]
            # [M35.2 fix] 跟 task 段 (1514) 同公式 - 真 joined 长 = sum(len(s)) + n - 1,
            # 余量 80 chars 保守防超 2000 (loop 行最大 ~80 char after 60-char name cap).
            current = sum(len(s) for s in text_lines) + len(text_lines) - 1
            if current + 4 + 80 > DIGEST_BLOCK4_MAX_CHARS:
                text_lines.append("...")
                truncated = True

    return text_lines, refs
