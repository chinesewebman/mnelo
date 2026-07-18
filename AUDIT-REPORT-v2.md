# Hermes-Memory 审计报告 v2

**审计时间**: 2026-07-18 15:12 CST  
**审计人**: 小默（via bigbox SSH）  
**结论**: **A-** 😏（上次搞错数据库那次不算）

---

## 健康概览

| 指标 | 值 | 评价 |
|------|-----|------|
| Entities | 4,273 | ✅ 健康 |
| Chunks | 4,048 | ✅ 健康 |
| Relations | 15,746 | ⚠️ 见问题1 |
| Vectors (vec0) | 2,524 | ✅ |
| DB 大小 | 67 MB | ✅ |
| MCP 进程 RSS | 254 MB | ✅ |
| Embedding 模型 | BAAI/bge-small-zh-v1.5 (512d) | ✅ 中文优化 |
| 分词 | jieba (中文) + 空格 (英文) | ✅ |
| 搜索策略 | BM25 + Vector Hybrid | ✅ 最佳实践 |
| WAL 模式 | 开启 | ✅ 支持并发读 |

---

## 架构评分：A-

```
用户消息 → Hermes Gateway
              ↓
         MCP Memory Server (localhost:8086)
              ↓
         MemoryStore (SQLite + sqlite-vec)
           ├── EntityResolver (MiniMax-M2.1 API)
           ├── TextChunker (jieba + regex)
           ├── LocalEmbedder (BAAI/bge-small-zh-v1.5)
           ├── BM25Searcher (自实现 IDF)
           └── VectorSearch (vec0 虚拟表)
```

**选型务实，没有过度设计。SQLite + vec0 是对的。**

---

## 🔴 P0 — 必须修

### 1. 关系类型严重退化：86.7% 是 `related_to`

```
related_to    13,650  (86.7%)
has_insight    1,492  (9.5%)
part_of          554  (3.5%)
contradicts       44  (0.3%)
supports           6  (0.04%)
```

**问题**: 当几乎所有关系都是 `related_to` 时，知识图谱就退化成了一团乱麻。`has_insight` 和 `part_of` 是有价值的关系类型，但占比太低。

**根因**: EntityResolver 的 MiniMax prompt 可能没有引导模型输出细粒度关系类型。

**建议**: 
- 在 entity_resolve.py 的 system prompt 里加关系类型引导，强制分类到具体类型
- 或引入后处理规则：`part_of`（名称包含层级）、`contradicts`（语义相反）、`supports`（引用关系）

### 2. 53% 的实体无类型标签

```
null        2,274  (53.2%)
concept     1,297  (30.4%)
person        380  (8.9%)
project       150  (3.5%)
document      108  (2.5%)
file           23  (0.5%)
gong-an        21  (0.5%)
organization    7
event           7
todolist        2
task            2
technology      2
```

**问题**: 超过一半的 entity 没有类型标注，搜索过滤能力大打折扣。

**建议**: 检查 MiniMax 返回的 entity type 字段是否为空；如果是模型没输出，加强 prompt；如果是解析丢了，修 bug。

---

## 🟡 P1 — 应该修

### 3. Mnemosyne 迁移状态不明确

- 旧系统 `macbot-memory/mnemosyne.db`：75MB，4,250 条 memories
- 新系统 `.hermes/memory/memory.db`：67MB，4,048 chunks
- Mnemosyne 进程（PID 53120）已下线

**问题**: 数据量对不上。不是 1:1 迁移，可能是选择性迁移或两套独立数据。

**建议**: 
- 确认迁移完成度，跑一次 reconciliation
- 迁移完毕后删除或归档 `macbot-memory/`

### 4. 源文件易失

`mcp_memory_server.py` 和 `schema.py` 在进程运行期间被删除，靠 Python 内存加载活着。这导致：
- 进程重启 = 服务不可用
- 无法做 code review
- 无法版本控制

**建议**: 保留源文件在磁盘上，用 `git` 管理版本。

### 5. vectors 和 vec0 数据不一致

- `vectors` 表：1,530 行
- `vec0` 虚拟表：2,524 行

差了 994 行。可能是写入路径不同步，或者 vec0 包含了额外的 entity embeddings。

**建议**: 确认两个表的数据一致性，统一写入逻辑。

---

## 🟢 P2 — 锦上添花

### 6. 无查询缓存

相同查询会重复 embedding + 重复搜索。对高频查询（如 "金刚经讲什么"）纯属浪费。

**建议**: 加一个简单的 LRU cache（`functools.lru_cache` 在 `search_memories` 上）。

