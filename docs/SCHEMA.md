# 自建知识图谱记忆系统 — Schema 设计文档

> **项目代号**: `mnelo` (HM, 后简称 hm_)
> **位置**: `~/.hermes/memory/`
> **目的**: 替换频繁出问题的 Mnemosyne, 主人口中 7/17 拍板 (本地 0 预算 + 知识图谱)
> **版本**: v1.0 draft — 等主人口中 review

---

## 1. 目标与边界

### 1.1 目标
- **替换 Mnemosyne** (sqlite + sqlite-vec + fastembed 三层架构, 频繁 lock)
- **本地方案, 0 预算** (Python + sqlite + NetworkX + fastembed)
- **不只向量检索**: 知识图谱模式 (nodes + edges + 时间维度)
- **支持 CRUD **: 增 / 删 / 改 / 查 一气呵成
- **可用**: trinity_daily cron 16:05 + weng 早报 08:06 + part 3 锚漂移 → 直接对接

### 1.2 非目标
- ❌ 不追求多用户 / 多 session 隔离 (单一主人 = `default`)
- ❌ 不做实时同步 / 跨设备 (本地优先)
- ❌ 不做 LLM 自动 entity 抽取 (1.0 手工/规则抽取; 2.0 加 LLM)
- ❌ 不做图可视化 UI (1.0 CLI + Markdown 输出)

---

## 2. 设计参考

| 来源 | 启发点 |
|---|---|
| Neo4j Property Graph | nodes + edges + properties 通用 KG 模型 |
| Memgraph Temporal KG | edges 带 `valid_from / valid_until` (4D 知识图谱) |
| Cassandra tombstone | soft delete + 异步 purge ("删除"≠ 物理删除) |
| GraphRAG (微软) | community detection + multi-hop retrieval |
| Cognee | dual-layer graph (semantic + lexical) |
| Notion / Obsidian | 双向链接 + backlinks |
| Mnemosyne 7 个月数据 | schema 直接 export 看字段真实形状 |

---

## 3. 核心表 (4 张)

### 3.1 `entities` — 实体节点

实体是知识图谱的核心 — 股票 / 概念 / 事件 / 人 / 文件 / 任务。

```sql
CREATE TABLE entities (
    id TEXT PRIMARY KEY,                    -- 'sh600089', '翁氏_D∩W', '2026-07-17-trinity'
    kind TEXT NOT NULL,                     -- 'stock' / 'concept' / 'event' / 'person' / 'chunk' / 'task'
    name TEXT,                              -- '特变电工'
    summary TEXT,                           -- 实体一句话描述 (人工/规则抽取)
    properties_json TEXT,                   -- 灵活属性 {"ticker":"sh600089","sector":"电力","industry_lv1":"公用事业"}
    aliases_json TEXT,                      -- 别名数组 ["特变", "TBEA", "特变电工股份"] - 用于 entity resolution
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 4D 时间维度
    valid_from TEXT,                        -- '2026-07-17T14:30:00'
    valid_until TEXT,                       -- soft delete 时填, NULL = 永久有效
    superseded_by TEXT,                     -- 指向新版本 entity id
    -- 元数据
    source TEXT,                            -- 提取来源: 'mnemosyne-import' / 'manual' / 'trinity_daily' / 'cron'
    importance REAL DEFAULT 0.5,            -- 0.0-1.0, 用于排序
    recall_count INTEGER DEFAULT 0,         -- 被 recall 次数
    last_recalled TEXT
);

CREATE INDEX idx_entities_kind ON entities(kind);
CREATE INDEX idx_entities_updated ON entities(updated_at);
CREATE INDEX idx_entities_valid ON entities(valid_from, valid_until);
CREATE INDEX idx_entities_supersede ON entities(superseded_by) WHERE superseded_by IS NOT NULL;
```

**映射 (Mnemosyne → hm_entities)**:
- `triples.subject/object` 中的 subject/object → entity (kind 提取)
- `canonical_facts` (8 条) → kind='canonical_fact' 的 entity + properties_json 存 body
- `annotations.kind+value` → 当 kind/value 是 stock/concept 时升级为 entity

### 3.2 `chunks` — 原文块

LLM 处理前的原始文本 — recall 时"原文"层。

