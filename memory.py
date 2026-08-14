#!/usr/bin/env python3
"""
memory.py — mnelo 核心 CRUD API (facade re-export)

[refactor 2026-08-12] 从 3853 行 monolithic 拆为 4 mixin 文件 (PR #11 benchmarks
子包拆分同样先例):
  - memory_core.py     MemoryCore         (init + CRUD + 4-way recall + graph + entities)
  - digest_mixin.py    DigestMixin        (4 digest methods)
  - audit_mixin.py     AuditMixin         (4 audit methods + GC constants)
  - l2_maintenance.py  L2MaintenanceMixin (L2 hygiene + promote + stats v2)

本文件 = module-level helpers + facade re-export. 公共 API 不变:
  from memory import Memory, DB_PATH, now, detect_query_intent, ValidationError

6 个核心接口: remember / recall / relate / forget / update / graph_query
4 路召回 (向量 + 图 + 元数据 + 实体) + RRF 融合
4D 时间维度 (valid_from / valid_until / soft delete + 自动级联)
单一 writer (单进程) + WAL + busy_timeout=30s 防 lock
"""

import contextlib
import logging
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

import sqlite_vec

# [P0 2026-08-11] scoping IDs — sentinel 用于 'agent_id filter 未传' 状态.
# 区别于 None: None = 调用方显式传 None (= 过滤 agent_id=None 的 chunk,
# 即召回无 agent_id 的旧数据). sentinel = filters 完全没 agent_id key
# (= 不应用 agent_id filter, backward compat).
_MISSING = object()

logger = logging.getLogger("mnelo")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

import config as _config_module  # [7/21 fix] DB 路径解析 (config.resolve_db_path)

# validation 模块从 conftest/repo 加载 (live == repo via hook sync).
# 注意: memory.py 不再硬编码 /Users/apple/.hermes/memory path — repo 自身是 single source of truth.
from validation import ValidationError  # re-exported in __all__

# [7/21 fix] DB 路径不再硬编码 — 从 config 解析 (env > config.toml > ~/.hermes/memory/memory.db)。
# 注: embedding 模型 + dim 不再在此处硬编码 — 见 embedder.py 从 config 读 (config.toml [embedder])
DB_PATH = _config_module.resolve_db_path()


# [P0 §3.0] 记忆类型谱系 — 提取/矛盾/卫生/召回按类型区分。
# 常量单一事实源在 validation.MEMORY_TYPES, 这里复用避免两份定义漂移。
from validation import MEMORY_TYPES as _MEMORY_TYPES

# [P2 2026-08-11] Temporal reasoning — 借鉴 mem0 4-intent read-time classification.
# Reuse classify._T2S / _normalize for 繁→简归一化, 不重复造轮子 (跟 P0 不引入新依赖一致).
try:
    from classify import _T2S as _T2S_TEMPORAL
    from classify import _normalize as _normalize_text
except ImportError:  # pragma: no cover — 防御性 fallback
    _T2S_TEMPORAL = {}

    def _normalize_text(x: str) -> str:  # type: ignore[no-redef]
        return x


def norm_memory_type(t) -> str:
    """校验 + 归一化 memory_type, 非法值抛 ValidationError."""
    if t is None:
        return "fact"
    t = str(t).strip().lower()
    if t not in _MEMORY_TYPES:
        raise ValidationError("memory_type", f"unknown memory_type {t!r} (allowed: {sorted(_MEMORY_TYPES)})")
    return t


def _kind_from_source(source: str) -> Optional[str]:
    """[8/5 普适化] 从 source 前缀提取 entity kind: 'entity:stock' → 'stock'.

    泛化自原硬编码的 `source.startswith('entity:stock')` — 现在任何
    `entity:<kind>` 前缀都能推导出 kind, 供 RRF boost 按可配置清单匹配。
    """
    if not source or not source.startswith("entity:"):
        return None
    kind = source[len("entity:") :].strip()
    return kind or None


