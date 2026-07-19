"""
i18n_messages.py — mnelo 中英双语 message table.

[7/18 patch F] 双语 messages
-  msg_id 是统一 key, all messages  zh + en 双语
-  contributor : 加新 msg  {key: {zh: '...', en: '...'}} shape
-  fallback :  locale miss → 'en' → msg_id ( debug)
"""

#  message table
# 规则:  message is short,  in english / 中文
MESSAGES = {
    # ============================================================
    # 启动 / 初始化 (startup / init)
    # ============================================================
    "startup.banner": {
        "zh": "━━━ mnelo 启动 ━━━",
        "en": "━━━ mnelo startup ━━━",
    },
    "startup.config_loaded": {
        "zh": "配置加载: tz={tz}, warm_up={warm}",
        "en": "Config loaded: tz={tz}, warm_up={warm}",
    },
    "startup.embedder_warmup": {
        "zh": "[P2-1] Embedder warm-up 完成 ({desc})",
        "en": "[P2-1] Embedder warm-up done ({desc})",
    },
    "startup.embedder_loaded": {
        "zh": "embedder 加载 {model} (dim={dim})...",
        "en": "embedder loading {model} (dim={dim})...",
    },
    "startup.embedder_ok": {
        "zh": "embedder OK",
        "en": "embedder OK",
    },
    # ============================================================
    # 数据库 (database)
    # ============================================================
    "db.exists": {
        "zh": "⚠️  memory.db 已存在: {path}",
        "en": "⚠️  memory.db exists: {path}",
    },
    "db.exists_hint": {
        "zh": "   如要重置, 请先删: rm {path}",
        "en": "   To reset, run: rm {path}",
    },
    "db.created": {
        "zh": "=== 1. 创建 memory.db ===",
        "en": "=== 1. Creating memory.db ===",
    },
    "db.tables_ready": {
        "zh": "✅ 已建 {count} 张表 (含 sqlite-vec)",
        "en": "✅ {count} tables created (incl. sqlite-vec)",
    },
    "db.connect": {
        "zh": "✅ db 连接成功 ({path})",
        "en": "✅ db connected ({path})",
    },
    "db.wal_mode": {
        "zh": "✅ WAL 模式 + 外键 + busy_timeout=30s",
        "en": "✅ WAL mode + foreign_keys + busy_timeout=30s",
    },
    # ============================================================
    # 健康检查 (daily_check)
    # ============================================================
    "check.banner": {
        "zh": "mnelo daily check — {ts}",
        "en": "mnelo daily check — {ts}",
    },
    "check.mcp_alive": {
        "zh": "✅ MCP server alive — PID {pid}, 启动 {uptime}",
        "en": "✅ MCP server alive — PID {pid}, uptime {uptime}",
    },
    "check.mcp_dead": {
        "zh": "❌ MCP server NOT alive (lsof 8086 无)",
        "en": "❌ MCP server NOT alive (no lsof on 8086)",
    },
    "check.wal_checkpoint": {
        "zh": "✅ WAL checkpoint — {done}/{total} pages flushed",
        "en": "✅ WAL checkpoint — {done}/{total} pages flushed",
    },
    "check.db_stats": {
        "zh": "📊 DB stats — entities {e_a}/{e_t}, chunks {c_a}/{c_t}, relations {r_a}/{r_t}, vectors {v}",
        "en": "📊 DB stats — entities {e_a}/{e_t}, chunks {c_a}/{c_t}, relations {r_a}/{r_t}, vectors {v}",
    },
    "check.db_size": {
        "zh": "💾 Size — db {db}, WAL {wal}, shm {shm}, journal_mode {mode}",
        "en": "💾 Size — db {db}, WAL {wal}, shm {shm}, journal_mode {mode}",
    },
    "check.recall_24h": {
        "zh": "📈 Recall 24h — {count} 次, 空 hits {empty} ({pct:.0f}%), latency p50={p50}ms p95={p95}ms avg={avg}ms",
        "en": (
            "📈 Recall 24h — {count} calls, empty hits {empty} ({pct:.0f}%), "
            "latency p50={p50}ms p95={p95}ms avg={avg}ms"
        ),
    },
    "check.kind_top": {
        "zh": "🏷️  Kind TOP-3 — {kinds}",
        "en": "🏷️  Kind TOP-3 — {kinds}",
    },
    "check.kind_skewed": {
        "zh": "⚠️  concept 占 {pct}% — kind 单一化, 考虑提升其他 kind 占比",
        "en": "⚠️  concept at {pct}% — kind distribution skewed, consider boosting others",
    },
    # ============================================================
    # 召回 (recall)
    # ============================================================
    "recall.skip_placeholder": {
        "zh": "⚠️  [P2+ #1] 跳过占位符 query: {query}",
        "en": "⚠️  [P2+ #1] Skipped placeholder query: {query}",
    },
    "recall.no_results": {
        "zh": '⚠️  query="{query}" 召回 0 条',
        "en": '⚠️  query="{query}" recalled 0 hits',
    },
    "recall.ok": {
        "zh": '✅ query="{query}" 召回 {n} 条 ({ms}ms)',
        "en": '✅ query="{query}" recalled {n} hits ({ms}ms)',
    },
    "recall.miss_vec0": {
        "zh": "❌ [P2+ #3] vec0 MATCH 召回 0 条 ( rowid 错位)",
        "en": "❌ [P2+ #3] vec0 MATCH recalled 0 (rowid mismatch)",
    },
    # ============================================================
    # client (api/mnelo_client.py)
    # ============================================================
    "client.banner": {
        "zh": "=== mnelo MCP 客户端自测 ===",
        "en": "=== mnelo MCP client selftest ===",
    },
    "client.ok": {
        "zh": "✅ 自测完成 — 客户端可用",
        "en": "✅ selftest done — client is live",
    },
    "client.error": {
        "zh": "❌ MCP call {tool} failed: {err}",
        "en": "❌ MCP call {tool} failed: {err}",
    },
    # ============================================================
    # entity resolve
    # ============================================================
    "entity.total": {
        "zh": "=== Entity 数量 ===",
        "en": "=== Entity counts ===",
    },
    "entity.duplicates_stock": {
        "zh": "=== 重复候选 (stock) ===",
        "en": "=== Duplicate candidates (stock) ===",
    },
    "entity.duplicates_fact": {
        "zh": "=== 重复候选 (canonical_fact) ===",
        "en": "=== Duplicate candidates (canonical_fact) ===",
    },
    # ============================================================
    # 通用 errors
    # ============================================================
    "error.out_of_range": {
        "zh": "{name} 必须在 [{lo}, {hi}],  {value}",
        "en": "{name} must be in [{lo}, {hi}], got {value}",
    },
    "error.connection": {
        "zh": "❌ MCP server 未启动: {err}",
        "en": "❌ MCP server not running: {err}",
    },
    "error.retry_failed": {
        "zh": "❌ MCP 失败重试 {n} 次: {err}",
        "en": "❌ MCP call failed after {n} retries: {err}",
    },
}