```sql
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,                    -- 'chunk_20260718_07_04_001'
    content TEXT NOT NULL,
    source TEXT,                            -- 'cron' / 'master' / 'trinity_daily' / 'mnemosyne-import' / 'weng-monitor'
    session_id TEXT DEFAULT 'default',
    timestamp TEXT NOT NULL,
    metadata_json TEXT,
    -- 更新语义
    superseded_by TEXT,                     -- 指向新版本 chunk (与 Mnemosyne working_memory.superseded_by 一致)
    valid_until TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_chunks_timestamp ON chunks(timestamp);
CREATE INDEX idx_chunks_source ON chunks(source);
CREATE INDEX idx_chunks_session ON chunks(session_id);
CREATE INDEX idx_chunks_valid ON chunks(valid_until) WHERE valid_until IS NOT NULL;
```

**映射**:
- `mnemosyne.working_memory` 3560 条 → chunks 主体
- `mnemosyne.episodic_memory` 334 条 → 也是 chunk (但加 `metadata.is_summary=true`)

### 3.3 `relations` — 关系 (4D 时态)

主人口中拍板"知识图谱"的核心 — 所有"两个实体之间有关系"都存在这里。

```sql
CREATE TABLE relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,                -- entity 或 chunk id
    target_id TEXT NOT NULL,                -- entity 或 chunk id
    relation TEXT NOT NULL,                 -- '翁氏_共振_于' / '_建仓_于' / '盈利_于' / 'belongs_to' / 'mentions'
    weight REAL DEFAULT 1.0,                -- 关系强度, 同 relation 多次出现会累加
    properties_json TEXT,
    -- 4D 时间维度 (关键!)
    valid_from TEXT,                        -- '2026-07-15T14:00:00'
    valid_until TEXT,                       -- 关系结束时间 (NULL = 永久)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- 来源与权威
    source TEXT,                            -- 关系来源
    confidence REAL DEFAULT 1.0,
    evidence_chunk_id TEXT                  -- 支撑这段关系的原文 chunk
);

CREATE INDEX idx_relations_src ON relations(source_id);
CREATE INDEX idx_relations_tgt ON relations(target_id);
CREATE INDEX idx_relations_relation ON relations(relation);
CREATE INDEX idx_relations_valid ON relations(valid_from, valid_until);
CREATE INDEX idx_relations_evidence ON relations(evidence_chunk_id);
```

**关系类型 (待扩展)**:
```
'翁氏_共振_于'        (e.g. sh600089 --翁氏_共振_于--> 2026-08-14_anchor)
'_建仓_于'        (master --_建仓_于--> sh600021 @ 14.22 on 2026-07-15)
'_减仓_于'        (同上, 减仓操作)
'浮盈亏_于'           (sh600021 --浮盈_于--> 6,486 on 2026-07-17)
'翁氏_命中_于'        (chunk --翁氏_命中_于--> date)
'mentions'            (chunk --mentions--> entity)
'supersedes'          (old_chunk --supersedes--> new_chunk)
'belongs_to'          (entity --belongs_to--> entity, e.g. sh600021 belongs_to 公用事业)
'depends_on'          (entity --depends_on--> entity, e.g. 翁氏 D∩W depends_on 翁氏 LOW)
'triggered_by'        (signal --triggered_by--> 翁氏 anchor)
```

### 3.4 `vectors` — 向量索引 (sqlite-vss)

复用 Mnemosyne 的 bge-small-zh-v1.5 (512d, 90MB), 迁移不重新嵌入。

```sql
CREATE VIRTUAL TABLE vectors USING vss0(
    embedding FLOAT[512],
    chunk_id TEXT
);
```

**映射**:
- `mnemosyne.legacy_embeddings` 3103 条 → vectors 表
- `mnemosyne.episodic_embeddings` 328 条 → vectors 表
- 新写入: `embedder.py` 用 fastembed 算

---

## 4. 辅助表

### 4.1 `meta` — 系统元数据

```sql
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- INSERT INTO meta (key, value) VALUES ('schema_version', '1.0');
-- INSERT INTO meta (key, value) VALUES ('embedding_model', 'BAAI/bge-small-zh-v1.5');
-- INSERT INTO meta (key, value) VALUES ('embedding_dim', '512');
-- INSERT INTO meta (key, value) VALUES ('created_from', 'mnemosyne-7.17-migration');
```

### 4.2 `recall_log` —  recall 审计