def now(tz: str = None) -> str:
    """Return current time as ISO 8601 string with seconds precision (e.g. '2026-07-18T15:48:00').

    Args:
        tz: Timezone setting.
            - None (default) → use config.timezone ('local' by default)
            - 'local' → datetime.now() (system local time)
            - 'utc' → datetime.utcnow()
            - 'Asia/Shanghai' (IANA name) → use that timezone

    Reads default from config.timezone unless overridden.

    Used as default for valid_from / valid_until / timestamp fields.
    """
    from config import config as _cfg

    if tz is None:
        tz = _cfg.timezone

    if tz == "local":
        return datetime.now().isoformat(timespec="seconds")
    elif tz == "utc":
        return datetime.utcnow().isoformat(timespec="seconds")
    else:
        # IANA tz (e.g. 'Asia/Shanghai'). Try zoneinfo (3.9+), fallback to manual offset
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
        except ImportError:
            # Python 3.8 fallback: manual offset
            from datetime import timedelta, timezone

            # Best-effort: use UTC and tell user to upgrade
            return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


# [P2 2026-08-11] Temporal reasoning — query intent classification.
# 借鉴 mem0 7-mode (我们对齐 4 core: current_state / historical / upcoming / soft_recency).
# 0-LLM 正则, ~0ms 开销 (跟 classify.py 同源设计).
# 优先级: upcoming > historical > current_state > soft_recency (默认).
_TEMPORAL_INTENT_MARKERS: Dict[str, Dict[str, List[str]]] = {
    "upcoming": {
        "cn": [
            "下个月",
            "下星期",
            "下周",
            "下年",
            "将要",
            "即将",
            "马上要",
            "准备去",
            "打算去",
            "将要",
            "将来",
            "未来",
            "之后要去",
        ],
        "en": [
            "next month",
            "next week",
            "next year",
            "going to",
            "will be",
            "will ",
            "plan to move",
            "planning to",
        ],
    },
    "historical": {
        "cn": [
            "以前",
            "曾经",
            "过去",
            "当时",
            "去年",
            "前年",
            "上个月",
            "上星期",
            "上周",
            "已经搬",
            "曾经住",
        ],
        "en": [
            "last year",
            "last month",
            "last week",
            "previously",
            "formerly",
            "used to",
            "in the past",
            "ago",
            "before",
        ],
    },
    "current_state": {
        "cn": [
            "现在",
            "目前",
            "当前",
            "如今",
            "此刻",
            "住在哪里",
            "住哪",
            "在哪",
        ],
        "en": [
            "now",
            "currently",
            "current",
            "at present",
            "right now",
            "where do i live",
            "where am i",
        ],
    },
    "soft_recency": {
        "cn": ["最近", "最新", "近期", "新近", "刚刚"],
        "en": ["recent", "recently", "latest", "newest", "lately"],
    },
}


def detect_query_intent(query: str) -> str:
    """[P2 2026-08-11] 借鉴 mem0 4-intent — classify query 时间意图 (0-LLM 正则).

    4 类 (对齐 mem0 简化版, 任务卡指定):
      - current_state: 默认当前态查询 ("现在住哪" / "where do i live now")
      - historical:    历史窗口查询 ("去年住哪" / "last year")
      - upcoming:      未来有效查询 ("下个月要去" / "going to move")
      - soft_recency:  软时效排序 ("最近" / "recent") — 也是无 marker 的默认

    优先级: upcoming > historical > current_state > soft_recency.
    多 marker 冲突时高优先级赢 (e.g. "去年计划下个月搬家" → upcoming).

    Args:
        query: 用户原始 query (可以是 str 或 None).

    Returns:
        intent 字符串 (4 个值之一). 空串/None 返 soft_recency (默认行为).
    """
    if not query or not isinstance(query, str):
        return "soft_recency"
    # 繁→简归一 (跟 classify._normalize 同源)
    norm = _normalize_text(query).lower()
    # 优先级顺序扫描
    for intent in ("upcoming", "historical", "current_state", "soft_recency"):
        markers = _TEMPORAL_INTENT_MARKERS[intent]
        for lang_markers in (markers["cn"], markers["en"]):
            for marker in lang_markers:
                if marker in norm:
                    return intent
    # 无 marker 命中 → 默认 soft_recency (跟默认 timestamp DESC 行为对齐)
    return "soft_recency"


