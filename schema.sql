-- ========================================
-- hermes-memory schema v1.0
-- 文件: ~/.hermes/memory/schema.sql
-- 实战: 4D 知识图谱 (节点 + 关系 + 时间 + 向量)
-- 主人口中 7/18 拍板 review SCHEMA.md
-- ========================================

-- 1. ENTITIES (节点) ----------------------------------
CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                     -- stock / concept / event / person / chunk / task / canonical_fact
    name TEXT,
    summary TEXT,
    properties_json TEXT,
    aliases_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    valid_from TEXT,
    valid_until TEXT,
    superseded_by TEXT,
    source TEXT,
    importance REAL DEFAULT 0.5,
    recall_count INTEGER DEFAULT 0,
    last_recalled TEXT
);
CREATE INDEX idx_entities_kind ON entities(kind);
CREATE INDEX idx_entities_updated ON entities(updated_at);
CREATE INDEX idx_entities_valid ON entities(valid_from, valid_until);
CREATE INDEX idx_entities_supersede ON entities(superseded_by) WHERE superseded_by IS NOT NULL;

-- 2. CHUNKS (原文块) ----------------------------------
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source TEXT,
    session_id TEXT DEFAULT 'default',
    timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    importance REAL DEFAULT 0.5,            -- 0.0-1.0, 实战排序 (entities 表也有, 冗余存方便排序)
    metadata_json TEXT,
    superseded_by TEXT,
    valid_until TEXT,
    recall_count INTEGER DEFAULT 0,
    last_recalled TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_chunks_timestamp ON chunks(timestamp);
CREATE INDEX idx_chunks_source ON chunks(source);
CREATE INDEX idx_chunks_session ON chunks(session_id);
CREATE INDEX idx_chunks_importance ON chunks(importance);
CREATE INDEX idx_chunks_valid ON chunks(valid_until) WHERE valid_until IS NOT NULL;

-- 3. RELATIONS (边) ----------------------------------
CREATE TABLE relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    weight REAL DEFAULT 1.0,
    properties_json TEXT,
    valid_from TEXT,
    valid_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    source TEXT,
    confidence REAL DEFAULT 1.0,
    evidence_chunk_id TEXT
);
CREATE INDEX idx_relations_src ON relations(source_id);
CREATE INDEX idx_relations_tgt ON relations(target_id);
CREATE INDEX idx_relations_relation ON relations(relation);
CREATE INDEX idx_relations_valid ON relations(valid_from, valid_until);
CREATE INDEX idx_relations_evidence ON relations(evidence_chunk_id);

-- 4. VECTORS (向量索引, sqlite-vec 0.1.x) ----------
-- vec0 是单列虚拟表: embedding + rowid, 与 chunks.id 1:1 映射 (rowid -> chunk_id)
-- dim 在 init_db.py 运行时从 config.toml 读 (默认 512) — 切 embedding 模型必须重新 init_db
CREATE VIRTUAL TABLE vectors USING vec0(
    embedding float[{EMBED_DIM}]
);

-- 5. META (系统元数据) -------------------------------
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 6. RECALL_LOG (召回审计) ----------------------------
CREATE TABLE recall_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    query_embedding_id TEXT,
    results_json TEXT,
    graph_hops INTEGER,
    latency_ms REAL,
    recall_details_json TEXT,   -- [P2+ #3 7/18 patch] 实战 feedback loop: top-5 ranks + method + distance/rrf_score + importance
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX idx_recall_query ON recall_log(query);
CREATE INDEX idx_recall_created ON recall_log(created_at);

-- 7. PURGED_QUEUE (软删除→物理删除) ------------------
CREATE TABLE purged_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    purged_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    done INTEGER DEFAULT 0
);
CREATE INDEX idx_purged_done ON purged_queue(done);
CREATE INDEX idx_purged_target ON purged_queue(target_id);

-- ========================================
-- 触发器 (自动维护)
-- ========================================

-- 8.1 维护 updated_at
CREATE TRIGGER trg_entities_updated AFTER UPDATE ON entities
BEGIN UPDATE entities SET updated_at = datetime('now', 'localtime') WHERE id = NEW.id; END;

CREATE TRIGGER trg_chunks_updated AFTER UPDATE OF superseded_by, valid_until ON chunks
BEGIN UPDATE chunks SET created_at = created_at WHERE id = NEW.id; END;
-- (chunks 表不用 updated_at, 触发器保持 created_at 不变)

-- 8.2 entity 被 supersede 时, 自动级联失效引用边 (核心创新!)
CREATE TRIGGER trg_entities_supersede AFTER UPDATE OF superseded_by ON entities
WHEN NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL
BEGIN
    UPDATE relations
    SET valid_until = datetime('now', 'localtime')
    WHERE (source_id = OLD.id OR target_id = OLD.id) AND valid_until IS NULL;
END;

-- 8.3 chunk 被 supersede 时, 自动级联失效引用边
CREATE TRIGGER trg_chunks_supersede AFTER UPDATE OF superseded_by ON chunks
WHEN NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL
BEGIN
    UPDATE relations
    SET valid_until = datetime('now', 'localtime')
    WHERE (source_id = OLD.id OR target_id = OLD.id) AND valid_until IS NULL;
END;

-- ========================================
-- 初始化 meta (schema_version + embedding 模型)
-- model + dim 占位符在 init_db.py 运行时替换 (env > config.toml > default)
-- ========================================
INSERT INTO meta (key, value) VALUES
    ('schema_version', '1.0'),
    ('embedding_model', '{EMBED_MODEL}'),
    ('embedding_dim', '{EMBED_DIM}'),
    ('created_at', datetime('now', 'localtime')),
    ('created_by', 'hermes-memory v1.0');

-- ========================================
-- 启用 WAL mode + busy_timeout (避免 lock 复发!)
-- ========================================
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 30000;
PRAGMA foreign_keys = ON;
