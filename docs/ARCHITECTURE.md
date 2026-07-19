# mnelo 架构与知识图谱分析

> **项目**: mnelo (mnelo)
> **位置**: `~/.hermes/memory/`
> **版本**: v1.2 (P0/P1/P2 审计后)
> **分析日期**: 2026-07-18
> **分析范围**: schema.sql (155 行) + memory.py (836 行) + mcp_server.py (410 行) + entity_resolve.py (243 行) + embedder.py (109 行)
> **配套文档**: `SCHEMA.md` (设计), `ARCHITECTURE.md` (本文件, 分析), `RUNBOOK.md` (过程 + 替换模板), `AUDIT-REPORT.md` (7/18 小默审计报告)
> **相关 skill**: `~/.hermes/skills/agent-memory-design/`
> **要求**: "调研笔记 / 评估文档" 默认简体中文 (SOUL.md carve-out 2026-06-26)

---

## 1. 系统定位

**mnelo** 是 Hermes Agent 的本地知识图谱记忆系统，2026-07-17 拍板自建、7/18 上线替换原 Mnemosyne。物理形态：

```
~/.hermes/memory/
├── memory.db          # 19 MB (7/18), 4 核 + 3 辅 + 4 触发器 + vec0
├── memory.db-wal      # 4 MB WAL (PASSIVE checkpoint 30s 内)
├── schema.sql         # 155 行, 单文件 schema 源
├── memory.py          # 836 行, Memory class + 工具函数
├── mcp_server.py      # 410 行, MCP server (stdio + SSE)
├── embedder.py        # 109 行, fastembed wrapper
├── entity_resolve.py  # 243 行, alias 合并 + find_duplicates
└── api/
    └── mnelo_client.py  # SSE 客户端 (MneloClient)
```

部署形态: 单进程 + 单文件 SQLite + 127.0.0.1:8086 SSE，跟随 `ai.mnelo.mcp` launchd plist 启动。

---

## 2. 知识图谱架构 (4D = 节点 + 边 + 时间 + 向量)

### 2.1 节点层 (entities)

```sql
CREATE TABLE entities (
    id TEXT PRIMARY KEY,        -- e.g. 'sh600089' / 'identity:user:location'
    kind TEXT NOT NULL,         -- stock / concept / event / person / canonical_fact / identity_fact
    name TEXT,
    summary TEXT,
    properties_json TEXT,       -- 自由 metadata
    aliases_json TEXT,          -- ['特变电工', 'TBEA', '特变'] 等别名
    created_at TEXT,
    updated_at TEXT,
    valid_from TEXT,            -- ← 时态维度起点
    valid_until TEXT,           -- ← 时态维度终点 (NULL = 当前活跃)
    superseded_by TEXT,         -- ← 链向替代版本 (entity 消歧合并)
    source TEXT,
    importance REAL,            -- [0, 1], clamped (P0 审计后)
    recall_count INTEGER,
    last_recalled TEXT
);
```

**关键设计**：
- **节点身份** = `id`（主键，无 auto-increment）。中用语义 id（股票代码、人名 slug），避免大整数 PK 在 import/migration 时混乱
- **`kind`** 决定节点语义，是 schema 的"模式"。 kind：stock / concept / event / person / canonical_fact / identity_fact
- **`aliases_json`** 支持一实体多名，自动合并（`entity_resolve.py`）
- **`importance`** 全表统一 [0, 1]，P0 审计后由 `clamp01()` 强制

### 2.2 原文层 (chunks)

```sql
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,        -- 'chunk_20260718_103045_123456'
    content TEXT NOT NULL,      -- 原文 (trinity_daily Part 1-5 等)
    source TEXT,                -- 'trinity_daily:part1' / 'session:master:0029'
    session_id TEXT,            -- 'default' / 'cron'
    timestamp TEXT,
    importance REAL,
    metadata_json TEXT,         -- {'tags': [...], 'supersedes': 'old_id'}
    superseded_by TEXT,         -- ← update() 创建新版本，老版本标这个
    valid_until TEXT,           -- ← 软删除时间
    recall_count INTEGER,
    last_recalled TEXT
);
```