```sql
CREATE TABLE recall_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    query_embedding_id TEXT,                -- query 向量是否入库
    results_json TEXT,                       -- 返回的 chunk_ids + scores
    graph_hops INTEGER,
    latency_ms REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_recall_query ON recall_log(query);
```

**价值**: 主人口中能看 recall 的历史, 调优 threshold/过滤。

### 4.3 `purged_queue` — 软删除转物理删除的待办

```sql
CREATE TABLE purged_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    target_kind TEXT NOT NULL,              -- 'entity' / 'chunk' / 'relation'
    purged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    done INTEGER DEFAULT 0
);
-- : 主人口中 forget → soft delete → 当晚 cron worker 跑物理删除
```

---

## 5. 触发器 (自动维护)

```sql
-- 5.1 维护 updated_at
CREATE TRIGGER trg_entities_updated AFTER UPDATE ON entities
BEGIN UPDATE entities SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

CREATE TRIGGER trg_chunks_updated AFTER UPDATE ON chunks
WHEN OLD.superseded_by IS NOT NEW.superseded_by OR OLD.valid_until IS NOT NEW.valid_until
BEGIN UPDATE chunks SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id; END;

-- 5.2 supersede 自动级联 (核心!)
-- 当 entity 被 supersede 时, 所有引用它的 open 关系自动 valid_until = now
CREATE TRIGGER trg_entities_supersede AFTER UPDATE OF superseded_by ON entities
WHEN NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL
BEGIN
    UPDATE relations
    SET valid_until = CURRENT_TIMESTAMP
    WHERE (source_id = OLD.id OR target_id = OLD.id) AND valid_until IS NULL;
END;

-- 5.3 chunk 被 supersede 时也级联关系
CREATE TRIGGER trg_chunks_supersede AFTER UPDATE OF superseded_by ON chunks
WHEN NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL
BEGIN
    UPDATE relations
    SET valid_until = CURRENT_TIMESTAMP
    WHERE (source_id = OLD.id OR target_id = OLD.id) AND valid_until IS NULL;
END;
```

---

## 6. CRUD  API

### 6.1 `remember` (写入)

```python
def memory_remember(
    content: str,
    source: str = 'manual',
    importance: float = 0.5,
    entities: list = None,           # [{id, kind, name, summary, aliases?, properties?}]
    relations: list = None,          # [{source_id, target_id, relation, weight, properties?, valid_from?, valid_until?}]
    tags: list = None,
    session_id: str = 'default',
    timestamp: str = None,          # None = now
) -> str:
    """写入一条 chunk + 提取的实体 + 关系.
    
    Returns: chunk_id
    """
```

### 6.2 `recall` (检索 — 3 路并行 + RRF 融合)

```python
def memory_recall(
    query: str,
    top_k: int = 5,
    graph_hops: int = 2,            # 多跳
    filters: dict = None,           # {kind, source, tag, time_range}
    strategy: str = 'rrf',          # 'rrf' / 'vector_only' / 'graph_only'
) -> list:
    """返回 [{chunk_id, score, related_entities, graph_path}, ...]
    
    三路:
      1. 向量 (sqlite-vss)
      2. 图遍历 (NetworkX 内存层)
      3. 元数据 (精确 LIKE + 时间近)
    
    RRF (Reciprocal Rank Fusion) 融合:
      score(d) = Σ 1 / (k + rank_i(d))   for each retrieval method
    """
```

### 6.3 `relate` (关系操作)

```python
def memory_relate(
    source_id: str,
    target_id: str,
    relation: str,
    weight: float = 1.0,
    valid_from: str = None,         # None = now
    valid_until: str = None,        # None = 永久
    evidence_chunk_id: str = None,
    properties: dict = None,
) -> int:
    """Returns: relation_id"""
```

### 6.4 `forget` (软删除)

```python
def memory_forget(
    target_id: str,
    reason: str = 'outdated',
    cascade: bool = True,
) -> dict:
    """软删除: valid_until = now, 同时 cascade 级联失效引用关系.
    主人口中拍板"删除无用知识" → soft delete + 异步 purge worker
    
    Returns: {entities_deleted: N, edges_invalidated: M, queued_purge: bool}
    """
```

### 6.5 `update` (更新 — 不 UPDATE, 创建新版本)

```python
def memory_update(
    old_id: str,
    new_content: str = None,        # None = 不改原文
    new_properties: dict = None,
    new_importance: float = None,
    reason: str = 'updated',
) -> str:
    """"更新知识": 不 UPDATE, 而创建新 chunk/entity.
    老 chunk/entity 通过 superseded_by 指向新版本 (触发器自动级联).
    
    Returns: new_id
    """
```