def _temporal_class_for_validity(valid_from: Optional[str], valid_until: Optional[str], now_ts: str) -> Optional[str]:
    """[P2 2026-08-11] write-time temporal signature — 根据时间窗自动归类.

    返回 metadata_json.temporal_class 字段值; None = 不写字段 (避免 metadata 膨胀).
    规则:
      - valid_until 已设 且 < now_ts → 'historical' (已失效, 但历史可查)
      - valid_from 已设 且 > now_ts → 'upcoming' (未来生效)
      - 其他 (current_state) → None (不写, 默认行为)
    """
    if valid_until and valid_until < now_ts:
        return "historical"
    if valid_from and valid_from > now_ts:
        return "upcoming"
    return None


def generate_id(prefix: str = "chunk") -> str:
    """Generate a unique chunk/entity/relation id with prefix + timestamp (microsecond precision).

    Format: '{prefix}_YYYYMMDD_HHMMSS_microseconds'
    Example: 'chunk_20260718_103045_123456'

    Collision risk: microsecond precision is enough for single-process; for multi-writer
    scenarios consider adding a random suffix.
    """
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def clamp01(value: float, name: str = "value") -> float:
    """Clamp importance/weight to [0.0, 1.0] with type and NaN validation.

    [P0 审计] : remember(importance) / relate(weight) / update(new_importance)
    / _upsert_entity(importance) 之前没 bounds check, 接受任意浮点 (5.0 / -0.3 / NaN).
    加 clamp + 类型校验保证 DB 写入合法.

    Args:
        value: 输入值 (int/float), 会被转 float
        name: 字段名, 用于错误信息 (e.g. 'importance', 'weight', 'new_importance')

    Returns:
        float ∈ [0.0, 1.0]

    Raises:
        TypeError: 非数值类型 (e.g. str, None, list)
        ValueError: NaN

    Examples:
        >>> clamp01(5.0)
        1.0
        >>> clamp01(-0.3)
        0.0
        >>> clamp01(0.7)
        0.7
        >>> clamp01('high')
        TypeError: importance must be numeric, got str
        >>> clamp01(float('nan'))
        ValueError: importance must not be NaN
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be numeric, got {type(value).__name__}")
    if value != value:  # NaN check (NaN != NaN)
        raise ValueError(f"{name} must not be NaN")
    return max(0.0, min(1.0, float(value)))


@contextlib.contextmanager
def _with_row_factory(conn, factory):
    """Temporarily swap conn.row_factory inside a context, restore on exit.

    [P0 审计] : sqlite-vec 0.1.x vec0 query 返回 plain tuple, 不受
    connection.row_factory = sqlite3.Row 控制. 之前在 memory.py / mcp_server.py
    / entity_resolve.py 重复 8 处 (5 + 1 + 2). 现在统一 helper.

    Args:
        conn: sqlite3.Connection
        factory: sqlite3.Row / None / 自定义 callable

    Yields:
        the same conn (以便 with 块内直接使用)

    Examples:
        >>> with _with_row_factory(conn, sqlite3.Row):
        ...     rows = conn.execute("SELECT v.rowid AS v_rowid FROM vectors v ...").fetchall()
        ...     # rows 是 sqlite3.Row 实例, 可用 r['v_rowid']
        # 退出 with 后 conn.row_factory 恢复原值
    """
    old = conn.row_factory
    conn.row_factory = factory
    try:
        yield conn
    finally:
        conn.row_factory = old


@contextlib.contextmanager
def _txn(conn):
    """[8/15 E-1] 显式事务包裹 helper. 用于 remember/update 写路径.

    主人 DESIGN §1.2 #7 短板修复: 之前 chunk+entities+relations+vector
    依赖 sqlite3 隐式事务, 中途异常 → 单例 conn 复用下次 commit 可能连同
    提交, 留下 vec0 rowid 漂移的孤儿 chunk.

    行为契约:
      - 进入: 显式 BEGIN
      - 正常退出: COMMIT
      - 异常: ROLLBACK + 重新 raise (不吞)

    注意事项 (usearch/zvec 索引独立于 SQLite 事务):
      - index.add() 失败 → SQLite ROLLBACK → chunk 不入库, index 也未污染 ✓
      - 顺序: SQLite 写完 → index.add → COMMIT. 如果 index.add 成功
        但 COMMIT 失败 (极低概率) → index 写了但 SQLite 没存, 需
        reverse index.remove. 这条留给 v0.16+ 处理 (主人确认当前
        threshold 接受).
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception as rb_err:  # noqa: BLE001
            logging.getLogger("mnelo").warning(f"[txn] ROLLBACK failed: {rb_err}")
        raise
    else:
        conn.execute("COMMIT")


