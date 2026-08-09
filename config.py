"""
mnelo config — load settings from environment variables or config file.

[ 7/18 P2, 7/19 v0.5.0]
来源优先级 (高到低):
1. 环境变量 MNELO_MEMORY_* (部署, systemd/launchd)
2. 配置文件 LIVE_ROOT/config.toml (本地覆盖, 默认 ~/.hermes/memory/config.toml)
3. 默认值 (localtime)

[配置项]
- timezone: 'local' / 'utc' / 'Asia/Shanghai' (任意 IANA tz)
  默认 'local' (用系统本地时区)
- warm_up_embedder: bool
  默认 True (Memory 启动时加载 embedding 模型, 避免首次 recall 1s 冷启动)
- embedder_model: str
  默认 'BAAI/bge-small-zh-v1.5' (中文原生, 512d)
  切换: 'BAAI/bge-small-en-v1.5' (英文, 384d)
       | 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2' (50+ 语种, 384d, 含日/韩/西/法)
- embedder_dim: int
  默认 512. 必须与模型实际输出维度一致 (mnelo 用它建 sqlite-vec 表 schema)
- server.host: str
  默认 '127.0.0.1' (loopback-only, P2-1 安全防线)
- server.port: int
  默认 8086 (与 launchd plist 默认一致, 可改到 1024-65535)
"""

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("mnelo.config")
from typing import Optional

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # 3.10 and below
    except ImportError:
        tomllib = None


# [7/21 fix] LIVE_ROOT 单一事实源 — 消除硬编码的 /Users/apple/.hermes/memory。
# 解析优先级: env MNELO_MEMORY_DIR > ~/.hermes/memory (默认, 与旧部署向后兼容)。
# 所有需要 live 目录/DB 路径的模块 (memory / entity_resolve / scripts) 都从这里读。
DEFAULT_LIVE_ROOT = Path(os.environ.get("MNELO_MEMORY_DIR", str(Path.home() / ".hermes" / "memory")))
CONFIG_PATH = Path(os.environ.get("MNELO_MEMORY_CONFIG", str(DEFAULT_LIVE_ROOT / "config.toml")))


def resolve_db_path() -> Path:
    """Resolve the memory.db path: env MNELO_MEMORY_DB_PATH > MNELO_MEMORY_DIR > ~/.hermes/memory.

    Used by memory.py / entity_resolve.py / scripts to avoid hardcoding an absolute path.
    """
    env_db = os.environ.get("MNELO_MEMORY_DB_PATH")
    if env_db:
        return Path(env_db)
    env_dir = os.environ.get("MNELO_MEMORY_DIR")
    if env_dir:
        return Path(env_dir) / "memory.db"
    return DEFAULT_LIVE_ROOT / "memory.db"


def _load_config_file(path: Path) -> dict:
    """Load TOML config file. Returns empty dict if not found or parse fails."""
    if not path.exists() or tomllib is None:
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"[config] WARN: failed to load {path}: {e}", file=sys.stderr)
        return {}


