# === task_states_core.py — exceptions + helpers + Task CRUD + transition() ===
# [refactor 2026-08-12] 拆分自原 task_states.py (1830 行 → 5 模块分工):
#   task_states_core.py:    异常族 + _default_now + _slugify + transition + Task CRUD (~700 行)
#   task_states_loop.py:    Loop CRUD + tick (~620 行)
#   task_states_stale.py:  Stale proposal pipeline (~360 行)
#   task_states_digest.py: digest 渲染 (list_active_tasks_and_loops + render_digest_block4) (~140 行)
#   task_states.py:        facade — re-export 全部向后兼容
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mnelo.task_states")


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
    # [bug fix D1 2026-08-16] Use memory.now() (second precision, T-sep) for
    # consistency with the rest of the codebase. Pre-fix: this returned
    # millisecond precision ('2026-08-16T10:30:00.123') which broke lex
    # comparisons with second-precision timestamps from memory.now() —
    # SQLite treats '2026-08-16T10:30:00' < '2026-08-16T10:30:00.500'
    # (shorter string sorts first) silently corrupting asof / valid_until
    # / supersede cascade queries.
    #
    # The original "milliseconds to avoid zero-length windows" rationale
    # is preserved: callers that need sub-second precision can pass
    # explicit `now=...` parameter (e.g. line 278: +1ms offset for
    # back-to-back transitions).
    from memory import now as _memory_now  # lazy import — avoid circular

    return _memory_now()


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
    return hashlib.md5(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]


# === Shared constants (refactor 2026-08-12) ===
DIGEST_BLOCK4_MAX_CHARS = 2000  # digest 渲染输出上限


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

    # [bug fix D5 2026-08-16] current_state must respect asof, not always real-time.
    # Pre-fix: queried WHERE valid_until IS NULL (the current active window right
    # now) regardless of asof — so asof='2020-01-01' could return current_state='open'
    # (from 2026 re-open) while windows=[] (no 2020 state) — incoherent time-travel.
    # Fix: derive current_state from the last window in `rows` (the one whose
    # valid_until IS NULL among the asof-filtered set, or the most recent one
    # if none are currently open).
    current_state = None
    if rows:
        # Prefer the open window (valid_until IS NULL) if present in filtered set
        for r in rows:
            if r[2] is None:  # valid_until is None
                current_state = r[0]
                break
        # Fallback: last window in chronological order
        if current_state is None:
            current_state = rows[-1][0]

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
