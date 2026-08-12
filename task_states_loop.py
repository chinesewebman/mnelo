# === task_states_loop.py — Loop CRUD + loop_tick ===
# [refactor 2026-08-12] 拆分自 task_states.py — 见 task_states_core.py 顶部注释.
# 跨模块依赖: self.exception 用 TaskLoopError + LoopNotFoundError (task_states_core re-export
# via facade); _default_now 从 task_states_core 复用.
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mnelo.task_states")

# [refactor 2026-08-12] 跨模块依赖从 task_states_core 取 (单源真相, 不经 facade
# 中介避免循环 import). helpers + exception classes 都在 core 定义.
from task_states_core import (  # noqa: E402
    LoopNotFoundError,
    TaskLoopError,
    _default_now,
    _slugify,
)


def loop_tick(
    conn: Any,
    *,
    loop_id: str,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Mechanically compute loop tick verdict (DESIGN §4.3).

    Args:
        conn: open sqlite3.Connection.
        loop_id: entities.id (kind='loop').
        now: optional timestamp override (default datetime.now local).

    Returns:
        dict {loop_id, verdict, active_task_id?, active_state?, last_cycle_done_at?, interval_hours?, enabled?}.

    Verdict (DESIGN §4.3 step 1-5):
      - dormant:        not enabled
      - waiting:        active_id 存在且 active_state ∉ {done, cancelled}
      - due:            last_cycle_done_at is None (first run) OR
                        elapsed >= interval_hours
      - not_due:        没 active_id 且 last 距今 < interval_hours

    Note: 跟 transition() 不同, loop_tick 不写 task_states — 仅返回 verdict.
    tick 判定不落行 (DESIGN §4.3 strict). 仅生命周期事件 (create/disable/pause)
    落行, M5 完整.
    """
    # 1. 定位 loop entity
    cur = conn.execute(
        "SELECT id, properties_json FROM entities WHERE id = ? AND kind = 'loop' AND valid_until IS NULL",
        (loop_id,),
    ).fetchone()
    if cur is None:
        raise LoopNotFoundError(
            f"loop_id '{loop_id}' 不存在或已软删",
            field="loop_id",
        )
    _, props_json = cur
    if not props_json:
        raise LoopNotFoundError(
            f"loop '{loop_id}' properties_json 为空",
            field="loop_id",
        )
    try:
        cfg = json.loads(props_json)
    except json.JSONDecodeError as e:
        raise LoopNotFoundError(
            f"loop '{loop_id}' properties_json 解析失败: {e}",
            field="loop_id",
        )

    enabled = bool(cfg.get("enabled", True))
    interval_hours = cfg.get("interval_hours", 24)
    active_task_id = cfg.get("active_task_id")
    last_cycle_done_at = cfg.get("last_cycle_done_at")

    out: Dict[str, Any] = {
        "loop_id": loop_id,
        "enabled": enabled,
        "interval_hours": interval_hours,
        "active_task_id": active_task_id,
        "last_cycle_done_at": last_cycle_done_at,
    }

    # 2. step 1 — not enabled — dormant
    if not enabled:
        out["verdict"] = "dormant"
        return out

    # 3. step 2 — active 在飞 — waiting
    active_state: Optional[str] = None
    if active_task_id:
        active_row = conn.execute(
            "SELECT state FROM task_states WHERE task_id = ? AND valid_until IS NULL",
            (active_task_id,),
        ).fetchone()
        if active_row is not None:
            active_state = active_row[0]
            if active_state not in ("done", "cancelled"):
                out["verdict"] = "waiting"
                out["active_state"] = active_state
                return out

    # 4. step 3 — last is None — first run
    if last_cycle_done_at is None:
        out["verdict"] = "due"
        if active_state is not None:
            out["active_state"] = active_state
        return out

    # 5. step 4-5 — elapsed vs interval
    try:
        from datetime import datetime as _dt

        last_dt = _dt.fromisoformat(last_cycle_done_at)
        now_dt = _dt.fromisoformat(now) if now else _dt.now()
        elapsed_hours = (now_dt - last_dt).total_seconds() / 3600.0
    except (ValueError, TypeError) as e:
        raise LoopNotFoundError(
            f"loop '{loop_id}' last_cycle_done_at 解析失败: {e}",
            field="loop_id",
        )

    out["elapsed_hours"] = round(elapsed_hours, 4)
    if elapsed_hours < interval_hours:
        out["verdict"] = "not_due"
    else:
        out["verdict"] = "due"
    return out


def loop_create(
    conn: Any,
    *,
    name: str,
    trigger: str,
    interval_hours: int = 24,
    enabled: bool = True,
    priority: int = 3,
    owner_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a loop entity (DESIGN §5.1 memory_loop_create)."""
    if not name or not name.strip():
        raise TaskLoopError("name 必填", field="name", code="InvalidInputError")
    if not trigger or not trigger.strip():
        raise TaskLoopError("trigger 必填", field="trigger", code="InvalidInputError")
    if interval_hours <= 0:
        raise TaskLoopError(
            f"interval_hours {interval_hours} 必须 > 0",
            field="interval_hours",
        )

    ts = now or _default_now()
    # [review-pass RF2] _slugify 含中文 → hashlib fallback
    slug = _slugify(name)
    loop_id = f"loop:{slug}"
    n = 0
    while conn.execute("SELECT 1 FROM entities WHERE id = ?", (loop_id,)).fetchone():
        n += 1
        loop_id = f"loop:{slug}-{n}"
        if n > 100:
            raise TaskLoopError(
                f"loop_id 撞名 100+ 次: {loop_id}",
                field="loop_id",
                code="LoopIdCollisionError",
            )

    props = {
        "trigger": trigger,
        "interval_hours": interval_hours,
        "enabled": enabled,
        "active_task_id": None,
        "last_cycle_done_at": None,
        "priority": priority,
        "owner_id": owner_id,
    }
    conn.execute(
        "INSERT INTO entities (id, kind, name, properties_json, memory_type) VALUES (?, ?, ?, ?, 'ephemeral')",
        (loop_id, "loop", name, json.dumps(props)),
    )
    # loop 初始状态: dormant (enabled=False) 或 不落窗 (默认 enabled=True; 等第一个 tick)
    if not enabled:
        conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, valid_until, reason, evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, NULL, ?)",
            (loop_id, "dormant", ts, "create disabled", ts),
        )
    return {"loop_id": loop_id, "enabled": enabled, "interval_hours": interval_hours}