### 6.6 `graph_query` (图遍历)

```python
def memory_graph_query(
    start_node: str,
    max_hops: int = 3,
    edge_types: list = None,         # None = 所有
    time_range: tuple = None,       # (start, end)
    min_weight: float = 0.0,
) -> dict:
    """返回 subgraph = {'nodes': [...], 'edges': [...], 'paths': [...]}
    
    :
      - graph_query('sh600089', hops=2) → 找出与特变电工相关的所有 2-hop 内节点 + 边
      - graph_query('翁氏_D∩W', hops=3, edge_types=['_建仓_于']) → 所有翁氏过的股票
    """
```

---

## 7. 删除/更新/检索三大能力的实现

### 7.1 删除 (主人口中"删除无用知识")

**问题**: 物理删除 = 数据丢失 = 后悔了找不到
**方案**: Tombstone (Cassandra 模式) — soft delete + 异步 worker 物理删除

```python
def memory_forget(target_id, reason='outdated', cascade=True):
    # 1. soft delete (主操作)
    cur.execute("""
        UPDATE entities
        SET valid_until = ?, recall_count = 0
        WHERE id = ? AND valid_until IS NULL
    """, (now(), target_id))
    
    # 2. cascade 失效边 (不级联 = 留下幽灵关系)
    if cascade:
        cur.execute("""
            UPDATE relations
            SET valid_until = ?
            WHERE (source_id = ? OR target_id = ?) AND valid_until IS NULL
        """, (now(), target_id, target_id))
    
    # 3. 入队 purge (30 天后物理删)
    cur.execute("""
        INSERT INTO purged_queue (target_id, target_kind, purged_at, done)
        VALUES (?, 'entity', ?, 0)
    """, (target_id, now_plus_30d()))
    
    return {'entities_deleted': 1, 'edges_invalidated': N, 'queued_purge': True}
```

**主人口中**: `memory_forget('2026-07-15-trinity_old')` 删除过时报告, **召回时这些 entity 不会出现 (valid_until IS NULL 过滤)**, 但 30 天内可查.

### 7.2 更新 (主人口中"更新知识")

**问题**: 直接 UPDATE = 失去历史
**方案**: Append-only + superseded_by 链 (跟 Mnemosyne 一致)

```python
def memory_update(old_id, new_content=None, new_properties=None, new_importance=None, reason='updated'):
    # 1. 读老 entity
    old = cur.execute("SELECT * FROM entities WHERE id = ? AND valid_until IS NULL", (old_id,)).fetchone()
    if not old:
        raise ValueError(f"entity {old_id} not found or already superseded")
    
    # 2. 创建新 entity
    new_id = generate_id()
    cur.execute("""
        INSERT INTO entities (id, kind, name, summary, properties_json, aliases_json,
                              source, importance, valid_from, recall_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
    """, (new_id, old['kind'], old['name'],
          new_content or old['summary'],
          json.dumps(new_properties) if new_properties else old['properties_json'],
          old['aliases_json'], reason, new_importance or old['importance'],
          now()))
    
    # 3. 老 entity 标 superseded_by (触发器自动级联)
    cur.execute("""
        UPDATE entities SET superseded_by = ?
        WHERE id = ? AND valid_until IS NULL
    """, (new_id, old_id))
    
    # 4. 复制老 entity 的入边/出边到新 entity (关系网迁移)
    cur.execute("""
        INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                               valid_from, valid_until, source, confidence, evidence_chunk_id)
        SELECT ?, target_id, relation, weight, properties_json, ?, NULL,
               source, confidence, evidence_chunk_id
        FROM relations WHERE source_id = ? AND valid_until IS NULL
    """, (new_id, now(), old_id))
    
    cur.execute("""
        INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                               valid_from, valid_until, source, confidence, evidence_chunk_id)
        SELECT source_id, ?, relation, weight, properties_json, ?, NULL,
               source, confidence, evidence_chunk_id
        FROM relations WHERE target_id = ? AND valid_until IS NULL
    """, (new_id, now(), old_id))
    
    return new_id
```

**主人口中**: sh600089 持仓从 18.96 → 19.12 → 18.99 → 18.77 (4 次操作) → 每次都是新 chunk + edges, **历史完整可追溯**.