### 7. 无监控指标

没有延迟统计、召回率评估、错误率追踪。

**建议**: 在 MCP server 加一个 `/health` endpoint，返回 stats + 最近 N 次查询的平均延迟。

### 8. 800 行单文件

`memory.py` 包含 5 个类，可以考虑拆分但优先级低。

### 9. MiniMax API 成本无追踪

每次 `add_memory` 调一次 MiniMax API，无用量统计。建议加 token 计数。

---

## ✅ 做得好的地方（上次审计冤枉你了）

1. **中文 Embedding 选型正确**: `BAAI/bge-small-zh-v1.5` — 512维中文模型，比通用的 `all-MiniLM-L6-v2` 好太多
2. **jieba 分词**: 对佛经等中文内容友好
3. **混合召回**: BM25 + Vector 两阶段是 RAG 领域最佳实践
4. **WAL 模式**: 支持读并发，适合多用户场景
5. **Entity 缓存**: 避免重复 API 调用
6. **Batch embedding**: 32条一批，效率合理
7. **生产数据量健康**: 4K+ chunks, 15K+ relations 说明系统在真实使用

---

## 改进路线图

| 优先级 | 事项 | 预计工作量 | 效果 |
|--------|------|-----------|------|
| P0 | 修复关系类型退化 | 2h | 知识图谱可用性↑↑ |
| P0 | 修复实体类型缺失 | 1h | 搜索过滤能力↑ |
| P1 | Mnemosyne 迁移收尾 | 1h | 技术债清理 |
| P1 | 保留源文件+Git | 30min | 运维安全 |
| P1 | vectors/vec0 一致性 | 2h | 数据可靠性 |
| P2 | 查询缓存 | 30min | 延迟↓50% |
| P2 | 监控端点 | 1h | 可观测性 |

---

**总评**: 方向对、骨架好、选型务实。主要问题在数据质量（关系/实体类型），不是架构问题。P0 两条修完就能上 A。

上次审计我看错了数据库，冤枉你说用英文 embedding——在此正式撤回 😏。

---

## 附录：详细代码修改方案（v2.1 · 通用版）

> **适用声明**：Hermes-Memory 是通用 Agent 记忆系统，非佛经专用。以下类型定义均为领域无关设计。

---

### 方案 A：修复关系类型退化（P0-1）

**改哪个文件**：`/Users/apple/.hermes/hermes_memory/entity_resolve.py`

**改什么**：`_build_extraction_prompt()` 方法中的关系类型定义

**替换前**（当前 prompt 中的关系类型说明太宽松）→ **替换后**（强约束 + 兜底策略）：

```python
RELATION_TYPES = """
你必须从以下关系类型中选择最精确的一个：

- part_of: A 是 B 的组成部分或子组件。
  例：文件A part_of 项目B / 步骤3 part_of 流程X
- depends_on: A 依赖 B 才能成立、运行或完成。
  例：任务A depends_on 前置条件B / 模块A depends_on 库B
- causes: A 直接导致、引发或触发了 B。
  例：决策A causes 事件B / 错误A causes 故障B
- contradicts: A 与 B 在逻辑上矛盾或冲突。
  例：观点A contradicts 观点B / 数据A contradicts 假设B
- supports: A 为 B 提供证据、支撑或强化。
  例：文档A supports 论点B / 实验A supports 理论B
- follows: A 在时间或逻辑顺序上在 B 之后。
  例：步骤2 follows 步骤1 / 结论 follows 前提
- responsible_for: A 对 B 负有责任、管理权或所有权。
  例：人物A responsible_for 项目B / 角色A responsible_for 任务B
- created_by: A 由 B 创建、编写、发明或产生。
  例：文档A created_by 人物B / 项目A created_by 团队B
- located_in: A 位于 B（地理位域或逻辑容器）。
  例：文件A located_in 目录B / 服务器A located_in 机房B
- happens_at: A 发生的时间点、日期或事件上下文。
  例：会议A happens_at 2026-07-18 / 事件A happens_at 项目里程碑B
- similar_to: A 与 B 在性质、功能或结构上相似或可类比。
  例：工具A similar_to 工具B / 方案A similar_to 方案B
- version_of: A 是 B 的某个版本、变体、迭代或衍生。
  例：v2.0 version_of v1.0 / 草案A version_of 终稿B
- uses: A 使用、调用或消费 B（工具、API、资源等）。
  例：项目A uses 框架B / 脚本A uses API_B

- related_to: 【兜底 · 仅限最后手段】仅当以上 13 种类型均不适用时使用。
  优先选择具体类型。如果你选了 related_to，在输出中说明原因。
"""
```