def loop_update(
    conn: Any,
    *,
    loop_id: str,
    enabled: Optional[bool] = None,
    trigger: Optional[str] = None,
    interval_hours: Optional[int] = None,
    priority: Optional[int] = None,
    owner_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """[8/6 M3 Step 12] 改 loop properties (DESIGN §5.1).

    只改明确提供的字段 (None = 不改). 不会动 active_task_id 或 last_cycle_done_at
    (那些由 transition() 终端簿记或 loop_tick 写).

    enabled=False 写 dormant 状态窗; enabled=True 关 dormant 窗 (若有).
    其他字段仅改 properties_json.

    [review-pass RF12 8/6] interval_hours 校验 (<=0 拒); 跟 loop_create 同型.
    [review-pass RF13 8/6] 排除已软删 loop (valid_until IS NULL filter).
    [review-pass RF14 8/6] enabled 切换走 CAS 单语句 (UPDATE WHERE active IS NULL +
                     INSERT NEW, 失败回滚由 _handle_task_simple 兜底).
    """
    # [RF13] 排除软删 loop
    row = conn.execute(
        "SELECT properties_json FROM entities WHERE id=? AND kind='loop' AND valid_until IS NULL",
        (loop_id,),
    ).fetchone()
    if row is None:
        raise LoopNotFoundError(
            f"loop_id 不存在或已软删: {loop_id}",
            field="loop_id",
            code="LoopNotFoundError",
        )

    # [RF12] interval_hours 校验 (跟 loop_create 一致)
    if interval_hours is not None and interval_hours <= 0:
        raise TaskLoopError(
            f"interval_hours 必须 > 0, got {interval_hours}",
            field="interval_hours",
            code="InvalidIntervalError",
        )

    cfg = json.loads(row[0])

    # 1. 改 properties (按需)
    changed: Dict[str, Any] = {}
    if trigger is not None:
        cfg["trigger"] = trigger
        changed["trigger"] = trigger
    if interval_hours is not None:
        cfg["interval_hours"] = interval_hours
        changed["interval_hours"] = interval_hours
    if priority is not None:
        cfg["priority"] = priority
        changed["priority"] = priority
    if owner_id is not None:
        cfg["owner_id"] = owner_id
        changed["owner_id"] = owner_id

    # 2. enabled 切换: CAS 关旧 + 开新 (单 SQL UPDATE WHERE, RF14)
    if enabled is not None and cfg.get("enabled", True) != enabled:
        cfg["enabled"] = enabled
        changed["enabled"] = enabled
        ts = now or _default_now()
        new_state = "dormant" if not enabled else "running"

        # [RF14 8/6] 单 SQL CAS 关旧窗 (UPDATE WHERE active), 0 行说明并发赢家已关
        # noqa: F841 — 保留 rowcount 便于未来加 metric/log; 现行 IntegrityError 兜底已足够
        affected = conn.execute(  # noqa: F841
            "UPDATE task_states SET valid_until = ? WHERE task_id = ? AND valid_until IS NULL",
            (ts, loop_id),
        ).rowcount
        # 即使 affected == 0 (无活动窗), 也允许 INSERT 新状态窗 (loop_create 路径
        # 同型); 不视为错误. 真并发两笔同方向 UPDATE 的赢家由 rowcount=1 区分,
        # 输家 (INSERT 已撞 ux_task_current_state) → IntegrityError 由 MCP RF8 兜底.
        conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, valid_until, reason, evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, NULL, ?)",
            (loop_id, new_state, ts, f"loop_update: enabled={enabled}", ts),
        )

    # 3. 写回 properties_json
    conn.execute(
        "UPDATE entities SET properties_json=? WHERE id=?",
        (json.dumps(cfg), loop_id),
    )

    return {
        "loop_id": loop_id,
        "changed": changed,
        "enabled": cfg.get("enabled", True),
        "interval_hours": cfg.get("interval_hours"),
    }