### 7.3 检索 (主人口中"检索相关知识")

**问题**: 向量检索忽略图结构, 纯图遍历忽略语义
**方案**: 3 路并行 + RRF 融合

```python
def memory_recall(query, top_k=5, graph_hops=2, filters=None):
    # === 路 1: 向量 (sqlite-vss) ===
    q_emb = embed(query)
    vector_results = cur.execute("""
        SELECT c.id, c.content, c.importance, vss_distance
        FROM vectors v
        JOIN chunks c ON c.id = v.chunk_id
        WHERE c.valid_until IS NULL
          AND vss_search(v.embedding, ?)
        ORDER BY vss_distance
        LIMIT ?
    """, (q_emb, top_k * 2)).fetchall()
    
    # === 路 2: 图遍历 (NetworkX 内存层) ===
    # 拿所有相关 chunk_ids (来自向量) 作为种子
    seed_ids = [r[0] for r in vector_results]
    # 提取每个 chunk 关联的 entity
    entities_per_chunk = cur.execute("""
        SELECT source_id, target_id, relation, source_id
        FROM relations
        WHERE (source_id IN (SELECT id FROM chunks WHERE id IN ?)
               OR target_id IN (SELECT id FROM chunks WHERE id IN ?))
          AND valid_until IS NULL
    """, (seed_ids, seed_ids)).fetchall()
    
    # 在 NetworkX 里 BFS 展开 graph_hops 跳
    G = nx.DiGraph()
    for src, tgt, rel in entities_per_chunk:
        G.add_edge(src, tgt, relation=rel)
    
    expanded = set()
    for seed in seed_ids:
        for target in nx.single_source_shortest_path_length(G, seed, cutoff=graph_hops):
            expanded.add(target)
    
    graph_results = cur.execute("""
        SELECT id, content, importance
        FROM chunks
        WHERE id IN ? AND valid_until IS NULL
        ORDER BY importance DESC LIMIT ?
    """, (list(expanded), top_k * 2)).fetchall()
    
    # === 路 3: 元数据 (精确 LIKE + 时间近) ===
    meta_results = cur.execute("""
        SELECT id, content, importance
        FROM chunks
        WHERE valid_until IS NULL
          AND content LIKE ?
          AND timestamp > datetime('now', '-30d')
        ORDER BY importance DESC, timestamp DESC LIMIT ?
    """, (f'%{query}%', top_k * 2)).fetchall()
    
    # === RRF 融合 ===
    rrf_score = defaultdict(float)
    for rank, (cid, *_) in enumerate(vector_results):
        rrf_score[cid] += 1.0 / (60 + rank)
    for rank, (cid, *_) in enumerate(graph_results):
        rrf_score[cid] += 1.0 / (60 + rank)
    for rank, (cid, *_) in enumerate(meta_results):
        rrf_score[cid] += 1.0 / (60 + rank)
    
    top = sorted(rrf_score.items(), key=lambda x: -x[1])[:top_k]
    # 返回时组装 related_entities + graph_path
    return format_results(top, G)
```

**主人口中**: recall("翁氏 D∩W  7/20")
- 向量: 找翁氏共振段 (5 条)
- 图: 翁氏 → 2026-08-14 anchor → sh600089/sh600021/sh600021/sh300058/sh300446/sh300364 → 共振类型
- 元: 最近 30 天 + LIKE
- RRF 融合: 最相关 = 8/14 anchor +  record (翁氏的 5 标的共振 + 主人口中建仓动作)

---

## 8. 主键关系表

| 主键 | 意义 |
|---|---|
| `entities.id` |  ID 例如 `sh600089` / `翁氏_D∩W` / `2026-08-14-anchor` |
| `chunks.id` | UUID 风格 (`chunk_20260718_07_04_001`) |
| `relations.id` | AUTOINCREMENT INTEGER |
| `vectors.rowid` | AUTOINCREMENT (sqlite-vss 内部) |
| `meta.key` | 系统级 key-value |

** ID 命名规则 (entities)**:
- 股票: `sh600021` / `sz300058` (与交易代码一致)
- 概念: `翁氏_D∩W` / `Trinity_3层`
- 事件: `2026-07-17-trinity-anchor` / `2026-08-14-anchor`
- 人: `master_2077_ling`
- 任务: `task_07_18_mnemosyne_migration`

---

## 9. 时间维度

