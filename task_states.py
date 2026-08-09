"""task_states.py — M2 task/loop 状态机核心 (DESIGN_TASK_LOOP §4.2).

[review-pass fixes, 8/6 owner code review]
- RF1: 毫秒级 timestamp (零长状态窗修复)
- RF2: 中文 slug 拼音 fallback (pypinyin, 顺序 1: pinyin, 2: 拼音前 4 字符, 3: 拼音首字母)
- RF3: task_create active_task_id 单一 UPDATE WHERE 原子 (防双 spawn)
- RF4: docstring + import 顺序合规 (PEP 257 / flake8 E402)
- RF6: transition() 文档明示调用方需包事务
- RF7: typing.List 显式导入
"""

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("mnelo.task_states")


# === 异常族 ===
class TaskLoopError(Exception):
    def __init__(self, message: str, field: Optional[str] = None, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code or self.__class__.__name__

    def to_dict(self) -> Dict[str, str]:
        out = {"code": self.code, "message": self.message}
        if self.field:
            out["field"] = self.field
        return out


class TaskNotFoundError(TaskLoopError):
    """task_id 无当前活动窗."""


class InvalidTransitionError(TaskLoopError):
    """from_state → to_state 不在允许图里 (force=False)."""


class NotCurrentStateError(TaskLoopError):
    """CAS 关旧窗 0 行 — 并发 / 重复."""


class EvidenceNotFoundError(TaskLoopError):
    """evidence_chunk_id 提供但 chunks.id 不存在."""


class ReasonRequiredError(TaskLoopError):
    """force=True 需要 reason (D8)."""


class TerminalLoopError(TaskLoopError):
    """cancelled 后再 transfer 拒收."""


# === 状态词汇 ===
TASK_STATES = frozenset(
    {
        "open",
        "in_progress",
        "waiting",
        "blocked",
        "done",
        "cancelled",
    }
)
LOOP_STATES = frozenset({"running", "dormant", "paused"})
ALL_STATES = TASK_STATES | LOOP_STATES


def _default_now() -> str:
    # [review-pass RF1] 毫秒级精度, 避免同秒双 transfer 产生零长状态窗
    # (asof 回放中途态会丢). 8/6 ship.
    return datetime.now().isoformat(timespec="milliseconds")


def _slugify(name: str) -> str:
    """[review-pass RF2 8/6 + RF10 8/6] name → URL-safe slug.

    分 2 路由:
      1. 纯 ASCII 含 a-z/0-9/-: 直接 lowercase + 保留 [a-z0-9-], 30 字符 max.
      2. 含中文等非 ASCII: 走 hashlib.md5(name.encode()).hexdigest()[:8]
         (DESIGN §2.1 弃 pinyin — 主人 8/6 P22 '永不偷工' 不加新依赖).

    例 (RF10 cross-check):
        "采购耗材"        → "a49a962a"
        "下单发货"        → "9e7a4e54"
        "耗材库存监控"    → "7b526973"
        "fix bug"         → "fix-bug"
        "Restock supplies" → "restock-supplies"

    注: 含中英混合名 ("更新 task 列表") 走 ASCII 路径 (含英文字母命中分支 1),
    返回 "task" 或 "task-1"; 此退化已知, 不在本次修 (RF2 已 ship, 后续可加
    segment 路由).
    """
    # 1. 走 regex 纯 ASCII 路径
    ascii_slug = re.sub(r"[^a-z0-9-]", "-", name.lower())[:30].strip("-")
    if ascii_slug and any(c.isalpha() for c in ascii_slug):
        return ascii_slug
    # 2. 含中文 / 全非 ASCII → hash
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:8]