**同时修改 temperature**：在 EntityResolver 调用 MiniMax API 处（同一文件，约 `resolve_entities()` 方法），将 temperature 从 `0.3` 降到 `0.1`。关系分类不需要创造性，需要稳定性。

```python
# 改前
response = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.3,
)

# 改后
response = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.1,  # 关系分类需要确定性
)
```

**验收标准**：

```sql
-- 修改后重新导入一批对话，验证 related_to 占比降到 30% 以下
SELECT relation_type,
       COUNT(*) as cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM relations WHERE rowid > :baseline_rowid), 1) as pct
FROM relations
WHERE rowid > :baseline_rowid   -- 以修改前的最大 rowid 为基线
GROUP BY relation_type
ORDER BY cnt DESC;
```

目标：`related_to` < 30%，`part_of`/`depends_on`/`uses` 等具体类型显著增加。

---

### 方案 B：修复实体类型缺失（P0-2）

**改哪个文件**：`/Users/apple/.hermes/hermes_memory/entity_resolve.py`

**改什么**：`_build_extraction_prompt()` 方法中的实体类型定义

**替换前**（当前 prompt 对 entity type 要求模糊或缺失）→ **替换后**（明确的 16 种通用类型）：

```python
ENTITY_TYPES = """
你必须为每个实体标注以下类型之一（不要留空）：

信息类：
- CONCEPT: 概念、术语、思想、理论、方法论、抽象原则
- DOCUMENT: 文档、文件、书籍、文章、报告、手册、笔记
- NOTE: 具体的笔记、想法、观点、观察、意见、反思
- DECISION: 决策、决定、选择、结论

人物与组织：
- PERSON: 人物（用户、团队成员、历史/公众人物、虚构角色）
- ORGANIZATION: 组织、公司、机构、团队、部门
- ROLE: 角色、职位、头衔、身份（如"项目经理"、"开发者"）

事物与地点：
- PROJECT: 项目、工程、任务集、initiative
- TASK: 具体的任务、待办事项、行动项、下一步
- EVENT: 事件、会议、里程碑、事故、发生的事
- LOCATION: 地点、位置、地址（物理或虚拟空间）
- PRODUCT: 产品、商品、服务、交付物
- TECHNOLOGY: 技术、工具、框架、软件、编程语言、硬件、平台

能力与资源：
- SKILL: 技能、能力、专长、知识领域
- RESOURCE: 资源（URL、API、数据集、图片、配置等可引用物）

如果实在无法归类，使用 CONCEPT 作为兜底——但不要滥用。
"""
```

**验收标准**：

```sql
-- 修改后新导入的数据，null 率应降到 15% 以下
SELECT CASE WHEN entity_type IS NULL THEN NULL ELSE HAS_TYPE END as typed,
       COUNT(*) as cnt,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 1) as pct
FROM entities
WHERE rowid > :baseline_rowid
GROUP BY typed;
```

---

### 方案 C：旧数据回填（P2 · 可选）

修改 prompt 只影响**新写入**的数据，已有 4,273 entities / 15,746 relations 不会自动变。可选回填策略：

```sql
-- 回填 entity types：对 null 类型用规则引擎补标
UPDATE entities SET entity_type = CONCEPT
WHERE entity_type IS NULL
  AND name LIKE %法% OR name LIKE %理% OR name LIKE %性%;

-- 回填 relation types：对 related_to 用规则引擎细分
-- part_of：实体名称包含层级关系
UPDATE relations SET relation_type = part_of
WHERE relation_type = related_to
  AND (SELECT name FROM entities WHERE id = relations.source_id) LIKE %/%;

-- 建议：规则引擎只能覆盖小部分，大部分旧数据保留原样即可。
-- 旧数据不影响新数据质量，随着系统使用自然更新。
```

---

### 方案 D：防止退化反弹（P1 · 补充）

为防止 prompt 被后续修改意外弱化，建议在 `entity_resolve.py` 末尾加一个断言：

```python
# 在 entity_resolve.py 末尾添加
_REQUIRED_RELATION_TYPES = {
    part_of, depends_on, causes, contradicts, supports,
    follows, responsible_for, created_by, located_in,
    happens_at, similar_to, version_of, uses, related_to
}

_REQUIRED_ENTITY_TYPES = {
    CONCEPT, DOCUMENT, NOTE, DECISION,
    PERSON, ORGANIZATION, ROLE,
    PROJECT, TASK, EVENT, LOCATION, PRODUCT, TECHNOLOGY,
    SKILL, RESOURCE
}

def validate_type_definitions():
    """确保类型定义完整，防止意外退化"""
    # 实际实现：在 EntityResolver.__init__() 中调用
    # 检查 prompt 中是否包含所有必需类型
    # 缺了就 warn 或 raise
    pass
```