| 类型 | 实现 |
|---|---|
| **持续有效** | `valid_until IS NULL` (默认) |
| **软删除** | `valid_until = '2026-07-17T15:00:00'` (查询自动过滤) |
| **持仓变动** | `relations.valid_from = 买入日期`, `relations.valid_until = 卖出日期` |
| **预测有效期** | `entity.valid_until = '2026-07-30'` (7/15 预测的 8/14 anchor, 7/30 后过期) |
| **过期自动清理** | 每周 cron worker: `SELECT id WHERE valid_until < now()` → purged_queue |

---

## 10. 实施路径

| 步骤 | 内容 | 文件 | 时间 |
|---|---|---|---|
| 1 | schema.sql 落地 (上面 6 张表) | `~/.hermes/memory/schema.sql` | 5 分钟 |
| 2 | memory.db 初始化 + 触发器 | `scripts/init_db.py` | 15 分钟 |
| 3 | embedder.py (复用 bge-small-zh-v1.5, 复用 venv) | `embedder.py` | 30 分钟 |
| 4 | memory.py 核心 6 API | `memory.py` | 2-3 小时 |
| 5 | import_from_mnemosyne.py (Mnemosyne → hm_ 全量迁移) | `scripts/import_from_mnemosyne.py` | 1-2 小时 |
| 6 | entity_resolve.py (alias + 相似度合并) | `entity_resolve.py` | 1 小时 |
| 7 | api/ 4 MCP tool (memory_remember / memory_recall / memory_relate / memory_forget) | `api/*.py` | 2 小时 |
| 8 |  cron 接入: trinity_daily.py Part 3 + weng 早报 (替换 mnemosyne_*) | `cron/` | 1-2 小时 |
| 9 | tests/ (CRUD + 3 路 recall + soft delete + 4D 时间) | `tests/test_*.py` | 1-2 小时 |

总: 1-2 天可跑通基础闭环 (步骤 1-5)。

---

## 11. 与 Mnemosyne 兼容层 (过渡期)

: 主人口中拍板"接口不保留原名" — 但中如果 trinity_daily 还在调 `mnemosyne_remember`, 过渡期需要 shim:

```python
# ~/.hermes/memory/api/mnemosyne_shim.py
# 过渡期: 把 mnemosyne_* 调用映射到 hm_*
def mnemosyne_remember(content, **kwargs):
    return memory_remember(content=content, source='mnemosyne-shim', **kwargs)

def mnemosyne_recall(query, **kwargs):
    return memory_recall(query=query, **kwargs)
```

**期**: shim 只为兜底, **主路径全部用 memory_*** (1 周过渡, 之后删 shim).

---

## 12. 风险与限制

| 风险 | 缓解 |
|---|---|
| sqlite lock 复发 | WAL mode + busy_timeout=30s + 单一 writer (单进程) |
| 实体消歧冲突 | alias 数组 + 相似度阈值 + 人工 review API |
| 向量索引大 | 复用 bge-small-zh-v1.5 (90MB, 3123 条 < 5MB) |
| 关系稀疏 | LLM 抽取 2.0 (1.0 手工/规则) |
| 时间维度边界 | 中 valid_until 偶尔冲突, 时人工确认 |

---

## 13. 主人口中 review 检查清单

主人口中 review 这个 SCHEMA.md 时, 请确认以下设计决策:

- [ ] **schema 版本**: v1.0 (4 张核心表 + 3 张辅助表)
- [ ] **删除策略**: soft delete + 30 天延迟 purge (yes / no / other?)
- [ ] **更新策略**: 不 UPDATE, 创建新版本 + superseded_by 链 (yes / no / other?)
- [ ] **检索策略**: 3 路并行 + RRF 融合 (yes / no / other?)
- [ ] **实体 ID 命名**:  ID (sh600089 / 翁氏_D∩W / 2026-08-14-anchor)
- [ ] **时间维度**: 4D (valid_from / valid_until / soft delete) (yes / no / other?)
- [ ] **过渡策略**: 1 周 shim, 主路径用 memory_*(yes / no / other?)
- [ ] **嵌入模型**: 复用 bge-small-zh-v1.5 (512d, 90MB) (yes / no / other?)
- [ ] **关系类型集**: 我列了 10 类 ('翁氏_共振_于' / '_建仓_于' / ...) — 够用吗?

主人口中 review 后告诉我哪些改, 哪些保留, 接下来开始 Step 1-5 实施.