# [8/8 P1] Entity namespace guard.
# 防御历史 importer 残留 (HonchoImporter anno:* NER mentions) + 随机 token
# (TOKEN_C_*) + 整句当 entity name. 只允许显式 namespace 前缀 + 无冒号的
# person / provider / event / task 类短 name. master_* prefix 是 SOUL §mnelo
# ops #4 拍板使用的主语前缀.
_ALLOWED_ENTITY_NAMESPACES = frozenset(
    {
        "identity:",  # identity_fact (主人身份, immutable)
        "stock:",  # A 股 / 美股代码
        "holding:",  # position_snapshot (持仓快照)
        "loop:",  # cron loop entity (DESIGN §5.6)
        "task:",  # task entity (DESIGN §5.5)
    }
)
_ALLOWED_ENTITY_PREFIXES = ("master_",)  # SOUL §mnelo ops #4: master_<subject>
# [8/8 P1] concept kind 允许的 name 长度上限 — 防"imported sleep runs at..." 这种
# 整句灌进 entity. 其他 kind (stock/identity_fact 等) 已有结构化字段, 不限.
_MAX_CONCEPT_NAME_LEN = 50


def _enforce_entity_namespace_guard(ent: Dict) -> None:
    """[8/8 P1] _upsert_entity 入口处拒绝历史 importer 残留命名空间.

    Raises ValidationError when entity id / name 命中以下任一:
      - id namespace 在黑名单 (anno:, TOKEN_C_, ...)
      - id 不在白名单 namespace 前缀 (`identity:`, `stock:`, `holding:`,
        `loop:`, `task:`) 也不在 `master_*` 前缀, 且不含 `:` (无 namespace)
      - concept kind + name > 50 chars (整句当 name)

    [A1 2026-08-10] Kind 词汇表本身**不**受限 (DESIGN §3.0.3 双谱系正交:
    kind × memory_type; AGENTS.md "open taxonomy — no registration needed").
    本 guard 只拒历史 importer 残留 (anno:* / TOKEN_*) + 整句当 name 的
    低质量 entity. 用户可任意引入新 kind (e.g. `product`, `lesson`,
    `recipe`), 不需要先注册白名单.
    """
    from validation import ValidationError  # 局部 import 避免循环

    eid = ent["id"]
    kind = ent["kind"]
    name = ent.get("name") or ""

    # 1) 黑名单: anno:* 是 HonchoImporter NER 历史残留, 直接拒
    if eid.startswith("anno:"):
        raise ValidationError(
            "entity.id",
            "namespace 'anno:*' is reserved for legacy HonchoImporter imports; use chunk metadata (properties_json.annotation_kind) instead",
        )
    # 2) 黑名单: 随机 token id (TOKEN_C_*, TOKEN_*, ...)
    if eid.startswith("TOKEN_"):
        raise ValidationError(
            "entity.id",
            "namespace 'TOKEN_*' (random session tokens) is not a valid entity id; use a stable, human-readable id",
        )

    # 3) 白名单: 显式 namespace 前缀 (identity:/stock:/holding:/loop:/task:
    # 或 master_*) — 通过即代表 id 已被命名空间隔离, 任何 kind 都接受.
    has_allowed_ns = any(eid.startswith(ns) for ns in _ALLOWED_ENTITY_NAMESPACES) or eid.startswith(_ALLOWED_ENTITY_PREFIXES)
    if has_allowed_ns:
        return  # 显式 namespace 必走结构化 id, 跳过下面无 namespace 的检查

    # 4) 无 namespace id (不含 `:` 也没 master_ 前缀): 仅检查 name, 不查 kind.
    #    [A1 2026-08-10] 旧版本用 _NAMELESS_KINDS 白名单强制要求 kind ∈
    #    {person, provider, event, task, setup, system, host,
    #    position_snapshot, concept, canonical_fact}. 这跟 DESIGN §3.0.3
    #    "kind 词汇表开放" 设计冲突, 文档也没说 (AGENTS.md 写
    #    "open taxonomy — no registration needed"). 移除 kind 限制后,
    #    用户可给任何无 namespace id 配任意 kind (e.g. master_<subject>
    #    不算, 那走 prefix 白名单; 这里指纯短 id 如 `sonnet` 配 `lesson`).

    # 5) concept kind 禁长句子 name (整句当 entity 的防御 — 任何 kind 都该限,
    #    但 DESIGN §3.0.3 说 concept 是 "收纳概念" 角色, 长名最常见, 故只查它).
    if kind == "concept" and len(name) > _MAX_CONCEPT_NAME_LEN:
        raise ValidationError(
            "entity.name",
            f"concept entity name must be <= {_MAX_CONCEPT_NAME_LEN} chars; got {len(name)} chars. Use chunk content for sentences, entity.name for short labels only.",
        )


