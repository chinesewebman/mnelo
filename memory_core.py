#!/usr/bin/env python3
"""memory_core.py — MemoryCore class: __init__ + CRUD + 4-way recall + graph + entities.

[refactor 2026-08-12] Split from memory.py (was 3853 lines monolithic, PR #11 benchmarks
子包拆分同样先例). Mixin pattern — composed in memory.py via:

    from memory_core import MemoryCore
    from digest_mixin import DigestMixin
    from audit_mixin import AuditMixin
    from l2_maintenance import L2MaintenanceMixin

    class Memory(MemoryCore, DigestMixin, AuditMixin, L2MaintenanceMixin):
        pass

This file contains the MemoryCore class body only.
Module-level helpers (DB_PATH, now, detect_query_intent, etc) remain in memory.py facade.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

# [8/15 E-A fix] 不在 module-level `from memory import _with_row_factory` —
# 触发 circular import (memory.py 558 调 `from memory_core import MemoryCore`,
# memory_core 顶部 import memory → 部分初始化). 改方法内 lazy `from memory import _with_row_factory`
# (跟 P1 #36 facade top-level import 占 dict 实证证伪同源).

# [8/15 E-A] 别名, 给 get_all 内部用 _sqlite.Row 避免 ruff I001 (跟文件顶部 import 冲突)
_sqlite = sqlite3

import config
import config as _config_module
from embedder import embed_bytes
from metrics import get_registry as _metrics_registry
from validation import (
    ValidationError,
    validate_chunk_content,
    validate_entity_payload,
    validate_id,
    validate_query,
)

logger = logging.getLogger("mnelo")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# Module-level globals (也保留在 memory.py facade — 两边值一致 = 单源真相, 通过
# `_config_module.resolve_db_path()` 计算). 避免 mixin 内的 default param 解析失败.
DB_PATH = _config_module.resolve_db_path()

# [P0 2026-08-11] scoping IDs — sentinel for 'agent_id filter not passed'.
_MISSING = object()

# [P0 §3.0] Memory type taxonomy — reuse validation.MEMORY_TYPES (single source of truth).

# Module-level helpers — 不在 module-level `from memory import ...` (会触发循环 import
# 当 memory.py partial-init 时). 改成 class body 内的 `from memory import X` lazy import
# 或方法内 lazy. helpers 仍在 memory.py facade 单源真相.
# 例: __init__ 用 _load_vec0_module → 改 `from memory import _load_vec0_module` 内联.


class MemoryCore:
    """核心 CRUD 接口."""

    def __init__(self, db_path: Path = DB_PATH):
        from memory import _load_vec0_module  # lazy import — avoid circular at module load

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
                # [bug fix D1 2026-08-16] Register Python-side ISO 8601 helpers for
                # SQL defaults. Pre-fix: `datetime('now', 'localtime')` returns
                # 'YYYY-MM-DD HH:MM:SS' (space-sep) but Python `now()` returns
                # 'YYYY-MM-DDTHH:MM:SS' (T-sep) — same instant, different format.
                # String compare `created_at > now()` lex-says space < T → recent
                # rows missed by L2 age queries, asof, audit_log etc.
                # Fix: register `iso_now()` + `iso_now_offset(days)` SQL functions
                # that return Python-equivalent ISO 8601 with T separator. schema.sql
                # uses these for new defaults.
                def _iso_now_local() -> str:
                    """SQL function: return current local time as ISO 8601 T-sep."""
                    from memory import now as _now

                    return _now("local")

                def _iso_now_offset(days: int) -> str:
                    """SQL function: return now + N days as ISO 8601 T-sep."""
                    from datetime import datetime, timedelta

                    from memory import now as _now

                    base = datetime.fromisoformat(_now("local"))
                    return (base + timedelta(days=days)).isoformat(timespec="seconds")

                self._conn.create_function("iso_now", 0, _iso_now_local)
                self._conn.create_function("iso_now_offset", 1, _iso_now_offset)
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

        # [bug fix D1 2026-08-16] Also re-register on every conn (idempotent —
        # create_function on an already-registered name is a no-op). Needed when
        # a test fixture pre-loads schema.sql via raw sqlite3 (so the schema-load
        # branch above is skipped) and Memory() opens a fresh conn that has no
        # iso_now. _migrate_schema's INSERT OR REPLACE meta (uses iso_now() default
        # from schema.sql) would fail without this.
        def _iso_now_local2() -> str:
            from memory import now as _now

            return _now("local")

        def _iso_now_offset2(days: int) -> str:
            from datetime import datetime, timedelta

            from memory import now as _now

            base = datetime.fromisoformat(_now("local"))
            return (base + timedelta(days=days)).isoformat(timespec="seconds")

        self._conn.create_function("iso_now", 0, _iso_now_local2)
        self._conn.create_function("iso_now_offset", 1, _iso_now_offset2)
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
        from memory import now  # lazy import — avoid circular at module load

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
        """Close the underlying SQLite connection + search index.

        [bug fix B1+B2 2026-08-16]
        - B2: wrap conn close in try/finally so it always runs even if index
          close raises. Previously, an index.close() raise would skip conn.close()
          and leak the file handle + WAL lock.
        - B2: wrap conn close in try/except so a sqlite close failure doesn't
          propagate to caller (we're best-effort during shutdown).
        - B1: purge self._conn from _txn_depth_by_id so id() reuse doesn't
          inherit stale depth state on the next Memory() instance.
        """
        # [bug fix B1 2026-08-16] track conn_id for dict cleanup
        conn_id = id(self._conn)
        try:
            try:
                self._index.close()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory.close] index close failed: {e}")
            # [bug fix B2 2026-08-16] ensure conn close runs even if index raised
        finally:
            try:
                self._conn.close()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[memory.close] conn close failed: {e}")
            # [bug fix B1 2026-08-16] purge txn-depth dict (avoids id() reuse pollution)
            try:
                from memory import _txn_depth_by_id

                _txn_depth_by_id.pop(conn_id, None)
            except ImportError:
                pass  # memory module not imported (shouldn't happen in normal flow)

    def __enter__(self) -> "Memory":  # noqa: F821  forward ref to composed class
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
        # [8/15 E-C.2] 规则化 mention 解析. 主人 6/29 “不抢决策” 原则:
        # auto_relate=False (默认) 行为不变, 默认 backward-compat.
        # auto_relate=True 后, 扫 content 里 @entity_id / #tag explicit mention,
        # 自动 create/check (dedup 默认 True) tag entity + relation.
        auto_relate: bool = False,
        entity_relation: str = "mentions",
        tag_relation: str = "tagged",
        dedup_check: bool = False,
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
        from memory import _enforce_entity_namespace_guard, _temporal_class_for_validity, _txn, clamp01, generate_id, norm_memory_type, now  # lazy import — avoid circular at module load

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

        # [8/15 E-1] 显式事务包裹整个写入序列 (chunk + entities + relations +
        # vector + PII audit). 任何步骤异常 → ROLLBACK → 数据一致.
        # index.add 失败 → SQLite ROLLBACK → chunk 不入库, vector 也没污染 ✓
        with _txn(self._conn):
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
            # [8/16 E-2] 手动 FTS5 sync (跟随 chunks.rowid INTEGER · 避免 trigger SIGSEGV)
            from memory import _fts_sync_upsert

            chunk_rowid = self._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()["rowid"]
            _fts_sync_upsert(self._conn, chunk_rowid, content, source, session_id)

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
            # [8/15 E-1] index.add 在事务内, 失败 → SQLite ROLLBACK → vector 没污染
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

            # 5. [8/15 E-C.2] @entity_id / #tag mention 解析 + 自动 relation.
            # 规则化, 不调 LLM. 主人 6/29 不抢决策原则: auto_relate=False (默认) 行为
            # 不变. auto_relate=True 后扫 content 提取 @entity_id / #tag mention,
            # 自动建/查 (dedup 默认 True) tag entity (kind="tag") + relation.
            # 不同/eve dedup_check / tag_entity 同 kind dedup (同一个 #tag 复用 同 entity_id).
            if auto_relate:
                from memory import _extract_mentions

                entity_mentions, tag_mentions = _extract_mentions(content)
                # Refresh ts 内部使用 (ts 在 _txn 外定义, but 这里在 with 块内,
                # 使用另一个被 _txn 上下文隔离的本地变量名以免与 _txn by ghost.
                # 踩到一个起名: 不重复定义, 直接使用 ts.
                _ar_ts = ts
                # 5a. entity mentions: chunk -[entity_relation]-> entity
                for eid in entity_mentions:
                    # 验证 entity_id 合法 (validate_id)
                    try:
                        validate_id(eid, "entity_id")
                    except Exception as e:
                        logger.warning(f"[auto_relate] skip invalid entity_id {eid!r}: {e}")
                        continue
                    # 检查 entity 是否存在
                    existing = self._conn.execute(
                        "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
                        (eid,),
                    ).fetchone()
                    if not existing:
                        logger.warning(f"[auto_relate] skip undeclared entity_id {eid!r} (entity not found)")
                        continue
                    # 检查 relation 是否存在 (dedup)
                    if dedup_check:
                        existing_rel = self._conn.execute(
                            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND relation=? AND valid_until IS NULL LIMIT 1",
                            (chunk_id, eid, entity_relation),
                        ).fetchone()
                        if existing_rel:
                            continue
                    self._conn.execute(
                        """
                        INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                               valid_from, valid_until, source, confidence, evidence_chunk_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            chunk_id,
                            eid,
                            entity_relation,
                            1.0,
                            "{}",
                            _ar_ts,
                            None,
                            source,
                            1.0,
                            chunk_id,
                        ),
                    )
                # 5b. tag mentions: 自动创建 tag entity (kind="tag") + relation
                for tag_name in tag_mentions:
                    tag_eid = f"tag:{tag_name}"
                    # tag entity 检查是否存在 (同 kind dedup)
                    existing_tag = self._conn.execute(
                        "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL",
                        (tag_eid,),
                    ).fetchone()
                    if not existing_tag:
                        # 创建 tag entity (以 tag:tagname 为 id, kind="tag")
                        self._upsert_entity(
                            {
                                "id": tag_eid,
                                "kind": "tag",
                                "name": tag_name,
                                "memory_type": memory_type,
                            }
                        )
                    # tag relation 检查 (dedup_check)
                    if dedup_check:
                        existing_tag_rel = self._conn.execute(
                            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND relation=? AND valid_until IS NULL LIMIT 1",
                            (chunk_id, tag_eid, tag_relation),
                        ).fetchone()
                        if existing_tag_rel:
                            continue
                    self._conn.execute(
                        """
                        INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                               valid_from, valid_until, source, confidence, evidence_chunk_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            chunk_id,
                            tag_eid,
                            tag_relation,
                            1.0,
                            "{}",
                            _ar_ts,
                            None,
                            source,
                            1.0,
                            chunk_id,
                        ),
                    )
        # _txn() 退出时已 COMMIT
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
        dedup_check: bool = False,
    ) -> int:
        """新建一条关系.

        [8/15 E-B] dedup_check 选项 (借鉴 Mem0 add_relations 行为 + DESIGN §3.7.1):
        - dedup_check=False (默认) → 行为不变, 允许重复 (backward-compat).
        - dedup_check=True → 三元组 (source_id, target_id, relation) 重复时
          返已有 relation_id, 不创建新行 (no-op). 软删 (valid_until 非 NULL)
          不算 dedup 命中 (历史已"消亡" — 允许重建).
        主人 6/29 不抢决策: dedup 默认 False, 不破坏老 caller 行为.
        """
        from memory import _txn, clamp01, now  # lazy import — avoid circular

        # [7/19 P1-1] id 格式验证 (白名单正则)
        source_id = validate_id(source_id, "source_id")
        target_id = validate_id(target_id, "target_id")
        if evidence_chunk_id is not None:
            evidence_chunk_id = validate_id(evidence_chunk_id, "evidence_chunk_id")

        # [8/15 E-B] dedup_check: 命中 (source_id, target_id, relation) 三元组
        # + valid_until IS NULL (软删算历史, 允许重建)
        if dedup_check:
            existing = self._conn.execute(
                """
                SELECT id FROM relations
                WHERE source_id = ? AND target_id = ? AND relation = ?
                  AND valid_until IS NULL
                LIMIT 1
            """,
                (source_id, target_id, relation),
            ).fetchone()
            if existing is not None:
                # 命中 dedup → 返已有 id, 不创建新
                return int(existing["id"])

        with _txn(self._conn):
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
            new_id = cur.lastrowid
        # [7/19 v0.5.3] metrics
        _metrics_registry().relate_total.inc()
        return new_id

    def get_all(
        self,
        kind: str = None,
        relation: str = None,
        user_id: str = None,
        limit: int = 1000,
        offset: int = 0,
        include_superseded: bool = False,
    ) -> Dict:
        """[8/15 E-A] 全量 dump: 返 entities + relations + chunks 列表 + 总数.

        借鉴 Mem0 memory.get_all(user_id="alice"): 主人调试 / 看库 / 数据
        迁移的便利工具. 与 Mem0 不同: 不做 LLM 自动抽取 (主人 6/29 iron law
        "不抢决策"), 只补"主人主动看库"的入口.

        Args:
            kind: 只返指定 kind 的 entity (None = 全部).
            relation: 只返指定 relation type (None = 全部).
            user_id: scoping_ids 过滤 (P0 8/11 已落地). 不传 = 不按 user 过滤
                (返所有 user 的 entities — 主人单机调试场景). 传了 = 只返该
                user 拥有的 entity (通过 chunk 的 properties_json.agent_id 推).
            limit: 单维度返回上限 (默认 1000, 避免一次拉 5000 卡死).
            offset: 分页起点 (与 limit 配对).
            include_superseded: 默认 False = 排除 valid_until 非 NULL (软删).

        Returns:
            Dict{
                    "entities": [list of dict],
                    "relations": [list of dict],
                    "chunks": [list of dict],
                    "totals": {entities, relations, chunks 总数 (含过滤+不含分页)},
                    "limit": 1000,
                    "offset": 0,
                }

        Example:
            >>> all_data = m.get_all()
            >>> companies = m.get_all(kind="company")
            >>> located_in = m.get_all(relation="located_in")
            >>> page2 = m.get_all(limit=500, offset=500)
        """
        # [8/15 E-A fix] lazy import — module-level 会 circular import
        from memory import _with_row_factory  # noqa: E402

        # === 1. entities ===
        e_where = []
        e_params: list = []
        if not include_superseded:
            e_where.append("valid_until IS NULL")
        if kind is not None:
            e_where.append("kind = ?")
            e_params.append(kind)
        # [8/15 E-A] user_id 过滤 — entities 表无 user_id 列, 走"关联 chunks 的
        # user_id 反推". SQL: entity 必须有 ≥1 chunk 的 session_id 匹配.
        # (P0 8/11 scoping_ids 约定: chunks.session_id == user_id 简化版 — 完整
        #  agent_id 走 metadata_json.agent_id). 失败时 (无 mentioned_entities
        # 字段) → 0 entity. 这是"best effort", 不是 100% 严格 scoping.
        if user_id is not None:
            e_where.append("""id IN (
                SELECT DISTINCT je.value
                FROM chunks c, json_each(json_extract(c.metadata_json, '$.mentioned_entities')) je
                WHERE c.valid_until IS NULL
                  AND c.session_id = ?
            )""")
            e_params.append(user_id)
        e_where_sql = ("WHERE " + " AND ".join(e_where)) if e_where else ""

        # totals 用同样过滤 (不加 limit/offset)
        total_entities = self._conn.execute(
            f"SELECT COUNT(*) FROM entities {e_where_sql}",
            e_params,
        ).fetchone()[0]

        # rows 用同样过滤 + limit/offset
        with _with_row_factory(self._conn, _sqlite.Row):
            e_rows = self._conn.execute(
                f"""
                SELECT id, kind, name, summary, importance, source, properties_json,
                       valid_from, valid_until
                FROM entities {e_where_sql}
                ORDER BY kind, name
                LIMIT ? OFFSET ?
            """,
                (*e_params, limit, offset),
            ).fetchall()
            entities = [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "summary": r["summary"],
                    "importance": float(r["importance"] or 0.5),
                    "source": r["source"],
                    "valid_from": r["valid_from"],
                    "valid_until": r["valid_until"],
                }
                for r in e_rows
            ]

        # === 2. relations ===
        r_where = []
        r_params: list = []
        if not include_superseded:
            r_where.append("valid_until IS NULL")
        if relation is not None:
            r_where.append("relation = ?")
            r_params.append(relation)
        r_where_sql = ("WHERE " + " AND ".join(r_where)) if r_where else ""

        total_relations = self._conn.execute(
            f"SELECT COUNT(*) FROM relations {r_where_sql}",
            r_params,
        ).fetchone()[0]

        with _with_row_factory(self._conn, _sqlite.Row):
            r_rows = self._conn.execute(
                f"""
                SELECT id, source_id, target_id, relation, weight, confidence,
                       valid_from, valid_until, source, evidence_chunk_id
                FROM relations {r_where_sql}
                ORDER BY relation, source_id
                LIMIT ? OFFSET ?
            """,
                (*r_params, limit, offset),
            ).fetchall()
            relations = [
                {
                    "id": r["id"],
                    "source_id": r["source_id"],
                    "target_id": r["target_id"],
                    "relation": r["relation"],
                    "weight": float(r["weight"] or 1.0),
                    "confidence": float(r["confidence"] or 1.0),
                    "valid_from": r["valid_from"],
                    "valid_until": r["valid_until"],
                    "source": r["source"],
                    "evidence_chunk_id": r["evidence_chunk_id"],
                }
                for r in r_rows
            ]

        # === 3. chunks ===
        c_where = []
        c_params: list = []
        if not include_superseded:
            c_where.append("valid_until IS NULL")
        c_where_sql = ("WHERE " + " AND ".join(c_where)) if c_where else ""

        total_chunks = self._conn.execute(
            f"SELECT COUNT(*) FROM chunks {c_where_sql}",
            c_params,
        ).fetchone()[0]

        with _with_row_factory(self._conn, _sqlite.Row):
            c_rows = self._conn.execute(
                f"""
                SELECT id, content, source, timestamp, importance, created_at, valid_until
                FROM chunks {c_where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """,
                (*c_params, limit, offset),
            ).fetchall()
            chunks = [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "source": r["source"],
                    "timestamp": r["timestamp"],
                    "importance": float(r["importance"] or 0.5),
                    "created_at": r["created_at"],
                    "valid_until": r["valid_until"],
                }
                for r in c_rows
            ]

        return {
            "entities": entities,
            "relations": relations,
            "chunks": chunks,
            "totals": {
                "entities": total_entities,
                "relations": total_relations,
                "chunks": total_chunks,
            },
            "limit": limit,
            "offset": offset,
        }

    # === [§1.2 #5 P1 #92 fix] ============================================
    # list_entities / search_relations 重构 raw-SQL handler (避免 mcp_tool_handlers.py
    # 重复 raw SQL · 不走 Memory 类· 无 namespace guard + pagination)。
    # ============================================================

    # [§1.2 #5 P1 #92.1 fix] limit 上限 100 (防大查询 hang)· 调用方在 mcp_tool_dispatcher 可加输入 limit
    _LIST_ENTITIES_LIMIT_MAX = 100

    def list_entities(
        self,
        kind: Optional[str] = None,
        min_importance: float = None,
        limit: int = 50,
        offset: int = 0,
        user_id: str = None,
        agent_id: str = None,
    ) -> List[Dict]:
        """[§1.2 #5 P1 #92 fix] List entities · 走 Memory 类 · 统一 namespace + pagination + filter.

        不再是 raw SQL · 走 Memory.list_entities · 可在 mcp_tool_dispatcher 调用.
        不再是 _CUSTOM_HANDLERS _handle_list_entities · 避免§1.2 #5 协议层 raw-SQL 旁路。

        Args:
            kind: filter by entity kind (如 'stock', 'identity_fact')。None = 不过滤
            min_importance: 最低重要性阈值 [0.0, 1.0]。None = 不过滤
            limit: 最大返回数 (default 50, 上限 100 · 防 hang)
            offset: 分页 offset (default 0)
            user_id: filter by metadata_json.user_id (Mem0 scope)
            agent_id: filter by metadata_json.agent_id

        Returns:
            List[Dict] · each dict has id/kind/name/summary/importance
        """
        # limit cap (P1 #92.3)
        limit = max(1, min(int(limit or 50), self._LIST_ENTITIES_LIMIT_MAX))
        offset = max(0, int(offset or 0))

        sql = "SELECT id, kind, name, summary, importance FROM entities WHERE valid_until IS NULL"
        params = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if min_importance is not None:
            sql += " AND importance >= ?"
            params.append(float(min_importance))
        if user_id:
            sql += " AND json_extract(metadata_json, '$.user_id') = ?"
            params.append(user_id)
        if agent_id:
            sql += " AND json_extract(metadata_json, '$.agent_id') = ?"
            params.append(agent_id)
        sql += " ORDER BY importance DESC, valid_from DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(sql, params).fetchall()
        # 不走 _hit_dict (entity 走中 metadata · _hit_dict 是 chunks recall 用的)。
        entities = [
            {
                "id": r[0],
                "kind": r[1],
                "name": r[2],
                "summary": r[3],
                "importance": float(r[4] or 0.5),
            }
            for r in rows
        ]
        # [8/16 P1 #93 fix] 吞后向兼容 test_dead_code_round13 · 原 _handle_list_entities 返 {"entities": [...], "count": N}
        return {"entities": entities, "count": len(entities)}

    # [§1.2 #5 P1 #92.5 fix] search_relations 上限 100 (同 list_entities)
    _SEARCH_RELATIONS_LIMIT_MAX = 100

    def search_relations(
        self,
        relation: str,
        asof: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[Dict]:
        """[§1.2 #5 P1 #92 fix] Search relations by type + time-as-of + pagination.

        Args:
            relation (required): relation type string (如 'owns', 'references')
            asof: ISO 8601 timestamp, default = now()
            limit: 最大返回数 (default 100, 上限 100)
            offset: 分页 offset
            source_id / target_id: 可选 filter

        Returns:
            List[Dict] · each has id/source_id/target_id/relation/weight/valid_from/valid_until
        """
        from memory import now as _now_helper  # lazy · P1 #63 避免 circular

        limit = max(1, min(int(limit or 100), self._SEARCH_RELATIONS_LIMIT_MAX))
        offset = max(0, int(offset or 0))
        asof = asof or _now_helper()

        sql = "SELECT id, source_id, target_id, relation, weight, valid_from, valid_until FROM relations WHERE relation = ? AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)"
        params = [relation, asof, asof]
        if source_id:
            sql += " AND source_id = ?"
            params.append(source_id)
        if target_id:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY weight DESC, valid_from DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(sql, params).fetchall()
        relations = [
            {
                "id": r[0],
                "source_id": r[1],
                "target_id": r[2],
                "relation": r[3],
                "weight": float(r[4] or 0.0),
                "valid_from": r[5],
                "valid_until": r[6],
            }
            for r in rows
        ]
        # [8/16 P1 #93 fix] 吞后向兼容 test_dead_code_round13 · 原 _handle_search_relations 返 {"relations": [...], "count": N}
        return {"relations": relations, "count": len(relations)}

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
        from memory import _txn, clamp01, generate_id, now  # lazy import — avoid circular at module load

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

        # [8/15 E-1] 显式事务包裹 update 全流程: 新 chunk + 老 chunk supersede +
        # 老 chunk metadata.temporal_class=historical + index.remove(old) +
        # index.add(new). 任何步骤异常 → ROLLBACK → 老 chunk valid_until
        # 仍为 NULL, 新 chunk 不入库, 不留不一致状态.
        #
        # 之前 bug: line 644-648 `try/except` 静默吞 embed 异常, 导致 index.add
        # 失败时老 chunk 已被 superseded, 新 chunk 已入库但 vector 缺席,
        # 召回断裂. 修复: 改用 _txn() 让 SQLite 数据回滚, 异常正常上抛供
        # 调用方感知.
        with _txn(self._conn):
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
            # [8/16 E-2] 手动 FTS5 sync 新 chunk (老 chunk valid_until 设为 now · stale row 过滤)
            from memory import _fts_sync_upsert

            new_rowid = self._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (new_id,)).fetchone()["rowid"]
            _fts_sync_upsert(
                self._conn,
                new_rowid,
                new_content or old["content"],
                "update:" + reason,
                old["session_id"],
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
            v_bytes = embed_bytes(new_content_for_embed)
            self._index.add(new_id, v_bytes, conn=self._conn)
        # _txn() 退出时已 COMMIT
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
        from memory import now  # lazy import — avoid circular at module load

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
        # [8/15 P1 #84 fix] forget() 上 _txn 包裹 (SQLite 多步一致).
        # zvec _index.remove 走 try/except · raise 后以 try/except 装载.
        # 灵活策略: SQLite commit 优先, zvec native fail 仅 log warning (后台 lazy delete).
        from memory import _txn  # lazy import — P1 #63 circular 避免

        # [P1 #84.4 fix] idempotent guard: 该 target 已 soft-deleted (在 purged_queue 中)→ 跳过.
        existing = self._conn.execute(
            "SELECT 1 FROM purged_queue WHERE target_id = ? AND done = 0 LIMIT 1",
            (target_id,),
        ).fetchone()
        if existing:
            logger.debug(f"[forget P1 #84] target={target_id} 已在 purged_queue · 跳过")
            return {"edges_invalidated": 0, "queued_purge": 0}

        edges_invalidated = 0
        _now = now()
        with _txn(self._conn):
            if target_kind == "chunk":
                self._conn.execute(
                    "UPDATE chunks SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
                    (_now, target_id),
                )
            elif target_kind == "entity":
                self._conn.execute(
                    "UPDATE entities SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
                    (_now, target_id),
                )
            elif target_kind == "relation":
                self._conn.execute(
                    "UPDATE relations SET valid_until = ? WHERE id = ? AND valid_until IS NULL",
                    (_now, target_id),
                )
            else:
                raise ValueError(f"unknown kind: {target_kind}")

            # cascade (主流程中, 触发器也会自动做)
            if cascade:
                cur = self._conn.execute(
                    """
                    UPDATE relations SET valid_until = ?
                    WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL
                """,
                    (_now, target_id, target_id),
                )
                edges_invalidated = cur.rowcount

            # 入队 30 天后物理删除
            self._conn.execute(
                """
                INSERT INTO purged_queue (target_id, target_kind, purged_at, done)
                VALUES (?, ?, iso_now_offset(30), 0)
            """,
                (target_id, target_kind),
            )

        # [P1 #84.5 fix] zvec _index.remove 走 try/except · 不 raise.
        # SQLite 已 commit, graph 一致; zvec stale 由 _maintenance_run / 下次 vector add
        # 复盖同 rowid 时被 lazy 替换 (后台 worker, 不阻塞 forget 主路径).
        if target_kind == "chunk":
            try:
                self._index.remove(target_id, conn=self._conn)
            except Exception as _e:  # noqa: BLE001
                logger.warning(f"[forget P1 #84] zvec _index.remove({target_id}) fail: {_e}. 后台 lazy delete.")
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
        from memory import _load_vec0_module, now  # lazy import — avoid circular at module load

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
            # [bug fix D2 2026-08-16] Use try/finally to close recall_conns even if
            # any f.result() raises. Pre-fix: close loop was AFTER the futures
            # joined — worker exception skipped close, leaking 4 SQLite connections
            # + vec0 module registrations per failed recall. Sustained load →
            # fd exhaustion.
            try:
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
            finally:
                # 关独立连接 — finally ensures close even on worker exception
                for c in recall_conns:
                    try:
                        c.close()
                    except Exception:
                        pass  # defensive — best-effort close on shutdown

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
        from memory import norm_memory_type  # lazy import — avoid circular at module load

        q_bytes = embed_bytes(query)
        # [审计 4.3 ] filter 多时, 多取一些确保过滤后还够 top_k; strategy 也加大召回
        fetch_limit = top_k * (8 if (filters or top_k >= 3) else 2)
        knn_hits = self._index.knn(q_bytes, fetch_limit, conn=conn)
        if not knn_hits:
            return []

        # [P0 2026-08-11] scoping IDs: 一次 SQL 把 chunk 元数据 + agent_id 拿回来.
        # 在 Python 侧过滤 agent_id (避免每行一次 json_extract SQL).
        # [audit fix #7 2026-08-16] user_id / run_id filter recall 也走 Python 侧
        # post-filter (跟 agent_id 同款 — json_extract NULL 兼容旧数据).
        agent_id_filter = (filters or {}).get("agent_id")
        agent_id_filter_norm = agent_id_filter if agent_id_filter is not None else _MISSING
        user_id_filter = (filters or {}).get("user_id")
        user_id_filter_norm = user_id_filter if user_id_filter is not None else _MISSING
        run_id_filter = (filters or {}).get("run_id")
        run_id_filter_norm = run_id_filter if run_id_filter is not None else _MISSING
        _scope_filters = (
            ("agent_id", agent_id_filter_norm),
            ("user_id", user_id_filter_norm),
            ("run_id", run_id_filter_norm),
        )
        _scope_active = [pair for pair in _scope_filters if pair[1] is not _MISSING]

        # [audit fix 4.2 2026-08-16] batch fetch — 1 次 SQL 拿全 knn_hits 的 chunk
        # 原 N+1: 每 knn_hit 1 次 SELECT (top_k=10 → 10 round-trip).
        # 现在: 1 次 IN(...) SELECT 拿全 batch.
        chunk_ids = [h.chunk_id for h in knn_hits]
        placeholders = ",".join("?" * len(chunk_ids))
        chunk_rows = conn.execute(
            f"""
            SELECT id, content, memory_type, source, timestamp, importance, metadata_json FROM chunks
            WHERE id IN ({placeholders}) AND (valid_until IS NULL OR valid_until > ?)
            """,
            (*chunk_ids, asof),
        ).fetchall()
        # Build dict by chunk_id for O(1) lookup
        chunks_by_id = {r["id"]: r for r in chunk_rows}

        results = []
        for hit in knn_hits:
            # [7/21 fix] asof: chunk 在 asof 时点有效 = valid_until IS NULL OR > asof
            # [P0 2026-08-11] 同时拿 metadata_json, Python 侧 json 解析 agent_id
            chunk = chunks_by_id.get(hit.chunk_id)
            if not chunk:
                continue
            if filters:
                if "source" in filters and chunk["source"] != filters["source"]:
                    continue
                if "type" in filters and chunk["memory_type"] != norm_memory_type(filters["type"]):
                    continue
                # [P0 2026-08-11] agent_id filter — 旧数据 metadata_json=NULL
                # 或不含 agent_id → JSON 解出 None → 不等于 filter, 保留.
                # [audit fix #7 2026-08-16] 同款 pattern 扩 user_id / run_id.
                if _scope_active:
                    raw = chunk["metadata_json"]
                    if raw is None or raw == "":
                        continue
                    try:
                        meta_obj = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    # 所有 active scope filter 必须 match (any mismatch → skip)
                    if any(meta_obj.get(k) != v for k, v in _scope_active):
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
        from memory import detect_query_intent, norm_memory_type, now  # lazy import — avoid circular at module load

        # [7/21 fix] asof: 只看 asof 时点仍有效的 chunk
        # [P0 2026-08-11] scoping: agent_id 走 json_extract SQL 过滤 (NULL 不误过滤)
        # [bug fix P1 2026-08-29] ESCAPE '\\' 让 _escape_like() 的 % 和 _ 转义生效
        sql = """
            SELECT id, content, memory_type, source, timestamp, importance FROM chunks
            WHERE (valid_until IS NULL OR valid_until > ?)
              AND content LIKE ? ESCAPE '\\'
        """
        from memory import _escape_like  # [bug fix P1 2026-08-29] LIKE wildcard escape

        params = [asof, f"%{_escape_like(query)}%"]
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
        # [audit fix #7 2026-08-16] user_id / run_id 同款 json_extract SQL filter
        if filters and "user_id" in filters:
            sql += " AND json_extract(metadata_json, '$.user_id') = ?"
            params.append(filters["user_id"])
        if filters and "run_id" in filters:
            sql += " AND json_extract(metadata_json, '$.run_id') = ?"
            params.append(filters["run_id"])
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
        from memory import _escape_like, norm_memory_type, now  # [bug fix P1 2026-08-29] _escape_like

        if " " in query.strip():
            tokens = query.strip().split()
        else:
            tokens = [query]

        chunk_results = []
        seen_chunk_ids = set()
        for tok in tokens:
            if not tok or len(tok) < 2:
                continue
            # [bug fix P1 2026-08-29] escape LIKE wildcards (% _) so user's literal
            # % and _ aren't interpreted as wildcards. ESCAPE '\\' must be in SQL.
            like = f"%{_escape_like(tok)}%"
            # [7/21 fix] asof: entity 在 asof 时点有效 = valid_from <= asof AND (valid_until IS NULL OR > asof)
            # [P0 2026-08-11] LEFT JOIN relations (self-ref) → chunks 拿 metadata_json.
            sql = """
                SELECT e.id, e.name, e.kind, e.summary, e.importance, e.aliases_json, c.metadata_json AS c_meta
                FROM entities e
                LEFT JOIN relations r ON r.source_id = e.id AND r.target_id = e.id
                LEFT JOIN chunks c ON c.id = r.evidence_chunk_id
                WHERE (e.valid_from IS NULL OR e.valid_from <= ?)
                  AND (e.valid_until IS NULL OR e.valid_until > ?)
                  AND (e.name LIKE ? ESCAPE '\\' OR e.aliases_json LIKE ? ESCAPE '\\')
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
            # [audit fix #7 2026-08-16] user_id / run_id 同款 post-filter
            _entity_scope_filters = (
                ("agent_id", (filters or {}).get("agent_id")),
                ("user_id", (filters or {}).get("user_id")),
                ("run_id", (filters or {}).get("run_id")),
            )
            _entity_scope_active = [(k, v) for k, v in _entity_scope_filters if v is not None]
            if _entity_scope_active:
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
                    if all(parsed.get(k) == v for k, v in _entity_scope_active):
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
        from memory import now  # lazy import — avoid circular at module load

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
        from memory import _escape_like, detect_query_intent, norm_memory_type, now  # [bug fix P1 2026-08-29] _escape_like

        # [7/21 fix] asof: 只看 asof 时点仍有效的 chunk
        # [P0 2026-08-11] scoping: agent_id 走 json_extract SQL 过滤
        # [bug fix P1 2026-08-29] ESCAPE '\\' 让 _escape_like() 的 % 和 _ 转义生效
        sql = """
            SELECT id, content, memory_type, source, timestamp, importance FROM chunks
            WHERE (valid_until IS NULL OR valid_until > ?)
              AND content LIKE ? ESCAPE '\\'
        """
        params = [asof, f"%{_escape_like(query)}%"]
        if filters and "source" in filters:
            sql += " AND source = ?"
            params.append(filters["source"])
        if filters and "type" in filters:
            sql += " AND memory_type = ?"
            params.append(norm_memory_type(filters["type"]))
        if filters and "agent_id" in filters:
            sql += " AND json_extract(metadata_json, '$.agent_id') = ?"
            params.append(filters["agent_id"])
        # [8/16 P1 #91 fix] user_id filter 也在 metadata_json · 跟 agent_id 平行
        if filters and "user_id" in filters:
            sql += " AND json_extract(metadata_json, '$.user_id') = ?"
            params.append(filters["user_id"])
        # [8/16 audit-2 #7 fix] run_id filter 同 user_id 模式
        if filters and "run_id" in filters:
            sql += " AND json_extract(metadata_json, '$.run_id') = ?"
            params.append(filters["run_id"])
        # [8/16 E-2 重启 non-trigger] FTS5 BM25 主路 + LIKE fallback.
        # 设计哲学 (上轮 P1 #67/#68/#69/#70 实战教諛· 避免 P1 #81 SIGSEGV):
        # - BM25 路: 完整中文句 · bm25 ASC 优先·importance DESC 加权·LIMIT 外移
        # - LIKE 路: 2-char 中文短查 · 符号 token · 起這云 fallback
        # - UNION ALL 后包 subquery (避免 P1 #69 ORDER BY/LIMIT 报错)· 去重走 Python set
        from memory import _fts_escape_query  # [P1 #63] lazy · 避免 circular

        # 状态变量：source / type / agent_id / user_id / run_id filter · temporal intent · valid_until
        fts_filter_clauses = []
        fts_filter_params = []
        like_filter_clauses = []
        like_filter_params = []
        if filters and "source" in filters:
            fts_filter_clauses.append("c.source = ?")
            fts_filter_params.append(filters["source"])
            like_filter_clauses.append("source = ?")
            like_filter_params.append(filters["source"])
        if filters and "type" in filters:
            _t = norm_memory_type(filters["type"])
            fts_filter_clauses.append("c.memory_type = ?")
            fts_filter_params.append(_t)
            like_filter_clauses.append("memory_type = ?")
            like_filter_params.append(_t)
        if filters and "agent_id" in filters:
            fts_filter_clauses.append("json_extract(c.metadata_json, '$.agent_id') = ?")
            fts_filter_params.append(filters["agent_id"])
            like_filter_clauses.append("json_extract(metadata_json, '$.agent_id') = ?")
            like_filter_params.append(filters["agent_id"])
        if filters and "user_id" in filters:  # [8/16 P1 #91 fix]
            fts_filter_clauses.append("json_extract(c.metadata_json, '$.user_id') = ?")
            fts_filter_params.append(filters["user_id"])
            like_filter_clauses.append("json_extract(metadata_json, '$.user_id') = ?")
            like_filter_params.append(filters["user_id"])
        if filters and "run_id" in filters:  # [8/16 audit-2 #7 fix]
            fts_filter_clauses.append("json_extract(c.metadata_json, '$.run_id') = ?")
            fts_filter_params.append(filters["run_id"])
            like_filter_clauses.append("json_extract(metadata_json, '$.run_id') = ?")
            like_filter_params.append(filters["run_id"])
        # [P2 2026-08-11] temporal intent 加成 — 跟 _meta_recall_with_conn 同源
        intent = detect_query_intent(query)
        if intent == "upcoming":
            _now_ts = asof if asof else now()
            fts_filter_clauses.append("c.timestamp > ?")
            fts_filter_params.append(_now_ts)
            like_filter_clauses.append("timestamp > ?")
            like_filter_params.append(_now_ts)
        elif intent == "current_state":
            fts_filter_clauses.append("c.valid_until IS NULL")
            like_filter_clauses.append("valid_until IS NULL")

        fts_where = " AND ".join(["(c.valid_until IS NULL OR c.valid_until > ?)", *fts_filter_clauses]) or "(c.valid_until IS NULL OR c.valid_until > ?)"
        like_where = " AND ".join(["(valid_until IS NULL OR valid_until > ?)", *like_filter_clauses]) or "(valid_until IS NULL OR valid_until > ?)"

        # FTS5 BM25 路：SELECT id, content, ..., bm25(chunks_fts) · LIMIT 外移
        fts_sql = (
            "SELECT c.id, c.content, c.memory_type, c.source, c.timestamp, c.importance, "
            "       bm25(chunks_fts) AS fts_score "
            "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? AND " + fts_where
        )
        fts_params = [_fts_escape_query(query), asof, *fts_filter_params]
        # LIKE fallback 路：SELECT id, content, ..., 0.0 AS fts_score
        # [bug fix P1 2026-08-29] ESCAPE '\' 让 _escape_like() 的 % 和 _ 转义生效
        like_sql = "SELECT id, content, memory_type, source, timestamp, importance, 0.0 AS fts_score FROM chunks WHERE " + like_where + " AND content LIKE ? ESCAPE '\\'"
        like_params = [asof, *like_filter_params, f"%{_escape_like(query)}%"]

        # UNION ALL 包 subquery (P1 #67/#69 避免) · 去重 (P1 #70 params 数量 严格匹配)
        union_sql = f"SELECT * FROM ({fts_sql} UNION ALL {like_sql}) ORDER BY 6 ASC, 5 DESC LIMIT ?"
        # fts_sql 选 bm25 ASC (越低越相关) · ORDER BY 6 = fts_score · 5 = importance
        union_params = tuple(fts_params) + tuple(like_params) + (top_k * 2,)

        try:
            rows = self._conn.execute(union_sql, union_params).fetchall()
        except Exception:
            # FTS5 不存在或 LIKE 单路 fallback (跨 schema 兼容)
            # [bug fix P1 2026-08-29] ESCAPE '\' 让 _escape_like() 的 % 和 _ 转义生效
            rows = self._conn.execute(
                "SELECT id, content, memory_type, source, timestamp, importance "
                "FROM chunks WHERE (valid_until IS NULL OR valid_until > ?) "
                "AND content LIKE ? ESCAPE '\\' ORDER BY importance DESC, timestamp DESC LIMIT ?",
                (asof, f"%{_escape_like(query)}%", top_k),
            ).fetchall()

        # 去重：同 id 优先 FTS5 (BM25 更智能排序) · 实际 LIMIT top_k
        seen = set()
        deduped = []
        for r in rows:
            cid = r["id"]
            if cid in seen:
                continue
            seen.add(cid)
            deduped.append(r)
        rows = deduped[:top_k]
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
        from memory import _escape_like, norm_memory_type, now  # [bug fix P1 2026-08-29] _escape_like

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
            # [audit fix #7 2026-08-16] user_id / run_id 同款 post-filter
            _ent_scope_filters_1 = (
                ("agent_id", (filters or {}).get("agent_id")),
                ("user_id", (filters or {}).get("user_id")),
                ("run_id", (filters or {}).get("run_id")),
            )
            _ent_scope_active_1 = [(k, v) for k, v in _ent_scope_filters_1 if v is not None]
            if _ent_scope_active_1:
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
                    if all(parsed.get(k) == v for k, v in _ent_scope_active_1):
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
        # [audit fix #7 2026-08-16] rows 在 phase 1 (identity_query) 内才定义,
        # phase 2 独立跑 (is_identity_query=False) 时必须初始化否则 UnboundLocalError.
        if not is_identity_query:
            rows = []
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
            # [bug fix P1 2026-08-29] ESCAPE '\' 让 _escape_like() 的 % 和 _ 转义生效
            like_clauses.append("(e.name LIKE ? ESCAPE '\\' OR e.id LIKE ? ESCAPE '\\' OR e.summary LIKE ? ESCAPE '\\')")
            params.extend([f"%{_escape_like(t)}%"] * 3)

        # 两轮: 高优先级 (强 fact), 后补 concept
        high_priority_kinds = ("identity_fact", "canonical_fact", "user")

        for kind_filter, _take in (
            (high_priority_kinds, top_k),
            (("concept",), top_k),  # 补足
        ):
            # [7/21 fix] asof: (valid_from IS NULL OR valid_from <= ?) 兼容无 valid_from 的旧数据
            # [P0 2026-08-11] scoping: LEFT JOIN chunks via relations.evidence_chunk_id
            # (entity → chunk 关联在 relations 表). LEFT JOIN 让老 entity 保留 —
            # c_meta NULL → post-filter 保留 (旧数据兼容).
            # [8/29 PR-B fix] production bug: phase 2 sql + cur_params 构造后未 execute,
            # rows 保留 phase 1 的值 (is_identity_query=True 时是 identity_fact/canonical_fact/user
            # 结果, False 时是 []). 导致 kind='concept' 的 entity 永远不进 candidate set.
            # 跟 phase 1 pattern (line 1974) 对比可知漏了 execute + fetchall.
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
            rows = self._conn.execute(sql, cur_params).fetchall()
            # [P0 2026-08-11] agent_id filter — SQL 不直接 json_extract (entity
            # 可能没关联 chunk); 改 Python 侧 post-filter 同第一阶段.
            # [audit fix #7 2026-08-16] user_id / run_id 同款 post-filter
            _ent_scope_filters_2 = (
                ("agent_id", (filters or {}).get("agent_id")),
                ("user_id", (filters or {}).get("user_id")),
                ("run_id", (filters or {}).get("run_id")),
            )
            _ent_scope_active_2 = [(k, v) for k, v in _ent_scope_filters_2 if v is not None]
            if _ent_scope_active_2:
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
                    if all(parsed.get(k) == v for k, v in _ent_scope_active_2):
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

        from memory import _kind_from_source  # lazy import — avoid circular at module load

        rrf_score: Dict[str, float] = {}
        rrf_hits: Dict[str, Dict] = {}
        # [8/15 E-4] methods accumulator — 同 chunk_id 多路命中时, 必须 accumulate
        # 所有参与的 method. 之前 `rrf_hits[cid] = h` 直接覆盖导致 lane 后者
        # 覆盖前者 (e.g. entity 永远在 hit_lists 末尾, 覆盖 vector/graph/meta),
        # 污染 recall_log.recall_details_json.method 字段, 拖累 E-3
        # memory_recall_stats method 分布数据 (DESIGN §1.2 #4).
        rrf_methods: Dict[str, List[str]] = {}
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
                # [8/15 E-4] 首次见 → 直接 set, 后续 → accumulate (保持第一路 hit 数据)
                if cid not in rrf_hits:
                    rrf_hits[cid] = h
                # [8/15 E-4] accumulate methods (按遍历序, 去重)
                m = h.get("method")
                if m and m not in rrf_methods.get(cid, []):
                    rrf_methods.setdefault(cid, []).append(m)
        ranked = sorted(rrf_score.items(), key=lambda x: -x[1])
        out = []
        for cid, score in ranked[:top_k]:
            h = rrf_hits[cid]
            h["rrf_score"] = score
            # [8/15 E-4] 写入完整 methods 列表 (保留 backward-compat method 字段 = 第一路)
            h["methods"] = rrf_methods.get(cid, [h.get("method")] if h.get("method") else [])
            out.append(h)
        return out

    def _log_recall(self, query: str, results: List[Dict], hops: int, latency_ms: float):
        """[P2+ #3 7/18 patch] 写入 recall_log 审计 (always local time via now() helper).

         feedback loop 数据:
        - results_json 已存 [chunk_id] 数组 (前: 只知道命中哪些 chunk)
        - 新存 recall_details_json: top-K 完整 dict (method, distance/score, importance)
          让 daily_check / analytics 能分析 召回质量 (用什么路召回的, 距离分布)
        """
        from memory import now  # lazy import — avoid circular at module load

        # [8/15 E-4] feedback loop: 每条命中的 methods 列表 (新) + method 单字段 (backward-compat)
        # methods 列表含所有 RRF 命中的 lane (e.g. ["vector", "graph"]), 修复
        # DESIGN §1.2 #4 RRF lane 覆盖问题 — 之前 rrf_hits[cid] = h 覆盖,
        # recall_details_json.method 只记最后遍历 lane, 拖累 E-3 recall_stats.
        detail = [
            {
                "rank": i + 1,
                "chunk_id": r.get("chunk_id"),
                "method": r.get("method"),  # backward-compat: 第一路
                "methods": r.get("methods", [r.get("method")] if r.get("method") else []),
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
        from memory import now  # lazy import — avoid circular at module load

        # [7/19 P1-1] start_node 格式验证
        start_node = validate_id(start_node, "start_node")
        asof = asof or now()
        # BFS: 拿 max_hops 跳内的所有节点
        # [audit fix 4.1 2026-08-16] batch IN(...) — 1 次 SQL 拿整 frontier 节点的边
        # 原 N+1: 每 hop × 每 frontier node 1 次 SELECT. max_hops=3 × frontier=20 → 60 round-trip.
        # 现在: 每 hop 1 次 SELECT (frontier IN (...)) → 3 round-trip 总数.
        # Python-side BFS dedupe (跟原版同语义: other = NOT in frontier).
        visited = {start_node}
        frontier = [start_node]
        edges = []
        seen_edge_keys = set()  # dedupe edges by (source_id, target_id, relation)
        for _hop in range(max_hops):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            sql = f"""
                SELECT * FROM relations
                WHERE (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
                  AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)
            """
            params = [*frontier, *frontier, asof, asof]
            if edge_types:
                sql += f" AND relation IN ({','.join('?' * len(edge_types))})"
                params.extend(edge_types)
            rows = self._conn.execute(sql, params).fetchall()
            next_frontier = []
            frontier_set = set(frontier)
            for r in rows:
                src, tgt = r["source_id"], r["target_id"]
                # Dedupe edges by (src, tgt, relation)
                edge_key = (src, tgt, r["relation"])
                if edge_key in seen_edge_keys:
                    continue
                seen_edge_keys.add(edge_key)
                edges.append(dict(r))
                # BFS dedupe: other = NOT in frontier (跟原版同语义)
                other = tgt if src in frontier_set else src if tgt in frontier_set else None
                if other is not None and other not in visited:
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
        from memory import _enforce_entity_namespace_guard, clamp01, now  # lazy import — avoid circular at module load

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
            # [bug fix D3+ 2026-08-16] SELECT target_id, not id! purged_queue.id is
            # autoincrement (not the chunk/entity/relation id). Pre-fix: due_ids
            # held autoincrement ids → DELETE FROM chunks WHERE id IN (autoinc)
            # matched 0 rows → no physical delete ever happened. Plus done=1
            # never got set.
            due_sql = """
                SELECT target_id FROM purged_queue
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
            # [bug fix D3 2026-08-16] FTS5 sync delete BEFORE physical DELETE.
            # Pre-fix: chunks_fts rows orphaned forever (no caller of _fts_sync_delete
            # or _fts_sync_cleanup_stale in the codebase). FTS5 bloat, wasted search.
            # For 'chunk' kind, look up rowids FIRST, then DELETE from chunks_fts.
            if kind == "chunk":
                try:
                    fts_rowids = [r[0] for r in self._conn.execute(f"SELECT rowid FROM chunks WHERE id IN ({ph})", due_ids).fetchall()]
                    if fts_rowids:
                        fts_ph = ",".join("?" * len(fts_rowids))
                        self._conn.execute(
                            f"DELETE FROM chunks_fts WHERE rowid IN ({fts_ph})",
                            fts_rowids,
                        )
                except Exception as e:
                    logger.warning(f"[purge_worker D3] chunks_fts cleanup failed: {e}")
            cur = self._conn.execute(f"DELETE FROM {table} WHERE id IN ({ph})", due_ids)
            deleted = cur.rowcount
            stats[f"{table}_physically_deleted"] = deleted

            # set done=1 — must use target_id (the actual chunk/entity/relation id)
            cur = self._conn.execute(
                f"UPDATE purged_queue SET done = 1 WHERE target_kind = ? AND target_id IN ({ph})",
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