**关键设计**：
- chunk 是**非结构化原文**（人类可读、LLM 可直接用）
- 与 entity 区别: chunk 表达"事实陈述"，entity 表达"概念身份"
- `superseded_by` + `valid_until` 实现**不可变历史**（immutable history）
- `metadata_json` 存 tags / supersedes / reason 等扩展

### 2.3 边层 (relations)

```sql
CREATE TABLE relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,     -- 'owns' / 'references' / 'mentions' / 'located_in'
    weight REAL DEFAULT 1.0,    -- [0, 1] via clamp01
    properties_json TEXT,
    valid_from TEXT,
    valid_until TEXT,
    source TEXT,
    confidence REAL DEFAULT 1.0,
    evidence_chunk_id TEXT      -- ← : 哪条 chunk 创立的这条边
);
```

**关键设计**：
- **通用 RDFS 风格**：`relation` 是 string label（不限枚举），灵活扩展
- `evidence_chunk_id` 是**关键创新** — 任何边都"生于"一条原文 chunk，可回溯
- `weight` + `confidence` 双字段：weight = 边强度，confidence = 来源可信度
- **`valid_from/until` 让边也有 4D 时态**（删除 entity 自动级联，见 §2.5）

### 2.4 向量层 (vectors + vec0)

```sql
CREATE VIRTUAL TABLE vectors USING vec0(
    embedding float[512]
);
```

**关键设计**：
- **单列虚拟表** + `rowid` ↔ `chunks.rowid` 1:1 映射
- 模型：bge-small-zh-v1.5（Chinese-native，512 维，C-MTEB 强）
- 用 `_with_row_factory()` context manager 处理 vec0 返回 plain tuple 的 quirk（sqlite-vec 0.1.x 已知 bug）

### 2.5 触发器 (4 个) — 自动维护的核心

```sql
-- 8.1 自动维护 updated_at
CREATE TRIGGER trg_entities_updated AFTER UPDATE ON entities
BEGIN UPDATE entities SET updated_at = datetime('now') WHERE id = NEW.id; END;

-- 8.2 entity 被 supersede → 级联失效所有引用边 (核心创新!)
CREATE TRIGGER trg_entities_supersede AFTER UPDATE OF superseded_by ON entities
WHEN NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL
BEGIN
    UPDATE relations SET valid_until = datetime('now')
    WHERE (source_id = OLD.id OR target_id = OLD.id) AND valid_until IS NULL;
END;

-- 8.3 chunk 被 supersede → 同样级联
CREATE TRIGGER trg_chunks_supersede AFTER UPDATE OF superseded_by ON chunks
WHEN NEW.superseded_by IS NOT NULL AND OLD.superseded_by IS NULL
BEGIN
    UPDATE relations SET valid_until = datetime('now')
    WHERE (source_id = OLD.id OR target_id = OLD.id) AND valid_until IS NULL;
END;
```

**核心创新**：删除一个 entity/chunk 时，**所有引用边自动级联失效**（不需要应用层手动清理）。这让"软删除 → 全图保持一致"成为 DB 强保证。

### 2.6 辅助表 (3 个)

- **meta**: 系统元数据（schema_version, embedding_model 等）
- **recall_log**: 每次 recall 的审计日志（query / results / latency_ms）— 分析召回质量
- **purged_queue**: 软删除 30 天后才物理清理的待办队列

---

## 3. 召回架构 (4 路 + RRF)

```python
recall(query, top_k=5, graph_hops=2, strategy='rrf', asof=None)
  ↓
  [vector, graph, meta, entity] 4 路并行
  ↓
  RRF 融合 (Reciprocal Rank Fusion)
  ↓
  返回统一 hit list
```

### 3.1 4 路召回设计

| 路 | 数据源 | 触发条件 | 时间复杂度 |
|---|---|---|---|
| `vector` | vec0 MATCH | 所有 query | O(log N) via HNSW |
| `graph` | relations 2-hop | seed_hits 非空 | O(M²) worst case |
| `meta` | LIKE + 时间近 | 短 query 命中 | O(N) but indexed |
| `entity` | name/aliases 匹配 | 含股票代码 / 人名 | O(K) K = 候选数 |

### 3.2 RRF 融合