def _load_vec0_module(conn: sqlite3.Connection, context: str = "init") -> None:
    """[8/10 refactor] 在任意 sqlite3.Connection 上加载 sqlite-vec vec0 扩展.

    init 阶段和 recall 阶段并发 conn 都走这个 helper. 三层 fallback:
      1) conn.enable_load_extension(True) + sqlite_vec.load(conn) — 本地 venv Python 默认路径.
      2) ctypes 加载 vec0.{dylib,so,dll} 调 sqlite3_vec_init + sqlite3_auto_extension —
         CI hostedtoolcache macOS arm64 sandbox (enable_load_extension 被 strip).
      3) 都不行则 warn 跳过 — vector 走 usearch 后端 (search_index.py).

    Args:
        conn: 目标 SQLite 连接 (init 阶段是 self._conn; recall 阶段是 4 个并发 worker conn).
        context: 调用上下文 ("init" / "recall-worker"), 仅用于日志.
    """
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return
    except AttributeError:
        # CI / restricted Python: enable_load_extension 被 strip. 走 ctypes fallback:
        # 1) ctypes 加载 vec0.{so,dylib,dll}, 拿到 sqlite3_vec_init entry.
        # 2) 调 vec0 的 init 函数直接对当前 conn 注入, 不依赖 auto_extension.
        pass

    # --- ctypes fallback ---
    import ctypes as _ct
    import platform as _platform

    _pkg_dir = os.path.dirname(sqlite_vec.__file__)
    if _platform.system() == "Darwin":
        _lib_name = "vec0.dylib"
    elif _platform.system() == "Windows":
        _lib_name = "vec0.dll"
    else:
        _lib_name = "vec0.so"
    _lib_path = os.path.join(_pkg_dir, _lib_name)
    _vec = _ct.CDLL(_lib_path)
    # sqlite3_vec_init 签名: int sqlite3_vec_init(sqlite3*, char**, const sqlite3_api_routines*)
    _init_fn = _vec.sqlite3_vec_init
    _init_fn.restype = _ct.c_int
    _init_fn.argtypes = [
        _ct.c_void_p,  # sqlite3*
        _ct.POINTER(_ct.c_char_p),  # char** errmsg
        _ct.c_void_p,  # const sqlite3_api_routines*
    ]
    # 拿 conn 的底层 sqlite3* handle (Py sqlite3 暴露 via _db_handle / _Connection__db_handle).
    # 找不到时直接 pass (后续 conn 走 auto_extension 即可).
    try:
        _db_handle = getattr(conn, "_db_handle", None)
        if _db_handle is None:
            _db_handle = getattr(conn, "_Connection__db_handle", None)
        if _db_handle is not None:
            _err = _ct.c_char_p()
            _rc = _init_fn(_db_handle(), _ct.byref(_err), None)
            if _rc != 0:
                raise RuntimeError(f"vec0 init failed rc={_rc}: {_err.value}")
        # 顺手注册 auto-extension 让未来 conn 自动 init (init 阶段一次性即可).
        if context == "init":
            _libsqlite3 = _ct.CDLL("sqlite3.dll" if _platform.system() == "Windows" else ("libsqlite3.dylib" if _platform.system() == "Darwin" else "libsqlite3.so.0"))
            _libsqlite3.sqlite3_auto_extension.argtypes = [_ct.c_void_p]
            _libsqlite3.sqlite3_auto_extension(_init_fn)
    except (OSError, AttributeError) as _e:
        # 极受限 Python: 找不到 libsqlite3, 或 _db_handle 私有属性已变.
        # 跳过 vec0 — 用 usearch index 做向量搜索 (8/5 已走这条路).
        logger.warning(f"[8/10] sqlite-vec auto-ext 不可用 ({context}, {type(_e).__name__}: {_e}); vector 走 usearch (search_index.py)")