def list_loops(
    conn: Any,
    *,
    enabled_only: bool = False,
    state: Optional[str] = None,
    asof: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """[8/6 M3 Step 12] 列 loop entities + 当前状态 (DESIGN §5.1).

    默认: 所有 loop + 各自 current_state.
    enabled_only=True: 仅 enabled=True 的 loop (DESIGN §4.3).
    state=... 过滤: 当前状态精确匹配 (e.g. 'running', 'dormant').
    asof 时间切片 (默认 = 当前).
    limit 上限.

    返回: {loops: [{loop_id, name, enabled, interval_hours, current_state,
                    active_task_id, last_cycle_done_at, trigger}], count, truncated}
    """
    asof_ts = asof or _default_now()

    # 1. 拉 loop entities (排除软删 — RF13 8/6)
    rows = conn.execute(
        "SELECT id, name, properties_json FROM entities WHERE kind='loop' AND valid_until IS NULL ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            cfg = json.loads(r[2])
        except (TypeError, ValueError):
            cfg = {}
        loop_enabled = cfg.get("enabled", True)
        if enabled_only and not loop_enabled:
            continue

        # 2. asof 当前状态 (active 窗 or 默认 'dormant' if entity disabled)
        win = conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?) ORDER BY valid_from DESC LIMIT 1",
            (r[0], asof_ts, asof_ts),
        ).fetchone()
        current_state = win[0] if win else ("dormant" if not loop_enabled else None)

        if state is not None and current_state != state:
            continue

        out.append(
            {
                "loop_id": r[0],
                "name": r[1],
                "enabled": loop_enabled,
                "interval_hours": cfg.get("interval_hours"),
                "current_state": current_state,
                "active_task_id": cfg.get("active_task_id"),
                "last_cycle_done_at": cfg.get("last_cycle_done_at"),
                "trigger": cfg.get("trigger"),
            }
        )

    truncated = len(rows) == limit
    return {"loops": out, "count": len(out), "truncated": truncated}


