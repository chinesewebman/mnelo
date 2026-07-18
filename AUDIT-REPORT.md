# Hermes-Memory 记忆系统架构审计报告

**审计人**: 小默 (xiaomo, Memoh AI Agent)  
**审计日期**: 2026-07-18  
**审计范围**: `~/.hermes/memory/` 核心系统 + `macbot-memory/` 仓库 + `hermes-agent/plugins/memory/` 代理插件 + Mnemosyne MCP Provider  
**系统版本**: 自建, 2026-07 上线, 当前数据库 42 条 memory / 138 chunks / 88 entities

---

## 一、总体评估

**评级: B+** — 方向正确、骨架扎实，但处于「能用但未打磨」阶段。核心架构四层清晰（存储→嵌入→实体→MCP），两阶段混合召回（BM25 FTS + embedding rerank）的思路是业界标准做法。但细节处有若干工程债和设计盲区需要在生产规模化之前解决。

### 得分卡

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构设计 | **A-** | 分层清晰, SQLite + MCP 选型务实, 两阶段召回合理 |
| 数据模型 | **B+** | 7 表规范化设计好, 但缺 importance/source/temporal 字段 |
| 嵌入 & 检索 | **C+** | 模型选型(纯英文)不适合中文场景, 无向量索引, 无混合权重调节 |
| 实体解析 | **B-** | LLM 提取无缓存, 每次调用成本高, 准确率未评估 |
| 工程实现 | **B** | 核心代码质量尚可, 但 API client 有 dead code, 并发控制弱 |
| 测试覆盖 | **D** | 仅有基础 CRUD 测试, 缺召回精度/嵌入质量/并发/迁移完整性测试 |
| 文档 | **F** | ARCHITECTURE.md 和 SCHEMA.md 为空文件 |
| 生产就绪度 | **C+** | 能跑但缺监控、缺降级、缺连接池、Chinese embedding 问题未解决 |

---

## 二、架构分析

### 2.1 现状架构

```
┌─────────────────────────────────────────────┐
│  Hermes Agent (gateway :18793)               │
│  └─ memory_plugin.py                        │
│     └─ recall / remember / forget / stats    │
└──────────────┬──────────────────────────────┘
               │ HTTP (MCP SSE :8086)
┌──────────────▼──────────────────────────────┐
│  MCP Server (mcp_server.py)                  │
│  ├─ memory_create(text, tags, metadata)      │
│  ├─ memory_recall(query, limit)  ← 两阶段    │
│  │   ├─ Phase 1: BM25 FTS (FTS5)             │
│  │   └─ Phase 2: Embedding Cosine Rerank     │
│  ├─ memory_forget(memory_id)                 │
│  └─ memory_stats()                           │
└──────────────┬──────────────────────────────┘
               │
┌──────────────▼──────────────────────────────┐
│  MemoryStore (memory.py)                     │
│  ├─ CRUD + Chunking + Tagging                │
│  ├─ EntityResolver (entity_resolve.py)       │
│  │   └─ MiniMax API → extract entities       │
│  ├─ Embedder (embedder.py)                   │
│  │   └─ all-MiniLM-L6-v2 (384d, English)     │
│  └─ Schema: 7 tables (schema.sql)            │
└──────────────┬──────────────────────────────┘
               │
┌──────────────▼──────────────────────────────┐
│  SQLite (memory.db, WAL mode)                │
│  ├─ memories (42 rows)                       │
│  ├─ memory_chunks (138 rows)                 │
│  ├─ entities (88 rows)                       │
│  ├─ memory_entities (78 rows)                │
│  ├─ entity_relations (1 row) ← ⚠️ 几乎未使用  │
│  ├─ tags (3 rows)                            │
│  └─ memory_tags (1 row)                      │
└──────────────────────────────────────────────┘
         │ Git backup
┌────────▼─────────────────────────────────────┐
│  macbot-memory/  (GitHub: macbot-memory)      │
│  └─ devops/mnemosyne-memory/                 │
│     └─ memory-latest.db (副本)               │
└──────────────────────────────────────────────┘
```

### 2.2 与 Legacy 系统的共存问题 ⚠️

**现状**: 新系统 (SQLite 42条) 与旧系统 (`~/.hermes/memories/MEMORY.md`, 582行) **并行运行**。