```python
score(d) = Σ 1/(k + rank)  for each route r where d in top_k(r)
```

中 `k=60`（标准值）。RRF **不需要各路分数归一化**（比加权融合简单），对单路噪声鲁棒。

### 3.3 4D 时间切片 (`asof` 参数)

所有召回都接受 `asof=ISO8601`：
- vector: 走 chunk.valid_until 过滤
- graph: 走 relation.valid_from/until 过滤
- meta: chunk.valid_until 过滤
- entity: entity.valid_until 过滤

**价值**：能问"2026-06-01 时点持有 sh600089 的依据是什么"（历史回放）。

---

## 4. 知识图谱的 6 大特点

### 4.1 4D 时态 (Temporal Bitemporal)

不是单一 `created_at`，而是双时间轴：

| 时间轴 | 字段 | 含义 |
|---|---|---|
| **事务时间** | `created_at` / `updated_at` | DB 写入时间 (实务时间) |
| **有效时间** | `valid_from` / `valid_until` | 知识"事实存在"的时间段 |

中 trinity_daily 写入 7/15 sh600089 建仓，valid_from=2026-07-15；后续 update 会建新版本，老版本标 superseded_by + valid_until=now。**历史不丢，但默认召回只看到活跃版本**。

### 4.2 不可变历史 (Immutable History)

- `update()` 不覆盖老 chunk，而是 INSERT 新 chunk + UPDATE 老 chunk（superseded_by 指向新版本）
- `forget()` 是软删除（valid_until=now，30 天后 purged_queue 才物理清理）
- 触发器保证：supersede 一节点 → 所有引用边自动级联失效

**价值**：派审计"5/25 我为什么买 sh600021" — 5/25 那条决策 chunk 现在还在 DB 里。

### 4.3 触发器驱动的数据一致性

4 个触发器让"复杂一致性逻辑"下推到 DB：

| 触发器 | 维护什么 | 收益 |
|---|---|---|
| `trg_entities_updated` | 自动 updated_at | 应用层不用手动维护 |
| `trg_chunks_updated` | 防 created_at 被改 | 保留事务时间 |
| `trg_entities_supersede` | entity 合并 → 级联失效边 | DB 强保证，避免 dangling edges |
| `trg_chunks_supersede` | chunk 替换 → 级联失效边 | 同上 |

### 4.4 RAG-Native 向量化

不是单纯"关键词匹配"，而是：

```
原文 chunk → bge-small-zh-v1.5 → 512 维 vec0 rowid
                                          ↕ (1:1 rowid 映射)
                       召回时: query → embed → vec0 MATCH → 拿 rowid → 回查 chunks 表
```

- vec0 MATCH 是 ANN（HNSW-like），召回时间 O(log N) 与数据集大小弱相关
- 与图遍历正交：纯语义（向量）/ 纯结构（图）/ 时间（meta）/ 实体（entity）4 路各占一边
-  2487 vectors + 4185 entities + 15745 relations 规模下，recall < 50ms

### 4.5 证据可回溯 (Evidence Provenance)

每条 relation 有 `evidence_chunk_id`：

```
relation sh600089 --[mentioned_in]--> chunk_20260718_103045_xxx
```

中能用 1 个 SQL 找出"所有引用 sh600089 的关系都来自哪几条原文"。这是 LLM 用 RAG 时的关键信任链。

### 4.6 双视角 (Entity + Chunk 分离)

| 视角 | 表 | 形态 | 何时用 |
|---|---|---|---|
| **Entity 视角** | entities | 概念身份（"sh600089 是特变电工"） | 召回回答"X 是什么" |
| **Chunk 视角** | chunks | 原文陈述（"7/15 建仓 sh600089"） | 召回回答"为什么 X 怎么样" |

 `_entity_recall`（line 511-560）做了"实体直接作为高价值答案"——identity_fact / canonical_fact 类 entity 第一跳直接返回，不用绕回 chunk。

---

## 5. 与典型 RAG 系统的对比