---

### 已知局限

1. **旧数据不回填（除非主动执行方案C）**：修改后只影响新写入的 entities 和 relations
2. **需要在 Hermes 实际对话中磨合**：这 16 种 entity type 和 14 种 relation type 是理论设计，实际使用中可能发现某个类型用得太多或太少，需要根据生产数据迭代
3. **MiniMax API 模型行为不可控**：即使 prompt 精准，模型仍可能在某些边界 case 上选 related_to。temperature 0.1 能降低但无法完全消除

---

> **审计人**: 小默  
> **最后更新**: 2026-07-18 15:20 CST  
> **更新内容**: 追加 P0 详细修改方案（通用领域无关版），替换此前佛经领域限定的设计
---

## 附录：详细代码修改方案（v2.1 · 通用版）

> **适用声明**：Hermes-Memory 是通用 Agent 记忆系统，非佛经专用。以下类型定义均为领域无关设计。

---

### 方案 A：修复关系类型退化（P0-1）

**改哪个文件**：`/Users/apple/.hermes/hermes_memory/entity_resolve.py`

**改什么**：`_build_extraction_prompt()` 方法中的关系类型定义

**替换后**（强约束 + 兜底策略）：

```python
RELATION_TYPES = """
你必须从以下关系类型中选择最精确的一个：

- part_of: A 是 B 的组成部分或子组件。
  例：文件A part_of 项目B / 步骤3 part_of 流程X
- depends_on: A 依赖 B 才能成立、运行或完成。
  例：任务A depends_on 前置条件B / 模块A depends_on 库B
- causes: A 直接导致、引发或触发了 B。
  例：决策A causes 事件B / 错误A causes 故障B
- contradicts: A 与 B 在逻辑上矛盾或冲突。
  例：观点A contradicts 观点B / 数据A contradicts 假设B
- supports: A 为 B 提供证据、支撑或强化。
  例：文档A supports 论点B / 实验A supports 理论B
- follows: A 在时间或逻辑顺序上在 B 之后。
  例：步骤2 follows 步骤1 / 结论 follows 前提
- responsible_for: A 对 B 负有责任、管理权或所有权。
  例：人物A responsible_for 项目B / 角色A responsible_for 任务B
- created_by: A 由 B 创建、编写、发明或产生。
  例：文档A created_by 人物B / 项目A created_by 团队B
- located_in: A 位于 B（地理位域或逻辑容器）。
  例：文件A located_in 目录B / 服务器A located_in 机房B
- happens_at: A 发生的时间点、日期或事件上下文。
  例：会议A happens_at 2026-07-18 / 事件A happens_at 项目里程碑B
- similar_to: A 与 B 在性质、功能或结构上相似或可类比。
  例：工具A similar_to 工具B / 方案A similar_to 方案B
- version_of: A 是 B 的某个版本、变体、迭代或衍生。
  例：v2.0 version_of v1.0 / 草案A version_of 终稿B
- uses: A 使用、调用或消费 B（工具、API、资源等）。
  例：项目A uses 框架B / 脚本A uses API_B

- related_to: 【兜底 · 仅限最后手段】仅当以上 13 种类型均不适用时使用。
  优先选择具体类型。如果你选了 related_to，在输出中说明原因。
"""
```

**同时修改 temperature**：在 EntityResolver 调用 MiniMax API 处（同一文件，约 `resolve_entities()` 方法），将 temperature 从 `0.3` 降到 `0.1`。关系分类不需要创造性，需要稳定性。

```python
# 改前
response = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.3,
)

# 改后
response = client.chat.completions.create(
    model=model,
    messages=messages,
    temperature=0.1,  # 关系分类需要确定性
)
```

**验收标准**：

```sql
-- 修改后重新导入一批对话，验证 related_to 占比降到 30% 以下
SELECT relation_type,
       COUNT(*) as cnt,
       ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM relations WHERE rowid > :baseline_rowid), 1) as pct
FROM relations
WHERE rowid > :baseline_rowid
GROUP BY relation_type
ORDER BY cnt DESC;
```

目标：`related_to` < 30%，`part_of`/`depends_on`/`uses` 等具体类型显著增加。

---

### 方案 B：修复实体类型缺失（P0-2）