- 迁移脚本 `migrate_to_hermes_memory.py` 存在但似乎未完全执行
- Agent 在 recall 时可能同时读到两套系统的数据，造成**记忆重复**
- Legacy MEMORY.md 有 token 限制问题（每次注入消耗上下文窗口），这正是新系统要解决的——但未彻底切换

**建议**: 明确迁移截止日期，迁移完成后将 legacy MEMORY.md 设为只读归档。

---

## 三、数据模型评审

### 3.1 表设计 (7表)

```sql
memories(id, content, metadata, created_at, updated_at)
memory_chunks(id, memory_id, chunk_index, content, embedding)
entities(id, name, type, description, created_at)
memory_entities(memory_id, entity_id)
entity_relations(source_entity_id, target_entity_id, relation_type)
tags(id, name, description)
memory_tags(memory_id, tag_id)
```

**优点**:
- 规范化程度合理，没有过度设计
- FTS5 全文索引合理利用 SQLite 内置能力
- embedding 存 BLOB 而非 JSON string，存储效率高

### 3.2 缺失的关键字段 🔴

| 缺失字段 | 影响 | 优先级 |
|----------|------|--------|
| `memories.importance` | 无法按重要性过滤/排序，低价值记忆污染召回 | **高** |
| `memories.source` | 不知道记忆来自哪个对话/会话，无法溯源 | **高** |
| `memories.access_count` | 无法做 LFU/LRU 淘汰或热度加权 | 中 |
| `memories.expires_at` | 无 TTL 机制，临时记忆永久存储 | 中 |
| `memories.memory_type` | 无法区分 fact / preference / event / summary | 中 |
| `memory_chunks.token_count` | 无法统计实际存储成本 | 低 |

### 3.3 entity_relations 几乎空白 🔴

88 个实体，仅 **1 条** relation。实体解析做了，但关系图谱完全没建起来。这等于有节点没边——知识图谱的一半价值丢了。

---

## 四、嵌入 & 检索深度分析

### 4.1 嵌入模型选型问题 🔴

**当前**: `all-MiniLM-L6-v2` (384维, 英文专用)

这是 **最严重的问题之一**。Hermes 的使用场景包含：
- 中文佛经 RAG (金刚经、楞伽经)
- 中文对话记忆
- 中文新闻/财经内容

`all-MiniLM-L6-v2` 对中文的语义理解非常弱。对比推荐：

| 模型 | 维度 | 中文支持 | 相对性能 |
|------|------|----------|----------|
| `all-MiniLM-L6-v2` (当前) | 384 | ❌ 差 | 基线 |
| `BAAI/bge-small-zh-v1.5` | 512 | ✅ 优秀 | MTEB-zh top-tier |
| `BAAI/bge-base-zh-v1.5` | 768 | ✅ 优秀 | 更高精度 |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 | ⚠️ 尚可 | 多语言折中 |

**代码中已经 import 了 `bge-small-en` 但从未使用** 😏 — 说明作者意识到问题但没动手改。

**建议**: 迁移到 `BAAI/bge-small-zh-v1.5` (512d)。需要重建所有 embedding。成本：约 138 条 chunk，秒级完成。

### 4.2 分块策略 (Chunking)

```python
class ChunkStrategy(enum.Enum):
    WORD = (50, 10)      # 50 words, 10 overlap
    CHAR = (200, 50)     # 200 chars, 50 overlap
    SENTENCE = (3, 1)    # 3 sentences, 1 overlap
    PARAGRAPH = (1, 0)   # 1 paragraph, no overlap
    NONE = (0, 0)        # single chunk
```

**问题**:
1. **WORD 模式对中文无效**: `text.split()` 按空格分词，中文没有空格 → 整段当做一个 "word"
2. **SENTENCE 模式**: 使用简单的 `.split('.')` + `!` + `?` 分隔，中文句号是 `。` 而不是 `.`
3. **没有 semantic chunking**: 不考虑语义边界，可能在句子中间切断
4. **PARAGRAPH = NONE**: 仅按 `\n\n` 分一段，基本等于不分

对于中文内容，实际生效的是 CHAR 模式 (200 chars)，这是合理的 fallback。但语义感知分块对于 RAG 质量至关重要。