| 维度 | 传统 RAG (向量库 + LLM) | mnelo |
|---|---|---|
| 数据模型 | 单层向量（chunk + embedding） | 4 维（chunk + entity + relation + vector） |
| 时态 | 无 / 只有 created_at | 双时态（事务 + 有效），4D 时间切片 |
| 实体理解 | 隐式（vector 自己学） | 显式 entity 表 + alias 合并 |
| 关系 | 隐式（cosine 相似度） | 显式 relations 表，weight/confidence |
| 一致性 | 应用层维护 | 触发器 + 软删除 + 级联 |
| 召回 | 纯向量 | 4 路 + RRF 融合 |
| 可解释性 | 黑盒（"chunk X 跟 query 相似"） | 白盒（"chunk X 引用 entity Y，weight=0.9"） |
| 规模 | 100K-10M chunks | 5K-50K 实体级（个人 AI agent 范围） |

**本质差异**：mnelo 是**结构化 + 时态** 的知识图谱，传统 RAG 是**非结构化 + 单维度**。

---

## 6. 中的设计取舍

### 6.1 为什么选 SQLite + vec0 而非专用图 DB (Neo4j / Memgraph)?

| 取舍 | 理由 |
|---|---|
| ✅ SQLite 单一文件 | 无外部依赖、launchd 自启、备份简单（cp 即可） |
| ✅ vec0 嵌入 SQLite | ANN 查询走 SQLite 协议，无独立服务 |
| ✅ 触发器下推一致性 | 关系型 DB 原生能力，省应用层代码 |
| ⚠️ 图遍历受限 | 2-hop 邻居查询用 SQL IN (...) 拼装，N 节点 → N+1 查询已优化为 1 次 SQL（审计 4.1） |
| ❌ 不适合规模 | 50K+ 实体 + 实时多 hop → 应迁 Neo4j |

### 6.2 为什么用 `valid_from/until` + 软删除 而非物理删除?

| 取舍 | 理由 |
|---|---|
| ✅ 不可变历史 | 派需要"5/25 决策 6/29 错的"完整链路 |
| ✅ 触发器级联 | 一致性自动保证 |
| ✅ 召回可按时间切片 | `asof=2026-06-01` 看历史时点知识状态 |
| ⚠️ 物理空间增长 | 30 天后 purged_queue 才清理（防止误删） |
| ⚠️ 召回需多带 WHERE valid_until IS NULL | 中所有召回都加了（已审计） |

### 6.3 为什么 single-process single-Memory 而非多 writer?

| 取舍 | 理由 |
|---|---|
| ✅ WAL + busy_timeout=30s | 单进程足够，lock 风险归零 |
| ✅ LaunchAgent 单实例 | `ai.mnelo.mcp.plist` KeepAlive 保证 |
| ❌ 不支持并发写 | 场景是单用户 cron + 偶尔手动调用，无并发需求 |

---

## 7. 4 路召回的语义对应

| 路 | 数据源 | 适合 query 类型 | 命中率 |
|---|---|---|---|
| `vector` | vec0 HNSW | 长查询 / 抽象概念（"派哲学"） | 中 |
| `graph` | relations 2-hop | 关联查询（"X 跟 Y 有什么关系"） | 高 |
| `meta` | LIKE + timestamp | 短查询 / 时间敏感（"今天加了啥"） | 中 |
| `entity` | name + aliases 精确 | 短代码（"sh600089" / "特变电工"） | 高 |

RRF 融合让 4 路互补：entity 命中强时直接把 identity_fact 当答案；vector 命中强时给概念匹配；graph 命中强时给关联链；meta 命中强时给时间线。

---

## 8. 总结 — 设计的 5 大支柱

1. **SQLite + vec0 单文件**: 部署简单、备份简单、依赖为零
2. **4D 时态**: 双时间轴让历史完整且可回放
3. **触发器强一致**: supersede → 级联失效边，应用层无需维护
4. **4 路召回 + RRF**: 语义 / 结构 / 时间 / 实体 互补融合
5. **证据可回溯**: 每条 relation → evidence_chunk_id → 原文

适合**个人 AI agent + 派**场景：单用户、几千-几万实体、需要时态和可解释性、不需要百万级 QPS。

---

*分析依据：schema.sql + memory.py + mcp_server.py + entity_resolve.py 全文 + 50 测试覆盖 + 7/18  1 周观察期*