# ============================================================
# [refactor 2026-08-12] Memory class = mixin composition (MRO 顺序见各 mixin 文件)
# ============================================================
from audit_mixin import AuditMixin  # noqa: E402
from digest_mixin import DigestMixin  # noqa: E402
from l2_maintenance import L2MaintenanceMixin  # noqa: E402
from memory_core import MemoryCore  # noqa: E402


class Memory(MemoryCore, DigestMixin, AuditMixin, L2MaintenanceMixin):
    """[refactor 2026-08-12] Mixin composition. 公共 API 100% 不变.

    MRO: Memory -> MemoryCore -> DigestMixin -> AuditMixin -> L2MaintenanceMixin -> object
    跨 mixin 依赖 (self.xxx) 运行时通过 MRO 解析 — 见各 mixin 文件 docstring.

    公共方法: remember / recall / relate / forget / update / graph_query
              run_maintenance / list_audit / audit_undo / get_digest / stats
    """

    pass  # 全部行为从 mixin 组合而来


# ============================================================
# __all__ — public API surface (向后兼容)
# ============================================================
__all__ = [
    "Memory",
    "DB_PATH",
    "now",
    "detect_query_intent",
    "norm_memory_type",
    "generate_id",
    "clamp01",
    "_normalize_text",
    "_temporal_class_for_validity",
    "ValidationError",
]


# === 自测 ===
# [refactor 2026-08-12] 保留原 memory.py 末尾的 demo block — test_main_blocks_coverage.py
# 依赖 `python memory.py` 能跑. 8 个 demo step: remember/relate/recall/graph_query/stats/
# update/forget/recall_after_updates.
if __name__ == "__main__":
    import time

    from memory import Memory as _Memory_for_demo

    with _Memory_for_demo() as m:
        # Use unique demo entities so this __main__ block doesn't collide
        # with real data in LIVE DB. The 'main_block_demo_<ts>:' suffix
        # keeps every run isolated.
        demo_ts = int(time.time())
        demo_stock = f"main_block_demo_{demo_ts}:sh600089"
        demo_person = f"main_block_demo_{demo_ts}:person_x"

        # 1. remember (8 entities, 1 relation, return chunk_id)
        cid = m.remember(
            f"测试插入: {demo_stock} 在 2026-08-12 持股 12,000 股 @ 18.96",
            entities=[
                {
                    "id": demo_stock,
                    "kind": "stock",
                    "name": "demo stock",
                    "aliases": ["demo stock", "DS"],
                    "properties": {"ticker": demo_stock, "sector": "demo"},
                },
                {
                    "id": demo_person,
                    "kind": "person",
                    "name": "demo person",
                },
            ],
            relations=[
                {
                    "source_id": demo_person,
                    "target_id": demo_stock,
                    "relation": "_建仓_于",
                    "weight": 1.0,
                    "properties": {"quantity": 12000, "price": 18.96, "amount": 227520},
                },
            ],
        )
        print(f"✅ remember → chunk_id: {cid}")

        # 2. relate
        rid = m.relate(demo_person, demo_stock, "_关注", weight=0.7, evidence_chunk_id=cid)
        print(f"✅ relate → relation_id: {rid}")

        # 3. recall
        results = m.recall(f"{demo_stock} demo stock", top_k=3)
        print(f"✅ recall → {len(results)} hits")
        for r in results:
            score = r.get("rrf_score", r.get("distance", "?"))
            print(f"  - {r['method']} | score={score} | {r['content'][:60]}")

        # 4. graph_query
        graph = m.graph_query(demo_stock, max_hops=2)
        print(f"✅ graph_query → {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        # 5. stats
        s = m.stats()
        print(f"✅ stats: {s}")

        # 6. update
        new_cid = m.update(cid, reason="修正", new_content=f"测试修正: {demo_stock} 实际 7,800")
        print(f"✅ update → new chunk_id: {new_cid}")

        # 7. forget
        f = m.forget(rid, target_kind="relation", reason="outdated")
        print(f"✅ forget → {f}")

        # 8. recall again
        results = m.recall("sh600089", top_k=3)
        print(f"✅ recall after updates → {len(results)} hits")
        for r in results:
            print(f"  - {r['method']} | {r['content'][:60]}")
