#!/usr/bin/env python3
"""
memory.py — mnelo 核心 CRUD API

- 6 个核心接口: remember / recall / relate / forget / update / graph_query
- 4 路召回 (向量 + 图 + 元数据 + 实体) + RRF 融合
- 4D 时间维度 (valid_from / valid_until / soft delete + 自动级联)
- 单一 writer (单进程) + WAL + busy_timeout=30s 防 lock
"""

import contextlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sqlite_vec

import config  # [G2 8/4] _build_digest 用 config.config

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
from embedder import embed_bytes
from metrics import get_registry as _metrics_registry  # [7/19 v0.5.3] observability

# validation 模块从 conftest/repo 加载 (live == repo via hook sync).
# 注意: memory.py 不再硬编码 /Users/apple/.hermes/memory path — repo 自身是 single source of truth.
from validation import (
    ValidationError,
    validate_chunk_content,
    validate_entity_payload,
    validate_id,
    validate_query,
)

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


class Memory:
    """核心 CRUD 接口."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        # [8/9 P1 follow-up] Memory() 自建库 — 7/19 init_db.py 父目录 mkdir
        # 责任迁到 Memory(). 测试 fixture (test_memory 等 setUpClass) 调 Memory()
        # 不再前置建目录,Memory() 一行兜底.
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False —  P2+ #2 让 recall 并发跑 4 路用独立 conn 时,
        # graph_recall (主 method) 仍然在主 thread 调, 但需要 main conn 也能被 worker 间接用
        # SQLite 检查是 dbapi-level strict — 一切 conn 都允许跨 thread 是务实做法
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        # [PRAGMA-fix 2026-08-07] synchronous=NORMAL: WAL 模式下每次 commit 不 fsync,
        # 性能 +10-100x, 崩溃风险 = 丢最后一两个 transaction (recall_log/audit_log 不 critical).
        self._conn.execute("PRAGMA synchronous = NORMAL")
        # [PRAGMA-fix 2026-08-07] wal_autocheckpoint=4000 pages (16MB, 默认 1000 pages ≈ 4MB).
        # 旧阈值太小 — 实测 memory.db-wal 累积到 196MB 才 auto-checkpoint, INSERT seek 慢 → 撞锁.
        # 配合下一行 startup TRUNCATE 双管齐下 — 平时让 WAL 自然长到 16MB, 重启时一次性清.
        self._conn.execute("PRAGMA wal_autocheckpoint = 4000")
        # [PRAGMA-fix 2026-08-07] 启动时强制 TRUNCATE checkpoint, 清积压 WAL 进主 db.
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # [7/18 patch G] SQLite page cache 64 MB — 让 working-set (24 MB db)
        # 在 RAM, vec0 cold-chunk 走 mmap/OS page cache 而不是每次 fetch
        # cache_size 单位是 page (default 4 KB); -64000 = -64*1024 KB
        self._conn.execute("PRAGMA cache_size = -64000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        # [8/10 refactor] init 阶段 + recall 阶段并发 conn 都走 _load_vec0_module().
        # 本地 venv Python (3.11.15 + sqlite 3.53) 走原 enable_load_extension 路径;
        # CI hostedtoolcache Python 走 ctypes fallback 路径 (vec0 dylib + sqlite3_vec_init).
        # 不改应用行为, 修应用代码兼容更多 Python build.
        _load_vec0_module(self._conn, context="init")
        self._conn.row_factory = sqlite3.Row

        # [P2-1 优化] warm-up Embedder 避免首次 recall 1s 冷启动
        # 实测: Demo 1 1030ms wall-clock (服务端 50ms), 980ms 是 Embedder model 加载到 RAM
        # 配置: warm_up_embedder=True by default, 可 config.toml 关闭
        from config import config as _cfg

        if _cfg.warm_up_embedder:
            from embedder import get_embedder

            get_embedder()  # lazy singleton, 第一次调用触发 model 加载
            logger.info(f"[P2-1] Embedder warmed-up ({_cfg.describe()})")
        else:
            logger.info(f"[P2-1] Embedder warm-up disabled ({_cfg.describe()})")

        # [P0 §3.0] 存量库轻量自动迁移 (幂等): 补 memory_type 列
        # [8/9 P1 follow-up] 7/19 前 init_db.py 跑 schema.sql 后 Memory() 自跑迁移.
        # 主人 8/9 拍板 "按最新版应用服务程序代码来调整测试代码" — Memory() 自建库后,
        # 如果表不存在 (init_db 没跑过 / 新 DB path) 就执行 schema.sql.
        # schema.sql 路径: 当前文件目录下的 schema.sql (memory.py 同级).
        # 占位符替换: {EMBED_DIM} / {EMBED_MODEL} (跟 init_db.py:69 一致).
        _tables = self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('entities','chunks','relations')").fetchall()
        if not _tables:
            _schema_path = Path(__file__).parent / "schema.sql"
            if _schema_path.exists():
                logger.info(f"[8/9] auto-loading schema.sql: {_schema_path}")
                _sql = _schema_path.read_text(encoding="utf-8")
                _sql = _sql.replace("{EMBED_DIM}", str(_cfg.embedder_dim))
                _sql = _sql.replace("{EMBED_MODEL}", _cfg.embedder_model)
                # [8/10 fix] vec0 CREATE VIRTUAL TABLE 单独 exec, 失败 (CI hostedtoolcache
                # 没 sqlite-vec wheel / module 不可用) 不阻塞核心表 (meta/chunks/...).
                # 走 usearch backend 时 vec0 表本来就没用, 这段失败应该 warn 而不是 fail.
                # schema.sql:85 的 vec0 段直接从 SQL 切掉, 避免 executescript 中断后续 DDL.
                import re as _re

                _vec0_stmt = _re.search(
                    r"CREATE\s+VIRTUAL\s+TABLE\s+vectors\s+USING\s+vec0\([^;]*\);",
                    _sql,
                    flags=_re.IGNORECASE | _re.DOTALL,
                )
                _vec0_sql = _vec0_stmt.group(0) if _vec0_stmt else None
                _sql_no_vec0 = (
                    _re.sub(
                        r"CREATE\s+VIRTUAL\s+TABLE\s+vectors\s+USING\s+vec0\([^;]*\);",
                        "",
                        _sql,
                        flags=_re.IGNORECASE | _re.DOTALL,
                    )
                    if _vec0_sql
                    else _sql
                )
                # 先 exec 其他 DDL (entities/chunks/relations/meta/recall_log/...
                # task_states/state_transitions + 索引), vec0 单独 exec.
                try:
                    self._conn.executescript(_sql_no_vec0)
                except Exception:
                    raise
                if _vec0_sql:
                    try:
                        self._conn.executescript(_vec0_sql)
                    except sqlite3.OperationalError as _e:
                        if "no such module: vec0" in str(_e) or "vec0" in str(_e).lower():
                            logger.warning(f"[8/10] sqlite-vec 不可用 ({type(_e).__name__}: {_e}); 跳过 vec0 虚拟表创建, vector 走 usearch (search_index.py)")
                        else:
                            raise
        self._migrate_schema()

        # [zvec 集成] SearchIndex 适配器 (DESIGN §3.6) — 默认 sqlite_vec,
        # zvec 可选 (子进程特性检测, 不可用自动回落)
        from search_index import build_search_index as _build_index

        self._index = _build_index(_cfg.search_backend, self.db_path, _cfg.embedder_dim)

    def _migrate_schema(self) -> None:
        """[P0 §3.0] + [H-1 8/4] 自动迁移存量库.

        [P0 §3.0] f1bc1bf: entities/chunks 补 memory_type 列
        [H-1 8/4] TASKS_L2_HYGIENE H0 的 3 schema 前置:
          - entities.user_confirmed (NOT NULL DEFAULT 0, partial index =1)
          - chunks/entities.processed_at (TEXT, NULL=未跑过 L2)
          - audit_log 表 (L2 自主层审计; UNIQUE 防同 run 重复 apply)

        幂等 — 已存在则跳过 (PRAGMA table_info 检查 + ALTER)。
        跟 schema.sql 双改一致 (A 修正, deepseek 8/4 cross-check)。
        """
        # [P0 §3.0] f1bc1bf — memory_type
        for table in ("entities", "chunks"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "memory_type" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN memory_type TEXT DEFAULT 'fact'")
                logger.info(f"[P0-3.0] migrated {table}: added memory_type column")

        # [H-1 §1] entities.user_confirmed — NOT NULL DEFAULT 0 (Q1 verdict)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(entities)").fetchall()}
        if "user_confirmed" not in cols:
            self._conn.execute("ALTER TABLE entities ADD COLUMN user_confirmed INTEGER NOT NULL DEFAULT 0")
            logger.info("[H-1 §1] migrated entities: added user_confirmed column")

        # [H-1 §2] chunks + entities processed_at — NULL=未跑过 L2 (Q2 verdict 双表)
        for table in ("chunks", "entities"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "processed_at" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN processed_at TEXT")
                logger.info(f"[H-1 §2] migrated {table}: added processed_at column")

        # [H-1 §3] audit_log 表 (Q3/Q4 verdict: 单表 + status; Q5 verdict: 显式 revert_sql; B 修正: created_at 不依赖 SQLite DEFAULT)
        # [审计 §3 注释] UNIQUE 缺 created_at, 极小概率同 microsecond 同 run_id 同 ref 同 status 撞
        # — 实际 8/4 评估认为可接受 (race condition 需要 L2 病态重入, 主人 §5.6 护栏会拦)
        # 未来如要更严, UNIQUE 加 created_at: UNIQUE(run_id, pass_name, action_type, ref_id, status, created_at)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                pass_name TEXT NOT NULL,
                action_type TEXT NOT NULL,
                ref_type TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                confidence REAL DEFAULT 1.0,
                llm_used INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                revert_sql TEXT,
                UNIQUE(run_id, pass_name, action_type, ref_id, status)
            )
        """)
        logger.info("[H-1 §3] audit_log table ensured (CREATE TABLE IF NOT EXISTS)")

        # [H-1 索引] 5 个新索引 — CREATE INDEX IF NOT EXISTS (sqlite 3.8+ 支持)
        for ddl in [
            "CREATE INDEX IF NOT EXISTS idx_entities_user_confirmed ON entities(user_confirmed) WHERE user_confirmed = 1",  # [C 修正] partial
            "CREATE INDEX IF NOT EXISTS idx_entities_processed_at ON entities(processed_at)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_processed_at ON chunks(processed_at)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_run ON audit_log(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_pass ON audit_log(pass_name, status)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_ref ON audit_log(ref_type, ref_id)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)",
        ]:
            self._conn.execute(ddl)
        logger.info("[H-1] 6 new indexes ensured (CREATE INDEX IF NOT EXISTS)")

        # [H-1 审计 §2 fix] 存量库也加 meta flag (H-1 跑过 = 1), 跟 schema.sql INSERT meta 块一致
        # H0 落地时 query `l2_audit_log_ready` 区分 "H-1 跑了" vs "H-1 没跑"
        existing_flag = self._conn.execute("SELECT value FROM meta WHERE key='l2_audit_log_ready'").fetchone()
        if not existing_flag:
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('l2_audit_log_ready', '1')")
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('l2_h1_migrated', ?)", (now(),))
            logger.info("[H-1 审计] meta flags l2_audit_log_ready=1 + l2_h1_migrated ensured")

        # [8/6 v0.2 M1 schema] task_states + state_transitions + seed 默认转移矩阵
        # 主人 DESIGN_TASK_LOOP.md §3 拍板. 不变量:
        #   1. 同一 task 同时最多 1 个当前状态行 (ux_task_current_state partial UNIQUE)
        #   2. state ∈ task (6) / loop (3) 词汇集 (CHECK 约束)
        #   3. kind IN ('task','loop') 排除 L2 TTL/decay (D11 硬规则, M5 落地)
        # M1 仅 schema + 不变量 + seed; M2 行为 + M3 API + M4 digest + M5 cron tick 后续.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS task_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'open','in_progress','waiting','blocked','done','cancelled',
                    'running','dormant','paused'
                )),
                valid_from TEXT NOT NULL,
                valid_until TEXT,
                reason TEXT,
                evidence_chunk_id TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES entities(id),
                FOREIGN KEY (evidence_chunk_id) REFERENCES chunks(id)
            )
        """)
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_task_current_state ON task_states(task_id) WHERE valid_until IS NULL")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_task_states_open ON task_states(state) WHERE valid_until IS NULL AND state NOT IN ('done','cancelled','dormant','paused')")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_task_states_task_valid ON task_states(task_id, valid_from, valid_until)")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS state_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL DEFAULT 'default',
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                UNIQUE(scope, from_state, to_state)
            )
        """)
        # Seed 默认转移矩阵 (DESIGN §3.2). INSERT OR IGNORE — 幂等.
        default_transitions = [
            ("default", "open", "in_progress"),
            ("default", "open", "done"),
            ("default", "open", "cancelled"),
            ("default", "in_progress", "waiting"),
            ("default", "in_progress", "blocked"),
            ("default", "in_progress", "done"),
            ("default", "in_progress", "cancelled"),
            ("default", "waiting", "in_progress"),
            ("default", "waiting", "done"),
            ("default", "waiting", "cancelled"),
            ("default", "blocked", "in_progress"),
            ("default", "blocked", "waiting"),
            ("default", "blocked", "done"),
            ("default", "blocked", "cancelled"),
            ("default", "done", "open"),  # reopen 逃生门 (D8)
        ]
        self._conn.executemany(
            "INSERT OR IGNORE INTO state_transitions (scope, from_state, to_state) VALUES (?, ?, ?)",
            default_transitions,
        )
        # schema_version bump (1.0 → 1.1) — 实际标记 M1 落地
        existing_version = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not existing_version or existing_version[0] != "1.1":
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '1.1')")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('task_loop_m1_migrated', ?)",
                (now(),),
            )
            logger.info("[M1 v0.2] task_states + state_transitions + seed ensured; schema_version → 1.1")

        self._conn.commit()

    def close(self) -> None:
        """Close the underlying SQLite connection + search index."""
        try:
            self._index.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[memory.close] index close failed: {e}")
        self._conn.close()

    def __enter__(self) -> "Memory":
        """Support `with Memory() as m:` — returns self."""
        return self

    def __exit__(self, *args) -> None:
        """Auto-close on context exit."""
        self.close()

    # === CRU ========================

    def remember(
        self,
        content: str,
        source: str = "manual",
        importance: float = 0.5,
        entities: List[Dict] = None,
        relations: List[Dict] = None,
        tags: List[str] = None,
        session_id: str = "default",
        timestamp: str = None,
        memory_type: Optional[str] = None,
        # [P0 2026-08-11] scoping IDs — 借鉴 Mem0 scoping IDs.
        # 写入侧: 这 3 字段 merge 进 chunks.metadata_json (JSON K-V),
        # 不覆盖现有 'tags' 键. None = 未指定, 不写入 (旧数据兼容).
        # 空串是显式选择 ('no scoping'), 保留.
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """写入一条 chunk + 实体 + 关系.

        entities = [{id, kind, name, summary?, aliases?, properties?}]
        relations = [{source_id, target_id, relation, weight?, properties?,
                      valid_from?, valid_until?, evidence_chunk_id?}]
        memory_type: [P0 §3.0] fact / preference / episode / decision / procedure / ephemeral.
                      [P1a E4 8/4] 默认 None 触发 P1a 规则分类; 调用方显式传值 (>None) 永远优先.
        [P0 2026-08-11] scoping IDs (agent_id / user_id / run_id) — 写入
            metadata_json (与现有 'tags' 键 merge). 召回时按这些字段过滤.
        """
        ts = timestamp or now()
        chunk_id = generate_id("chunk")

        # [P1a E4 8/4] 写路径集成: 显式类型 > 规则分类 > fact
        # 调用方显式传 "fact" 也会被规则覆盖? 不! 文档说 "显式传值永远尊重"
        # 语义: None (=未指定) 触发分类, "fact" 也是显式选择 → 尊重
        # 但主人 §5.4 验收 "remember('我偏好简洁日报') → preference"
        # → 假设: 默认不传 → 自动分类; 传 "fact" → 显式 fact; 传其他 → 显式
        if memory_type is None:
            from classify import classify_memory_type

            inferred = classify_memory_type(content)
            if inferred is not None:
                memory_type = inferred
                logger.info(f"[P1a] auto-classified: {chunk_id} -> {memory_type}")
            else:
                memory_type = "fact"  # 默认 fallback
        memory_type = norm_memory_type(memory_type)

        # [7/19 P0-3] chunk content 大小 + 控制字符 + bidi override 验证
        content = validate_chunk_content(content)
        # [7/19 P1-1] id 来源 = generate_id (服务端生成), 无需 validate_id

        # [P0 2026-08-11] 构造 metadata_json: 跟现有 'tags' 键 merge,
        # 再把非 None 的 scoping 字段加进去. 空串也算显式选择 (保留).
        meta_dict: Dict[str, object] = {"tags": tags or []}
        for k, v in (("agent_id", agent_id), ("user_id", user_id), ("run_id", run_id)):
            if v is not None:
                meta_dict[k] = v

        # [P2 2026-08-11] write-time temporal signature — 根据 timestamp/valid_until
        # 自动归类. historical (已 supersede) 通过 update() 路径触发; 这里只处理
        # upcoming (timestamp > now) + current_state (timestamp <= now, 不写字段).
        # 注意: chunks 表没有 valid_from 列, 未来事件靠 timestamp 列识别.
        _temporal_cls = _temporal_class_for_validity(
            valid_from=timestamp,  # timestamp > now → upcoming
            valid_until=None,  # remember() 不接受 valid_until (supersede 走 update)
            now_ts=now(),
        )
        if _temporal_cls is not None:
            meta_dict["temporal_class"] = _temporal_cls

        # 0.5 [8/8 P1 fix] 预校验 entities — 必须在 INSERT chunk 之前
        # 否则 namespace guard 抛 ValidationError 时 chunk INSERT 已进 SQLite WAL,
        # mcp_server 单例 Memory conn 复用下次 commit 可能连同提交, 留下孤儿 chunk.
        # relation 验证依赖 chunk_id (evidence_chunk_id), 留到 step 3 之后再做.
        for ent in entities or []:
            _ent = dict(ent)
            _ent.setdefault("memory_type", memory_type)
            validate_entity_payload(_ent)
            _enforce_entity_namespace_guard(_ent)

        # 1. 写 chunk
        self._conn.execute(
            """
            INSERT INTO chunks (id, content, memory_type, source, session_id, timestamp, importance, metadata_json, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
            (
                chunk_id,
                content,
                memory_type,
                source,
                session_id,
                ts,
                clamp01(importance, "importance"),
                json.dumps(meta_dict, ensure_ascii=False),
            ),
        )

        # 2. 写 entities (insert or ignore — 实体可能已存在)
        for ent in entities or []:
            ent = dict(ent)
            ent.setdefault("memory_type", memory_type)
            self._upsert_entity(ent)

        # 3. 写 relations
        for rel in relations or []:
            self._conn.execute(
                """
                INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                       valid_from, valid_until, source, confidence, evidence_chunk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    rel["source_id"],
                    rel["target_id"],
                    rel["relation"],
                    rel.get("weight", 1.0),
                    json.dumps(rel.get("properties", {}), ensure_ascii=False),
                    rel.get("valid_from", ts),
                    rel.get("valid_until"),  # None = NULL
                    rel.get("source", source),
                    rel.get("confidence", 1.0),
                    rel.get("evidence_chunk_id", chunk_id),
                ),
            )

        # 4. 写向量索引 (SearchIndex 适配器, DESIGN §3.6)
        # [7/21] 原 vec0 直接 INSERT + rowid 冲突 REPLACE 逻辑下沉到 SQLiteVecIndex.add
        # (行为不变); backend=zvec 时走 zvec 后端.
        v_bytes = embed_bytes(content)
        self._index.add(chunk_id, v_bytes, conn=self._conn)

        # 4.5 [8/6 E 路线] PII advisory scan — 命中只写 audit_log, 不改 content 不 throw
        # mnelo 不读内容、不加密、不主动 block; 调用方自决 ("最多提醒一下").
        #
        # [8/9 fix] PII audit_log 假 fail bug: audit_log UNIQUE constraint
        # (run_id, pass_name, action_type, ref_id, status) 防同 run 重复 apply.
        # run_id 必须 idempotent per (chunk_id, pii_category) — 同一 chunk 同类 PII
        # retry 时, INSERT OR IGNORE 撞 UNIQUE 静默跳过, 既保留去重 audit trail
        # 又不让 IntegrityError 把整个 remember 拉下水.
        # (历史 issue: 8/9 VPS 迁移阶段观察到 23 个 IntegrityError 重复写入同一 content)
        from validation import scan_pii_warnings as _scan_pii

        for hit in _scan_pii(content):
            _audit_run_id = f"pii_advisory_{chunk_id}_{hit['category']}"
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO audit_log (run_id, pass_name, action_type, ref_type, ref_id, after_json, llm_used, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'applied', ?)",
                    (
                        _audit_run_id,
                        "pii_audit",
                        f"detected_{hit['category']}",
                        "chunk",
                        chunk_id,
                        json.dumps(
                            {"category": hit["category"], "offset": hit["offset"], "length": hit["length"]},
                            ensure_ascii=False,
                        ),
                        ts,
                    ),
                )
            except sqlite3.IntegrityError as _e:
                # INSERT OR IGNORE 通常已吸收; 兜底 catch 防御 schema 变化引入新 UNIQUE.
                logger.warning(f"[pii_audit] audit_log skip for {chunk_id}/{hit['category']}: {_e}")

        self._conn.commit()
        # Digest only depends on identity facts and high-importance decisions/episodes.
        if any(ent.get("kind") == "identity_fact" for ent in (entities or [])) or (memory_type in ("decision", "episode") and importance >= config.config.digest_importance_threshold):
            self._mark_digest_dirty()
        # [7/19 v0.5.3] metrics
        _metrics_registry().remember_total.inc(source=source or "unknown")
        return chunk_id

    def relate(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float = 1.0,
        valid_from: str = None,
        valid_until: str = None,
        evidence_chunk_id: str = None,
        properties: Dict = None,
    ) -> int:
        """新建一条关系."""
        # [7/19 P1-1] id 格式验证 (白名单正则)
        source_id = validate_id(source_id, "source_id")
        target_id = validate_id(target_id, "target_id")
        if evidence_chunk_id is not None:
            evidence_chunk_id = validate_id(evidence_chunk_id, "evidence_chunk_id")
        cur = self._conn.execute(
            """
            INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                   valid_from, valid_until, source, confidence, evidence_chunk_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', 1.0, ?)
        """,
            (
                source_id,
                target_id,
                relation,
                clamp01(weight, "weight"),
                json.dumps(properties or {}, ensure_ascii=False),
                valid_from or now(),
                valid_until,
                evidence_chunk_id,
            ),
        )
        self._conn.commit()
        # [7/19 v0.5.3] metrics
        _metrics_registry().relate_total.inc()
        return cur.lastrowid

    def update(
        self,
        old_id: str,
        reason: str = "updated",
        new_content: str = None,
        new_properties: Dict = None,
        new_importance: float = None,
    ) -> str:
        """Update by creating new chunk version + superseding old (immutable history).
        老 chunk 不直接覆盖, 而是标 superseded_by + valid_until=now, 触发器自动级联:
        所有引用老 chunk 的边 valid_until = now. 历史完整保留.

        [7/19 P0-3] 新 content 也走 sanitize (None = 保留老内容, 跳过)
        [7/19 P1-1] id 格式验证

        Args:
            old_id: 要更新的 chunk id (active 的, 否则 ValueError)
            reason: 标记更新原因 (写进 source = 'update:<reason>')
            new_content: 新内容, None = 保留老内容
            new_properties: 新 properties (暂未实现)
            new_importance: 新重要性, None = 保留老值, 否则 clamp01

        Returns:
            新 chunk id (新版本 id)
        """
        # [7/19 P1-1] id 格式验证
        old_id = validate_id(old_id, "old_id")
        # [7/19 P0-3] 新 content 也走 sanitize (None = 保留老内容, 跳过)
        if new_content is not None:
            new_content = validate_chunk_content(new_content)

        old = self._conn.execute("SELECT * FROM chunks WHERE id = ? AND valid_until IS NULL", (old_id,)).fetchone()
        if not old:
            raise ValueError(f"chunk {old_id} not found or already superseded")

        # 1. 创建新 chunk
        new_id = generate_id("chunk")
        # [P0 审计] new_importance 也走 clamp01 防止越界
        if new_importance is not None:
            importance_value = clamp01(new_importance, "new_importance")
        else:
            importance_value = old["importance"] if old["importance"] is not None else 0.5
        self._conn.execute(
            """
            INSERT INTO chunks (id, content, source, session_id, timestamp, importance, metadata_json, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
            (
                new_id,
                new_content or old["content"],
                "update:" + reason,
                old["session_id"],
                now(),
                importance_value,
                json.dumps({"supersedes": old_id, "reason": reason}, ensure_ascii=False),
            ),
        )

        # 2. 老 chunk 标 superseded_by + valid_until (中 supersede 后不再召回)
        self._conn.execute(
            "UPDATE chunks SET superseded_by = ?, valid_until = ? WHERE id = ? AND valid_until IS NULL",
            (new_id, now(), old_id),
        )
        # [P2 2026-08-11] write-time temporal signature — 老 chunk 已被 valid_until 设
        # (= 历史), 标 metadata_json.temporal_class='historical' 供后续 read-time
        # historical intent query 直接召回. 读旧 metadata_json (None 也 OK).
        try:
            old_meta_row = self._conn.execute("SELECT metadata_json FROM chunks WHERE id = ?", (old_id,)).fetchone()
            if old_meta_row:
                old_meta_raw = old_meta_row[0]
                old_meta = json.loads(old_meta_raw) if old_meta_raw else {}
                if "temporal_class" not in old_meta:
                    old_meta["temporal_class"] = "historical"
                    self._conn.execute(
                        "UPDATE chunks SET metadata_json = ? WHERE id = ?",
                        (json.dumps(old_meta, ensure_ascii=False), old_id),
                    )
        except Exception as e:  # noqa: BLE001 — 失败不阻塞 supersede 主流程
            logger.warning(f"[P2] failed to mark historical on supersede {old_id}: {e}")
        # [7/21] 向量索引变更下沉到 SearchIndex 适配器
        # (原 v0.5.6: 删旧向量 + 重嵌新内容 — 行为不变)
        self._index.remove(old_id, conn=self._conn)
        new_content_for_embed = new_content if new_content is not None else old["content"]
        try:
            v_bytes = embed_bytes(new_content_for_embed)
            self._index.add(new_id, v_bytes, conn=self._conn)
        except Exception as e:
            logger.warning(f"failed to embed new chunk {new_id} during update: {e}")

        self._conn.commit()
        self._mark_digest_dirty()
        # [7/19 v0.5.3] metrics
        _metrics_registry().update_total.inc()
        return new_id

    def forget(
        self,
        target_id: str,
        target_kind: str = "chunk",  # 'chunk' / 'entity' / 'relation' / 'task' / 'loop'
        reason: str = "outdated",
        cascade: bool = True,
    ) -> Dict[str, int]:
        """软删除: valid_until = now, cascade 级联失效引用边.
        主人口中"删除无用知识" — 不直接物理删, 30 天后 worker 物理清理.

        [8/6 M5.3 + DESIGN §10.2 D11 + M28 fix] task/loop kind 一律拦截 (D11
        核心不变量: mnelo 永不自动删活跃任务). 显式删除必须走
        task_states.forget_task / task_states.forget_loop 显式路径,
        写 audit_log (pass_name='forced_forget', ref_type='task'/'loop',
        status='applied') 留痕.
        chunk/entity/relation 走原 L2 decay 路径 (无豁免).
        """
        # [7/19 P1-1] id 格式验证
        target_id = validate_id(target_id, "target_id")
        # [M5.3 + 8/6 M28 fix] D11 TTL 豁免 — task/loop 一律拦截, 强制显式路径.
        # 设计核心: mnelo 永不自动删活跃任务. 显式删除走 task_states.forget_task / forget_loop.
        if target_kind in ("task", "loop"):
            raise ValueError(
                f"D11 TTL 豁免: forget(target_kind='{target_kind}') 需走 "
                f"task_states.forget_task 或 task_states.forget_loop 显式路径, "
                f"不能直接 mnelo forget() 删除活跃任务. "
                f"reason={reason!r} 仅审计用, 不执行."
            )
        if target_kind == "chunk":
            self._conn.execute("UPDATE chunks SET valid_until = ? WHERE id = ? AND valid_until IS NULL", (now(), target_id))
            # [7/21] 向量索引删除下沉到 SearchIndex 适配器 (原 v0.5.6 drift fix 逻辑)
            # 软删 chunk 的 embedding 是死数据 — 清掉防 vec0 rowid 漂移/碰撞.
            self._index.remove(target_id, conn=self._conn)
        elif target_kind == "entity":
            self._conn.execute("UPDATE entities SET valid_until = ? WHERE id = ? AND valid_until IS NULL", (now(), target_id))
        elif target_kind == "relation":
            self._conn.execute("UPDATE relations SET valid_until = ? WHERE id = ? AND valid_until IS NULL", (now(), target_id))
        else:
            raise ValueError(f"unknown kind: {target_kind}")

        # cascade (主流程中, 触发器也会自动做)
        edges_invalidated = 0
        if cascade:
            cur = self._conn.execute(
                """
                UPDATE relations SET valid_until = ?
                WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL
            """,
                (now(), target_id, target_id),
            )
            edges_invalidated = cur.rowcount

        # 入队 30 天后物理删除
        self._conn.execute(
            """
            INSERT INTO purged_queue (target_id, target_kind, purged_at, done)
            VALUES (?, ?, datetime('now', '+30 days'), 0)
        """,
            (target_id, target_kind),
        )

        self._conn.commit()
        # [7/19 v0.5.3] metrics
        _metrics_registry().forget_total.inc(kind=target_kind or "unknown")
        return {"edges_invalidated": edges_invalidated, "queued_purge": 1}

    # === R = Recall (4 路 + RRF) ===================

    def recall(
        self,
        query: str,
        top_k: int = 5,
        graph_hops: int = 2,
        filters: Dict = None,
        strategy: str = "rrf",
        asof: str = None,
    ) -> List[Dict]:
        """4 路召回 + RRF 融合 ( 7/18 加 entity 路).
        [7/19 P1-4] query 大小 + 控制字符 + bidi 验证

        strategy: 'rrf' / 'vector_only' / 'graph_only' / 'meta_only' / 'entity_only'
        asof: 时间切片查询 ('2026-07-17T15:00:00')
        """
        # [P2+ #1 7/18 patch] Skip noisy / placeholder queries  recall_log 信号纯度
        # 数据: 24h 919 recall, 80 (8%) 空 hits — 一半是 'anything' / test_crud_xxx 占位符
        # 这些 query 没意义, 不应该污染 recall_log / recall_count / last_recalled
        if not query or not query.strip():
            return []
        # [7/19 P1-4] query 验证 (sanitize + size cap) — 必须在 empty check 之后,
        # 否则空 query 会被 validation 拒掉而不是返 []
        query = validate_query(query)
        clean = query.strip()
        # 占位符白名单 (case insensitive)
        _PLACEHOLDER_QUERIES = {
            "anything",
            "something",
            "test",
            "foo",
            "bar",
            "baz",
            "q",
            "?",
            "placeholder",
            "dummy",
            "demo",
            "sample",
            "foo bar",
        }
        if clean.lower() in _PLACEHOLDER_QUERIES:
            return []
        # 单字符无意义 (除了短股票代码 e.g. 'a' 单字母 + 中文概念单字)
        # 中文/unicode 单字可能有意义, 不过滤. ASCII 单字符全部过滤
        if len(clean) == 1 and clean.isascii():
            return []
        # query validation passed, replace with cleaned version
        query = clean

        import time

        t0_start = time.time()

        asof = asof or now()

        if strategy == "rrf":
            # [P2+ #2 7/18 patch] 4 路召回并发 —  p95 70ms → 25ms 目标
            # 串行慢原因: vec0 MATCH ~3.5ms + meta LIKE 0-11ms + entity name ~2-9ms + graph 0-7ms 累加
            # WAL mode SQLite 允许多 conn 并发读, 每路开独立 conn + 共享 Embedder
            # 用 ThreadPoolExecutor 跑 4 task 并行, 取最长耗时 (vs 串行累加)
            from concurrent.futures import ThreadPoolExecutor

            # 4 个独立 SQLite connection (避免同一 conn threading 冲突)
            # check_same_thread=False 让 conn 跨 thread 可用 (主 thread 创建, worker 用)
            recall_conns = [sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False) for _ in range(4)]  # noqa: E501
            for c in recall_conns:
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA busy_timeout = 30000")
                # [7/18 patch G] 每个 worker conn 也设 64 MB cache
                c.execute("PRAGMA cache_size = -64000")
                # [8/10 fix] 每个 worker conn 走 _load_vec0_module(), CI 上 enable_load_extension
                # 被 strip 时自动 fallback 到 ctypes (init 阶段已注册 auto-extension, 通常走它).
                _load_vec0_module(c, context="recall-worker")
                c.row_factory = sqlite3.Row

            # [7/19 v0.5.3] Per-lane timing for metrics (vector first, parallel meta/entity/graph)
            with ThreadPoolExecutor(max_workers=4) as ex:
                t_vec_0 = time.time()
                f_vec = ex.submit(self._vector_recall_with_conn, recall_conns[0], query, top_k * 2, filters, asof)
                f_meta = ex.submit(self._meta_recall_with_conn, recall_conns[1], query, top_k * 2, filters, asof)
                f_entity = ex.submit(self._entity_recall_with_conn, recall_conns[2], query, top_k * 2, filters, asof)

                vector_hits = f_vec.result()
                vec_ms = (time.time() - t_vec_0) * 1000
                # graph 等 vector 完成再开始 (graph 依赖 vector_hits 作为 seed)
                t_graph_0 = time.time()
                f_graph = ex.submit(self._graph_recall, vector_hits, graph_hops, asof)
                meta_hits = f_meta.result()
                entity_hits = f_entity.result()
                graph_hits = f_graph.result()
                graph_ms = (time.time() - t_graph_0) * 1000

            # 关独立连接
            for c in recall_conns:
                c.close()

            results = self._rrf_fuse([vector_hits, graph_hits, meta_hits, entity_hits], top_k)
            # meta/entity roughly parallel (no separate timers; record 0 to skip metric)
            lane_latencies = {"vector": vec_ms, "graph": graph_ms, "meta": 0.0, "entity": 0.0}
        elif strategy == "vector_only":
            t0 = time.time()
            results = self._vector_recall(query, top_k, filters, asof)
            lane_latencies = {"vector": (time.time() - t0) * 1000}
        elif strategy == "graph_only":
            t0 = time.time()
            vector_hits = self._vector_recall(query, top_k, filters, asof)
            graph_hits = self._graph_recall(vector_hits, graph_hops, asof)
            results = graph_hits[:top_k]
            lane_latencies = {
                "vector": (time.time() - t0) * 1000,
                "graph": 0.0,
            }
        elif strategy == "meta_only":
            t0 = time.time()
            results = self._meta_recall(query, top_k, filters, asof)
            lane_latencies = {"meta": (time.time() - t0) * 1000}
        elif strategy == "entity_only":
            t0 = time.time()
            results = self._entity_recall(query, top_k, filters, asof)
            lane_latencies = {"entity": (time.time() - t0) * 1000}
        else:
            raise ValueError(f"unknown strategy: {strategy}")

        latency_ms = (time.time() - t0_start) * 1000

        # [7/19 v0.5.3] metrics: per-lane counter + latency + hit count + top_k
        _reg = _metrics_registry()
        for lane, lane_ms in lane_latencies.items():
            _reg.recall_total.inc(method=lane)
            if lane_ms > 0:
                _reg.recall_latency.observe(lane_ms / 1000.0, method=lane)
        _reg.recall_hits.inc(result="empty" if not results else "non_empty")
        _reg.recall_top_k.inc(k=str(top_k))

        #  recall audit
        self._log_recall(query, results, graph_hops, latency_ms)

        # 更新 recall_count + last_recalled
        if results:
            ids = [r["chunk_id"] for r in results if "chunk_id" in r]
            if ids:
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"""
                    UPDATE chunks
                    SET recall_count = recall_count + 1, last_recalled = ?
                    WHERE id IN ({placeholders})
                """,
                    [now()] + ids,
                )
                self._conn.commit()

        return results

    def _vector_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 1: 向量检索 (SearchIndex 适配器, DESIGN §3.6)."""
        return self._vector_recall_with_conn(self._conn, query, top_k, filters, asof)

    def _vector_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] vector recall — 索引 KNN 走 SearchIndex 适配器.

        [P0 2026-08-11] scoping IDs: 当 filters 含 agent_id, KNN 召回的
        chunk 必须在 metadata_json 里有相同 agent_id (json_extract NULL
        不匹配 → 旧数据无 agent_id 的 chunk 自动保留, 不误过滤).

        Args:
            conn: 独立 sqlite3 connection (每路独立; 用于 chunk 侧查询)
        """
        q_bytes = embed_bytes(query)
        # [审计 4.3 ] filter 多时, 多取一些确保过滤后还够 top_k; strategy 也加大召回
        fetch_limit = top_k * (8 if (filters or top_k >= 3) else 2)
        knn_hits = self._index.knn(q_bytes, fetch_limit, conn=conn)
        if not knn_hits:
            return []

        # [P0 2026-08-11] scoping IDs: 一次 SQL 把 chunk 元数据 + agent_id 拿回来.
        # 在 Python 侧过滤 agent_id (避免每行一次 json_extract SQL).
        agent_id_filter = (filters or {}).get("agent_id")
        agent_id_filter_norm = agent_id_filter if agent_id_filter is not None else _MISSING

        results = []
        for hit in knn_hits:
            # [7/21 fix] asof: chunk 在 asof 时点有效 = valid_until IS NULL OR > asof
            # [P0 2026-08-11] 同时拿 metadata_json, Python 侧 json 解析 agent_id
            chunk = conn.execute(
                "SELECT id, content, memory_type, source, timestamp, importance, metadata_json FROM chunks WHERE id = ? AND (valid_until IS NULL OR valid_until > ?)",
                (hit.chunk_id, asof),
            ).fetchone()
            if not chunk:
                continue
            if filters:
                if "source" in filters and chunk["source"] != filters["source"]:
                    continue
                if "type" in filters and chunk["memory_type"] != norm_memory_type(filters["type"]):
                    continue
                # [P0 2026-08-11] agent_id filter — 旧数据 metadata_json=NULL
                # 或不含 agent_id → JSON 解出 None → 不等于 filter, 保留.
                if agent_id_filter_norm is not _MISSING:
                    raw = chunk["metadata_json"]
                    if raw is None or raw == "":
                        continue
                    try:
                        meta_obj = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if meta_obj.get("agent_id") != agent_id_filter_norm:
                        continue
            results.append(self._hit_dict(chunk, method="vector", distance=float(hit.distance)))
        return results[:top_k]  # type: ignore

    def _meta_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 meta recall.

        [P0 2026-08-11] scoping IDs: 当 filters 含 agent_id, SQL 走
        json_extract(metadata_json, '$.agent_id') = ? 过滤. NULL metadata_json
        或缺 agent_id → json_extract 返回 NULL → != filter → 自动保留
        (旧数据不误过滤).

        [P2 2026-08-11] temporal reasoning: detect_query_intent(query) 决定
        SQL 加成 (跟 P0 scoping 共存, 加 AS 额外约束, 不替换).
          - current_state: AND valid_until IS NULL (强制当前态)
          - upcoming:      AND timestamp > ? (未来事件, 用 now() 作 asof 基准)
          - historical:    不排斥 valid_until (supersede 历史浮出, 默认 ASof 仍过滤 < now)
          - soft_recency:  默认行为 (不变)
        """
        # [7/21 fix] asof: 只看 asof 时点仍有效的 chunk
        # [P0 2026-08-11] scoping: agent_id 走 json_extract SQL 过滤 (NULL 不误过滤)
        sql = """
            SELECT id, content, memory_type, source, timestamp, importance FROM chunks
            WHERE (valid_until IS NULL OR valid_until > ?)
              AND content LIKE ?
        """
        params = [asof, f"%{query}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        if filters and "type" in filters:
            sql += " AND memory_type = ?"
            params.append(norm_memory_type(filters["type"]))
        if filters and "agent_id" in filters:
            # [P0 2026-08-11] json_extract 路径: '$.agent_id'
            # NULL metadata_json 或缺键 → json_extract 返回 NULL → 不 = filter.
            # 这天然保证旧数据兼容.
            sql += " AND json_extract(metadata_json, '$.agent_id') = ?"
            params.append(filters["agent_id"])
        # [P2 2026-08-11] temporal intent 加成 — 用 detect_query_intent
        # 注意: recall() 入口处已 validate_query(), 这里 query 非空.
        intent = detect_query_intent(query)
        # [P2 2026-08-11] upcoming 用 asof 作基准. 当 caller 传 asof=None, 跟 SQL
        # 主条件一致归一为 now() (避免 NULL 比较失败).
        if intent == "upcoming":
            _now_ts = asof if asof else now()
            sql += " AND timestamp > ?"
            params.append(_now_ts)
        elif intent == "current_state":
            # 强制当前态: valid_until 必须 IS NULL (排除已 supersede)
            sql += " AND valid_until IS NULL"
        # historical / soft_recency: 不加约束 (历史浮出, 默认行为)
        sql += " ORDER BY importance DESC, timestamp DESC LIMIT ?"
        params.append(top_k)
        rows = conn.execute(sql, params).fetchall()
        return [self._hit_dict(r, method="meta") for r in rows]

    def _entity_recall_with_conn(self, conn, query, top_k, filters, asof) -> List[Dict]:
        """[P2+ #2] 独立 conn 版 entity recall.

        [P0 2026-08-11] scoping IDs: entity_recall 默认走 entities 表 token
        LIKE (强身份事实); 加 agent_id filter 后, 关联 chunk 必须
        metadata_json.agent_id = filter (json_extract). entity → chunk 关联
        在 relations 表 (自引用 evidence relation: src=entity_id, tgt=entity_id,
        evidence_chunk_id=chunk_id, 见 3027 行). LEFT JOIN 让老 entity (无
        evidence relation) 保留 — c_meta NULL → 旧数据兼容.
        """
        if " " in query.strip():
            tokens = query.strip().split()
        else:
            tokens = [query]

        chunk_results = []
        seen_chunk_ids = set()
        for tok in tokens:
            if not tok or len(tok) < 2:
                continue
            like = f"%{tok}%"
            # [7/21 fix] asof: entity 在 asof 时点有效 = valid_from <= asof AND (valid_until IS NULL OR > asof)
            # [P0 2026-08-11] LEFT JOIN relations (self-ref) → chunks 拿 metadata_json.
            sql = """
                SELECT e.id, e.name, e.kind, e.summary, e.importance, e.aliases_json, c.metadata_json AS c_meta
                FROM entities e
                LEFT JOIN relations r ON r.source_id = e.id AND r.target_id = e.id
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE (e.valid_from IS NULL OR e.valid_from <= ?)
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                  AND (e.name LIKE ? OR e.aliases_json LIKE ?)
            """
            params = [asof, asof, like, like]
            if filters and "type" in filters:
                sql += " AND e.memory_type = ?"
                params.append(norm_memory_type(filters["type"]))
            if filters and "agent_id" in filters:
                # [P0 2026-08-11] SQL 没法直接 json_extract (chunk 可能不存在);
                # Python 侧 post-filter, NULL metadata_json 保留 (旧数据兼容).
                pass  # 见下面 post-filter 循环
            sql += " ORDER BY e.importance DESC LIMIT ?"
            params.append(top_k)
            rows = conn.execute(sql, params).fetchall()
            # [P0 2026-08-11] agent_id post-filter (SQL LEFT JOIN 后 Python 侧 filter)
            if filters and "agent_id" in filters:
                target_agent = filters["agent_id"]
                kept = []
                for r in rows:
                    c_meta = r["c_meta"]
                    if c_meta is None or c_meta == "":
                        # 无关联 chunk / 空 metadata_json → 保留 (旧数据兼容)
                        kept.append(r)
                        continue
                    try:
                        parsed = json.loads(c_meta)
                    except (json.JSONDecodeError, TypeError):
                        kept.append(r)  # 解析失败保留 (defensive)
                        continue
                    if parsed.get("agent_id") == target_agent:
                        kept.append(r)
                rows = kept
            for r in rows:
                # [7/19 v0.5.5] Robust aliases parsing:
                # aliases_json may be NULL (SQL), 'null' (JSON literal),
                # '[]' (empty list), or '[...]' (actual list).
                # Handle all cases defensively to avoid TypeError on `for a in None`.
                raw = r["aliases_json"]
                if not raw or raw == "null":
                    aliases = []
                else:
                    try:
                        parsed = json.loads(raw)
                        aliases = parsed if isinstance(parsed, list) else []
                    except (json.JSONDecodeError, TypeError):
                        aliases = []
                content = r["summary"] or r["name"]
                if not content:
                    continue
                hit = {
                    "chunk_id": f"entity:{r['id']}",
                    "content": content,
                    "source": f"entity:{r['kind']}",
                    "timestamp": now(),
                    "importance": float(r["importance"] or 0.5),
                    "method": "entity",
                    "entity_id": r["id"],
                    "entity_name": r["name"],
                    "entity_kind": r["kind"],
                }
                if any(tok.lower() in a.lower() for a in aliases):
                    hit["importance"] = min(1.0, hit["importance"] + 0.2)
                if hit["chunk_id"] not in seen_chunk_ids:
                    seen_chunk_ids.add(hit["chunk_id"])
                    chunk_results.append(hit)
        return chunk_results[:top_k]  # type: ignore

    def _graph_recall(self, seed_hits: List[Dict], hops: int, asof: str) -> List[Dict]:
        """路 2: 图遍历 (NetworkX 内存层 + hops 跳)."""
        if not seed_hits:
            return []
        seed_ids = {h["chunk_id"] for h in seed_hits}
        # [审计 4.1 优化] 1 次 SQL 拿全部 seed chunks 的关联 entities (避免 N+1)
        placeholders = ",".join("?" * len(seed_ids))
        rows = self._conn.execute(
            f"""
            SELECT source_id, target_id FROM relations
            WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
              AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
        """,
            (*seed_ids, *seed_ids, asof, asof),
        ).fetchall()
        seed_entities = set()
        for r in rows:
            if r["source_id"] not in seed_ids:
                seed_entities.add(r["source_id"])
            if r["target_id"] not in seed_ids:
                seed_entities.add(r["target_id"])

        # [审计 4.1 优化] 1 次 SQL 拿 entities 关联的 chunks (2 跳)
        if not seed_entities:
            return []
        placeholders = ",".join("?" * len(seed_entities))
        rows = self._conn.execute(
            f"""
            SELECT source_id, target_id FROM relations
            WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
              AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
        """,
            (*seed_entities, *seed_entities, asof, asof),
        ).fetchall()
        entity_chunks = set()
        for r in rows:
            if r["source_id"] not in seed_entities:
                entity_chunks.add(r["source_id"])
            if r["target_id"] not in seed_entities:
                entity_chunks.add(r["target_id"])

        # 排除原 seed, 取剩下的 entity_chunks
        new_chunks = entity_chunks - seed_ids - seed_entities
        if not new_chunks:
            return []

        # [ 7/18 A 方案] 第一跳就关联的 identity_fact / canonical_fact
        # 类高价值 entity 自身已是结构化答案, 直接以 entity 形式返回
        # (不必绕回 chunk)
        entity_hits = []
        if seed_entities:
            placeholders_e = ",".join("?" * len(seed_entities))
            e_rows = self._conn.execute(
                f"""
                SELECT id, kind, name, summary, importance FROM entities
                WHERE id IN ({placeholders_e}) AND valid_until IS NULL
                  AND kind IN ('identity_fact', 'canonical_fact')
            """,
                list(seed_entities),
            ).fetchall()
            for er in e_rows:
                entity_hits.append(
                    {
                        "chunk_id": f"entity:{er['id']}",
                        "content": er["summary"] or er["name"],
                        "source": f"entity:{er['kind']}",
                        "timestamp": now(),
                        "importance": float(er["importance"] or 0.5),
                        "method": "graph_entity",
                        "entity_id": er["id"],
                        "entity_name": er["name"],
                        "entity_kind": er["kind"],
                    }
                )

        placeholders = ",".join("?" * len(new_chunks))
        rows = self._conn.execute(
            f"""
            SELECT id, content, source, timestamp, importance FROM chunks
            WHERE id IN ({placeholders}) AND valid_until IS NULL
            ORDER BY importance DESC, timestamp DESC
        """,
            list(new_chunks),
        ).fetchall()
        chunk_hits = [self._hit_dict(r, method="graph") for r in rows]
        # entity 在前 (偏重结构化答案)
        return entity_hits + chunk_hits

    def _meta_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 3: 元数据 (精确 LIKE + 时间近).

        [P0 2026-08-11] scoping IDs: 与 _meta_recall_with_conn 同语义.
        agent_id 走 json_extract SQL 过滤; NULL metadata_json / 缺 agent_id
        保留 (旧数据兼容). 这是 meta_only 策略走的 sequential fallback,
        必须跟并行 _with_conn 行为一致 — 漏一路即失败 (P0 验收).

        [P2 2026-08-11] temporal reasoning: 与 _meta_recall_with_conn 同语义.
        current_state / upcoming 加 SQL 约束; historical / soft_recency 默认.
        """
        # [7/21 fix] asof: 只看 asof 时点仍有效的 chunk
        # [P0 2026-08-11] scoping: agent_id 走 json_extract SQL 过滤
        sql = """
            SELECT id, content, memory_type, source, timestamp, importance FROM chunks
            WHERE (valid_until IS NULL OR valid_until > ?)
              AND content LIKE ?
        """
        params = [asof, f"%{query}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        if filters and "type" in filters:
            sql += " AND memory_type = ?"
            params.append(norm_memory_type(filters["type"]))
        if filters and "agent_id" in filters:
            sql += " AND json_extract(metadata_json, '$.agent_id') = ?"
            params.append(filters["agent_id"])
        # [P2 2026-08-11] temporal intent 加成 — 跟 _meta_recall_with_conn 同源
        intent = detect_query_intent(query)
        if intent == "upcoming":
            _now_ts = asof if asof else now()
            sql += " AND timestamp > ?"
            params.append(_now_ts)
        elif intent == "current_state":
            sql += " AND valid_until IS NULL"
        # historical / soft_recency: 不加约束
        sql += " ORDER BY importance DESC, timestamp DESC LIMIT ?"
        params.append(top_k)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._hit_dict(r, method="meta") for r in rows]

    def _entity_recall(self, query: str, top_k: int, filters: Dict, asof: str) -> List[Dict]:
        """路 4: 实体精确/模糊匹配 ( 7/18 加).

        场景: 用户问'我住在哪里' / '主人GitHub' 类强身份事实,
        向量召回可能因为 chunk 文本太长而被埋没; 直接走 entity.name LIKE
        + entity.aliases_json 反查是更稳的路径.

        拆词策略:
        - ASCII 单词: 全部按空格切, 全词 LIKE (避免 token 太宽)
        - 中文: 只取 2+ 字连续片段 (避免'我''在'单字噪声); 取所有 2-gram + 3-gram
        - 高优先级 token (主人 / user / 我) 不参与单字 token, 全词 LIKE 即可

        降噪: identity_fact / canonical_fact 强优先级, concept 仅补足
        (concept 类实体大量含'在''住'等单字 token, 噪声很大)

        意图增强 (7/18): query 含'我'/'主人'/'ling2077'/'2077 Ling'/'user'
        等任一时, 直接拉 user 所有 identity_fact 关系 (无需 query-token 重叠,
        这是关键 — '我住在哪里' token 与 '北京市大兴区亦庄镇' 无 2-gram 重叠).
        """
        hits = []
        seen_ids = set()

        # === 第一阶段: 意图增强 (user identity 询问) ===
        identity_query_keys = ("我", "主人", "user", "ling2077", "2077 Ling")
        is_identity_query = any(k in query for k in identity_query_keys)
        if is_identity_query:
            # [7/21 fix] asof: 只取 asof 时点仍有效的 entity/relation
            # [P0 2026-08-11] scoping: LEFT JOIN chunks 拿 metadata_json.
            # entity → chunk 关联在 relations.evidence_chunk_id (3027 行
            # 创建, evidence 关系: src=entity_id, tgt=entity_id, evidence_chunk_id=chunk_id).
            # LEFT JOIN 让老 entity (无 evidence relation) 保留 — c.id NULL
            # → c.metadata_json NULL → json_extract NULL → 不匹配 filter → 保留.
            rows = self._conn.execute(
                """
                SELECT e.id, e.kind, e.name, e.summary, e.importance, c.metadata_json AS c_meta
                FROM relations r
                JOIN entities e ON e.id = r.target_id
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE r.source_id = 'user'
                  AND (r.valid_until IS NULL OR r.valid_until > ?)
                  AND e.kind IN ('identity_fact', 'canonical_fact')
            """,
                (asof, asof),
            ).fetchall()
            # [P0 2026-08-11] agent_id filter — SQL 已经 LEFT JOIN chunks,
            # 但 chunk 可能不存在 (老 entity); 改为 Python 侧 post-filter.
            # NULL metadata_json / 缺 agent_id 的 chunk 保留 (旧数据兼容).
            if filters and "agent_id" in filters:
                target_agent = filters["agent_id"]
                kept_rows = []
                for r in rows:
                    c_meta = r["c_meta"]
                    if c_meta is None or c_meta == "":
                        # 无关联 chunk 或空 metadata_json → 保留 (旧数据兼容)
                        kept_rows.append(r)
                        continue
                    try:
                        parsed = json.loads(c_meta)
                    except (json.JSONDecodeError, TypeError):
                        kept_rows.append(r)  # 解析失败也保留 (defensive)
                        continue
                    if parsed.get("agent_id") == target_agent:
                        kept_rows.append(r)
                rows = kept_rows
            for r in rows:
                seen_ids.add(r["id"])
                hits.append(
                    {
                        "chunk_id": f"entity:{r['id']}",
                        "content": r["summary"] or r["name"],
                        "source": f"entity:{r['kind']}",
                        "timestamp": now(),
                        "importance": float(r["importance"] or 0.9),
                        "method": "entity_intent",
                        "entity_id": r["id"],
                        "entity_name": r["name"],
                        "entity_kind": r["kind"],
                    }
                )

        # === 第二阶段: 通用 token LIKE (高优先级 → 补 concept) ===
        tokens = set()
        for w in re.split(r'[\s,;.!?\(\)\[\]\{\}"\'`]+', query):
            w = w.strip().lower()
            if len(w) >= 2:
                tokens.add(w)
            elif len(w) == 1 and w.isascii():
                tokens.add(w)
        for n in (2, 3):
            for i in range(len(query) - n + 1):
                seg = query[i : i + n]
                if all("\u4e00" <= ch <= "\u9fff" for ch in seg):
                    tokens.add(seg)
        if not tokens:
            return hits
        like_clauses = []
        params = []
        for t in tokens:
            # [P0 2026-08-11] 限定 e.id / e.name / e.summary — JOIN 后 'id' 歧义.
            like_clauses.append("(e.name LIKE ? OR e.id LIKE ? OR e.summary LIKE ?)")
            params.extend([f"%{t}%"] * 3)

        # 两轮: 高优先级 (强 fact), 后补 concept
        high_priority_kinds = ("identity_fact", "canonical_fact", "user")

        for kind_filter, take in (
            (high_priority_kinds, top_k),
            (("concept",), top_k),  # 补足
        ):
            # [7/21 fix] asof: (valid_from IS NULL OR valid_from <= ?) 兼容无 valid_from 的旧数据
            # [P0 2026-08-11] scoping: LEFT JOIN chunks via relations.evidence_chunk_id
            # (entity → chunk 关联在 relations 表). LEFT JOIN 让老 entity 保留 —
            # c_meta NULL → post-filter 保留 (旧数据兼容).
            sql = f"""
                SELECT e.id, e.kind, e.name, e.summary, e.importance, e.recall_count, c.metadata_json AS c_meta
                FROM entities e
                LEFT JOIN relations r ON r.source_id = e.id AND r.target_id = e.id
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE (e.valid_from IS NULL OR e.valid_from <= ?)
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                  AND e.kind IN ({",".join("?" * len(kind_filter))})
                  AND ({" OR ".join(like_clauses)})
            """
            cur_params = [asof, asof] + list(kind_filter) + params
            if filters and "type" in filters:
                sql += " AND e.memory_type = ?"
                cur_params.append(norm_memory_type(filters["type"]))
            # [P0 2026-08-11] agent_id filter — SQL 不直接 json_extract (entity
            # 可能没关联 chunk); 改 Python 侧 post-filter 同第一阶段.
            sql += " ORDER BY e.importance DESC, e.recall_count DESC LIMIT ?"
            cur_params.append(take)
            rows = self._conn.execute(sql, cur_params).fetchall()
            if filters and "agent_id" in filters:
                target_agent = filters["agent_id"]
                filtered_rows = []
                for r in rows:
                    c_meta = r["c_meta"]
                    if c_meta is None or c_meta == "":
                        filtered_rows.append(r)  # 旧数据/无关联 chunk 保留
                        continue
                    try:
                        parsed = json.loads(c_meta)
                    except (json.JSONDecodeError, TypeError):
                        filtered_rows.append(r)
                        continue
                    if parsed.get("agent_id") == target_agent:
                        filtered_rows.append(r)
                rows = filtered_rows
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                seen_ids.add(r["id"])
                hits.append(
                    {
                        "chunk_id": f"entity:{r['id']}",
                        "content": r["summary"] or r["name"],
                        "source": f"entity:{r['kind']}",
                        "timestamp": now(),
                        "importance": float(r["importance"] or 0.5),
                        "method": "entity",
                        "entity_id": r["id"],
                        "entity_name": r["name"],
                        "entity_kind": r["kind"],
                    }
                )
        return hits

    def _rrf_fuse(self, hit_lists: List[List[Dict]], top_k: int) -> List[Dict]:
        """Reciprocal Rank Fusion: score(d) = Σ 1/(k + rank).

        [P2+ #4 7/18 patch] stock entity boost:
        : kind=stock 的 entity_hit (e.g. 'sh600089') 是关心的高价值答案,
        默认 RRF 把 chunk 当事实, 但 stock entity 关联 chunk 是结构的语义提升.
        BOOST = 0.05 / rank^0.5 —  trade-off: 不压倒既有排序, 但 stock always 浮顶.
        """
        import math

        rrf_score: Dict[str, float] = {}
        rrf_hits: Dict[str, Dict] = {}
        k = 60
        # [8/5 普适化] RRF 实体 boost — 可配置 kind 清单 (config [recall].boost_kinds / env
        # MNELO_MEMORY_RECALL_BOOST_KINDS)。默认 ['stock'] 兼容旧行为; 设自己领域的
        # kind (product/category/...) 或 [] 禁用。机制通用: 已知品类代码实体命中浮顶。
        from config import config as _cfg

        boost_kinds = getattr(_cfg, "recall_boost_kinds", ["stock"])
        ENTITY_BOOST = 0.05
        for hits in hit_lists:
            for rank, h in enumerate(hits):
                # [ 7/18] 主键区分实体 vs chunk — 用 chunk_id 字段统一
                # 实体 hit 的 chunk_id = 'entity:<entity_id>'
                # chunk hit 的 chunk_id = '<chunk_id>'
                # 同 ID 合并(实体 hit 和 chunk hit 可能是同一事实在不同层的表达)
                cid = h["chunk_id"]
                rank_score = 1.0 / (k + rank + 1)
                # [P2+ #4 泛化] 实体 boost — 从 entity_kind 或 source 前缀 'entity:<kind>' 推导 kind
                kind = h.get("entity_kind") or _kind_from_source(h.get("source", ""))
                if kind and kind in boost_kinds and h.get("method") == "entity":
                    # 0.05 / rank^0.5 boost — 浮顶但不压倒 RRF 排序
                    boost = ENTITY_BOOST / math.sqrt(rank + 1)
                    rank_score += boost
                rrf_score[cid] = rrf_score.get(cid, 0) + rank_score
                rrf_hits[cid] = h
        ranked = sorted(rrf_score.items(), key=lambda x: -x[1])
        out = []
        for cid, score in ranked[:top_k]:
            h = rrf_hits[cid]
            h["rrf_score"] = score
            out.append(h)
        return out

    def _log_recall(self, query: str, results: List[Dict], hops: int, latency_ms: float):
        """[P2+ #3 7/18 patch] 写入 recall_log 审计 (always local time via now() helper).

         feedback loop 数据:
        - results_json 已存 [chunk_id] 数组 (前: 只知道命中哪些 chunk)
        - 新存 recall_details_json: top-K 完整 dict (method, distance/score, importance)
          让 daily_check / analytics 能分析 召回质量 (用什么路召回的, 距离分布)
        """
        #  feedback loop: 每条命中的 method + 距离 + 排名 (top-5 by RRF score)
        detail = [
            {
                "rank": i + 1,
                "chunk_id": r.get("chunk_id"),
                "method": r.get("method"),
                "distance": r.get("distance"),  # 0.0-2.0 越小越相似 (vector_only)
                "rrf_score": r.get("rrf_score"),  # RRF 融合分数 (rrf strategy)
                "importance": r.get("importance"),
            }
            for i, r in enumerate(results[:5])  # top-5
        ]
        self._conn.execute(
            """
            INSERT INTO recall_log (query, results_json, graph_hops, latency_ms, created_at, recall_details_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                query,
                json.dumps([r.get("chunk_id") for r in results]),
                hops,
                latency_ms,
                now(),
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    # === 图遍历 ====================

    def graph_query(
        self,
        start_node: str,
        max_hops: int = 3,
        edge_types: List[str] = None,
        asof: str = None,
    ) -> Dict:
        """子图: start_node 起, max_hops 跳内的所有节点 + 边."""
        # [7/19 P1-1] start_node 格式验证
        start_node = validate_id(start_node, "start_node")
        asof = asof or now()
        # BFS: 拿 max_hops 跳内的所有节点
        visited = {start_node}
        frontier = [start_node]
        edges = []
        for _hop in range(max_hops):
            next_frontier = []
            for node in frontier:
                sql = """
                    SELECT * FROM relations
                    WHERE (source_id = ? OR target_id = ?)
                      AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
                """
                params = [node, node, asof, asof]
                if edge_types:
                    sql += f" AND relation IN ({','.join('?' * len(edge_types))})"
                    params.extend(edge_types)
                rows = self._conn.execute(sql, params).fetchall()
                for r in rows:
                    edges.append(dict(r))
                    other = r["target_id"] if r["source_id"] == node else r["source_id"]
                    if other not in visited:
                        visited.add(other)
                        next_frontier.append(other)
            frontier = next_frontier

        # 拿节点详情
        nodes = []
        if visited:
            placeholders = ",".join("?" * len(visited))
            rows = self._conn.execute(
                f"""
                SELECT id, kind, name, summary FROM entities
                WHERE id IN ({placeholders}) AND valid_until IS NULL
            """,
                list(visited),
            ).fetchall()
            nodes = [dict(r) for r in rows]

        return {"nodes": nodes, "edges": edges, "asof": asof}

    # === 内部 helper ====================

    @staticmethod
    def _hit_dict(row, method: str, **extra) -> Dict:
        """4 路召回统一返回格式 (RRF 融合需要)。

        Args:
            row: sqlite3.Row from chunks (含 id/content/source/timestamp/importance)
            method: 'vector' / 'graph' / 'meta' / 'entity' / 'rrf'
            **extra: 召回方法特有的字段 (e.g. distance=0.123 for vector)

        Returns:
            dict 含 chunk_id/content/source/timestamp/importance/method + extra
        """
        return {
            "chunk_id": row["id"],
            "content": row["content"],
            "source": row["source"],
            "timestamp": row["timestamp"],
            "importance": row["importance"],
            "method": method,
            **extra,
        }

    def _upsert_entity(self, ent: Dict) -> None:
        """Insert or update entity, preserving valid_until=NULL latest.

        If entity with id=ent['id'] exists and is active (valid_until IS NULL),
        update its name/summary/aliases/properties via COALESCE (None = keep old).
        Otherwise INSERT new entity with importance clamped to [0.0, 1.0].

        Args:
            ent: dict with keys:
                - id (str, required): entity id (e.g. 'sh600089', 'identity:')
                - kind (str, required): 'stock'/'concept'/'identity_fact'/etc
                - name, summary (str, optional): human-readable
                - aliases (list, optional): known aliases for entity_resolve
                - properties (dict, optional): free-form metadata
                - source (str, optional): defaults to 'manual'
                - importance (float, optional): defaults to 0.5, clamped
        """
        # [7/19 P1-1 + P1-2 + P1-5] entity 整体清洗 (id 验证 + name/summary/kind 剥离控制 + bidi)
        ent = validate_entity_payload(ent)
        # [8/8 P1] namespace 防御 — 阻止历史 importer 残留 (Honcho anno:*) + 随机 ID (TOKEN_C_*)
        # + 句子当 entity.name. 白名单只允许: 显式 namespace prefix / master_* / 无冒号的 person 类短 name.
        _enforce_entity_namespace_guard(ent)
        existing = self._conn.execute("SELECT id, kind FROM entities WHERE id = ? AND valid_until IS NULL", (ent["id"],)).fetchone()
        if existing:
            # [7/19 P1-2] identity_fact 类实体拒绝覆盖 name/aliases/properties (防伪造主人身份)
            # 只能新增 (valid_until 旧版 + 新版)
            if existing["kind"] == "identity_fact":
                raise ValidationError("entity.identity_fact", "identity_fact entities are immutable; create a new version instead")
            # 更新 fields
            self._conn.execute(
                """
                UPDATE entities
                SET memory_type = COALESCE(?, memory_type),
                    name = COALESCE(?, name),
                    summary = COALESCE(?, summary),
                    aliases_json = COALESCE(?, aliases_json),
                    properties_json = COALESCE(?, properties_json)
                WHERE id = ? AND valid_until IS NULL
            """,
                (
                    ent.get("memory_type"),
                    ent.get("name"),
                    ent.get("summary"),
                    json.dumps(ent.get("aliases", []), ensure_ascii=False) if "aliases" in ent else None,
                    json.dumps(ent.get("properties", {}), ensure_ascii=False) if "properties" in ent else None,
                    ent["id"],
                ),
            )
        else:
            # [7/19 v0.5.8] Soft-deleted entity handling:
            # If a previous version exists with valid_until IS NOT NULL (e.g. from
            # a forgotten/then-remembered entity), reactivate it instead of INSERT
            # (which would fail UNIQUE). Skip this for identity_fact — those
            # have a separate immutable path and the soft-deleted state is intentional.
            existing_inactive = self._conn.execute(
                "SELECT id, kind FROM entities WHERE id = ? AND valid_until IS NOT NULL",
                (ent["id"],),
            ).fetchone()
            if existing_inactive and existing_inactive["kind"] != "identity_fact":
                # Reactivate historical row with new values
                self._conn.execute(
                    "UPDATE entities SET valid_until = NULL, valid_from = ? WHERE id = ?",
                    (now(), ent["id"]),
                )
                # Also reactivate any relationships where this entity is involved
                # (preserving the audit trail but making them active again)
                # Note: relations aren't auto-reactivated to avoid surprising the
                # caller — they may have been intentionally soft-deleted.
                # The entity itself gets a fresh row in history (via UPDATE).
                # Also update its metadata fields
                self._conn.execute(
                    """
                    UPDATE entities
                    SET memory_type = COALESCE(?, memory_type),
                        name = COALESCE(?, name),
                        summary = COALESCE(?, summary),
                        aliases_json = COALESCE(?, aliases_json),
                        properties_json = COALESCE(?, properties_json),
                        importance = COALESCE(?, importance),
                        source = COALESCE(?, source)
                    WHERE id = ?
                """,
                    (
                        ent.get("memory_type"),
                        ent.get("name"),
                        ent.get("summary"),
                        json.dumps(ent.get("aliases", []), ensure_ascii=False) if "aliases" in ent else None,
                        json.dumps(ent.get("properties", {}), ensure_ascii=False) if "properties" in ent else None,
                        clamp01(ent.get("importance", 0.5), "entities[].importance"),
                        ent.get("source", "manual"),
                        ent["id"],
                    ),
                )
                return  # skip the INSERT path
            # Plain INSERT
            self._conn.execute(
                """
                INSERT INTO entities (id, kind, memory_type, name, summary, aliases_json, properties_json,
                                      source, importance, valid_from, valid_until)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
                (
                    ent["id"],
                    ent["kind"],
                    ent.get("memory_type", "fact"),
                    ent.get("name"),
                    ent.get("summary"),
                    json.dumps(ent.get("aliases", []), ensure_ascii=False),
                    json.dumps(ent.get("properties", {}), ensure_ascii=False),
                    ent.get("source", "manual"),
                    clamp01(ent.get("importance", 0.5), "entities[].importance"),
                    now(),
                ),
            )

    # === 统计 ====================

    # [7/19 P2-4] 显式白名单, 防止以后误把 user input 传进来 → SQL injection
    _ALLOWED_TABLES = frozenset({"entities", "chunks", "relations"})

    def stats(self) -> Dict:
        """统计."""
        stats = {}
        for t in self._ALLOWED_TABLES:  # 永远是 3 个白名单字符串
            total = self._conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            active = self._conn.execute(f"SELECT count(*) FROM {t} WHERE valid_until IS NULL").fetchone()[0]
            stats[t] = {"total": total, "active": active, "deleted": total - active}
        # [8/5] vectors 按实际 search 后端计数 (usearch/zvec 下 sqlite_vec 的 vectors 表恒 0)
        try:
            stats["vectors"] = self._index.size()
        except Exception as e:
            logger.warning(f"[stats] search index size failed: {e}")
            stats["vectors"] = 0
        stats["recall_log"] = self._conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]
        return stats

    def cleanup_orphan_vectors(self, dry_run: bool = False) -> Dict[str, int]:
        """[8/6 plan §3] 后端感知孤儿向量清理 — 薄委托给 self._index.cleanup_orphans.

        旧版 (7/19 v0.5.6) 直接 SQL 查/删 vec0 `vectors` 表; usearch/zvec 下该表
        恒空, 真实孤儿 (索引里但 chunks 行已删/软删) 不被清. 改为委托后端感知实现:
          - UsearchIndex: list(_index.keys) → 查 chunks.valid_until → remove 孤儿
          - ZvecIndex: iter_all() → 查 chunks.valid_until → delete 孤儿
        SQLite 事务由调用方管 (本方法只 commit 写操作).

        Args:
            dry_run: if True, report counts without removing (for safe inspection).

        Returns:
            Dict with counts:
              - `soft_deleted_cleaned`: index entries whose chunks.valid_until 非空
              - `truly_orphan_cleaned`: index entries with no matching chunk row
              - `vectors_remaining`: count after cleanup
              - `dry_run`: True if no changes were made
        """
        result = self._index.cleanup_orphans(conn=self._conn, dry_run=dry_run)
        if not dry_run:
            try:
                self._conn.commit()
            except Exception as e:
                logger.warning(f"[cleanup_orphan_vectors] commit failed: {e}")
        return result

    def run_purge_worker(
        self,
        dry_run: bool = False,
        clean_orphan_target_ids: bool = True,
        batch_size: int = 200,
    ) -> Dict[str, int]:
        """[8/4 fix] Purge worker — 30 天延迟清的真正实现 (deepseek 提议 + hermes 评审).

        修 v0.5.12 实际发现的 2 个 bug:
        - bug 1: forget() 后 purged_queue.done 永远 0 — 30 天延迟清 半完成
        - bug 2: 100% target_id 命名错位 (placeholder id) — 即使加 worker 也清不动

        修法 (deepseek 提议, v0.3 报告 §6 采纳):
        1. bug 2 先: clean_orphan_target_ids=True → DELETE purged_queue WHERE target_id 不在主表
        2. bug 1 后: dry_run=False → 物理删 chunks/entities/relations WHERE valid_until != NULL AND id IN purged_queue done=0 AND purged_at < today

        Args:
            dry_run: 只报数, 不删. 默认 False (跟 cleanup_orphan_vectors 一致).
            clean_orphan_target_ids: 是否先清命名错位的脏数据. 默认 True (v0.5.12 100% 是脏数据).
            batch_size: 单次物理删批量上限. 默认 200 (v0.3 报告 §9 H-1 推荐值).

        Returns:
            Dict:
              - `orphan_purged_queue_rows`: 清掉的 placeholder id 行数
              - `chunks_physically_deleted`: 真物理删的 chunk 数
              - `entities_physically_deleted`: 真物理删的 entity 数
              - `relations_physically_deleted`: 真物理删的 relation 数
              - `vectors_orphans_cleaned`: cleanup_orphan_vectors 跑过的孤儿数
              - `dry_run`: 是否 dry-run
        """
        from datetime import datetime as _dt

        stats = {
            "orphan_purged_queue_rows": 0,
            "chunks_physically_deleted": 0,
            "entities_physically_deleted": 0,
            "relations_physically_deleted": 0,
            "vectors_orphans_cleaned": 0,
            "dry_run": dry_run,
        }

        today_iso = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")

        # === Phase 1: clean_orphan_target_ids — 清命名错位的脏数据 (bug 2) ===
        if clean_orphan_target_ids:
            if dry_run:
                # dry_run: 只数, 不删
                count_sql = """
                    SELECT COUNT(*) FROM purged_queue
                    WHERE done = 0
                      AND (
                        (target_kind = 'chunk'    AND target_id NOT IN (SELECT id FROM chunks))
                        OR (target_kind = 'entity'   AND target_id NOT IN (SELECT id FROM entities))
                        OR (target_kind = 'relation' AND target_id NOT IN (SELECT id FROM relations))
                      )
                """
                stats["orphan_purged_queue_rows"] = self._conn.execute(count_sql).fetchone()[0]
            else:
                orphan_sql = """
                    DELETE FROM purged_queue
                    WHERE done = 0
                      AND (
                        (target_kind = 'chunk'    AND target_id NOT IN (SELECT id FROM chunks))
                        OR (target_kind = 'entity'   AND target_id NOT IN (SELECT id FROM entities))
                        OR (target_kind = 'relation' AND target_id NOT IN (SELECT id FROM relations))
                      )
                """
                cur = self._conn.execute(orphan_sql)
                stats["orphan_purged_queue_rows"] = cur.rowcount
                logger.info(f"[purge_worker] cleaned {cur.rowcount} orphan purged_queue rows (placeholder id)")

        if dry_run:
            return stats

        # === Phase 2: 30 天延迟清 — 物理删 chunks/entities/relations (bug 1) ===
        # 选 done=0 AND purged_at < today 的 target_id, 物理删 + set done=1
        # 注意: 一次 batch_size 限制, 避免大批量 delete 阻塞
        for kind, table in [("chunk", "chunks"), ("entity", "entities"), ("relation", "relations")]:
            due_sql = """
                SELECT id FROM purged_queue
                WHERE done = 0
                  AND target_kind = ?
                  AND purged_at < ?
                ORDER BY purged_at
                LIMIT ?
            """
            due_ids = [r[0] for r in self._conn.execute(due_sql, (kind, today_iso, batch_size)).fetchall()]
            if not due_ids:
                continue

            # 物理删 (从主表) — 用 cursor 拿 rowcount
            ph = ",".join("?" * len(due_ids))
            cur = self._conn.execute(f"DELETE FROM {table} WHERE id IN ({ph})", due_ids)
            deleted = cur.rowcount
            stats[f"{table}_physically_deleted"] = deleted

            # set done=1
            cur = self._conn.execute(
                f"UPDATE purged_queue SET done = 1 WHERE target_kind = ? AND id IN ({ph})",
                [kind, *due_ids],
            )
            updated = cur.rowcount
            logger.info(f"[purge_worker] {kind}: deleted {deleted} rows from {table}, marked {updated} purged_queue rows done=1")

        # === Phase 3: vec0 orphan vectors 清理 (跟 cleanup_orphan_vectors 同款) ===
        try:
            vec_stats = self.cleanup_orphan_vectors(dry_run=False)
            stats["vectors_orphans_cleaned"] = vec_stats.get("soft_deleted_cleaned", 0) + vec_stats.get("truly_orphan_cleaned", 0)
        except Exception as e:
            logger.warning(f"[purge_worker] cleanup_orphan_vectors failed: {e}")

        self._conn.commit()
        return stats

    # ============================================================
    # [H-0 + H-1 8/4] L2 自主层基础设施 (DESIGN §5.7-5.9 + TASKS_L2_HYGIENE v0.2)
    # ============================================================

    # L2 配置项默认值 (跟 DESIGN §5.7 config 模板一致; 实际读 meta 表)
    _L2_DEFAULTS: Dict[str, Any] = {
        "enabled": False,  # 主人 §5.7: 全局默认 false, 显式开启
        "dry_run": True,  # 主人 §5.7: 全局默认 dry-run
        "importance_floor": 0.1,  # hygiene pass 的 floor (§5.6)
        "caps": {"supersede": 20, "merge": 20, "purge": 50},
    }

    # TTL 规则按 memory_type (TASKS_L2_HYGIENE H3 §3 + DESIGN §3.0.5)
    # 实际分布 (8/4 v0.2): fact 95.4% / procedure 3.4% / ephemeral 1.2%
    _MEMORY_TYPE_TTL_DAYS: Dict[str, Optional[int]] = {
        # ephemeral 7d: 实际 1.2% (草稿/临时, 主人 §3.0.5 + LLM 草稿衰减)
        "ephemeral": 7,
        # fact 365d: 实际 95.4% (事实/对话, 主人 §3.0.5 默认)
        "fact": 365,
        # preference 180d: 主人偏好 (实际 1 个, 但 schema 必备)
        "preference": 180,
        # episode 730d: 实际事件 (2年)
        "episode": 730,
        # decision 730d: 决策
        "decision": 730,
        # procedure 永久 (None = 不衰减)
        "procedure": None,
    }

    def _l2_get(self, key: str, default: Any = None) -> Any:
        """[H-1] 从 meta 表读 L2 配置项.

        Args:
            key: 'l2.enabled' / 'l2.dry_run' / 'l2.last_run.hygiene' 等
        """
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        v = row[0]
        # [fix 8/4] bool 解析优先 (否则 "0" / "1" 会被解析为 0.0 / 1.0)
        if v == "1":
            return True
        if v == "0":
            return False
        # [fix 8/4] 先 float (0.1 应解 float 不是 int)
        try:
            return float(v)
        except (ValueError, TypeError):
            pass
        try:
            return int(v)
        except (ValueError, TypeError):
            return v

    def _l2_set(self, key: str, value: Any) -> None:
        """[H-1] 写 L2 配置项到 meta 表."""
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )
        self._conn.commit()

    def _exec_clean(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """[H-0 fix 8/4] SQLite execute() 不支持 SQL 注释 (#, --, /* */ 任意 unicode).
        Strip 所有 inline 注释后 execute. 这样 mnelo Python 源码可以保留
        §/¶ 等标记方便阅读, 不影响 SQL 语法.
        """
        # 简单 strip: 移除整行 # / -- 注释 + 移除 /* ... */ 块注释
        import re

        cleaned = re.sub(r"#[^\n]*", "", sql)  # 整行 # 注释 (含 §)
        cleaned = re.sub(r"--[^\n]*", "", cleaned)  # 整行 -- 注释
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)  # /* */ 块
        # 折叠多空行
        cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
        return self._conn.execute(cleaned, params)

    def list_audit(
        self,
        run_id: Optional[str] = None,
        status: Optional[str] = None,
        pass_name: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict]:
        """[H-1 §5.7] 查 audit_log (提案历史). DESIGN §5.9.1 状态机.

        Args:
            run_id: 过滤特定 run (可选)
            status: proposed / applied / reverted / skipped (可选)
            pass_name: 过滤特定 pass (可选)
            limit: max rows (默认 50, §5.7 memory_audit_list)
            offset: 跳过行

        Returns:
            List[{id, run_id, pass_name, action_type, ref_type, ref_id,
                  before_json, after_json, confidence, status, created_at}]
        """
        wheres, params = [], []
        if run_id:
            wheres.append("run_id=?")
            params.append(run_id)
        if status:
            wheres.append("status=?")
            params.append(status)
        if pass_name:
            wheres.append("pass_name=?")
            params.append(pass_name)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        sql = f"""SELECT id, run_id, pass_name, action_type, ref_type, ref_id,
                         before_json, after_json, confidence, llm_used, status,
                         created_at, revert_sql
                  FROM audit_log
                  {where_sql}
                  ORDER BY id DESC
                  LIMIT ? OFFSET ?"""
        params.extend([limit, offset])

        rows = self._conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            # 解析 json 字段
            before = json.loads(r["before_json"]) if r["before_json"] else None
            after = json.loads(r["after_json"]) if r["after_json"] else None
            result.append(
                {
                    "id": r["id"],
                    "run_id": r["run_id"],
                    "pass_name": r["pass_name"],
                    "action_type": r["action_type"],
                    "ref_type": r["ref_type"],
                    "ref_id": r["ref_id"],
                    "before": before,
                    "after": after,
                    "confidence": r["confidence"],
                    "llm_used": bool(r["llm_used"]),
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "revert_sql": r["revert_sql"],
                }
            )
        return result

    def audit_undo(self, audit_id: int) -> Dict[str, Any]:
        """Undo one applied audit record using its trusted, stored revert script."""
        row = self._conn.execute("SELECT * FROM audit_log WHERE id = ?", (audit_id,)).fetchone()
        if not row:
            raise ValueError(f"audit record {audit_id} not found")
        if row["status"] != "applied":
            raise ValueError(f"audit record {audit_id} is not applied")
        revert_sql = row["revert_sql"]
        if not revert_sql:
            raise ValueError(f"audit record {audit_id} has no revert_sql")
        # executescript is intentional: TTL undo stores UPDATE + DELETE.
        self._conn.executescript(revert_sql)
        ts = now()
        self._conn.execute(
            """INSERT INTO audit_log
               (run_id, pass_name, action_type, ref_type, ref_id,
                before_json, after_json, confidence, llm_used, status,
                created_at, revert_sql)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reverted', ?, NULL)""",
            (row["run_id"], row["pass_name"], row["action_type"], row["ref_type"], row["ref_id"], row["after_json"], row["before_json"], row["confidence"], row["llm_used"], ts),
        )
        self._conn.commit()
        return {"audit_id": audit_id, "status": "reverted", "ref_id": row["ref_id"]}

    def run_maintenance(
        self,
        passes: Optional[List[str]] = None,
        dry_run: Optional[bool] = None,
        since: Optional[str] = None,
        confirm_destructive: bool = False,
    ) -> Dict:
        """[H-1 §5.7] L2 自主层入口. DESIGN §5.9 事务粒度 + watermark.

        Args:
            passes: ['hygiene', 'decay', 'ttl', 'purge', ...] (None = 全 enabled)
            dry_run: True/False/None (None = 用 meta.l2.dry_run 默认)
            since: ISO 时间戳, 仅处理 chunks WHERE created_at > since
            confirm_destructive: purge pass 需要 True 才真删 (§5.9.2)

        Returns:
            {passes_run, proposals: {pass_name: [proposal_dicts]},
             applied, skipped, failed, watermark_updated, gc_stats}
        """
        # 1. 校验 L2 是否启用
        enabled = self._l2_get("l2.enabled", self._L2_DEFAULTS["enabled"])
        if not enabled:
            return {
                "status": "disabled",
                "message": "L2 自主层未启用 ([l2].enabled=false). 设 l2.enabled=1 开启 (主人 §5.7).",
                "passes_run": [],
            }

        # 2. 防重叠 (meta.l2.running)
        # [fix 8/4] _l2_get 解析 boolean -> True/False, 直接判 bool 不是 str
        existing_running = self._l2_get("l2.running", False)
        if existing_running is True or existing_running == "1":
            return {
                "status": "already_running",
                "message": "另一 pass 正在跑 (l2.running=1). 等其完成.",
            }
        self._l2_set("l2.running", "1")

        try:
            # 3. dry_run 默认
            if dry_run is None:
                dry_run = self._l2_get("l2.dry_run", self._L2_DEFAULTS["dry_run"])

            # 4. 决定跑哪些 pass
            if passes is None:
                passes = ["hygiene"]  # 默认只跑 hygiene (§6.5 工具收敛原则)

            # 4.5 实际 GC audit_log (调 _run_audit_gc, 实际 v0.2 TASKS §3 L2 hygiene GC)
            # 实际 fix 8/4 audit #5 — 1yr 估算 150MB 不受控增长
            gc_enabled = self._l2_get("l2.gc.enabled", True)  # 默认 enabled
            gc_stats = {"applied_removed": 0, "skipped_removed": 0, "proposed_removed": 0}
            if gc_enabled and not dry_run:
                gc_stats = self._run_audit_gc()
            # dry_run 时也跑 (实际只 reports, 不真删)
            elif gc_enabled and dry_run:
                gc_stats = self._run_audit_gc(dry_run=True)

            # 5. run_id + timestamp
            import time as _time

            run_id = f"run_{int(_time.time() * 1000)}"

            # 6. 逐 pass 跑
            results: Dict[str, Any] = {
                "run_id": run_id,
                "dry_run": dry_run,
                "passes_run": [],
                "proposals": {},  # pass_name -> [proposal_dicts]
                "applied": 0,
                "skipped": 0,
                "failed": 0,
                "watermark_updated": [],
                "gc_stats": gc_stats,  # [H-3 audit #5] audit_log GC 实际
            }

            for pname in passes:
                if pname == "hygiene":
                    # [fix 8/4] ensure bool (l2.dry_run meta returns Optional)
                    actual_dry_run = bool(dry_run) if dry_run is not None else True
                    res = self._run_hygiene_pass(
                        run_id=run_id,
                        dry_run=actual_dry_run,
                        confirm_destructive=confirm_destructive,
                    )
                    results["passes_run"].append("hygiene")
                    results["proposals"]["hygiene"] = res["proposals"]
                    # [H4 §3.4] purge_candidates 聚合: 只挑 ttl_soft_delete 的 proposals
                    # (decay_importance 是降权不是真删, 不算 purge)
                    results["purge_candidates"] = [p for p in res["proposals"] if p.get("action") == "ttl_soft_delete"]
                    results["applied"] += res["applied"]
                    results["skipped"] += res["skipped"]
                    results["failed"] += res.get("failed", 0)
                    if res.get("watermark_updated"):
                        results["watermark_updated"].append("hygiene")
                elif pname == "promote":
                    # [P1-P4 8/5] TASKS_L2_SESSION_STATE Part 2: 事实晋升机制
                    actual_dry_run = bool(dry_run) if dry_run is not None else True
                    res = self._run_promote_pass(
                        run_id=run_id,
                        dry_run=actual_dry_run,
                        confirm_destructive=confirm_destructive,
                    )
                    results["passes_run"].append("promote")
                    results["proposals"]["promote"] = res["proposals"]
                    # promote pass 暴露 candidates 给上层 (admin UI / API 报告)
                    results.setdefault("promote_candidates", []).extend(res["candidates"])
                    results["applied"] += res["applied"]
                    results["skipped"] += res["skipped"]
                    results["failed"] += res.get("failed", 0)
                    if res.get("watermark_updated"):
                        results["watermark_updated"].append("promote")
                else:
                    results.setdefault("warnings", []).append(f"unknown pass '{pname}', skipped")

            return results
        finally:
            # 7. 清 l2.running flag
            self._l2_set("l2.running", "0")

    def _run_hygiene_pass(
        self,
        run_id: str,
        dry_run: bool,
        importance_floor: Optional[float] = None,
        confirm_destructive: bool = False,
    ) -> Dict:
        """[H-1 + H-3 8/4] hygiene pass — P1 §5.6 + DESIGN §5.9 + TASKS_L2_HYGIENE H3.

        严格 §5.9 语义 (8/4 实际):
          - Phase 1: importance decay 候选 (0.1-0.3 区间) — 真跑时 UPDATE chunks.importance
          - Phase 2: TTL 候选 (按 memory_type, 实际 ephemeral 7d 52 chunks) — 真跑 + confirm_destructive=True 才 soft-delete
          - dry_run=True: 全 proposed (不 apply, 不真改数据)
          - dry_run=False + confirm_destructive=True (Phase 2): 真 soft-delete
          - 每 proposal 一事务 (§5.9 "细粒度事务")
          - watermark 推进只在 pass 全 success (§5.9.2)
          - 失败 proposal 标 skipped + 错误记入 audit_log
          - applied 状态写 audit_log 第二次行 (append-only §5.9.1)
          - revert_sql 字段填 (§5.9.3 重放)
        """
        if importance_floor is None:
            importance_floor = self._l2_get(
                "l2.importance_floor",
                self._L2_DEFAULTS["importance_floor"],
            )
        # [fix Pyright] ensure float (None check; _l2_get may return None)
        if importance_floor is None:
            importance_floor = 0.1
        importance_floor = float(importance_floor)

        proposals = []
        applied = 0
        skipped = 0
        failed = 0
        cap_purge = 50  # §5.7 l2.caps.purge
        ts = now()

        # ============================================================
        # Phase 1: importance decay (实际 8/4 ~2259 候选, cap 50/批)
        # ============================================================
        decay_candidates = self._exec_clean(
            """SELECT id, memory_type, importance, content, timestamp
               FROM chunks
               WHERE valid_until IS NULL
                 AND importance > 0
                 AND importance < ?
                 AND memory_type != 'procedure'
               ORDER BY importance ASC, timestamp ASC
               LIMIT 50""",
            (importance_floor * 3,),
        ).fetchall()

        for i, row in enumerate(decay_candidates):
            if i >= cap_purge:
                skipped += 1
                continue

            chunk_id = row["id"]
            before = {"importance": row["importance"], "memory_type": row["memory_type"]}
            after = {"importance": max(0.0, row["importance"] - 0.05), "memory_type": row["memory_type"]}
            # revert_sql (§5.9.3): 重放回 before 状态
            revert_sql = f"UPDATE chunks SET importance = {before['importance']:.6f} WHERE id = '{chunk_id}' AND valid_until IS NULL"

            # === 写 audit_log proposed 状态 (§5.9.1) ===
            try:
                self._conn.execute(
                    """
                    INSERT INTO audit_log
                        (run_id, pass_name, action_type, ref_type, ref_id,
                         before_json, after_json, confidence, llm_used, status,
                         created_at, revert_sql)
                    VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                            ?, ?, 1.0, 0, 'proposed', ?, NULL)
                """,
                    (
                        run_id,
                        chunk_id,
                        json.dumps(before, ensure_ascii=False),
                        json.dumps(after, ensure_ascii=False),
                        ts,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # UNIQUE 撞 (同 run_id 同 ref_id 同 status) — idempotent skip
                skipped += 1
                continue

            proposals.append(
                {
                    "ref_type": "chunk",
                    "ref_id": chunk_id,
                    "before": before,
                    "after": after,
                    "action": "decay_importance",
                    "reason": f"importance {before['importance']:.2f} < {importance_floor * 3:.2f} (floor={importance_floor})",
                    "revert_sql": revert_sql,
                }
            )

            # === Apply 路径 (dry_run=False) — 每 proposal 一事务 (§5.9) ===
            if not dry_run:
                # [H5 P1] decay 也要包 try/except, 跟 ttl_soft_delete 路径对称.
                # 内层 _apply_decay_importance 自己 try/except 处理 rowcount=0 等场景
                # (返回 False); 外层处理 _apply 自身抛异常的边界.
                try:
                    apply_ok = self._apply_decay_importance(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        before=before,
                        after=after,
                        revert_sql=revert_sql,
                        ts=ts,
                    )
                    if apply_ok:
                        applied += 1
                    else:
                        failed += 1
                except Exception as e:  # noqa: BLE001 — 提案级隔离
                    logger.exception(f"[H5] decay_importance apply raised: {chunk_id}")
                    self._mark_skipped(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        ts=ts,
                        reason=f"decay_importance apply raised: {type(e).__name__}: {e}",
                        action_type="decay_importance",
                    )
                    failed += 1

        # ============================================================
        # Phase 2: TTL 候选 (按 memory_type)
        # [H-3 8/4 实际] ephemeral 7d 52 chunks / fact 365d 0 / ...
        # 真 apply 需 confirm_destructive=True (§5.9.2 "purge 是破坏性操作")
        # ============================================================
        for mtype, ttl_days in self._MEMORY_TYPE_TTL_DAYS.items():
            if ttl_days is None:
                continue  # procedure 永久

            cutoff_iso = (datetime.now() - timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%S")

            # 取过期 chunks (报告 + 真 apply 候选)
            ttl_candidates = self._exec_clean(
                """SELECT id, memory_type, timestamp FROM chunks
                   WHERE valid_until IS NULL
                     AND memory_type = ?
                     AND timestamp < ?
                   ORDER BY timestamp ASC
                   LIMIT 50""",
                (mtype, cutoff_iso),
            ).fetchall()

            if not ttl_candidates:
                # 仍报 0 候选 (主人 §6 报告)
                proposals.append(
                    {
                        "ref_type": "report",
                        "ref_id": f"ttl_{mtype}",
                        "before": {"memory_type": mtype, "ttl_days": ttl_days},
                        "after": None,
                        "action": "ttl_candidate_report",
                        "reason": f"0 chunks older than {ttl_days} days (memory_type={mtype})",
                    }
                )
                continue

            # 每个 candidate 写 audit_log + (apply 路径) soft-delete
            for i, chunk_row in enumerate(ttl_candidates):
                if i >= cap_purge:
                    skipped += 1
                    continue

                chunk_id = chunk_row["id"]
                before = {"memory_type": mtype, "valid_until": None, "timestamp": chunk_row["timestamp"]}
                after = {"memory_type": mtype, "valid_until": ts, "timestamp": chunk_row["timestamp"]}
                # Undo must revive the chunk and cancel its delayed physical purge.
                revert_sql = f"UPDATE chunks SET valid_until = NULL WHERE id = '{chunk_id}'; DELETE FROM purged_queue WHERE target_id = '{chunk_id}' AND target_kind = 'chunk' AND done = 0"

                try:
                    self._conn.execute(
                        """
                        INSERT INTO audit_log
                            (run_id, pass_name, action_type, ref_type, ref_id,
                             before_json, after_json, confidence, llm_used, status,
                             created_at, revert_sql)
                        VALUES (?, 'hygiene', 'ttl_soft_delete', 'chunk', ?,
                                ?, ?, 1.0, 0, 'proposed', ?, NULL)
                    """,
                        (
                            run_id,
                            chunk_id,
                            json.dumps(before, ensure_ascii=False),
                            json.dumps(after, ensure_ascii=False),
                            ts,
                        ),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    skipped += 1
                    continue

                proposals.append(
                    {
                        "ref_type": "chunk",
                        "ref_id": chunk_id,
                        "before": before,
                        "after": after,
                        "action": "ttl_soft_delete",
                        "reason": f"memory_type={mtype} > {ttl_days} days",
                        "revert_sql": revert_sql,
                    }
                )

                # === Apply 路径 (dry_run=False + confirm_destructive=True) ===
                if not dry_run:
                    if not confirm_destructive:
                        # [§5.9.2] 没 confirm_destructive 标 skipped (破坏性操作需显式)
                        # [8/4 audit #6+8 fix] action_type 跟原 action 一致 ('ttl_soft_delete')
                        self._mark_skipped(
                            run_id=run_id,
                            chunk_id=chunk_id,
                            ts=ts,
                            reason=f"ttl_soft_delete needs confirm_destructive=True (got {confirm_destructive})",
                            action_type="ttl_soft_delete",
                        )
                        # Status is skipped, but the run records a blocked destructive
                        # action as failed so the watermark cannot advance.
                        failed += 1
                    else:
                        # [H5 §5.9.1] 每 proposal 一事务 + 异常隔离
                        # 外层 try/except 包 _apply_ttl_soft_delete, 避免任一 proposal
                        # 异常打断整轮 — 失败 proposal 标 skipped + audit_log, 其它继续.
                        try:
                            apply_ok = self._apply_ttl_soft_delete(
                                run_id=run_id,
                                chunk_id=chunk_id,
                                mtype=mtype,
                                before=before,
                                after=after,
                                revert_sql=revert_sql,
                                ts=ts,
                            )
                            if apply_ok:
                                applied += 1
                            else:
                                failed += 1
                        except Exception as e:  # noqa: BLE001 — 提案级隔离, 详记日志
                            logger.exception(f"[H5] ttl_soft_delete apply raised: {chunk_id}")
                            self._mark_skipped(
                                run_id=run_id,
                                chunk_id=chunk_id,
                                ts=ts,
                                reason=f"ttl_soft_delete apply raised: {type(e).__name__}: {e}",
                                action_type="ttl_soft_delete",
                            )
                            failed += 1

        # ============================================================
        # Phase 3: watermark (§5.9.2)
        # [fix 8/4] applied==0 AND failed==0 = 没成功也没失败, 推 watermark (idempotent 软写)
        # [fix 8/4] failed > 0 = 有失败, 不推 (下次重跑失败项)
        # [fix 8/4] applied > 0 = 成功, 推 (不论 skipped, 因 skipped 是 cap 超限)
        # ============================================================
        watermark_updated = False
        if failed > 0:
            watermark_updated = False
        elif dry_run:
            if proposals:
                self._l2_set("l2.last_dry_run.hygiene", ts)
        else:
            # 真跑 + 无失败 (可能 applied=0 也推, 因为是 idempotent 软写)
            self._l2_set("l2.last_run.hygiene", ts)
            watermark_updated = True

        self._conn.commit()

        return {
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "proposals": proposals,
            "watermark_updated": watermark_updated,
        }

    def _apply_decay_importance(
        self,
        run_id: str,
        chunk_id: str,
        before: Dict,
        after: Dict,
        revert_sql: str,
        ts: str,
    ) -> bool:
        """[H-3 §5.9 实际] 真 UPDATE chunks.importance + 写 audit_log applied 行.

        Returns True on success, False on failure (skipped + 错误记入 audit_log).
        """
        try:
            # 1. 真改数据 (用 _exec_clean 保证 # 注释不报错)
            # [8/4 audit #3 fix] atomic UPDATE CAS (compare-and-swap) importance guard:
            #   SELECT-then-UPDATE race 窗口; §5.9.1 '每提案一事务'
            #   atomic CAS: WHERE importance = before.importance SELECT value
            #   Client 2 UPDATE 后 importance 已变 → CAS fail (rowcount=0) → _mark_skipped
            #   实际 (v0.2): 用 = (CAS) 而不是 < (range scan) 让 race fail-safe
            cur = self._exec_clean(
                """UPDATE chunks SET importance = ?
                   WHERE id = ? AND valid_until IS NULL
                     AND importance = ?""",
                (after["importance"], chunk_id, before["importance"]),
            )
            if cur.rowcount == 0:
                # chunk 已被别人改/删 (race condition) 或 importance 已变
                raise RuntimeError(f"chunk {chunk_id} not found, already soft-deleted, or importance changed (rowcount=0, expected={before['importance']:.4f})")

            # 2. 写 audit_log applied 行 (append-only §5.9.1)
            # [fix 8/4] UNIQUE(run_id, pass_name, action_type, ref_id, status)
            # 同 run_id 同 ref 已 proposed, 现在写 applied = 同 ref 不同 status
            # 但 UNIQUE 5 字段含 status, status 不同 = OK
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'hygiene', 'decay_importance', 'chunk', ?,
                        ?, ?, 1.0, 0, 'applied', ?, ?)
            """,
                (
                    run_id,
                    chunk_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    ts,
                    revert_sql,
                ),
            )
            self._conn.commit()
            return True
        except Exception as e:
            # [H5 P0 fix] rollback 必须在 _mark_skipped 前 — 否则 UPDATEs + purged_queue
            # INSERT 会被 _mark_skipped 末尾的 commit() 一并提交, 留半更新 (chunk
            # 软删除 + queue 入队 + audit_log skipped = 操作员被骗).
            self._conn.rollback()
            # [§5.9.1] 失败标 skipped + 错误记入
            # [8/4 audit #6+8 fix] action_type 跟 applied 行一致 (实际是 'decay_importance', 不是 'failed')
            self._mark_skipped(
                run_id=run_id,
                chunk_id=chunk_id,
                ts=ts,
                reason=f"decay_importance apply failed: {type(e).__name__}: {e}",
                action_type="decay_importance",
            )
            return False

    def _apply_ttl_soft_delete(
        self,
        run_id: str,
        chunk_id: str,
        mtype: str,
        before: Dict,
        after: Dict,
        revert_sql: str,
        ts: str,
    ) -> bool:
        """[H-3 §5.9 实际] TTL 过期 soft-delete (UPDATE valid_until + INSERT purged_queue) + 写 audit_log applied 行.

        [§5.9 设计意图] soft-delete = UPDATE valid_until (软写, 可回滚);
        物理删 = 30 天后 run_purge_worker 自动清 (commit 4bd654d).
        """
        try:
            # 1. UPDATE chunks.valid_until = now
            cur = self._exec_clean(
                """UPDATE chunks SET valid_until = ?
                   WHERE id = ? AND valid_until IS NULL""",
                (ts, chunk_id),
            )
            if cur.rowcount == 0:
                raise RuntimeError(f"chunk {chunk_id} not found or already soft-deleted")

            # 2. INSERT purged_queue (30 天延迟物理清, 跟 DESIGN §3.8 一致)
            # [fix 8/4 audit #4] 用 Python now() + timedelta 而不是 SQLite 'now', '+30 days',
            #    实际避免 T+ ISO vs 空格秒混用 (v0.3 报告 §0 nuance B)
            from datetime import timedelta as _td

            purged_at_iso = (datetime.now() + _td(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
            self._exec_clean(
                """INSERT INTO purged_queue
                       (target_id, target_kind, purged_at, done)
                   VALUES (?, 'chunk', ?, 0)""",
                (chunk_id, purged_at_iso),
            )

            # 3. 写 audit_log applied 行 (§5.9.1 append-only)
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'hygiene', 'ttl_soft_delete', 'chunk', ?,
                        ?, ?, 1.0, 0, 'applied', ?, ?)
            """,
                (
                    run_id,
                    chunk_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    ts,
                    revert_sql,
                ),
            )
            self._conn.commit()
            return True
        except Exception as e:
            # [H5 P0 fix] rollback 必须在 _mark_skipped 前 — 否则 UPDATE valid_until +
            # INSERT purged_queue 会被 _mark_skipped 末尾的 commit() 一并提交, 留半更新.
            self._conn.rollback()
            # [8/4 audit #6+8 fix] action_type 跟 applied 行一致
            self._mark_skipped(
                run_id=run_id,
                chunk_id=chunk_id,
                ts=ts,
                reason=f"ttl_soft_delete apply failed: {type(e).__name__}: {e}",
                action_type="ttl_soft_delete",
            )
            return False

    def _mark_skipped(
        self,
        run_id: str,
        chunk_id: str,
        ts: str,
        reason: str,
        action_type: str = "failed",  # [8/4 audit #6+8 fix] 默认 'failed' 兼容旧调用; 调用方传 action 跟 applied 一致
    ) -> None:
        """[§5.9.1] 失败 proposal 标 skipped + 错误记入 audit_log (append-only).

        Args:
            action_type: 实际 passed-in action ('decay_importance' / 'ttl_soft_delete' / 'failed')
                实际: §5.9.1 '失败 proposal 标 skipped' 不应改原 action_type
                实际: 默认 'failed' 兼容旧调用 (L0/L1 阶段)
        """
        try:
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'hygiene', ?, 'chunk', ?,
                        NULL, ?, 0, 0, 'skipped', ?, NULL)
            """,
                (
                    run_id,
                    action_type,
                    chunk_id,
                    json.dumps({"reason": reason}, ensure_ascii=False),
                    ts,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            # UNIQUE 撞, 已写过一个 skipped 同 run_id + ref_id — OK
            pass

    # [P2/P3 P0-fix] 常量
    _PROMOTE_RECALL_THRESHOLD = 20
    _PROMOTE_REF_DEGREE_THRESHOLD = 10
    _PROMOTE_LONG_IMP_THRESHOLD = 0.8
    _PROMOTE_LONG_DAYS = 90
    _PROMOTE_DEMOTE_DAYS = 90
    _PROMOTE_DEMOTE_REF_THRESHOLD = 3
    _PROMOTE_MAX_CANONICAL = 50

    def _run_promote_pass(
        self,
        run_id: str,
        dry_run: bool = True,
        confirm_destructive: bool = False,
    ) -> Dict[str, Any]:
        """[P1-P3 8/5] TASKS_L2_SESSION_STATE §2.3.

        Part 2 事实晋升机制 — 把高频验证的 fact chunk 晋升为 canonical_fact 实体,
        久未召回的 canonical_fact 降级, 总量上限强制淘汰.

        Args:
            run_id: audit_log 关联 run id.
            dry_run: True 只生成 proposals 不写; False 真应用 (P2 promote / P3 demote).
            confirm_destructive: demote / 上限淘汰需要此显式确认 (跟 hygiene confirm_destructive 一致).

        Returns:
            {
              "candidates": [{chunk_id, signals, action: "promote"}],  # P1 扫描结果
              "demote_candidates": [{entity_id, reason}],  # P3 降级候选
              "proposals": [audit_log proposal dicts],
              "applied": int,
              "skipped": int,
              "failed": int,
              "promoted_entity_ids": [新晋升的 canonical_fact entity_id],
              "demoted_entity_ids": [降级的 entity_id],
            }
        """
        from datetime import datetime as _dt

        ts = _dt.now().strftime("%Y-%m-%dT%H:%M:%S")

        # ===== P1: 扫描候选 =====
        # 三信号: recall_count ≥ 20, ref_degree ≥ 10, 长期 importance ≥ 0.8
        # 信号强度: recall + ref_degree*2 + long_imp_bonus (供排序)
        rows = self._conn.execute(
            """
            SELECT c.id, c.content, c.importance, c.recall_count,
                   (SELECT COUNT(*) FROM relations r
                    WHERE r.evidence_chunk_id = c.id AND r.valid_until IS NULL) AS ref_degree,
                   c.timestamp
            FROM chunks c
            WHERE c.valid_until IS NULL
              AND c.memory_type = 'fact'
            """
        ).fetchall()

        candidates: List[Dict[str, Any]] = []
        for row in rows:
            signals: Dict[str, Any] = {}
            score = 0
            if row["recall_count"] >= self._PROMOTE_RECALL_THRESHOLD:
                signals["recall_count"] = row["recall_count"]
                score += row["recall_count"]
            if row["ref_degree"] >= self._PROMOTE_REF_DEGREE_THRESHOLD:
                signals["ref_degree"] = row["ref_degree"]
                score += row["ref_degree"] * 2
            # 长期 importance ≥ 0.8 (timestamp < now - 90d)
            try:
                chunk_age_days = (_dt.now() - _dt.fromisoformat(row["timestamp"])).days
            except (ValueError, TypeError):
                chunk_age_days = 0
            if row["importance"] >= self._PROMOTE_LONG_IMP_THRESHOLD and chunk_age_days >= self._PROMOTE_LONG_DAYS:
                signals["long_high_imp"] = {
                    "importance": row["importance"],
                    "age_days": chunk_age_days,
                }
                score += 50  # 长期高重要给固定权重
            if signals:
                candidates.append(
                    {
                        "chunk_id": row["id"],
                        "signals": signals,
                        "score": score,
                        "action": "promote",
                    }
                )
        # 按 score 降序
        candidates.sort(key=lambda c: c["score"], reverse=True)

        # ===== P3 上限检查: canonical_fact 总数 =====
        canonical_count_row = self._conn.execute("SELECT COUNT(*) FROM entities WHERE kind='canonical_fact' AND valid_until IS NULL").fetchone()
        canonical_count = canonical_count_row[0]
        need_evict = max(0, canonical_count + len(candidates) - self._PROMOTE_MAX_CANONICAL)

        # ===== P3 降级候选: canonical_fact 90 天未召回 + ref_degree < 3 =====
        demote_candidates: List[Dict[str, Any]] = []
        demote_rows = self._conn.execute(
            """
            SELECT e.id, e.importance, e.last_recalled,
                   (SELECT COUNT(*) FROM relations r
                    WHERE (r.source_id = e.id OR r.target_id = e.id)
                      AND r.valid_until IS NULL) AS ref_degree
            FROM entities e
            WHERE e.kind = 'canonical_fact' AND e.valid_until IS NULL
            """
        ).fetchall()
        for ent in demote_rows:
            last_recalled = ent["last_recalled"]
            if last_recalled is None:
                # 从未召回 — 当作"老召回"算
                age_days = 9999
            else:
                try:
                    age_days = (_dt.now() - _dt.fromisoformat(last_recalled)).days
                except (ValueError, TypeError):
                    age_days = 0
            if age_days >= self._PROMOTE_DEMOTE_DAYS and ent["ref_degree"] < self._PROMOTE_DEMOTE_REF_THRESHOLD:
                demote_candidates.append(
                    {
                        "entity_id": ent["id"],
                        "reason": f"90d未召回(ref_degree={ent['ref_degree']})",
                        "importance": ent["importance"],
                    }
                )

        # ===== 上限腾位: 按 importance asc 补 demote 候选 =====
        if need_evict > 0:
            evict_rows = self._conn.execute(
                """
                SELECT e.id, e.importance
                FROM entities e
                WHERE e.kind = 'canonical_fact' AND e.valid_until IS NULL
                ORDER BY e.importance ASC
                LIMIT ?
                """,
                (need_evict,),
            ).fetchall()
            for ent in evict_rows:
                # 避免重复添加
                if any(d["entity_id"] == ent["id"] for d in demote_candidates):
                    continue
                demote_candidates.append(
                    {
                        "entity_id": ent["id"],
                        "reason": f"canonical_fact 上限{self._PROMOTE_MAX_CANONICAL}触发腾位",
                        "importance": ent["importance"],
                    }
                )

        # ===== P4 audit 接入: proposals =====
        proposals: List[Dict[str, Any]] = []
        # promote proposals
        for cand in candidates:
            proposals.append(
                {
                    "action_type": "promote_to_canonical",
                    "ref_type": "chunk",
                    "ref_id": cand["chunk_id"],
                    "signals": cand["signals"],
                    "score": cand["score"],
                }
            )
        # demote proposals
        for d in demote_candidates:
            proposals.append(
                {
                    "action_type": "demote_canonical",
                    "ref_type": "entity",
                    "ref_id": d["entity_id"],
                    "reason": d["reason"],
                }
            )

        result = {
            "candidates": candidates,
            "demote_candidates": demote_candidates,
            "proposals": proposals,
            "applied": 0,
            "skipped": 0,
            "failed": 0,
            "promoted_entity_ids": [],
            "demoted_entity_ids": [],
            "watermark_updated": False,
        }

        if dry_run:
            return result

        # ===== 真应用 (P2 promote + P3 demote) =====
        # demote 是 destructive — 需 confirm_destructive
        apply_demotes = confirm_destructive

        # 1. demote (先降级腾位, 给 promote 留位)
        if apply_demotes:
            for d in demote_candidates:
                ok = self._apply_demote_canonical(
                    run_id=run_id,
                    entity_id=d["entity_id"],
                    reason=d["reason"],
                    ts=ts,
                )
                if ok:
                    result["applied"] += 1
                    result["demoted_entity_ids"].append(d["entity_id"])
                else:
                    result["failed"] += 1

        # 2. promote (限上限 — 已 demote 后计数)
        demoted_set = set(result["demoted_entity_ids"])
        effective_canonical = canonical_count - len(demoted_set)
        if effective_canonical + len(candidates) > self._PROMOTE_MAX_CANONICAL:
            slots = max(0, self._PROMOTE_MAX_CANONICAL - effective_canonical)
        else:
            slots = len(candidates)

        for cand in candidates[:slots]:
            ok = self._apply_promote_to_canonical(
                run_id=run_id,
                chunk_id=cand["chunk_id"],
                signals=cand["signals"],
                ts=ts,
            )
            if ok:
                result["applied"] += 1
                result["promoted_entity_ids"].append(cand["chunk_id"])
            else:
                result["failed"] += 1

        # [P4] watermark 推进
        if result["failed"] == 0:
            self._l2_set("l2.last_run.promote", ts)
            result["watermark_updated"] = True

        return result

    def _apply_promote_to_canonical(
        self,
        run_id: str,
        chunk_id: str,
        signals: Dict[str, Any],
        ts: str,
    ) -> bool:
        """[P2 8/5] chunk → canonical_fact entity + evidence 关系.

        §2.3 P2:
          - 抽核心事实 (内容截断 ≤200 字 + 首句)
          - id = "canonical:<slug(content 前 40 字)>"
          - 建 evidence_chunk_id → 源 chunk 关系 (existing relation.evidence_chunk_id 字段)
        """
        try:
            # 1. 取 chunk content
            row = self._conn.execute(
                "SELECT content, importance FROM chunks WHERE id = ? AND valid_until IS NULL",
                (chunk_id,),
            ).fetchone()
            if not row:
                raise RuntimeError(f"chunk {chunk_id} not found or soft-deleted")

            content = row["content"]
            # 核心事实: 内容截断 ≤200 字 + 首句 (第一句 80 字内)
            first_sentence = content.split("。")[0].split(".")[0].strip()
            if len(first_sentence) > 80:
                first_sentence = first_sentence[:80]
            core_fact = first_sentence[:200] if len(first_sentence) > 200 else first_sentence

            # 2. slug id (取核心前 40 字, alphanumeric + underscore)
            # [Part 2 review MEDIUM fix] 追加 chunk_id 短 hash 后缀, 防不同 chunk 共享
            # 首 40 字导致 slug 撞 → silent overwrite canonical_fact summary.
            import re as _re

            slug = _re.sub(r"[^a-zA-Z0-9_]", "_", core_fact[:40]).strip("_")
            if not slug:
                # fallback: 用 chunk_id[:16] (原行为)
                slug = chunk_id[:16]
                entity_id = f"canonical:{slug}"
            else:
                # 6-char hash suffix (16M 空间足够) — 防 slug collision
                import hashlib as _hl_slug

                _hash_suffix = _hl_slug.md5(chunk_id.encode(), usedforsecurity=False).hexdigest()[:6]
                entity_id = f"canonical:{slug}_{_hash_suffix}"

            # 3. upsert canonical_fact entity (重要性沿用 chunk)
            self._conn.execute(
                """
                INSERT INTO entities (id, kind, name, summary, importance, last_recalled)
                VALUES (?, 'canonical_fact', ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary = excluded.summary,
                    importance = MAX(importance, excluded.importance)
                """,
                (entity_id, core_fact, core_fact, row["importance"], ts),
            )

            # 4. 建 evidence 关系: entity ← chunk (relation.source_id=entity_id, target_id=entity_id, evidence_chunk_id=chunk_id)
            # 用一个特殊 relation kind 'canonical_evidence_of' 避免冲突
            # 注意: relations.id 是 INTEGER AUTOINCREMENT, 不能用 TEXT id — 用 chunk_id hash
            # [Part 2 review LOW fix] 防 silent drop: hash collision 时改用 max+1, 且事后 verify INSERT 生效
            import hashlib as _hl

            rel_id_hash = int.from_bytes(
                _hl.md5(f"{chunk_id}|{entity_id}".encode(), usedforsecurity=False).digest()[:4],
                "big",
                signed=False,
            )
            max_row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM relations").fetchone()
            base_id = rel_id_hash % (2**31) or (max_row[0] + 1)
            # 试探 INSERT; 如因 id collision 失败, 退到 MAX+1
            rel_id = base_id
            for attempt in range(3):
                try:
                    self._conn.execute(
                        """
                        INSERT INTO relations
                            (id, source_id, target_id, relation, weight, valid_from, evidence_chunk_id)
                        VALUES (?, ?, ?, 'canonical_evidence_of', 1.0, ?, ?)
                        """,
                        (rel_id, entity_id, entity_id, ts, chunk_id),
                    )
                    break
                except sqlite3.IntegrityError:
                    rel_id = max_row[0] + 1 + attempt + 1
            else:
                raise RuntimeError(f"evidence relation insert failed after 3 attempts for {chunk_id}|{entity_id}")

            # 5. audit_log applied
            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'promote', 'promote_to_canonical', 'chunk', ?,
                        NULL, ?, 1.0, 0, 'applied', ?, ?)
                """,
                (
                    run_id,
                    chunk_id,
                    json.dumps(
                        {
                            "entity_id": entity_id,
                            "core_fact": core_fact,
                            "signals": signals,
                        },
                        ensure_ascii=False,
                    ),
                    ts,
                    f"DELETE FROM relations WHERE id='{rel_id}'; UPDATE entities SET valid_until='{ts}' WHERE id='{entity_id}' AND valid_until IS NULL;",
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            return False

    def _apply_demote_canonical(
        self,
        run_id: str,
        entity_id: str,
        reason: str,
        ts: str,
    ) -> bool:
        """[P3 8/5] canonical_fact 降级 → concept (kind 变更走版本链).

        §2.3 P3: supersede 为普通 concept; 历史保留.
        简化实现: kind 变更 (canonical_fact → concept) — valid_until IS NULL 保留可逆.
        """
        try:
            # 取旧 kind
            row = self._conn.execute(
                "SELECT kind FROM entities WHERE id = ? AND valid_until IS NULL",
                (entity_id,),
            ).fetchone()
            if not row:
                raise RuntimeError(f"canonical_fact entity {entity_id} not found")
            old_kind = row["kind"]

            # 更新 kind (P3 §2.3 简化实现: 不拆 entity, 只改 kind 标签)
            self._conn.execute(
                "UPDATE entities SET kind = 'concept' WHERE id = ?",
                (entity_id,),
            )

            self._conn.execute(
                """
                INSERT INTO audit_log
                    (run_id, pass_name, action_type, ref_type, ref_id,
                     before_json, after_json, confidence, llm_used, status,
                     created_at, revert_sql)
                VALUES (?, 'promote', 'demote_canonical', 'entity', ?,
                        ?, ?, 1.0, 0, 'applied', ?, ?)
                """,
                (
                    run_id,
                    entity_id,
                    json.dumps({"kind": old_kind}, ensure_ascii=False),
                    json.dumps({"kind": "concept", "reason": reason}, ensure_ascii=False),
                    ts,
                    f"UPDATE entities SET kind='{old_kind}' WHERE id='{entity_id}';",
                ),
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            return False

    # audit_log GC 默认 retention (8/4 review §3 L2 hygiene GC)
    _AUDIT_GC_APPLIED_DAYS = 90
    _AUDIT_GC_SKIPPED_DAYS = 30
    _AUDIT_GC_PROPOSED_DAYS = 7  # 仅当 ref_id 已 applied 才清 proposed

    def _run_audit_gc(self, dry_run: bool = False) -> Dict[str, int]:
        """[H-3 audit #5 8/4] audit_log GC 实际 v0.2 TASKS §3 L2 hygiene.

        实际策略:
          - applied + created_at < now-90d → DELETE (实际 90 天审计 trace)
          - skipped + created_at < now-30d → DELETE (skipped 不持久)
          - proposed + created_at < now-7d AND 同一 ref_id 已 applied → DELETE
            (实际: applied 留下, proposed 占位清掉)
          - reverted 不动 (实际 v0.5 §5.9.1 "被 undo 实际保留")

        Returns:
            {applied_removed, skipped_removed, proposed_removed}

        实际每 runs (8/4 实测 ~13445 行累积, 实际若 GC 开, 删 ~30% = 实际 减少 ~4000 行).
        """
        from datetime import timedelta as _td

        now = datetime.now()
        applied_cutoff = (now - _td(days=self._AUDIT_GC_APPLIED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        skipped_cutoff = (now - _td(days=self._AUDIT_GC_SKIPPED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        proposed_cutoff = (now - _td(days=self._AUDIT_GC_PROPOSED_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

        stats = {"applied_removed": 0, "skipped_removed": 0, "proposed_removed": 0}

        if dry_run:
            # 只统计, 不真删
            stats["applied_removed"] = self._exec_clean(
                """SELECT COUNT(*) FROM audit_log
                   WHERE status = 'applied' AND created_at < ?""",
                (applied_cutoff,),
            ).fetchone()[0]  # type: ignore[arg-type]
            stats["skipped_removed"] = self._exec_clean(
                """SELECT COUNT(*) FROM audit_log
                   WHERE status = 'skipped' AND created_at < ?""",
                (skipped_cutoff,),
            ).fetchone()[0]  # type: ignore[arg-type]
            stats["proposed_removed"] = self._exec_clean(
                """SELECT COUNT(*) FROM audit_log p
                   WHERE p.status = 'proposed' AND p.created_at < ?
                     AND EXISTS (
                       SELECT 1 FROM audit_log a
                       WHERE a.ref_id = p.ref_id AND a.status = 'applied'
                         AND a.run_id = p.run_id
                     )""",
                (proposed_cutoff,),
            ).fetchone()[0]  # type: ignore[arg-type]
            return stats

        # 真删 — 实际 §5.9 "每事务细粒度", GC 实际 one DELETE per status
        try:
            cur = self._exec_clean(
                """DELETE FROM audit_log
                   WHERE status = 'applied' AND created_at < ?""",
                (applied_cutoff,),
            )
            stats["applied_removed"] = cur.rowcount
        except Exception as e:
            logger.warning(f"[audit_gc] applied DELETE failed: {e}")

        try:
            cur = self._exec_clean(
                """DELETE FROM audit_log
                   WHERE status = 'skipped' AND created_at < ?""",
                (skipped_cutoff,),
            )
            stats["skipped_removed"] = cur.rowcount
        except Exception as e:
            logger.warning(f"[audit_gc] skipped DELETE failed: {e}")

        try:
            cur = self._exec_clean(
                """DELETE FROM audit_log
                   WHERE status = 'proposed' AND created_at < ?
                     AND EXISTS (
                       SELECT 1 FROM audit_log a
                       WHERE a.ref_id = audit_log.ref_id AND a.status = 'applied'
                         AND a.run_id = audit_log.run_id
                     )""",
                (proposed_cutoff,),
            )
            stats["proposed_removed"] = cur.rowcount
        except Exception as e:
            logger.warning(f"[audit_gc] proposed DELETE failed: {e}")

        self._conn.commit()
        return stats

    def _mark_digest_dirty(self) -> None:
        """[G3 8/4] TASKS_L2_DIGEST §3.3 — dirty 追踪, set meta.digest_dirty=1."""
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('digest_dirty', '1')")
        self._conn.commit()

    def _rebuild_digest(self) -> Optional[str]:
        """[G4 8/4] TASKS_L2_DIGEST §3.4 — digest chunk 生命周期 + 双时态."""
        cfg = config.config
        if not cfg.digest_enabled:
            return None
        text, line_refs, truncated = self._build_digest()
        if not text:
            return None
        ts = now()
        new_id = generate_id("chunk") + "_digest"
        metadata = json.dumps(
            {
                "digest": True,
                "line_refs": line_refs,
                "truncated": truncated,
                "built_at": ts,
            },
            ensure_ascii=False,
        )
        try:
            old_row = self._conn.execute("SELECT value FROM meta WHERE key='digest_chunk_id'").fetchone()
            old_id = old_row["value"] if old_row else None
            self._exec_clean(
                """INSERT INTO chunks
                       (id, content, source, session_id, timestamp, importance,
                        memory_type, metadata_json, valid_until)
                   VALUES (?, ?, 'digest', NULL, ?, 1.0, 'fact', ?, NULL)""",
                (new_id, text, ts, metadata),
            )
            if old_id:
                cur_meta = self._exec_clean("SELECT metadata_json FROM chunks WHERE id = ?", (old_id,)).fetchone()
                m: Dict[str, Any] = {}
                if cur_meta and cur_meta["metadata_json"]:
                    try:
                        m = json.loads(cur_meta["metadata_json"])
                    except Exception:
                        m = {}
                m["superseded_by"] = new_id
                m["superseded_at"] = ts
                self._exec_clean(
                    "UPDATE chunks SET metadata_json = ?, valid_until = ? WHERE id = ?",
                    (json.dumps(m, ensure_ascii=False), ts, old_id),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('digest_chunk_id', ?)",
                (new_id,),
            )
            self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('digest_dirty', '0')")
            self._conn.commit()
            return new_id
        except Exception as e:
            logger.warning(f"[rebuild_digest] 实际 错误: {e}")
            return None

    def get_digest(self, ref: Optional[str] = None) -> Dict[str, Any]:
        """[G5 8/4] TASKS_L2_DIGEST §3.5 — 双模式."""
        cfg = config.config
        if not cfg.digest_enabled:
            return {"enabled": False, "content": "", "line_refs": {}, "truncated": False, "built_at": None}
        meta_dirty = self._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()
        is_dirty = meta_dirty and meta_dirty["value"] == "1"
        chunk_id_row = self._conn.execute("SELECT value FROM meta WHERE key='digest_chunk_id'").fetchone()
        chunk_id = chunk_id_row["value"] if chunk_id_row else None
        if is_dirty or not chunk_id:
            chunk_id = self._rebuild_digest()
        if not chunk_id:
            return {"enabled": True, "content": "", "line_refs": {}, "truncated": False, "built_at": None}
        row = self._exec_clean("SELECT content, metadata_json FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            return {"enabled": True, "content": "", "line_refs": {}, "truncated": False, "built_at": None}
        meta_obj: Dict[str, Any] = {}
        if row["metadata_json"]:
            try:
                meta_obj = json.loads(row["metadata_json"])
            except Exception:
                meta_obj = {}
        line_refs = meta_obj.get("line_refs", {})
        truncated = meta_obj.get("truncated", False)
        built_at = meta_obj.get("built_at")
        if ref is None:
            return {
                "enabled": True,
                "content": row["content"],
                "chunk_id": chunk_id,
                "line_refs": line_refs,
                "truncated": truncated,
                "built_at": built_at,
            }
        ref_ids = line_refs.get(str(ref))
        if not ref_ids:
            return {"error": f"ref {ref} not found", "chunk_id": chunk_id}
        source_chunks = []
        for rid in ref_ids:
            entity_row = self._exec_clean(
                "SELECT id, name, summary, kind, importance FROM entities WHERE id = ?",
                (rid,),
            ).fetchone()
            if entity_row:
                source_chunks.append(
                    {
                        "type": "entity",
                        "id": entity_row["id"],
                        "name": entity_row["name"],
                        "summary": entity_row["summary"],
                        "importance": entity_row["importance"],
                    }
                )
            else:
                ck_row = self._exec_clean(
                    "SELECT id, content, memory_type, importance, timestamp FROM chunks WHERE id = ?",
                    (rid,),
                ).fetchone()
                if ck_row:
                    source_chunks.append(
                        {
                            "type": "chunk",
                            "id": ck_row["id"],
                            "content": ck_row["content"],
                            "memory_type": ck_row["memory_type"],
                            "importance": ck_row["importance"],
                            "timestamp": ck_row["timestamp"],
                        }
                    )
        return {
            "enabled": True,
            "ref": ref,
            "chunk_id": chunk_id,
            "source_chunks": source_chunks,
        }

    def _build_digest(self) -> Tuple[str, Dict[str, List[str]], bool]:
        """[G2 8/4] TASKS_L2_DIGEST §3.2 — 三块 + line_refs.

        纯规则, 无 LLM (§0 v0.2 拍板: deterministic).
        Returns:
            (text, line_refs, truncated)
        """
        cfg = config.config
        max_chars = cfg.digest_max_chars
        recent_window_days = cfg.digest_recent_window_days
        importance_threshold = cfg.digest_importance_threshold

        block1_lines = []
        block1_refs = {}
        n = 0
        try:
            for f in self._exec_clean(
                """SELECT id, name, summary, importance FROM entities
                   WHERE kind='identity_fact' AND valid_until IS NULL
                   ORDER BY importance DESC LIMIT 50"""
            ).fetchall():
                n += 1
                val = f["summary"] or f["name"]
                block1_lines.append(f"身份: {val}")
                block1_refs[str(n)] = [f["id"]]
        except Exception as e:
            logger.debug(f"[build_digest] identity 块 1 实际 错误: {e}")

        block2_lines = []
        block2_chunk_ids = []
        cutoff = (datetime.now() - timedelta(days=recent_window_days)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            for c in self._exec_clean(
                """SELECT id, content, memory_type FROM chunks
                   WHERE valid_until IS NULL
                     AND memory_type IN ('decision', 'episode')
                     AND importance >= ?
                     AND timestamp >= ?
                   ORDER BY importance DESC LIMIT 20""",
                (importance_threshold, cutoff),
            ).fetchall():
                head = (c["content"] or "").split("\n")[0][:50]
                block2_lines.append(f"{c['memory_type']}: {head}")
                block2_chunk_ids.append(c["id"])
        except Exception as e:
            logger.debug(f"[build_digest] chunks 块 2 实际 错误: {e}")

        block3_lines = []
        block3_chunk_ids = []
        try:
            for s in self._exec_clean(
                """SELECT id, content FROM chunks
                   WHERE valid_until IS NULL AND source != 'digest'
                   ORDER BY timestamp DESC LIMIT 5"""
            ).fetchall():
                head = (s["content"] or "").split("\n")[0][:50]
                block3_lines.append(f"近期: {head}")
                block3_chunk_ids.append(s["id"])
        except Exception as e:
            logger.debug(f"[build_digest] chunks 块 3 实际 错误: {e}")

        # [8/6 M4 digest 集成 §4.4] block4 — 未闭环 task + dormant loop
        # 用 task_states.list_active_tasks_and_loops + render_digest_block4.
        from task_states import list_active_tasks_and_loops as _ts_list_active
        from task_states import render_digest_block4 as _ts_render_b4

        try:
            active_block = _ts_list_active(self._conn, now=None, stale_days_threshold=7, limit=50)
            block4_lines, block4_refs = _ts_render_b4(active_block)
        except Exception as e:
            logger.debug(f"[build_digest] block4 (active loops) 实际 错误: {e}")
            block4_lines = []
            block4_refs = {}

        all_lines = block1_lines + block2_lines + block3_lines + block4_lines
        full_text = "\n".join(all_lines)
        truncated = len(full_text) > max_chars
        if truncated:
            full_text = full_text[:max_chars]

        all_refs: Dict[str, List[str]] = dict(block1_refs)
        cur_n = len(block1_lines)
        for cid in block2_chunk_ids:
            cur_n += 1
            all_refs[str(cur_n)] = [cid]
        for cid in block3_chunk_ids:
            cur_n += 1
            all_refs[str(cur_n)] = [cid]
        # block4 refs 拼接
        for _k, v in block4_refs.items():
            cur_n += 1
            all_refs[str(cur_n)] = v

        return full_text, all_refs, truncated

    def stats(self) -> Dict:  # noqa: F811 — 2 个 stats() 是 design decision (8/4 396c432 修过时保留, 第 2 个是 hygiene 版)
        """统计 + [H-1 §6.5 v0.2 TASKS] hygiene 子键.

        [§6.5 工具收敛] 不新加 memory_hygiene_stats; 这里是 stats 的 hygiene 子键
        """
        stats = {}
        for t in self._ALLOWED_TABLES:  # 永远是 3 个白名单字符串
            total = self._conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            active = self._conn.execute(f"SELECT count(*) FROM {t} WHERE valid_until IS NULL").fetchone()[0]
            stats[t] = {"total": total, "active": active, "deleted": total - active}
        # [8/5] vectors 按实际 search 后端计数 (usearch/zvec 下 sqlite_vec 的 vectors 表恒 0)
        try:
            stats["vectors"] = self._index.size()
        except Exception as e:
            logger.warning(f"[stats] search index size failed: {e}")
            stats["vectors"] = 0
        stats["recall_log"] = self._conn.execute("SELECT count(*) FROM recall_log").fetchone()[0]

        # [H-1 §6.5] hygiene 子键 (§1.1 v0.12 + TASK v0.2): 不新加 memory_hygiene_stats
        floor = self._l2_get("l2.importance_floor", self._L2_DEFAULTS["importance_floor"])
        decay_candidates = self._exec_clean(
            """SELECT COUNT(*) FROM chunks
               WHERE valid_until IS NULL
                 AND importance > 0 AND importance < ?
                 AND memory_type != 'procedure'""",
            (floor * 3,),
        ).fetchone()[0]  # type: ignore[arg-type]
        decay_floor_chunks = self._exec_clean(
            "SELECT COUNT(*) FROM chunks WHERE valid_until IS NULL AND importance <= ?",
            (floor,),
        ).fetchone()[0]  # type: ignore[arg-type]  # noqa
        purge_backlog = self._exec_clean("SELECT COUNT(*) FROM purged_queue WHERE done=0").fetchone()[0]  # type: ignore[arg-type]
        audit_log_total = self._exec_clean("SELECT COUNT(*) FROM audit_log").fetchone()[0]  # type: ignore[arg-type]
        freshness = self._exec_clean(
            """SELECT COALESCE(
                 CAST(SUM(CASE WHEN datetime(timestamp) >= datetime('now', '-30 days') THEN 1 ELSE 0 END) AS REAL)
                 / NULLIF(COUNT(*), 0), 0.0) AS freshness
               FROM chunks WHERE valid_until IS NULL"""
        ).fetchone()["freshness"]
        # [H4 §3.4] purge_candidates: 现在可被 purge 的 chunk 数 (TTL 过期 + 仍 active).
        # 跟 purge_backlog (已在 purged_queue, 等 30 天延迟) 区分 — 这是待入队的候选.
        # 每个 memory_type 用自己的 TTL 下界 (memory.py:1612 _MEMORY_TYPE_TTL_DAYS) —
        # 聚合求和, 跟 run_maintenance Phase 2 报告数严格对齐 (reviewer P1-1).
        per_type = []
        params = []
        for _mtype, _ttl in self._MEMORY_TYPE_TTL_DAYS.items():
            if _ttl is None:
                continue  # procedure 永久
            per_type.append(f"SELECT COUNT(*) AS n FROM chunks WHERE valid_until IS NULL AND memory_type = '{_mtype}' AND timestamp < datetime('now', ?)")
            params.append(f"-{_ttl} days")
        if per_type:
            union_sql = " UNION ALL ".join(per_type)
            row = self._conn.execute(f"SELECT COALESCE(SUM(n), 0) FROM ({union_sql})", params).fetchone()
            purge_candidates = row[0] if row else 0  # type: ignore[index]
        else:
            purge_candidates = 0
        stats["hygiene"] = {
            "importance_floor": floor,
            "decay_candidates": decay_candidates,
            "decay_floor_chunks": decay_floor_chunks,
            "purge_candidates": purge_candidates,
            "purge_backlog": purge_backlog,
            "audit_log_total": audit_log_total,
            "freshness": float(freshness or 0.0),
            "last_run_hygiene": self._l2_get("l2.last_run.hygiene"),
            "last_dry_run_hygiene": self._l2_get("l2.last_dry_run.hygiene"),
        }
        return stats


# === 自测 ===
if __name__ == "__main__":
    import time

    with Memory() as m:
        # Use unique demo entities so this __main__ block doesn't collide
        # with real data in LIVE DB. The 'main_block_demo_<ts>:' suffix
        # ensures each run starts fresh.
        ts = int(time.time())
        # [8/9 P1 follow-up] demo_stock 用 host: namespace 防 validation reject.
        # 旧 non-namespaced id + kind='stock' 触发 ValidationError (require namespace
        # 或 kind IN whitelist). 改 host: namespace — 8/9 决定 host prefix 表外部 data.
        demo_stock = f"host:stock_demo_{ts}"
        demo_person = f"host:person_demo_{ts}"
        demo_source = f"main_block_demo_{ts}"

        # 1. remember
        cid = m.remember(
            content=f"测试: {demo_stock} 建仓 12000 @ 18.96",
            source=demo_source,
            importance=0.9,
            entities=[
                {
                    "id": demo_stock,
                    "kind": "stock",
                    "name": "demo stock",
                    "aliases": ["demo stock", "DS"],
                    "properties": {"ticker": demo_stock, "sector": "demo"},
                },
                {"id": demo_person, "kind": "person", "name": "demo person"},
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
            print(f"  - {r['method']} | score={r.get('rrf_score', r.get('distance', '?')):.3f} | {r['content'][:60]}")

        # 4. graph_query
        graph = m.graph_query(demo_stock, max_hops=2)
        print(f"✅ graph_query → {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

        # 5. stats
        stats = m.stats()
        print(f"✅ stats: {stats}")

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