def forget_loop(
    conn: Any,
    loop_id: str,
    *,
    reason: str,
    now: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """[8/6 M5.3 + DESIGN §10.2 D11] 显式软删 loop (D11 TTL 豁免路径).

    跟 forget_task 类似, 但额外级联:
      - 关闭 loop 的 task_states (enabled/dormant 当前行)
      - 不级联 loop 内 task (task 由自己的生命周期管理, 不随 loop 删除)

    Args:
        conn: open sqlite3.Connection.
        loop_id: 要删的 loop id.
        reason: 必填 (D8).
        now: 时间参考 (默认 = 当前).
        run_id: 自定义 run_id.

    Returns:
        {"loop_id", "forgotten_at", "run_id", "rows_invalidated"}
    """
    import uuid as _uuid

    # [M35.1 fix] reason isinstance(str) 守卫 — 跟 forget_task 同, API 边界一致.
    if not isinstance(reason, str):
        raise TaskLoopError(
            f"reason 必须 str 类型, got {type(reason).__name__}",
            field="reason",
            code="InvalidReasonTypeError",
        )
    # [M33.2 fix] reason 强校验 — strip + 非空 + min length 5.
    reason_clean = reason.strip() if reason else ""
    if not reason_clean:
        raise TaskLoopError(
            "forget_loop 必须提供非空 reason (D8 显式纠正门)",
            field="reason",
            code="ReasonRequiredError",
        )
    if len(reason_clean) < 5:
        raise TaskLoopError(
            f"forget_loop reason 长度需 >=5 字符 (审计可读), got {len(reason_clean)}",
            field="reason",
            code="ReasonTooShortError",
        )
    now_ts = now or _default_now()
    # [M33.1 fix] BEGIN IMMEDIATE 事务 — check + UPDATE 原子化, 跟 forget_task 同.
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT valid_until FROM entities WHERE id=? AND kind='loop'",
            (loop_id,),
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"loop_id={loop_id} 不存在或已软删",
                field="loop_id",
                code="LoopNotFoundError",
            )
        if row[0] is not None:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"loop_id={loop_id} 已于 {row[0]} 软删, 重复 forget 拒绝",
                field="loop_id",
                code="LoopAlreadyForgotten",
            )

        # 关闭 task_states 当前行 (loop 用 task_states 记录 enabled/dormant)
        invalidated = conn.execute(
            """UPDATE task_states SET valid_until = ?
               WHERE task_id=? AND valid_until IS NULL""",
            (now_ts, loop_id),
        ).rowcount

        # 软删 entity
        conn.execute(
            """UPDATE entities SET valid_until = ?
               WHERE id=? AND valid_until IS NULL""",
            (now_ts, loop_id),
        )

        # cascade relations
        rels = conn.execute(
            """UPDATE relations SET valid_until = ?
               WHERE (source_id=? OR target_id=?) AND valid_until IS NULL""",
            (now_ts, loop_id, loop_id),
        ).rowcount

        rid = run_id or f"forced_forget-{now_ts}-{_uuid.uuid4().hex[:8]}"
        conn.execute(
            """INSERT INTO audit_log (
                run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                "forced_forget",
                "explicit_softdelete",
                "loop",
                loop_id,
                json.dumps({"status_before": "active", "rows_invalidated": invalidated}, ensure_ascii=False),
                json.dumps({"reason": reason_clean, "forgotten_at": now_ts, "relations_cascade": rels}, ensure_ascii=False),
                1.0,
                "applied",
                now_ts,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {
        "loop_id": loop_id,
        "forgotten_at": now_ts,
        "run_id": rid,
        "rows_invalidated": invalidated,
        "relations_cascade": rels,
    }