**建议**: 引入简单的 semantic chunking（如按 `。！？\n` 智能分隔，保证 chunk 在语义边界上）。

### 4.3 检索质量 🔴

**两阶段召回**: FTS5 BM25 → Embedding Cosine Rerank

**问题**:
1. **FTS5 不是真正的 BM25**: SQLite FTS5 的 `bm25()` 函数需要显式启用。代码中使用的是自定义 BM25 实现，需要验证与标准 BM25 的一致性
2. **无混合权重参数**: BM25 和 embedding 之间没有可调节的 α 权重。某些查询语义更重要，某些关键词更重要
3. **暴力搜索**: 138 chunks 目前 O(n) cosine 没问题，但无向量索引意味着不能 scale
4. **无召回质量评估**: 没有 precision@k / recall@k / NDCG 测试

### 4.4 嵌入存储

Embedding 存为 `BLOB` (float32 x 384 直接序列化)，这是正确的做法。但没有向量索引 (FAISS/hnswlib)，未来超过 1000 chunk 时 cosine 计算会成为瓶颈。不过当前 138 chunks 完全不是问题——这是「提前担忧」而非「当前问题」。

---

## 五、实体解析评审

### 5.1 实现

`entity_resolve.py` 调用 MiniMax API，用 LLM 从 memory 文本中提取实体 (person/project/concept/event)，返回结构化 JSON。

**问题**:
1. **无缓存**: 相同或相似文本重复调用 API，浪费 token。应该对文本 hash 做 memoization
2. **无批量处理**: 逐条调用，N 条 memory = N 次 API call
3. **准确率未评估**: 88 个实体，不知道准确率和召回率
4. **LLM prompt 无版本管理**: 改了 prompt 后，历史实体的格式可能不兼容
5. **entity_relations 几乎空白**: 实体提取了但没建关系，说明 resolve 函数的 relation 提取部分基本没工作

---

## 六、工程实现评审

### 6.1 API Client (`hermes_memory_client.py`) 🔴

```python
# Dead code — 没有 gRPC 服务
import memory_pb2
import memory_pb2_grpc

# 自我 import — 奇怪的循环
from .hermes_memory_client import HermesMemoryClient
```

**问题汇总**:
- `memory_pb2` / `memory_pb2_grpc` import 是 dead code
- 自我 import 模式 (`from .hermes_memory_client import HermesMemoryClient`) 虽然不报错但不规范
- httpx → aiohttp → requests 三层 fallback 增加了维护复杂度
- 无 retry、无 circuit breaker、无 timeout 配置
- `HermesMemoryClient` 和 `MemoryClient` 两个类职责重叠

### 6.2 Embedder (`embedder.py`)

```python
import bge_small_en  # 从未使用
```

代码注释说 `SentenceTransformer doesn't set self.model`，需要用 `self.model = self._model` hack。这是 `sentence-transformers` 的已知行为，但应该用 `self.model = self` 或直接重命名变量更清晰。

### 6.3 MCP Server 并发

- 每次请求创建新的 `MemoryStore()` 实例 → 重复加载 embedding 模型
- SQLite 虽然 WAL 模式支持并发读，但写操作串行化。`memory_create` 包含 chunk + embed + entity resolve + insert —— 这是个长事务
- 没有连接池

### 6.4 错误处理

整体 `try/except Exception as e` 捕获太宽，且错误信息返回过于简单：
```python
except Exception as e:
    return {"error": str(e)}
```

应该区分：`MemoryNotFoundError`、`EmbeddingError`、`DatabaseError` 等。

---

## 七、Agent 集成评审

### 7.1 Memory Plugin

`hermes-agent/plugins/memory/memory_plugin.py` 作为 Hermes Gateway 的插件，在对话 pipeline 中调用 MCP 的 recall / remember。

**工作流**:
1. 用户发消息 → gateway 处理
2. **Recall**: 根据用户 query 调用 `memory_recall`，将相关记忆注入 context
3. **Response**: LLM 生成回复
4. **Remember**: 将重要对话内容调用 `memory_create` 存储

**问题**:
- Recall 和 Response 串行 → 增加延迟
- 没有 recall 质量反馈循环（检索到的记忆是否有用？→ 无记录）
- Remember 的触发阈值（什么值得记？）逻辑不透明