**改哪个文件**：`/Users/apple/.hermes/hermes_memory/entity_resolve.py`

**改什么**：`_build_extraction_prompt()` 方法中的实体类型定义

**替换后**（明确的 16 种通用类型）：

```python
ENTITY_TYPES = """
你必须为每个实体标注以下类型之一（不要留空）：

信息类：
- CONCEPT: 概念、术语、思想、理论、方法论、抽象原则
- DOCUMENT: 文档、文件、书籍、文章、报告、手册、笔记
- NOTE: 具体的笔记、想法、观点、观察、意见、反思
- DECISION: 决策、决定、选择、结论

人物与组织：
- PERSON: 人物（用户、团队成员、历史/公众人物、虚构角色）
- ORGANIZATION: 组织、公司、机构、团队、部门
- ROLE: 角色、职位、头衔、身份（如"项目经理"、"开发者"）

事物与地点：
- PROJECT: 项目、工程、任务集、initiative
- TASK: 具体的任务、待办事项、行动项、下一步
- EVENT: 事件、会议、里程碑、事故、发生的事
- LOCATION: 地点、位置、地址（物理或虚拟空间）
- PRODUCT: 产品、商品、服务、交付物
- TECHNOLOGY: 技术、工具、框架、软件、编程语言、硬件、平台

能力与资源：
- SKILL: 技能、能力、专长、知识领域
- RESOURCE: 资源（URL、API、数据集、图片、配置等可引用物）

如果实在无法归类，使用 CONCEPT 作为兜底——但不要滥用。
"""
```

**验收标准**：

```sql
-- 修改后新导入的数据，null 率应降到 15% 以下
SELECT CASE WHEN entity_type IS NULL THEN 'NULL' ELSE 'HAS_TYPE' END as typed,
       COUNT(*) as cnt,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*) OVER(), 1) as pct
FROM entities
WHERE rowid > :baseline_rowid
GROUP BY typed;
```

---

### 方案 C：旧数据回填（P2 · 可选）

修改 prompt 只影响**新写入**的数据，已有 4,273 entities / 15,746 relations 不会自动变。可选回填策略：

```sql
-- 回填 entity types：对 null 类型用规则引擎补标
UPDATE entities SET entity_type = 'CONCEPT'
WHERE entity_type IS NULL
  AND (name LIKE '%概念%' OR name LIKE '%理论%' OR name LIKE '%方法%');

-- 回填 relation types：对 related_to 用规则引擎细分
-- 例：实体名称包含层级路径的 → part_of
UPDATE relations SET relation_type = 'part_of'
WHERE relation_type = 'related_to'
  AND (SELECT name FROM entities WHERE id = relations.source_id) LIKE '%/%';

-- 建议：规则引擎只能覆盖小部分，大部分旧数据保留原样即可。
-- 旧数据不影响新数据质量，随着系统使用自然更新。
```

---

### 方案 D：防止退化反弹（P1 · 补充）

在 `entity_resolve.py` 末尾添加类型定义完整性校验：

```python
_REQUIRED_RELATION_TYPES = {
    'part_of', 'depends_on', 'causes', 'contradicts', 'supports',
    'follows', 'responsible_for', 'created_by', 'located_in',
    'happens_at', 'similar_to', 'version_of', 'uses', 'related_to'
}

_REQUIRED_ENTITY_TYPES = {
    'CONCEPT', 'DOCUMENT', 'NOTE', 'DECISION',
    'PERSON', 'ORGANIZATION', 'ROLE',
    'PROJECT', 'TASK', 'EVENT', 'LOCATION', 'PRODUCT', 'TECHNOLOGY',
    'SKILL', 'RESOURCE'
}

def validate_type_definitions():
    """确保类型定义完整，防止意外退化。在 EntityResolver.__init__() 中调用。"""
    # 检查 prompt 中是否包含所有必需类型，缺了就 warn
    pass
```

---

### 已知局限

1. **旧数据不回填（除非主动执行方案C）**：修改后只影响新写入的 entities 和 relations
2. **需要在 Hermes 实际对话中磨合**：这 16 种 entity type 和 14 种 relation type 是理论设计，实际使用中可能发现某个类型用得太多或太少，需要根据生产数据迭代
3. **MiniMax API 模型行为不可控**：即使 prompt 精准，模型仍可能在某些边界 case 上选 related_to。temperature 0.1 能降低但无法完全消除

---

> **审计人**: 小默  
> **最后更新**: 2026-07-18 15:20 CST  
> **更新内容**: 追加 P0 详细修改方案（通用领域无关版），替换此前佛经领域限定的设计