def transition(
    conn: Any,
    *,
    task_id: str,
    to_state: str,
    reason: str,
    evidence_chunk_id: Optional[str] = None,
    force: bool = False,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Single CAS transfer (DESIGN §4.2 literal step 1-5).

    [review-pass RF6 8/6] 事务要求: 调用方必须用 BEGIN/COMMIT 包裹, 否则
    关旧窗 (UPDATE) 提交 + 开新窗 (INSERT) 失败时, task 变 "无活动窗",
    后续 transfer 报 TaskNotFoundError. transition() 内部不自动 commit —
    跟 SELECT FOR UPDATE 一致, 把事务边界交给调用方.

    Args:
        conn: open sqlite3.Connection (FK on, WAL mode ready, transaction open).
        task_id: entities.id (kind='task' or kind='loop').
        to_state: 6 task states + 3 loop states.
        reason: required; 含 actor 痕迹 (D8 强制). 恒必填, 不依赖 force.
        evidence_chunk_id: optional FK to chunks.id.
        force: bypass allowed graph; reason 仍必填 (D8 纠正门需审计痕迹).
        now: optional timestamp override.

    Returns:
        dict 含 window_id, from_state, to_state, valid_from,
        以及 optional 'terminal_bookkeeping' 块.

    Raises:
        TaskNotFoundError, NotCurrentStateError (并发 CAS 0 行),
        InvalidTransitionError, ReasonRequiredError, EvidenceNotFoundError.

    Example:
        with conn:  # 事务包裹
            result = transition(conn, task_id=tid, to_state="done",
                                reason="收货", now="2026-08-06T10:30")
    """
    # [M36 fix] task_id / to_state isinstance 守卫 — API 边界一致.
    if not isinstance(task_id, str):
        raise TaskLoopError(
            f"task_id 必须 str, got {type(task_id).__name__}",
            field="task_id",
            code="InvalidInputError",
        )
    if not isinstance(to_state, str):
        raise TaskLoopError(
            f"to_state 必须 str, got {type(to_state).__name__}",
            field="to_state",
            code="InvalidInputError",
        )

    # 0. 状态词汇校验
    if to_state not in ALL_STATES:
        raise InvalidTransitionError(
            f"to_state '{to_state}' 不在状态词汇集 (task: {sorted(TASK_STATES)}, loop: {sorted(LOOP_STATES)})",
            field="to_state",
        )

    # [M36 fix] reason 必填 + isinstance 守卫 — D8 显式纠正门, 不依赖 force.
    # 旧 'if force and not reason' 只在 force=True 校验, 但 docstring 写
    # 'reason: required', 行为不一. 统一为 always required + str 类型.
    if not isinstance(reason, str):
        raise ReasonRequiredError(
            f"reason 必须 str 类型, got {type(reason).__name__}",
            field="reason",
        )
    if not reason.strip():
        raise ReasonRequiredError(
            "transition reason 必填 (D8 显式纠正门, docstring 契约)",
            field="reason",
        )

    # [M36 fix] evidence_chunk_id isinstance 守卫 — str 或 None.
    if evidence_chunk_id is not None and not isinstance(evidence_chunk_id, str):
        raise TaskLoopError(
            f"evidence_chunk_id 必须 str 或 None, got {type(evidence_chunk_id).__name__}",
            field="evidence_chunk_id",
            code="InvalidInputError",
        )

    # 0.2 evidence_chunk_id 存在性校验
    if evidence_chunk_id is not None:
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE id = ? AND valid_until IS NULL",
            (evidence_chunk_id,),
        ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(
                f"evidence_chunk_id '{evidence_chunk_id}' 不存在或已软删",
                field="evidence_chunk_id",
            )

    # 1. 定位当前活动窗
    cur = conn.execute(
        "SELECT id, state, valid_from FROM task_states WHERE task_id = ? AND valid_until IS NULL",
        (task_id,),
    ).fetchone()
    if cur is None:
        raise TaskNotFoundError(
            f"task_id '{task_id}' 无活动状态窗 (no open window)",
            field="task_id",
        )
    current_id, from_state, current_valid_from = cur

    # 1.1 cancelled 是 terminal — 任何 transfer 都拒
    if from_state == "cancelled":
        raise TerminalLoopError(
            f"task '{task_id}' 处于 terminal 'cancelled'; 任何 transfer 都拒",
            field="task_id",
        )

    # 1.2 done 的去向: 仅 reopen 逃生门
    if from_state == "done" and to_state != "open" and not force:
        raise InvalidTransitionError(
            f"done 是 terminal (除 reopen 逃生门 done→open): 拒 done→{to_state}",
            field="to_state",
        )

    # 2. 允许图校验
    if not force:
        # 优先查 task 关联的 loop_id (M5 完整), 此处简化: entities.properties_json 里读
        loop_id = conn.execute(
            "SELECT properties_json FROM entities WHERE id = ? AND valid_until IS NULL",
            (task_id,),
        ).fetchone()
        loop_scope = None
        if loop_id and loop_id[0]:
            try:
                props = json.loads(loop_id[0])
                loop_scope = props.get("loop_id")
            except (json.JSONDecodeError, TypeError):
                loop_scope = None

        allowed = conn.execute(
            "SELECT 1 FROM state_transitions WHERE (scope = ? OR scope = 'default')   AND from_state = ? AND to_state = ?",
            (loop_scope or "default", from_state, to_state),
        ).fetchone()
        if not allowed:
            raise InvalidTransitionError(
                f"转移 {from_state}→{to_state} 不在允许图里 (scope={loop_scope or 'default'})",
                field="to_state",
            )

    # 3. CAS 关旧 + 开新
    ts = now or _default_now()
    # [RF11 + RF17 8/6 review-pass] 严格递增: 防 0 窗 + 负长窗.
    #   取 max(已关窗最大 valid_until, 当前活动窗 valid_from), 若 >= ts 推进 1ms.
    #   - 修 RF11: 旧 valid_until == ts → 零长
    #   - 修 RF17: caller 回拨 now 早于当前活动窗 valid_from → 负长
    #     (取 max(closed, active_from) 让 ts 至少 >= active 起点)
    _ts_row = conn.execute(
        "SELECT valid_until, valid_from FROM task_states WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    _max_floor_ts = None
    if _ts_row is not None:
        # _ts_row[0] = 最新一窗的 valid_until (None = 该窗仍是活动)
        # _ts_row[1] = 该窗的 valid_from
        # 若最新一窗 valid_until IS NULL (活动窗), 取它的 valid_from 作 floor
        # 若最新一窗 valid_until 有值 (刚关), 取它作 floor
        _max_floor_ts = _ts_row[0] if _ts_row[0] else _ts_row[1]
    if _max_floor_ts and _max_floor_ts >= ts:
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        try:
            _t = _dt.fromisoformat(_max_floor_ts)
            _t = _t + _td(milliseconds=1)
            ts = _t.isoformat(timespec="milliseconds")
        except (ValueError, TypeError):
            pass  # ts 无法 parse, 留原值 (caller 责任)
    affected = conn.execute(
        "UPDATE task_states SET valid_until = ? WHERE id = ? AND task_id = ? AND valid_until IS NULL",
        (ts, current_id, task_id),
    ).rowcount
    if affected == 0:
        raise NotCurrentStateError(
            f"CAS 关旧窗 0 行 (task_id={task_id}, id={current_id}); 并发冲突 / 重复提交",
            field="task_id",
        )
    cur2 = conn.execute(
        "INSERT INTO task_states (task_id, state, valid_from, valid_until, reason,  evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (task_id, to_state, ts, reason, evidence_chunk_id, ts),
    )
    new_window_id = cur2.lastrowid

    # 4. 终端簿记 (done/cancelled 且是 loop active_task_id)
    bookkeeping: Optional[Dict[str, Any]] = None
    if to_state in ("done", "cancelled"):
        loops = conn.execute("SELECT id, properties_json FROM entities WHERE kind = 'loop' AND valid_until IS NULL").fetchall()
        for loop_id, props_json in loops:
            if not props_json:
                continue
            try:
                props = json.loads(props_json)
            except json.JSONDecodeError:
                continue
            if props.get("active_task_id") == task_id:
                props["active_task_id"] = None
                props["last_cycle_done_at"] = ts
                conn.execute(
                    "UPDATE entities SET properties_json = ? WHERE id = ?",
                    (json.dumps(props), loop_id),
                )
                bookkeeping = {
                    "loop_id": loop_id,
                    "last_cycle_done_at": ts,
                    "action": "clear_active_task",
                }
                logger.info(f"[task_states] 终端簿记: loop {loop_id} active_task_id={task_id} → NULL, last_cycle={ts}")
                break

    result: Dict[str, Any] = {
        "task_id": task_id,
        "from_state": from_state,
        "to_state": to_state,
        "from_valid_from": current_valid_from,
        "valid_from": ts,
        "window_id": new_window_id,
    }
    if bookkeeping:
        result["terminal_bookkeeping"] = bookkeeping
    return result


class LoopNotFoundError(TaskLoopError):
    """loop_id 不存在或 valid_until 已设."""


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


def list_tasks(
    conn: Any,
    *,
    state: Optional[str] = None,
    loop_id: Optional[str] = None,
    asof: Optional[str] = None,
    stale_days: bool = False,
    limit: int = 50,
) -> Dict[str, Any]:
    """List task / loop windows (DESIGN §5.1 memory_task_list).

    Args:
        conn: open sqlite3.Connection.
        state: filter by current state (None = active only, 即非 done/cancelled/dormant/paused).
        loop_id: filter by parent loop (从 entities.properties_json 读 loop_id).
        asof: optional timestamp; only valid windows at asof returned.
        stale_days: if True, only windows with valid_from > threshold days ago.
        limit: max rows returned.

    Returns:
        dict {tasks: [{task_id, name, state, state_valid_from, loop_id?, owner_id?,
                       stale_days?}]}.

    Note: 把 list_tasks 放 step 3, 不在 transition() 一起, 是为可独立 ship.
    """
    where = []
    params: List[Any] = []

    # 当前活动窗过滤 (valid_until IS NULL)
    if asof is None:
        where.append("ts.valid_until IS NULL")
    else:
        # asof: 匹配 valid_from <= asof AND (valid_until IS NULL OR valid_until > asof)
        where.append("ts.valid_from <= ?")
        params.append(asof)
        where.append("(ts.valid_until IS NULL OR ts.valid_until > ?)")
        params.append(asof)  # [M5.4 e2e fix] asof 占位 + 参数绑定对齐

    if state is not None:
        if state not in ALL_STATES:
            raise InvalidTransitionError(
                f"state '{state}' 不在状态词汇集",
                field="state",
            )
        where.append("ts.state = ?")
        params.append(state)
    elif asof is None:
        # 默认: 仅 active (排除 done/cancelled/dormant/paused)
        where.append("ts.state NOT IN ('done','cancelled','dormant','paused')")

    if loop_id is not None:
        # properties_json 含有 loop_id 字段 (M3 task_create 写入, M5 完整)
        # 简化: 用 LIKE 匹配 JSON 字符串. 精准方案走 json_extract.
        where.append("(e.properties_json LIKE ?)")
        params.append(f'%"loop_id": "{loop_id}"%')

    sql = (
        "SELECT ts.task_id, e.name, ts.state, ts.valid_from, "
        "       e.properties_json, e.aliases_json "
        "FROM task_states ts JOIN entities e ON e.id = ts.task_id "
        "WHERE e.kind = 'task' AND " + " AND ".join(where) + " "
        "ORDER BY ts.valid_from ASC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()

    tasks = []
    for r in rows:
        task_id, name, st, vf, props_json, aliases_json = r
        loop_id_val = None
        owner_id_val = None
        if props_json:
            try:
                p = json.loads(props_json)
                loop_id_val = p.get("loop_id")
                owner_id_val = p.get("owner_id")
            except (json.JSONDecodeError, TypeError):
                pass
        entry = {
            "task_id": task_id,
            "name": name,
            "state": st,
            "state_valid_from": vf,
            "loop_id": loop_id_val,
            "owner_id": owner_id_val,
        }
        if stale_days:
            # 算 valid_from 距今多少天 (best-effort, 不严格 to-the-second)
            try:
                vf_dt = datetime.fromisoformat(vf)
                age_days = (datetime.now() - vf_dt).days
            except (ValueError, TypeError):
                age_days = None
            entry["stale_days"] = age_days
        tasks.append(entry)

    return {"tasks": tasks, "count": len(tasks), "truncated": len(tasks) >= limit}


def replay_task(
    conn: Any,
    *,
    task_id: str,
    asof: Optional[str] = None,
) -> Dict[str, Any]:
    """Replay a task's full state-window history (DESIGN §5.1 memory_task_replay).

    Args:
        conn: open sqlite3.Connection.
        task_id: entities.id.
        asof: optional timestamp; if given, only include windows valid at asof.

    Returns:
        dict {task_id, current_state, window_count, windows: [...]}.
    """
    params: List[Any] = [task_id]
    where = "task_id = ?"
    if asof is not None:
        where += " AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)"
        params.extend([asof, asof])

    rows = conn.execute(
        "SELECT state, valid_from, valid_until, reason, evidence_chunk_id FROM task_states WHERE " + where + " ORDER BY valid_from ASC",
        params,
    ).fetchall()

    cur = conn.execute(
        "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
        (task_id,),
    ).fetchone()
    current_state = cur[0] if cur else None

    windows = [
        {
            "state": r[0],
            "valid_from": r[1],
            "valid_until": r[2],
            "reason": r[3],
            "evidence_chunk_id": r[4],
        }
        for r in rows
    ]
    return {
        "task_id": task_id,
        "current_state": current_state,
        "window_count": len(windows),
        "windows": windows,
    }


def task_create(
    conn: Any,
    *,
    name: str,
    loop_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    priority: int = 3,
    summary: Optional[str] = None,
    evidence_chunk_id: Optional[str] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a task entity + open state window (DESIGN §5.1 memory_task_create).

    Args:
        conn: open sqlite3.Connection.
        name: human label (e.g. '采购耗材').
        loop_id: optional parent loop; if given, requires loop tick-verdict ready.
        owner_id: optional entity id (default person:yanru).
        priority: 0-5, default 3.
        summary: optional.
        evidence_chunk_id: optional FK to chunks.id.
        now: optional timestamp override.

    Returns:
        dict {task_id, current_state, loop_id?, created_at, open_window_id}.

    Raises:
        InvalidLoopError: loop_id provided but loop not found.
        LoopDisabledError: loop is disabled (enabled=False).
        LoopHasActiveTaskError: loop already has active_task_id (防双 spawn).
        EvidenceNotFoundError: evidence_chunk_id not found.
    """
    if not name or not name.strip():
        raise TaskLoopError("name 必填", field="name", code="InvalidInputError")

    if priority < 0 or priority > 5:
        raise TaskLoopError(
            f"priority {priority} 不在 0-5 范围",
            field="priority",
            code="InvalidInputError",
        )

    # 0. evidence_chunk_id 校验
    if evidence_chunk_id is not None:
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE id = ? AND valid_until IS NULL",
            (evidence_chunk_id,),
        ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(
                f"evidence_chunk_id '{evidence_chunk_id}' 不存在或已软删",
                field="evidence_chunk_id",
            )

    # 1. loop 校验 (loop_id 提供了)
    if loop_id is not None:
        loop_row = conn.execute(
            "SELECT id, properties_json FROM entities WHERE id = ? AND kind = 'loop' AND valid_until IS NULL",
            (loop_id,),
        ).fetchone()
        if loop_row is None:
            raise LoopNotFoundError(
                f"loop_id '{loop_id}' 不存在",
                field="loop_id",
            )
        _, props_json = loop_row
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
        if not cfg.get("enabled", True):
            raise TaskLoopError(
                f"loop '{loop_id}' 已禁用 (enabled=False)",
                field="loop_id",
                code="LoopDisabledError",
            )
        if cfg.get("active_task_id"):
            raise TaskLoopError(
                f"loop '{loop_id}' 已有 active_task_id={cfg['active_task_id']} (防双 spawn, §边界 #8)",
                field="loop_id",
                code="LoopHasActiveTaskError",
            )

    # 2. 生成 task_id (DESIGN §2.1: task:YYYYMMDD-<slug>)
    # [review-pass RF2] _slugify 含中文 → hashlib fallback, 避免 'task' 退化
    ts = now or _default_now()
    try:
        date_part = ts[:10].replace("-", "")
    except (TypeError, ValueError):
        date_part = "00000000"
    slug = _slugify(name)
    task_id = f"task:{date_part}-{slug}"

    # 确保 id 唯一 (collide 试 1-2 次)
    n = 0
    while conn.execute("SELECT 1 FROM entities WHERE id = ?", (task_id,)).fetchone():
        n += 1
        task_id = f"task:{date_part}-{slug}-{n}"
        if n > 100:
            raise TaskLoopError(
                f"task_id 撞名 100+ 次: {task_id}",
                field="task_id",
                code="TaskIdCollisionError",
            )

    # 3. INSERT entity (kind=task, memory_type=ephemeral, properties_json={loop_id, owner_id, priority, summary})
    props = {
        "loop_id": loop_id,
        "owner_id": owner_id,
        "priority": priority,
    }
    if summary:
        props["summary"] = summary
    conn.execute(
        "INSERT INTO entities (id, kind, name, summary, properties_json, memory_type) VALUES (?, ?, ?, ?, ?, 'ephemeral')",
        (task_id, "task", name, summary, json.dumps(props)),
    )

    # 4. INSERT task_states: open 窗
    cur = conn.execute(
        "INSERT INTO task_states (task_id, state, valid_from, valid_until, reason, evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (task_id, "open", ts, "task_create", evidence_chunk_id, ts),
    )
    open_window_id = cur.lastrowid

    # 5. 关联 loop: 写 loop.properties_json.active_task_id
    # [review-pass RF3 8/6] 单语句 UPDATE WHERE active_task_id IS NULL 原子
    # (防 check-then-write 双 spawn 竞态). 若 rowcount = 0, 别的 task_create 已抢先.
    if loop_id is not None:
        affected = conn.execute(
            "UPDATE entities SET properties_json = json_set(  COALESCE(properties_json, '{}'), '$.active_task_id', ?) WHERE id = ? AND json_extract(properties_json, '$.active_task_id') IS NULL",
            (task_id, loop_id),
        ).rowcount
        if affected == 0:
            # 双 spawn 失败 — entity 状态不一致, 事务回滚 (调用方负责)
            raise TaskLoopError(
                f"loop '{loop_id}' race: 别的 task_create 抢先写 active_task_id",
                field="loop_id",
                code="LoopHasActiveTaskError",
            )
        # 6. 写 loop 状态窗 'running' (DESIGN §4.3 生命周期事件)
        # 先看 loop 是否有 active 状态窗
        cur_loop_win = conn.execute(
            "SELECT id FROM task_states WHERE task_id = ? AND valid_until IS NULL",
            (loop_id,),
        ).fetchone()
        if cur_loop_win is None:
            conn.execute(
                "INSERT INTO task_states (task_id, state, valid_from, valid_until, reason, evidence_chunk_id, created_at) VALUES (?, ?, ?, NULL, ?, NULL, ?)",
                (loop_id, "running", ts, "loop: spawn task", ts),
            )
        # else: 已是 running 不重复落行

    result: Dict[str, Any] = {
        "task_id": task_id,
        "current_state": "open",
        "created_at": ts,
        "open_window_id": open_window_id,
    }
    if loop_id:
        result["loop_id"] = loop_id
    return result


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


def propose_stale_tasks(
    conn: Any,
    *,
    now: Optional[str] = None,
    stale_days_threshold: Optional[int] = None,  # [8/9 P1-yanru] 默认从 config.task_stale_days_threshold 读
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """[8/6 M5.2 + DESIGN TASK_LOOP 4.4 + M30/M32] 扫描 stale task, 写 audit_log Proposal.

    走 audit_log 现有表 (pass_name='stuck_task', status='proposed'). 跟 M5.1
    loop_tick_cron 提议 due loop 同模式, 但 pass_name 不同, 便于 L2 audit
    区分. mnelo 绝不自主转移任务 (D5) - Proposal 只做提议, 真正的 transition
    必须由 agent/用户经 memory_task_transition 显式 apply.

    收缩分桶 (DESIGN TASK_LOOP 4.4 D7):
      open > 7d       -> stale, 提议 review
      waiting > 14d   -> stale, 提议 review
      blocked > 3d    -> stale, 提议 review
      in_progress     -> stale_days_threshold (默认 7d) 单桶

    [M30 fix] 输入校验 - stale_days_threshold 必须正整数. 负数 / 0 / 非 int
    会让所有 task 触发 stale 或永不触发, 后果严重. 早抛错优于静默.

    Args:
        conn: open sqlite3.Connection.
        now: 时间参考 (默认 = 当前).
        stale_days_threshold: 通用 fallback (默认 7d). 上面分桶覆盖更具体.
        run_id: 自定义 run_id (默认 = 'stuck_task-<now>-<uuid8>').

    Returns:
        {
            "run_id": str,
            "scanned": int,
            "proposed": int,
            "skipped_existing": int,
            "proposals": [{"task_id", "state", "age_days", "threshold_days", "is_stale"}],
        }
    """
    # [8/9 P1-yanru] stale_days_threshold fallback — None 时从 config 读 (必须在 M30 校验前)
    if stale_days_threshold is None:
        from config import config as _cfg

        stale_days_threshold = _cfg.task_stale_days_threshold

    if not isinstance(stale_days_threshold, int) or stale_days_threshold < 1:
        raise TaskLoopError(
            f"stale_days_threshold 必须正整数, got {stale_days_threshold!r}",
            field="stale_days_threshold",
            code="InvalidThreshold",
        )

    import uuid as _uuid
    from datetime import datetime as _dt

    now_ts = now or _default_now()
    try:
        ref_now = _dt.fromisoformat(now_ts)
    except (ValueError, TypeError):
        ref_now = _dt.fromisoformat(_default_now())

    # 阈值桶 (DESIGN TASK_LOOP 4.4 D7)
    thresholds = {
        "open": 7,
        "waiting": 14,
        "blocked": 3,
        "in_progress": stale_days_threshold,
    }

    # 1. 扫 active task
    rows = conn.execute(
        """SELECT t.task_id, t.state, t.valid_from, t.reason, t.evidence_chunk_id
           FROM task_states t
           JOIN entities e ON e.id = t.task_id
           WHERE t.valid_until IS NULL
             AND t.state NOT IN ('done', 'cancelled')
             AND e.kind = 'task'"""
    ).fetchall()

    proposals = []
    skipped = 0
    for r in rows:
        state = r[1]
        threshold = thresholds.get(state, stale_days_threshold)
        try:
            vf = _dt.fromisoformat(r[2])
            age_days = (ref_now - vf).total_seconds() / 86400.0
        except (ValueError, TypeError):
            age_days = 0.0
        if age_days < threshold:
            continue
        # [8/6 M28 fix] 去重 - 排除已有未 resolved 的 Proposal.
        existing = conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task'
                 AND ref_type='task' AND ref_id=?
                 AND NOT EXISTS (
                     SELECT 1 FROM audit_log al2
                     WHERE al2.pass_name='stuck_task'
                       AND al2.action_type='stale_resolved'
                       AND al2.status='applied'
                       AND al2.ref_id=?
                 )""",
            (r[0], r[0]),
        ).fetchone()
        if existing:
            skipped += 1
            continue
        proposals.append(
            {
                "task_id": r[0],
                "state": state,
                "age_days": round(age_days, 2),
                "threshold_days": threshold,
                "is_stale": True,
                "last_reason": r[3],
            }
        )

    # 2. 写 audit_log Proposal
    rid = run_id or f"stuck_task-{now_ts}-{_uuid.uuid4().hex[:8]}"
    for p in proposals:
        conn.execute(
            """INSERT INTO audit_log (
                run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rid,
                "stuck_task",
                "stale_review",
                "task",
                p["task_id"],
                None,
                json.dumps(
                    {
                        "state": p["state"],
                        "age_days": p["age_days"],
                        "threshold_days": p["threshold_days"],
                        "proposed_at": now_ts,
                        "prompt": (
                            f"task {p['task_id']} 处于 {p['state']} 状态 {p['age_days']:.1f} 天, 超过阈值 {p['threshold_days']} 天. 建议: transition 到 done / cancelled / 下一步, 或更新 reason."
                        ),
                    },
                    ensure_ascii=False,
                ),
                0.7,
                "proposed",
                now_ts,
            ),
        )
    conn.commit()
    return {
        "run_id": rid,
        "scanned": len(rows),
        "proposed": len(proposals),
        "skipped_existing": skipped,
        "proposals": proposals,
    }


def apply_stale_proposal(
    conn: Any,
    proposal_id: int,
    *,
    applied_action: str,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    # [M5.2 fix] UNIQUE(run_id, pass_name, action_type, ref_id, status) 约束
    # proposal_id 全局唯一, 自带 part of run_id 防并发 apply 碰撞.
    """[8/6 M5.2 + DESIGN §4.4] 标记 stuck_task Proposal 为 applied (用户/agent 评估后).

    走 audit_log 现有 status 字段 ('proposed' → 'applied'). 不自主转移任务,
    只记录人类决策. 真正的 transition 已经由 agent 经 memory_task_transition
    显式发起 (跟 status 解耦).

    Args:
        conn: open sqlite3.Connection.
        proposal_id: audit_log row id (propose_stale_tasks 写入行).
        applied_action: 文字描述用户做了啥 (e.g. 'transitioned to done',
                        'ignored', 'will_revisit_at_2026-08-10').
        now: 时间参考 (默认 = 当前).

    Returns:
        {"proposal_id": int, "status": "applied", "ref_id": str, "applied_action": str}
    """
    now_ts = now or _default_now()
    # [M30 fix] race condition — 旧实现 check + insert 分两步, 两 agent 并发
    # apply 同 proposal_id 都过 check + 双写 audit_log (UNIQUE run_id 不同不冲突).
    # 修: SQLite WAL + 显式 BEGIN IMMEDIATE 拿写锁, 整个 check+insert 原子化.
    # 应用层幂等性: 仍保留 stale_resolved/applied 行检测 (重复 apply 显式报错).
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, pass_name, status, ref_id FROM audit_log WHERE id=?",
            (proposal_id,),
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"proposal_id={proposal_id} 不存在",
                field="proposal_id",
                code="ProposalNotFound",
            )
        if row[1] != "stuck_task":
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"proposal_id={proposal_id} 不是 stuck_task Proposal (pass_name={row[1]})",
                field="proposal_id",
                code="ProposalMismatch",
            )
        # [M5.2 fix] 校验是否已 applied — 同 ref_id 已有 stale_resolved 行
        resolved = conn.execute(
            """SELECT id FROM audit_log
               WHERE pass_name='stuck_task' AND action_type='stale_resolved'
                 AND ref_id=? AND status='applied'""",
            (row[3],),
        ).fetchone()
        if resolved:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"ref_id={row[3]} 已被 applied (resolved_id={resolved[0]}), 不能重复",
                field="proposal_id",
                code="ProposalAlreadyResolved",
            )
        # [M5.2 fix] UNIQUE(run_id, pass_name, action_type, ref_id, status) 约束
        # run_id 跟原 Proposal 不同 (含 proposal_id 区分), status='applied' 跟原
        # 'proposed' 自然错开. 整个 check+insert 在 BEGIN IMMEDIATE 事务里, 并发
        # apply 会被 SQLite 序列化, 第二个拿到锁时 resolved check 命中失败抛错.
        conn.execute(
            """INSERT INTO audit_log (
                run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"resolved-{now_ts}-{proposal_id}",
                "stuck_task",
                "stale_resolved",
                "task",
                row[3],
                json.dumps({"original_proposal_id": proposal_id, "status": "proposed"}, ensure_ascii=False),
                json.dumps({"applied_action": applied_action, "resolved_at": now_ts}, ensure_ascii=False),
                1.0,
                "applied",
                now_ts,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        # 兜底: 任何异常回滚 (BEGIN IMMEDIATE 没 COMMIT 必须 ROLLBACK, 否则 SQLite 锁死)
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {
        "proposal_id": proposal_id,
        "status": "applied",
        "ref_id": row[3],
        "applied_action": applied_action,
    }


def list_stale_proposals(
    conn: Any,
    *,
    status: str = "proposed",
    limit: int = 50,
) -> Dict[str, Any]:
    """[8/6 M5.2 + 8/6 fix + M34 fix] 列 stuck_task Proposal (DESIGN §4.4 上浮).

    [8/6 fix] status='proposed' 过滤排除已 resolved 的 Proposal (audit_log 中同
    ref_id 有 stale_resolved 行) — 让 propose/apply 闭环后 list_stale_proposals
    不再返回本 Proposal.

    [M34 fix] status 白名单 - 拒绝 invalid 状态. 旧实现任意字符串都过 else 分
    支当 SQL 参数, 静默返 'unfiltered' 行迷惑 caller. 显式抛错.

    Args:
        conn: open sqlite3.Connection.
        status: 'proposed' / 'applied' / 'reverted' / 'all'.
        limit: 上限.

    Returns:
        {"proposals": [{"id", "run_id", "ref_id", "action_type",
                        "after_json", "status", "created_at"}], "count": int}

    Raises:
        TaskLoopError: status 不在白名单.
    """
    if status not in ("proposed", "applied", "reverted", "all"):
        raise TaskLoopError(
            f"status {status!r} 不在白名单 (proposed/applied/reverted/all)",
            field="status",
            code="InvalidStatusError",
        )
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise TaskLoopError(
            f"limit 必须 1-1000 整数, got {limit!r}",
            field="limit",
            code="InvalidLimitError",
        )
    if status == "proposed":
        # 排除已 resolved (有 stale_resolved 行的 ref_id)
        rows = conn.execute(
            """SELECT id, run_id, action_type, ref_id, after_json, status, created_at
               FROM audit_log
               WHERE pass_name='stuck_task' AND action_type='stale_review'
                 AND status='proposed'
                 AND ref_id NOT IN (
                     SELECT ref_id FROM audit_log
                     WHERE pass_name='stuck_task' AND action_type='stale_resolved'
                       AND status='applied'
                 )
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    elif status == "all":
        # [M34 fix] 'all' 白名单 - 审计追溯需要看所有阶段 (无 not-resolved 过滤)
        rows = conn.execute(
            """SELECT id, run_id, action_type, ref_id, after_json, status, created_at
               FROM audit_log
               WHERE pass_name='stuck_task'
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, run_id, action_type, ref_id, after_json, status, created_at
               FROM audit_log
               WHERE pass_name='stuck_task' AND status=?
               ORDER BY id DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    proposals = []
    for r in rows:
        try:
            after = json.loads(r[4]) if r[4] else None
        except (ValueError, TypeError):
            after = None
        proposals.append(
            {
                "id": r[0],
                "run_id": r[1],
                "action_type": r[2],
                "ref_id": r[3],
                "after_json": after,
                "status": r[5],
                "created_at": r[6],
            }
        )
    return {"proposals": proposals, "count": len(proposals)}


# [M30.3 fix] digest block 4 输出上限 — DESIGN §4.4 + README 契约: digest
# 500-2000 字符. block 4 是 digest 一部分, 单独应 ≤2000 chars. 大量 stale
# task 会让 block 4 撑爆 digest 注入 agent 上下文, 必须截断.
DIGEST_BLOCK4_MAX_CHARS = 2000


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


def forget_task(
    conn: Any,
    task_id: str,
    *,
    reason: str,
    now: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """[8/6 M5.3 + DESIGN §10.2 D11] 显式软删 task (D11 TTL 豁免路径).

    不走 L2 decay — 必须 agent/用户显式调用, 写 audit_log (pass_name='forced_forget')
    留审计痕迹. 软删 entity + 关闭 task_states 当前行 + cascade.

    Args:
        conn: open sqlite3.Connection.
        task_id: 要删的 task id.
        reason: 必填 (D8 显式纠正门), 'transition_to_cancelled' / 'duplicate' / etc.
        now: 时间参考 (默认 = 当前).
        run_id: 自定义 run_id.

    Returns:
        {"task_id", "forgotten_at", "run_id", "rows_invalidated"}

    Raises:
        TaskLoopError: task_id 不存在 / 已经是 valid_until IS NOT NULL / reason 缺失.
    """
    import uuid as _uuid

    # [M35.1 fix] reason isinstance(str) 守卫 - 非 str 类型 (e.g. int 12345)
    # 旧实现 .strip() 抛 AttributeError, 未收敛到 TaskLoopError. M34 给 limit
    # 加了 isinstance 守卫, M33 reason 校验未加, 两批「API 边界」风格不一.
    if not isinstance(reason, str):
        raise TaskLoopError(
            f"reason 必须 str 类型, got {type(reason).__name__}",
            field="reason",
            code="InvalidReasonTypeError",
        )
    # [M33.2 fix] 强校验 reason: 必填 + 去前后空白 + min length 5 (审计可读).
    # 旧 `if not reason` 接受 ' ' (空格) 当真, audit trace 后人看不懂.
    reason_clean = reason.strip() if reason else ""
    if not reason_clean:
        raise TaskLoopError(
            "forget_task 必须提供非空 reason (D8 显式纠正门)",
            field="reason",
            code="ReasonRequiredError",
        )
    if len(reason_clean) < 5:
        raise TaskLoopError(
            f"forget_task reason 长度需 >=5 字符 (审计可读), got {len(reason_clean)}",
            field="reason",
            code="ReasonTooShortError",
        )
    now_ts = now or _default_now()

    # [M33.1 fix] BEGIN IMMEDIATE 事务 - check + UPDATE 原子化, 并发 forget 同
    # task_id 第二个拿锁时 row[0] IS NOT NULL, 抛 TaskAlreadyForgotten.
    # 旧实现 check + UPDATE 跨语句, 两 agent 都过 check, 双 UPDATE 同 task_id
    # 双 audit_log 行 (同 reason), audit trace 重复污染.
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1. 校验 task 存在 + 当前未软删 (valid_until IS NULL)
        row = conn.execute(
            "SELECT valid_until FROM entities WHERE id=? AND kind='task'",
            (task_id,),
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"task_id={task_id} 不存在或已软删",
                field="task_id",
                code="TaskNotFoundError",
            )
        # [8/6 M28 fix] row[0] 是 valid_until 列; 若已非 NULL, 重复 forget 应抛错.
        if row[0] is not None:
            conn.execute("ROLLBACK")
            raise TaskLoopError(
                f"task_id={task_id} 已于 {row[0]} 软删, 重复 forget 拒绝",
                field="task_id",
                code="TaskAlreadyForgotten",
            )

        # 2. 关闭 task_states 当前行
        invalidated = conn.execute(
            """UPDATE task_states SET valid_until = ?
               WHERE task_id=? AND valid_until IS NULL""",
            (now_ts, task_id),
        ).rowcount

        # 3. 软删 entity
        conn.execute(
            """UPDATE entities SET valid_until = ?
               WHERE id=? AND valid_until IS NULL""",
            (now_ts, task_id),
        )

        # 4. cascade relations
        rels = conn.execute(
            """UPDATE relations SET valid_until = ?
               WHERE (source_id=? OR target_id=?) AND valid_until IS NULL""",
            (now_ts, task_id, task_id),
        ).rowcount

        # 5. audit_log (M5.3 D11 留痕)
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
                "task",
                task_id,
                json.dumps({"status_before": "active", "rows_invalidated": invalidated}, ensure_ascii=False),
                json.dumps({"reason": reason_clean, "forgotten_at": now_ts, "relations_cascade": rels}, ensure_ascii=False),
                1.0,
                "applied",  # 立即 applied — 这不是 Proposal, 是显式 delete
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
        "task_id": task_id,
        "forgotten_at": now_ts,
        "run_id": rid,
        "rows_invalidated": invalidated,
        "relations_cascade": rels,
    }


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