---

## 八、运维 & 可观测性

| 能力 | 状态 |
|------|------|
| 健康检查 (MCP stats) | ✅ 有 `/stats` |
| 日志 | ⚠️ 仅 print, 无结构化日志 |
| 指标 (prometheus) | ❌ 无 |
| 告警 | ❌ 无 |
| 数据库备份 | ⚠️ macbot-memory Git push 手动 |
| launchd 自动重启 | ❓ 不确定, plist 未找到 |
| 迁移回滚 | ❌ 无 |

---

## 九、测试覆盖

`test_memory.py` 仅覆盖 4 个基础场景:
1. ✅ Memory CRUD
2. ✅ 重复 memory 检测
3. ✅ Tag 操作
4. ✅ Stats

**缺失的关键测试**:
- ❌ 嵌入质量测试 (相似文本得相似向量)
- ❌ 召回精度测试 (precision@k / recall@k)
- ❌ 两阶段一致性测试 (FTS 和 embedding 结果应高度相关)
- ❌ 并发测试 (多连接同时读写)
- ❌ 迁移完整性测试 (legacy → new 不丢数据)
- ❌ 中文内容端到端测试
- ❌ Entity 提取准确率测试

---

## 十、优先级排序的改进建议

### 🔴 P0 (阻塞性 — 立即修复)

1. **替换 embedding 模型为中文兼容模型** (`BAAI/bge-small-zh-v1.5`)
   - 影响: 当前英文模型对中文内容基本无效
   - 成本: 重 embed 138 chunks, < 1 分钟
   
2. **清理 API Client dead code**
   - 删除 `memory_pb2` / `memory_pb2_grpc` import
   - 修复自我 import 模式
   - 统一 `HermesMemoryClient` / `MemoryClient` 为单一接口

3. **完成 Legacy → New 迁移并关闭 Legacy**
   - 运行 `migrate_to_hermes_memory.py` 并验证
   - 将 legacy MEMORY.md 改为只读归档

### 🟡 P1 (高优先级 — 本周)

4. **添加 `memories.importance` 和 `memories.source` 字段**
   - Schema migration + 回填脚本

5. **修复中文 Chunking**
   - 按中文标点 (`。！？；`) 分词
   - 验证 SENTENCE 模式对中文有效

6. **Entity 提取加缓存**
   - 文本 hash → 结果 memoization
   - 已有实体的文本不再调 API

7. **补充 ARCHITECTURE.md 和 SCHEMA.md 文档**
   - 目前两个文件都是空的 😏

### 🟢 P2 (中优先级 — 本月)

8. **引入 memory 合并/摘要机制**
   - 相关 memories 聚类 → 生成 summary memory
   - 减少 context 膨胀

9. **添加 entity_relations 自动构建**
   - Co-occurrence 分析
   - LLM 辅助关系提取

10. **添加 recall 质量评估**
    - 标注少量测试 query → ground truth
    - 定期跑 precision@k / NDCG

11. **结构化日志 + 基本监控**

### 🔵 P3 (低优先级 — 下个迭代)

12. **向量索引 (FAISS/hnswlib)** — 当前数据量不需要
13. **记忆衰减/遗忘曲线** — 让旧记忆自动降权
14. **Conflict detection** — 检测矛盾事实
15. **Temporal awareness** — 理解「上周说的」vs「去年说的」

---

## 十一、总结

Hermes-Memory 走在了正确的方向上。核心设计决策——SQLite 本地存储、两阶段混合召回、MCP 标准化接口、Entity 解析——都是务实且合理的。42 条 memory / 138 chunks / 88 entities 的起步数据说明系统已经在生产中使用。

但有三件事**不能等**：
1. **中文 embedding** — 这是地基问题，英文模型跑中文数据就是在浪费算力
2. **清理 dead code & 修复 chunking** — 工程债越拖越贵
3. **完成 legacy 迁移** — 双轨运行是最大的不确定因素

整体来看，这个系统像一个刚出 MVP 的产品——骨架对，肌肉还没长全。如果 P0/P1 问题在未来两周内解决，评级可以从 B+ 升到 A-。

---

**审计完成。等待 Ling 审阅反馈。** 😏