def _resolve_tz(value: Optional[str]) -> str:
    """Resolve timezone setting.

    Args:
        value: 'local' / 'utc' / '<IANA tz>' / None

    Returns:
        - 'local' → use system local time (datetime.now(tz=None))
        - 'utc' → use UTC
        - '<IANA tz>' → use that tz (e.g. 'Asia/Shanghai')

    Raises:
        ValueError: invalid value
    """
    if value is None:
        return "local"  # 默认
    v = value.strip().lower()
    if v in ("local", "utc"):
        return v
    # IANA tz name (e.g. Asia/Shanghai). 不强制 import pytz, 让 datetime 自己解析
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var the way people actually type it.

    '1'/'true'/'yes'/'on' (case-insensitive) → True; anything else, including
    '0'/'false'/'no'/'' → False. Missing env → default.

    [8/5 fix] 不能用 bool(os.environ.get(...)) — 非空字符串都是 truthy,
    'false'/'0' 会错误地变成 True.
    """
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    """Loaded config singleton."""

    _instance: Optional["Config"] = None

    def __init__(self):
        self._raw = _load_config_file(CONFIG_PATH)

        # Timezone: env > file > default (local)
        self.timezone = _resolve_tz(os.environ.get("MNELO_MEMORY_TIMEZONE") or self._raw.get("timezone"))

        # Warm-up: env > file > default (True)
        warm_str = os.environ.get("MNELO_MEMORY_WARM_UP_EMBEDDER") or str(self._raw.get("warm_up_embedder", True))
        self.warm_up_embedder = warm_str.lower() not in ("false", "0", "no", "off")

        # Embedder model: env > file > default (bge-small-zh-v1.5, 512d)
        # 允许 env override (e.g. MNELO_MEMORY_EMBEDDER_MODEL=BAAI/bge-small-en-v1.5)
        # TOML key: embedder.model (嵌套 section)
        embedder_section = self._raw.get("embedder", {}) if isinstance(self._raw.get("embedder"), dict) else {}
        self.embedder_model = (
            os.environ.get("MNELO_MEMORY_EMBEDDER_MODEL")
            or embedder_section.get("model")
            or self._raw.get("embedder_model")  # 兼容旧扁平 key
            or "BAAI/bge-small-zh-v1.5"
        )

        # Embedder dim: env > file > default (512)
        # 必须与 model 实际输出维度一致 — 错配会让 sqlite-vec insert 失败
        dim_str = os.environ.get("MNELO_MEMORY_EMBEDDER_DIM") or str(embedder_section.get("dim", "")) or str(self._raw.get("embedder_dim", "")) or "512"
        try:
            self.embedder_dim = int(dim_str)
        except ValueError:
            print(f'[config] WARN: embedder_dim "{dim_str}" 不是整数, 回落 512', file=sys.stderr)
            self.embedder_dim = 512

        # [Round 2 quality audit] server.host + server.port 配置
        server_section = self._raw.get("server", {}) if isinstance(self._raw.get("server"), dict) else {}
        self.server_host = os.environ.get("MNELO_MEMORY_SERVER_HOST") or server_section.get("host") or "127.0.0.1"
        port_str = os.environ.get("MNELO_MEMORY_SERVER_PORT") or str(server_section.get("port", "")) or ""
        try:
            port = int(port_str) if port_str else 8086
            if not (1024 <= port <= 65535):
                raise ValueError(f"port {port} out of range")
        except ValueError as e:
            print(f'[config] WARN: server.port "{port_str}" invalid ({e}); 回落 8086', file=sys.stderr)
            port = 8086
        self.server_port = port

        # [7/21 fix] Storage location: env MNELO_MEMORY_DIR/MNELO_MEMORY_DB_PATH
        # > config.toml [storage].dir > ~/.hermes/memory (backward compatible).
        storage_section = self._raw.get("storage", {}) if isinstance(self._raw.get("storage"), dict) else {}
        env_dir = os.environ.get("MNELO_MEMORY_DIR")
        env_db = os.environ.get("MNELO_MEMORY_DB_PATH")
        self.db_dir = Path(env_dir or storage_section.get("dir") or DEFAULT_LIVE_ROOT)
        self.db_path = Path(env_db or (self.db_dir / "memory.db"))

        # [8/5 TASKS_BACKUP_RESTORE] backup config (env > config.toml [backup] > None).
        # Snapshot dir / retention. 缺省由 backup_db._default_snapshot_dir() 推导.
        backup_section = self._raw.get("backup", {}) if isinstance(self._raw.get("backup"), dict) else {}
        self.backup_snapshot_dir = os.environ.get("MNELO_MEMORY_BACKUP_SNAPSHOT_DIR") or backup_section.get("snapshot_dir")
        _ret_env = os.environ.get("MNELO_MEMORY_BACKUP_RETENTION")
        if _ret_env is not None:
            try:
                self.backup_retention = int(_ret_env)
            except ValueError:
                self.backup_retention = 30
        else:
            self.backup_retention = int(backup_section.get("retention", 30) or 30)
        # [8/5 fix] 布尔 env 显式解析, 否则 'false'/'0' 会变成 True
        self.backup_enabled = _env_bool("MNELO_MEMORY_BACKUP_ENABLED", backup_section.get("enabled", False))

        # [8/6 plan §1] SearchIndex 后端 (DESIGN §3.6/§8.3): 向量库必选二选一.
        # env MNELO_MEMORY_SEARCH_BACKEND > config.toml [search].backend > 'auto'.
        # 合法值 {auto, usearch, zvec}; 旧值 sqlite_vec → warning + coerce auto (升级不炸).
        search_section = self._raw.get("search", {}) if isinstance(self._raw.get("search"), dict) else {}
        backend_raw = os.environ.get("MNELO_MEMORY_SEARCH_BACKEND") or search_section.get("backend") or "auto"
        if backend_raw == "sqlite_vec":
            logger.warning("[config] search.backend='sqlite_vec' 已淘汰 (8/6 plan) — coerce 为 'auto'. 如需禁用 sqlite-vec 包可选移除, 见 plan §schema.sql vec0 段.")
            backend_raw = "auto"
        elif backend_raw not in ("auto", "usearch", "zvec"):
            logger.warning(f"[config] search.backend='{backend_raw}' 未知 — coerce 为 'auto'")
            backend_raw = "auto"
        self.search_backend = backend_raw

        # [8/5 普适化] RRF 实体 boost 的 kind 清单 — 哪些 kind 的 entity 命中给 boost。
        # 默认 ['stock'] 兼容旧行为; 用户设自己领域的 kind (如 product/category) 或 [] 禁用。
        # env MNELO_MEMORY_RECALL_BOOST_KINDS='product,location' > config.toml [recall].boost_kinds > ['stock'].
        recall_section = self._raw.get("recall", {}) if isinstance(self._raw.get("recall"), dict) else {}
        boost_kinds_raw = os.environ.get("MNELO_MEMORY_RECALL_BOOST_KINDS") or recall_section.get("boost_kinds")
        if boost_kinds_raw:
            if isinstance(boost_kinds_raw, list):
                self.recall_boost_kinds = [str(k).strip() for k in boost_kinds_raw if str(k).strip()]
            else:
                self.recall_boost_kinds = [k.strip() for k in str(boost_kinds_raw).split(",") if k.strip()]
        else:
            self.recall_boost_kinds = ["stock"]  # 默认兼容旧行为

        # [G1 8/4] TASKS_L2_DIGEST §1.4 — [digest] config block
        digest_section = self._raw.get("digest", {}) if isinstance(self._raw.get("digest"), dict) else {}
        digest_enabled_str = os.environ.get("MNELO_MEMORY_DIGEST_ENABLED") or str(digest_section.get("enabled", True))
        self.digest_enabled = digest_enabled_str.lower() not in ("false", "0", "no", "off")
        digest_max_chars_str = os.environ.get("MNELO_MEMORY_DIGEST_MAX_CHARS") or str(digest_section.get("max_chars", 2000))
        self.digest_max_chars = int(digest_max_chars_str)
        digest_recent_window_str = os.environ.get("MNELO_MEMORY_DIGEST_RECENT_WINDOW_DAYS") or str(digest_section.get("recent_window_days", 30))
        self.digest_recent_window_days = int(digest_recent_window_str)
        digest_imp_threshold_str = os.environ.get("MNELO_MEMORY_DIGEST_IMPORTANCE_THRESHOLD") or str(digest_section.get("importance_threshold", 0.8))
        self.digest_importance_threshold = float(digest_imp_threshold_str)
        digest_inject_str = os.environ.get("MNELO_MEMORY_DIGEST_INJECT_ON_INITIALIZE") or str(digest_section.get("inject_on_initialize", False))
        self.digest_inject_on_initialize = digest_inject_str.lower() not in ("false", "0", "no", "off")

        health_section = self._raw.get("health", {}) if isinstance(self._raw.get("health"), dict) else {}

        def _health_threshold(env_name: str, key: str) -> int:
            raw = os.environ.get(env_name)
            value = raw if raw is not None and raw != "" else health_section.get(key, 100)
            try:
                if isinstance(value, bool) or isinstance(value, float):
                    raise ValueError("must be an integer")
                parsed = int(value)
                if parsed < 1:
                    raise ValueError("must be >= 1")
                return parsed
            except (TypeError, ValueError) as e:
                print(f'[config] WARN: health.{key} "{value}" invalid ({e}); 回落 100', file=sys.stderr)
                return 100

        self.health_purge_backlog_threshold = _health_threshold("MNELO_MEMORY_HEALTH_PURGE_BACKLOG_THRESHOLD", "purge_backlog_threshold")
        self.health_floor_chunks_threshold = _health_threshold("MNELO_MEMORY_HEALTH_FLOOR_CHUNKS_THRESHOLD", "floor_chunks_threshold")

        # [8/9 P1-yanru] Rate limit 提到 config.toml — 不再硬编码.
        # env MNELO_MEMORY_RATE_LIMIT_MAX_PER_WINDOW / MNELO_MEMORY_RATE_LIMIT_WINDOW_SEC
        # > config.toml [rate_limit].max_per_window / .window_sec
        # > 默认 60 / 60 (历史行为).
        # 注意: 改完需重启 mcp_server 进程 (config 是模块级单例).
        rate_limit_section = self._raw.get("rate_limit", {}) if isinstance(self._raw.get("rate_limit"), dict) else {}

        def _rl_int(env_name: str, key: str, default: int, lo: int = 1) -> int:
            raw = os.environ.get(env_name)
            value = raw if raw is not None and raw != "" else rate_limit_section.get(key, default)
            try:
                parsed = int(value)
                if parsed < lo:
                    raise ValueError(f"must be >= {lo}")
                return parsed
            except (TypeError, ValueError) as e:
                print(f'[config] WARN: rate_limit.{key} "{value}" invalid ({e}); 回落 {default}', file=sys.stderr)
                return default

        self.rate_limit_max_per_window = _rl_int("MNELO_MEMORY_RATE_LIMIT_MAX_PER_WINDOW", "max_per_window", 60)
        self.rate_limit_window_sec = _rl_int("MNELO_MEMORY_RATE_LIMIT_WINDOW_SEC", "window_sec", 60, lo=1)

        # [8/9 P1-yanru] validation.py 5 个 MAX_* 常量 — 提到 config.
        # env MNELO_MEMORY_VALIDATION_MAX_* > config.toml [validation].max_* > 默认.
        # 默认等于原硬编码值 (行为不变, 仅可调).
        validation_section = self._raw.get("validation", {}) if isinstance(self._raw.get("validation"), dict) else {}

        def _val_int(env_name: str, key: str, default: int, lo: int = 1) -> int:
            raw = os.environ.get(env_name)
            value = raw if raw is not None and raw != "" else validation_section.get(key, default)
            try:
                parsed = int(value)
                if parsed < lo:
                    raise ValueError(f"must be >= {lo}")
                return parsed
            except (TypeError, ValueError) as e:
                print(f'[config] WARN: validation.{key} "{value}" invalid ({e}); 回落 {default}', file=sys.stderr)
                return default

        self.validation_max_chunk_content_bytes = _val_int("MNELO_MEMORY_VALIDATION_MAX_CHUNK_CONTENT_BYTES", "max_chunk_content_bytes", 8 * 1024)
        self.validation_max_query_bytes = _val_int("MNELO_MEMORY_VALIDATION_MAX_QUERY_BYTES", "max_query_bytes", 1024)
        self.validation_max_id_len = _val_int("MNELO_MEMORY_VALIDATION_MAX_ID_LEN", "max_id_len", 256)
        self.validation_max_entity_name_len = _val_int("MNELO_MEMORY_VALIDATION_MAX_ENTITY_NAME_LEN", "max_entity_name_len", 200)
        self.validation_max_entity_summary_len = _val_int("MNELO_MEMORY_VALIDATION_MAX_ENTITY_SUMMARY_LEN", "max_entity_summary_len", 1000)
        self.validation_max_holding_field_len = _val_int("MNELO_MEMORY_VALIDATION_MAX_HOLDING_FIELD_LEN", "max_holding_field_len", 200)

        # [8/9 P1-yanru] task_states.py stale_days_threshold — 提到 config.
        # env MNELO_MEMORY_TASK_STALE_DAYS_THRESHOLD > config.toml [task].stale_days_threshold > 7.
        task_section = self._raw.get("task", {}) if isinstance(self._raw.get("task"), dict) else {}
        _stale_env = os.environ.get("MNELO_MEMORY_TASK_STALE_DAYS_THRESHOLD")
        if _stale_env is not None and _stale_env != "":
            try:
                self.task_stale_days_threshold = int(_stale_env)
                if self.task_stale_days_threshold < 1:
                    raise ValueError("must be >= 1")
            except (TypeError, ValueError) as e:
                print(f'[config] WARN: task.stale_days_threshold "{_stale_env}" invalid ({e}); 回落 7', file=sys.stderr)
                self.task_stale_days_threshold = 7
        else:
            try:
                self.task_stale_days_threshold = int(task_section.get("stale_days_threshold", 7) or 7)
                if self.task_stale_days_threshold < 1:
                    raise ValueError("must be >= 1")
            except (TypeError, ValueError) as e:
                print(f'[config] WARN: task.stale_days_threshold "{task_section.get("stale_days_threshold")}" invalid ({e}); 回落 7', file=sys.stderr)
                self.task_stale_days_threshold = 7

        # [8/9 P1-yanru] mnelo_remote_client.py DEFAULT_TAILSCALE_HOST — 提到 config.
        # env MNELO_MEMORY_CLIENT_TAILSCALE_HOST > config.toml [client].tailscale_host > 默认.
        client_section = self._raw.get("client", {}) if isinstance(self._raw.get("client"), dict) else {}
        self.client_tailscale_host = os.environ.get("MNELO_MEMORY_CLIENT_TAILSCALE_HOST") or client_section.get("tailscale_host") or "mnelo.tail6a710.ts.net"

    @classmethod
    def load(cls) -> "Config":
        """Get the loaded config singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def config_path(self) -> Path:
        """Where the config file is being loaded from."""
        return CONFIG_PATH

    def describe(self) -> str:
        """One-line summary for startup banner."""
        digest_part = f" digest={'on' if self.digest_enabled else 'off'}/{self.digest_max_chars}c" if self.digest_enabled else ""
        return f"tz={self.timezone} warm_up={self.warm_up_embedder} embedder={self.embedder_model}/{self.embedder_dim}d{digest_part}"


# Eager load on import
config = Config.load()
