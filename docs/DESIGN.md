# mnelo 顶层设计蓝图

> **定位**：本文件是 mnelo 的**演进蓝图**——描述目标架构、各层设计与演进路线。
> **现状基线**：`ARCHITECTURE.md`（当前实现分析）、`SCHEMA.md`（SQL schema 参考）。
> **版本**：v0.15 · 2026-08-15 · 写路径显式事务 (commit 8519089) + 召回质量分析 (99ae38d) + RRF methods 累加 (28c846c) + §6.7 / §7.1 同步落地。
> **v0.15 变更**（8/15 实战 3 E 闭环）——§6.7 新增 v0.15 写路径 + 召回质量章节：3 大 E 改进的设计哲学（**mnelo 自身痛点驱动，非 Mem0 借鉴**）、落地架构表、用法变化、已知坑（usearch 索引独立于 SQLite 事务 / RRF lane 覆盖 / 标签数据污染 / numpy percentile linear interpolation）、CI 实战 3-commit chain 0 fail 验证。**关键 reflect**：v0.15 3 个 E 改进**不是 Mem0 借鉴**——是 mnelo 自身 §1.2 短板修复 + 实战数据驱动决策。真 Mem0 借鉴（scoping_ids / memory_correct / dedup_check）落地状态见 §6.7.4 "Mem0 借鉴落地对照表"。主人 8/9 SKILL "凡是不符合最新情况的, 都改, 全面 reflect, 不补丁式" → 纠正 v0.14 顶部含糊的 "Mem0 借鉴" 表述，老老实实分类每个改进的真驱动。§7.1 召回质量指标从"待设计"升级为"已落地"（E-3 `memory_recall_stats` 工具 + 17 个 Prometheus 指标升级到 19 个）。
> **v0.14 变更**（8/14 mcp_server split 实战落地）——§6.6 新增 mcp_server.py 拆分 + PEP 562 facade 设计章节：6 个子模块职责表 + facade 代码 pattern + 4 条已知坑（PEP 562 setattr 不 work / from import value-binding / _load_from_repo separate instance / facade import 占 dict）+ Test contract 表 + CI 实战 aggregate 0 fail 验证。Subagent 8/14 通过隔离测试发现 PEP 562 setattr 限制，反向 commit c9697f8 修正 db522b3 错设计（**专家纪律价值示范**：subagent self-verify 救主）→ 主人 8/6 教训升级。
> **v0.13 变更**（8/4 可逆压缩设计）——§4.5.2 新增**可逆压缩**（⟵ 借鉴 Headroom CCR）：摘要行带 `source_chunk_ids` provenance 指针 + `memory_get_digest(ref=...)` 按需展开；信息单源不破，截断可恢复。§5.7 工具清单同步 `memory_get_digest(ref=None)` 双模式。
> **v0.12 变更**（hermes 实际数据评审 8/4 + 主人 deepseek-v4-flash 交叉验证 + bug 修复）——§1.1 **实际回灌**：实测召回量 1.1 次/日（人脑级）+ Phase 1 placeholder id 100% 命名错位 + Phase 2 30 天延迟清的"延期/清"两步半完成状态；**§3.8 §5.6 done bug 修**：`run_purge_worker()` 落地 (commit 4bd654d, 125 行) — 3 phase (clean_orphan_target_ids / 物理删 + set done=1 / vec0 orphan cleanup)；§1.1 标注 TASKS 未建 schema 前置 (`user_confirmed` / `processed_at` / `audit_log` 仍缺 — H0 真前置)；§3.0 memory_type 字段已落地但实际 0% non-fact (根因：写入方不分类 + 无 P1 提取器)；§8.3 P3 升级档触发条件实际 scale 评估 (4344 chunks 距 50 万差 115 倍，延迟 30ms 内 — 升级档面向未来备选)。
> **v0.13 变更**（8/4 可逆压缩设计）——§4.5.2 新增**可逆压缩**（⟵ 借鉴 Headroom CCR）：摘要行带 `source_chunk_ids` provenance 指针 + `memory_get_digest(ref=...)` 按需展开；信息单源不破，截断可恢复。§5.7 工具清单同步 `memory_get_digest(ref=None)` 双模式。
> **v0.3 变更**：全方位专家评审后补入——产品边界（§1.4）、记忆类型谱系（§3.0）、双轨组织模型（§4.8）、新近度加权（§4.9）、来源可信度（§4.10）、并发与保留（§3.9）、工具收敛（§6.5）。
> **v0.4 变更**：采纳 hermes agent 评审反馈——P1 提取拆 P1a(规则)/P1b(LLM)（§5.2）、correct() 与 user_confirmed 边界明确化（§3.7）、工具收敛提前到 P1 末（§9）、git 快照改 `VACUUM INTO` 且不进主仓（§3.8）。
> **v0.5 变更**：Q4 修正——快照改 `sqlite3 .backup` → `snapshots/YYYYMMDD.db.gz` 归档、rsync 到 NAS，git 跟踪二进制方案排除；修正 DB 体积基线（实测 44.72MB+WAL，README ~24MB 已过期）。Q5——健康度权重不预设，P2 等权 + 0.6 警戒线起步。
> **v0.6 变更**：§3.0 从"记忆类型谱系"扩展为**正式数据模型**——记忆=chunk+entity+relation 双表示、三对象边界定义、kind×memory_type 双谱系正交澄清、entity 建置判定规则、relation 语义（weight/confidence/evidence 分工）。
> **v0.7 变更**：细化四个设计空档——召回请求模型（§4.11 意图化查询 + lane 路由 + 排序因子合成）、ID 命名空间策略（§3.10 SYSTEM/SEMANTIC/RESERVED + 冲突矩阵）、L2 执行原子性与失败语义（§5.9 每 proposal 一事务 + watermark 门控）、快照恢复流程（§3.11 六步恢复 + 损坏检测）、安全与信任边界（§12 存储内容反噬防线）。
> **v0.8 变更**：深化到可实施粒度——§4.11 分解管线（entity spotter/aspect/时间词/intent 判定规则 + intent 行为调整 + 与 reason/topics 关系澄清 + 多意图/失败语义）；§3.10 命名空间文法正则 + slug 化规则 + validate_id 前缀强制 + relation id 不回收 + chunk↔rowid 映射；§5.9 提案生命周期状态机 + watermark schema + 回退级联；§3.11 双层完整性校验 + 坏快照降级链 + 恢复自动化脚本；§12 输出数据围栏格式 + 威胁模型表（in/out）。
> **v0.9 变更**（主人指示 + 整体复查修订）——§12 确立**无道德立场（amoral by design）**原则：mnelo 不做内容价值判断（合法/涉密/冒犯），威胁模型只覆盖"存取机制被滥用"，内容价值判断从设计中移除；§3.0.6 定案 entity 路 type 软加权（硬过滤只限 chunk 路，关掉开放决策）；§4.11.4 补 aspect 消费端（lane 偏向 + 权重映射）；§9 P0 标注 §3.0 已落地。
> **v0.10 变更**：6 处遗留加深到可实施粒度——§3.4.1 写事务边界表 + embed 同步/异步取舍；§4.1.1-4.1.3 FTS5 中文分词器决策（trigram）+ 外部内容表触发器 + 软删一致性 + BM25×importance 查询；§4.8.1 location 各 lane 过滤语义 + 空子树/复合约束；§4.11 排序因子默认值（λ₁=0.3/λ₂=0.2/α=0/半衰期 30 天）；§4.5.1 digest 生成刷新机制（三块来源 + dirty 触发 + LLM 可选）；§3.7.1 dedup_check 结构化三元组匹配键 + 场景表。
> **v0.11 变更**（hermes 评审 8/4 采纳）——§8.3 适配器分档加 usearch 档 + **fail-fast 回落策略**（显式配置不可用默认报错，`ALLOW_FALLBACK=1` 才回落）；§9 阶段说明（P3 已落地，P1 卫生 pass **已排期 Q3 末**，任务分解在 `docs/TASKS_L2_HYGIENE.md`）；§3.6 跨存储一致性（Q1/Q2）→ 指到 TASKS A7（含 auto-repair + drift 指标）。
> **v0.12 变更**（实际数据评审 + deepseek 交叉 + bug 修 8/4）——见顶部说明。
> **约定**：`P0/P1/P2/P3` = 演进阶段，见 §9。所有设计遵循现有六条 design tenets（local-first / 单文件 / 标准 MCP / 双语 / boring & predictable / measured）。
> **借鉴来源**：标 `⟵ 借鉴 <系统>` 的条目，其思路来自对 Mem0 / Letta(MemGPT) / Zep(Graphiti) / Cognee / LangMem / SuperMemory / Hindsight 的调研（2026-08），按 mnelo 的 local-first 单机约束裁剪。

---

## 1. 现状评估

### 1.1 已核对的架构事实

| 维度 | 现状 |
|---|---|
| 核心类 | 单巨石 `Memory`（`memory.py` ~1600 行，v0.5.12 + 8/4 +125 行 `run_purge_worker`）：CRUD + 4 路召回 + RRF + 实体管理 + 统计 + 清理 + **purge worker (3 phase)** |
| 存储 | 单文件 SQLite，11 表（entities/chunks/relations/vectors/recall_log/purged_queue/meta + 4 触发器） |
| 召回 | 4 路：vector（sqlite-vec vec0）/ graph（2-hop BFS）/ meta（`LIKE %q%`）/ entity（name/alias），RRF（k=60）融合 |
| 时态 | 双时态软删除（valid_from/valid_until + created_at/updated_at），superseded_by 链 + 触发器级联 |
| 协议 | MCP over SSE（127.0.0.1:8086），10 个工具，Bearer token + loopback-only + 限流 60/min/tool |
| 可观测性 | 17 个 Prometheus 指标（运维 RED 视角），`recall_log` 审计表 |
| 客户端 | `api/mnelo_client.py` MneloClient（每调用新建 SSE 连接） |
| 校验 | `validation.py` 已完善（8KB/1KB 上限、控制字符/bidi/零宽清洗、ValidationError 带字段） |
| 维护 | **8/4 落地 `run_purge_worker()`**（commit 4bd654d）：3 phase = ①清 placeholder id（v0.5.12 100% 脏数据已清 4198 项）② 30 天延迟清的物理删 + set done=1（不破坏 §3.8 意图）③ vec0 orphan cleanup。**forget() 行为完全不变**——worker 是独立清理入口，主人 cron 可调 |

### 1.1.1 实际数据回灌 (2026-08-04, 8/4 hermes 评审 + deepseek 交叉验证)

> 详细审计报告：`mnelo_schema_audit.md` (v0.3, 38 KB / 650 行)
> 实际 DB 真值: `$MNELO_MEMORY_DIR/memory.db` 44.72 MB / 4344 chunks / 4498 entities / 56730 relations / 9015 recall_log

**实际规模与用法 (主人 14 天, 7/19-8/4):**

| 指标 | 实际真值 | DESIGN 假设 | 偏差 |
|---|---|---|---|
| 召回频次 | **1.1 次/日** (7/21-8/4 真战, 116 次/13 days) | 持续活跃 | **人脑级, 不是搜索引擎级** |
| p50/p95/p99 latency | 13.7ms / 30.9ms / 68.2ms | "<~50万向量才换 zvec" | 当前 scale 完全够用 |
| 召回空窗率 (实际 13/13 天) | **0%** | — | demo 阶段 3.3%, 实际 0% |
| memory_type 分布 | **100% fact** (4344/4344) | 6 类系统落地 | **6 类实际空架子** — 根因：写入方 (Hermes agent, conv 68%) 不分类 + 无 P1 提取器 |
| chunks soft-deleted | 12 (14 天) | — | forget() 几乎没用过 |
| entities soft-deleted | 115 (14 天) | — | 同上 |
| purged_queue 入队 | 4308 (7/18 升级脏数据) | — | **100% target_id 命名错位** (placeholder id) — 8/4 bug 2 修后剩 110 真软删项 |

**TASKS 依赖的 schema 前置 (8/4 实际验证, 仍缺):**

| 主人 DESIGN § 引用 | 预期 schema | 实际状态 | 影响 |
|---|---|---|---|
| §3.7 实体纠正 `Memory.correct()` 受 `user_confirmed` 不可变规则约束 | entities `user_confirmed` 列 | **缺** | §3.7 设计落不了, TASKS H0 之前需建 |
| TASKS_L2_HYGIENE H5 watermark 需 `chunks/entities.processed_at` | 两表 `processed_at` 列 | **缺** | H5 落不了, H0 之前需建 |
| TASKS_L2_HYGIENE H0 audit_log + Proposal/Policy/Applier 基建 | `audit_log` 表 | **缺** | H0 整个 L2 落不了, 必前置 |
| §3.0 memory_type 6 类 | chunks/entities `memory_type` 列 | ✅ 落地 (但 0% 实际) | 字段已加, 实际全 fact |
| 双时态 `valid_from/valid_until` | 两表 | ✅ | 真用 |

**实际数据驱动的 actionable 调整 (按 v0.3 报告 §9):**

- §1.4 边界: mnelo 是"个人单机记忆系统", 实际召回量 1.1 次/日确认 = 不是搜索引擎设计
- §8.3 P3 升级档触发条件: 实际 4344 chunks / 30ms 内延迟 = 升级档面向未来备选, 不是当前瓶颈 (P3 已在 v0.11 落地 usearch + zvec 适配器)
- §3.0 memory_type: 6 类系统落不了, **必须先有 P1 提取器** (P1a 规则 + P1b LLM, §5.2)
- §3.8 §5.6 purge: **8/4 done bug 已修** (`run_purge_worker`), 但主人 cron 需手动调 (`m.run_purge_worker(dry_run=False)` 周期跑)

### 1.2 结构性短板（本蓝图的动因）

1. **无记忆生命周期**——纯被动存储。存什么、何时作废全由调用方（Agent）决定；无提取、无矛盾检测、无整合、无记忆卫生。
2. **单巨石无分层**——存储/检索/实体管理耦合，无存储抽象缝，"可迁移路径"无从谈起。
3. **双时态不完整**——chunks 无 `valid_from`；向量在 update/forget 时**物理删除**，语义层无法回放历史。
4. **检索层三个缺陷**——meta 路 `LIKE %q%` 无索引 O(N)；entity 路不匹配实体 **id**（只 name/alias）；RRF 的 `method` 标签被后处理的 lane 覆盖，污染召回反馈环数据。
5. **协议层有旁路**——`memory_entity_resolve` / `memory_list_entities` / `memory_search_relations` 三个工具是 **raw-SQL handler**（`_CUSTOM_HANDLERS`），绕过 `Memory` 类直查 SQLite，无分页、无统一校验。
6. **无召回质量评测**——`benchmark.py` 只测延迟；`recall_log` 里的 `recall_details_json`（每 hit 的 method/rank/score）**无人消费**做质量分析。17 个指标全是运维视角，没有 precision@k。
7. **写路径无显式事务/回滚**——remember/update 的 chunk+entities+relations+vector 多步写入依赖 Python sqlite3 的隐式事务（无 `BEGIN`/`ROLLBACK` 包裹）。中途异常（如 embed 失败）时隐式事务保持打开，单例连接复用下后续操作可能把部分数据一并提交。

### 1.2.1 实际数据驱动的 8/4 新增短板

> 8/4 hermes 实际数据评估 (v0.3 报告) + deepseek-v4-flash 交叉验证 + commit 4bd654d 修复

- **8. §3.8 §5.6 30 天延迟清半完成**（已修）——v0.5.12 之前 `forget()` 入 `purged_queue done=0` 后，**没有任何代码路径 set done=1**，也没物理删 worker。8/4 落地 `run_purge_worker()` 3 phase 修复：①清 placeholder id (v0.5.12 100% 脏数据已清 4198 项) ② 物理删 + set done=1 ③ vec0 orphan cleanup。**实际影响**: 8/16 起 30 天到期的真软删项 (110 项, 12 chunk + 97 entity 软删过 + 1 entity edge) 会自动 Phase 2 物理删
- **9. §3.0 memory_type 6 类系统实际空架子**（未修，待 P1 提取器）——memory_type 字段已加 (f1bc1bf), 但 4344/4344 chunks 100% fact。根因：写入方 (Hermes agent, conv 68%) 从不分类 + 系统无 P1 提取器 (P1a 规则 + P1b LLM, §5.2) 自动推断。**实际意义**: 6 套生命周期 + 6 套矛盾规则 + H3 TTL 表 = 等 P1 提取器落地才能真用
- **10. §1.1 现状与实际 schema 漂移 3 项**（未修，待 H-1）——TASKS 依赖的 `user_confirmed` / `processed_at` / `audit_log` 仍缺（详见 §1.1.1 TASKS schema 前置表）— H-1 是 H0 真前置，不修 H-1 整个 L2 落不了
- **11. §8.3 P3 升级档触发条件实际 scale 错配**（v0.11 已定档，**scale 评估不需改**）——实际 4344 chunks 距 §8.3 "向量 >~50 万" 升级档差 115 倍, 实际延迟 30ms 内；usearch/zvec 面向未来备选, 不是当前瓶颈
- **12. P3 跨阶段做事 (v0.11 已落地)** 配对风险**——P3 之后应优先补 P1 卫生 pass (TASKS_L2_HYGIENE H0-H8) 防索引漂移，否则 usearch/zvec 索引可能长期与 chunks 不一致

### 1.3 现存正确设计（保留，不推倒重来）

- **4 路召回 + RRF**：rank-only 融合，跨异质检索路线的教科书级正确选择
- **双时态 + 软删除 + 触发器级联**：DB 强保证的图一致性（`superseded_by` 时引用边自动失效）
- **证据可回溯**：每条 relation 带 `evidence_chunk_id`，可 1 条 SQL 追回原文
- **identity_fact 不可变 + 白名单**：防身份伪造的设计，值得泛化
- **validation.py**：输入清洗面完整（含 Trojan Source bidi 防护）
- **占位符查询过滤**：保护 recall_log 信号纯度

### 1.4 产品边界（mnelo 是什么 / 不是什么）
防止 scope creep 的定位声明，所有设计决策以此为准绳：

| mnelo 是 | mnelo 不是 |
|---|---|
| 本地优先的**记忆存储 + 检索层** | 完整 Agent 运行时（Agent 永远是调用方，不是被 mnelo 托管） |
| 显式可回溯的**个人知识图谱** | 通用知识库/文档系统（不做富文本、协作、权限） |
| 提供**自主维护管线**（可选） | 替主人做判断的记忆体（决策权留在 Agent/人） |
| 单机、单文件、可备份 | 分布式 / 多租户平台 |

**推论**：新增能力必须回答"它是在让记忆被更好地**存取**，还是在让 mnelo 变成 Agent？"——后者应拒绝或外包。

**通用优先原则（v0.13 修订）**：mnelo 能力**默认通用**（任何 MCP 客户端可用）；**客户端专属件单列 + 附加说明**，作为通用层的薄适配器，不为某客户端新造机制。例：Session 状态注入 = 通用 MCP initialize 注入（primary）+ Claude Code SessionStart 钩子（optional adapter，见 TASKS_L2_SESSION_STATE Part1）。判断标准：能力是否只服务于单一 agent → 若是，降级为适配层而非核心机制。

---

## 2. 目标架构：5 层

```
┌─────────────────────────────────────────────────────────┐
│  L3 协议层  MCP tools (统一契约) / 客户端                    │
├─────────────────────────────────────────────────────────┤
│  L2 记忆管理层 (新增)  6-pass 自主维护管线 + audit 可撤销    │
├─────────────────────────────────────────────────────────┤
│  L1 检索层  4-lane + RRF + 质量评测                        │
├─────────────────────────────────────────────────────────┤
│  L0 存储层  SQLite(图/时态/实体/证据真相源) + search-index  │
│            适配器(向量+FTS 可插拔)                          │
├─────────────────────────────────────────────────────────┤
│  L4 可观测性  Prometheus 指标 + recall_log 质量反馈闭环     │
└─────────────────────────────────────────────────────────┘
```

| 层 | 现状 | 优化方向 |
|---|---|---|
| **L0 存储层** | 11 表，双时态不完整 | **记忆类型谱系（§3.0）**、chunk.valid_from、FK、向量软删保留历史、**search-index 适配器**、schema 迁移、写路径事务化 + **纠正传播/写入去重/git 快照/并发与保留**（§3.7-3.9） |
| **L1 检索层** | 4 路 + RRF | FTS5、entity id 匹配、RRF 标签修正、**双轨组织（显式容器树）/ 常驻摘要 / 多跳推理 / 会话隔离 / 新近度加权 / 来源可信度**（§4.5-4.10）、质量评测 harness |
| **L2 记忆管理层 (新增)** | 无 | **6-pass** 自主维护管线（含社区检测），Proposal/Policy/Applier，dry-run 默认，审计可撤销 |
| **L3 协议层** | 10 工具，3 个 raw-SQL 旁路 | **工具收敛到 ~10（§6.5）**、消除旁路、批量/分页、客户端长连接 |
| **L4 可观测性** | 17 运维指标 | 召回质量指标 + **记忆健康度评分**、反馈闭环 |

**核心原则**：分层但**不引入进程/服务依赖**——仍是单进程、单文件、local-first。分层的意义在**职责边界与可替换缝**，不在部署形态。

---

## 3. L0 存储层

### 3.0 正式数据模型（chunk / entity / relation）

#### 3.0.1 核心问题：一条「记忆」是什么

**记忆（Memory）** = 一个原子的事实陈述 / 偏好 / 事件 / 决策 / 流程，可被独立召回、作废、版本化。它是 mnelo 的领域概念。

存储上采用**双表示（dual representation）**：
- **原文表示（chunk）**：人类可读的完整陈述——保真、可回溯
- **结构化表示（entity + relation）**：图谱化的概念节点与语义边——可导航、可推理

> **一条记忆 = chunk（原文）+ 零或多个 entity（它提及的概念）+ 零或多个 relation（概念间的边）**。
> 两条设计结论由此而来：① chunk 永远保留原文（可回溯的根基）；② entity/relation 只是"从 chunk 里抽出来的索引视图"，**从不携带 chunk 没有的信息**（信息单源）。

#### 3.0.2 三对象边界（正式定义）

| 对象 | 定义 | 例 | ID | 时间语义 |
|---|---|---|---|---|
| **chunk** | 一条**原文陈述**（非结构化、保真） | "7/15 建仓 sh600089 12000@18.96" | 生成 id（`chunk_ts_seq`） | `timestamp`（陈述时间）+ `valid_until`（作废时间） |
| **entity** | 一个**可指称的概念**（结构化、可复用） | sh600089 / 特变电工 / 主人 | 语义 id（`sh600089`、`identity:predicate:value`），全局唯一 | `valid_from`/`valid_until`（概念有效窗口）+ `superseded_by`（版本链） |
| **relation** | 一条**有向语义边** | `建仓_于` / `located_in` | 自增 + 组合唯一约束 | `valid_from`/`valid_until` + `evidence_chunk_id` |

**判定规则（何时建 entity）**——概念满足任一条件即应建 entity：
- (a) 会被**跨 chunk 引用**（去重/合并有价值）
- (b) 有**别名**（一物多名，需归一）
- (c) 有**属性**需要稳定承载（如持仓数量、时区）
- (d) 是**图导航锚点**（主人、股票、项目、常驻摘要）

否则只写 chunk（纯陈述，无引用价值）——**防止 entity 爆炸**（个人库规模下 entity 是稀缺的，chunk 是廉价的）。

#### 3.0.3 双谱系正交：kind × memory_type

entity 上有**两个正交维度**，不是层级关系：

| 维度 | 回答 | 决定什么 | 例 |
|---|---|---|---|
| **kind**（概念角色） | 这个节点在图里**扮演什么** | **结构行为**（identity_fact 不可变、user 是主人锚点、stock 走符号别名强制、container 是收纳节点） | stock / concept / identity_fact / container |
| **memory_type**（记忆类型） | 这条记忆**生命周期如何** | **生命周期行为**（fact 可作废要校验、preference 可被纠正覆盖、episode 永不合并、procedure 优先保留、ephemeral 短 TTL） | fact / preference / episode / decision / procedure / ephemeral |

**正交性澄清（关键）**：
- 一个 entity 同时有 `kind` 和 `memory_type`，二者独立。例：`sh600089` = kind `stock` × memory_type `fact`
- **`memory_type` 的权威载体是 chunk**（记忆的类型）。entity 上的 `memory_type` 是**便捷冗余/派生**：当 remember 不指定 entity 类型时继承 chunk 的类型；当同一 entity 被多条不同类型的记忆共享时，反映最近/主要关联，**不保证严格**
| **`identity_fact` 的不可变规则来自 kind，不来自 memory_type**——一个 `kind=identity_fact, memory_type=fact` 的实体不可变；一个 `kind=concept, memory_type=fact` 的实体可正常作废

#### 3.0.3.5 entity id namespace guard（[8/8 P1]，A1 2026-08-10 修订）

Kind 词汇表本身**开放**（无注册、用户可任意引入新 `kind`，如 `product` /
`lesson` / `recipe`），但 entity `id` 受 guard 检查——`memory._enforce_entity_namespace_guard`
在 `_upsert_entity` 入口拒三种东西：

| 拒 | 例 | 原因 |
|---|---|---|
| 黑名单前缀 `anno:` | `anno:foo` | HonchoImporter NER 历史残留 (8/8 前导入器写入) |
| 黑名单前缀 `TOKEN_` | `TOKEN_C_xxx` | 随机 session token，sentinel-like |
| `concept` kind + `name > 50` chars | `kind=concept, name="imported sleep runs at midnight"` | 整段话当 entity，污染图 |

**其余 id 不限**（A1 修复 2026-08-10）：之前版本还要求 id 必须配
`master_` 前缀 / 显式 namespace / 10 个 `_NAMELESS_KINDS` 白名单之一。
该限制违反 §3.0.3 双谱系正交 + AGENTS.md "open taxonomy" 承诺；A1 移除。
详见 commit (`feat(memory): drop _NAMELESS_KINDS, align with §3.0.3 open taxonomy`)
+ `tests/test_namespace_guard_p1_2026_08_08.py` 新增 2 test（any kind pass + kind length limit）。

写入路径 (`memory.remember`) 先 dry-run validate 所有 entities 再
INSERT chunk（[8/8 P1 fix]），避免 ValidationError 上抛时 chunk 留
SQLite WAL 变孤儿。

#### 3.0.4 关系语义（正式）

```
relation = (source_id, target_id, relation_label, weight, confidence,
            evidence_chunk_id, valid_from, valid_until)
```

- **`relation_label`**：开放字符串，但遵循命名规范（`<谓词>`：`建仓_于`、`located_in`、`is_identity_fact_for`），同义谓词必须归一（L2 消歧职责）
- **`evidence_chunk_id`（可回溯保证）**：每条边必须"生于"一条原文 chunk；边不携带证据链之外的信息
- **weight vs confidence 分工**：`weight` = 边强度（语义上多强）；`confidence` = 来源可信度（这条边多可靠）。§4.10 来源可信度进排序用的是 confidence/source
- **relation 没有 memory_type**——边的类型由它的证据 chunk 决定，不重复标注

#### 3.0.5 记忆类型谱系（生命周期行为，同 §3.0.3 memory_type 维度的展开）

| 类型 | 语义 | 生命周期 | 关键规则 |
|---|---|---|---|
| `fact` 事实 | 持股、住址、能力 | 单调、可作废 | 校验严格；作废要证据 |
| `preference` 偏好 | 报告风格、沟通习惯 | 会变 | 纠正传播（§3.7）；可被新偏好覆盖 |
| `episode` 事件 | 某日建仓、某次对话 | 不重复、带时间点 | 永不合并；时态回溯主对象 |
| `decision` 决策 | 为什么买/不买 | 带理由链 | 需回溯理由（evidence 链）；不轻易作废 |
| `procedure` 步骤/流程 | 周报怎么写 | 稳定、可复用 | 优先保留；重复写入去重 |
| `ephemeral` 瞬时 | 临时草稿 | 短命 | TTL 短；低 importance |

**收益**：L2 提取器知道"要提什么类型"；矛盾检测按类型定规则（fact 可作废、procedure 几乎不作废）；卫生按类型定 TTL/衰减；召回可按类型过滤。

#### 3.0.6 决策定案（v0.8 复查修订）
- **entity.memory_type 的召回语义（定案）**：因 memory_type 权威在 chunk、entity 上是"便捷冗余不保证严格"（3.0.3），**`type` 过滤是硬约束只作用于 chunk 路**（vector/meta 路）；**entity 路对 `type` 只做软加权**（命中 memory_type 的实体加分、不命中不排除）——避免共享实体被误杀。⚠️ 已实现的 entity 路硬过滤（f1bc1bf）需在实施中改为软加权
- **chunk 是否可无 entity 关联**：允许（纯陈述），entity 是可选索引视图
- **多语句 chunk**：一条 chunk 应承载**一个原子记忆**；复合陈述（"既建仓又清仓"）应由调用方拆分，或由 L2 P5 整合拆分

### 3.1 双时态补全
- `chunks` 增加 `valid_from`（现状只有 `timestamp` + `valid_until`，无法表达"从 T1 到 T2 有效"）
- 全表统一语义：`有效于 asof T ⟺ valid_from ≤ T AND (valid_until IS NULL OR valid_until > T)`
- 向量历史：**改为软删除**（update/forget 不物理删向量，靠 valid_until 过滤），让语义层也能回放历史；代价是 vec0 行数增长，需配 `repair_vectors` 类清理任务

### 3.2 约束强化
- FK：`relations.source_id/target_id → entities.id`、`relations.evidence_chunk_id → chunks.id`（`PRAGMA foreign_keys=ON` 已开）
- 注意：FK 需要实体先存在；remember 的写序（先实体后关系）已满足，需补测试覆盖

### 3.3 全文检索
- meta 路从 `LIKE %q%`（无索引 O(N)）换成 **FTS5**（`chunks_fts` 虚拟表 + 触发器维护，见 §4.1）
- 保留 LIKE 作为回退/精确匹配通道（FTS5 的 tokenizer 对股票代码 `sh600089` 可能切碎）

### 3.4 写路径事务化
- `remember()` / `update()` 的 chunk+entities+relations+vector 多步写入包**显式事务**（`BEGIN`/`COMMIT`，异常 `ROLLBACK`），杜绝部分写入

#### 3.4.1 事务边界（精确）

| 写操作 | 事务包含 | 提交点 | 失败回滚 |
|---|---|---|---|
| `remember()` | chunk + entities + relations + **vector 嵌入** | 全部成功才 COMMIT | 任一步异常 → ROLLBACK，**不留孤儿**（chunk 无向量 / 实体无 chunk） |
| `update()` | 新 chunk + 旧 chunk supersede + 触发器级联 + 向量删旧嵌新 | 同上 | 同上 |
| `correct()`（§3.7） | 实体属性 + 别名 + 级联关系 | 同上 | 同上 |
| `forget()` | 软删 + 级联失效 + purged_queue 入队 | 同上 | 同上 |

**关键点——embed 在事务内**：向量嵌入（`embed_bytes`）是**外部 IO + CPU 密集**，放事务内会让写路径变慢、且 embed 失败会回滚整条写。权衡决策：
- **默认**：embed **在事务内**（一致性优先——chunk 必须有向量，宁慢勿残）
- **可选**：`MNELO_MEMORY_EMBED_ASYNC=1` 时 embed 移出事务（异步补嵌入队列），写路径快、但向量可能短暂缺失——由 `repair_vectors.py` / 异步 worker 兜底
- 二者都保证：**不存在"chunk 写成功但向量永远缺"的静默状态**（同步=回滚保证；异步=队列保证）

### 3.5 Schema 迁移机制
- 基于 `meta.schema_version`（现 1.0）建立正式迁移流程：`scripts/migrate/*.py` 逐版本升级，禁止跳版本
- 现有 `migrate_to_mnelo.py` 归入此框架

### 3.6 存储适配器（可迁移路径的缝）
```
Memory
 ├─ StorageBackend (graph + temporal + entities)   → SQLite 默认; 未来 Neo4j 可选
 └─ SearchIndex (vector + FTS)                     → sqlite-vec + FTS5 默认; 未来 zvec / Qdrant/Milvus
```
- `Memory` 内部定义两个薄接口：`GraphStore`（节点/边/时态查询）与 `SearchIndex`（embed + KNN + FTS 匹配）
- L2 的所有变更也走这两个接口，**永不 raw SQL**——未来换后端只动适配器
- 默认实现保持 SQLite 单文件（不引外部服务）

### 3.7 写路径增强：实体纠正传播 + 写入去重 ⟵ 借鉴 Mem0
现状 `update()` 只换 chunk，**不改实体和关系**——"特变电工改名了"不会联动实体 name/aliases 和引用它的边。这是比 L2 更基础的一层，两个能力：

- **实体纠正传播（self-editing）**：新增 `Memory.correct(entity_id, changes)` 动作——更新实体属性/别名 + 级联更新指向它的关系属性 + 记录 `superseded_by`
  - **不可变边界（明确化）**：`master` 用户实体 = **100% 不可变**（任何路径含 correct() 都拒绝）；其它 `user_confirmed=1` 实体**仅豁免 L2 自动 pass**，`correct()` 显式调用仍允许；`identity_fact` 走专用路径（identity_fact_manager）
  - 这样既防"自主层悄悄改主人身份"，又不堵死"主人自己明确要改"的唯一入口
- **写入时去重（NOOP 决策）**：`remember()` 可选开关 `dedup_check=True`——写入前检索同主语同谓词的现存事实，命中则走 update/合并而非新增。默认关（保持显式语义 + 写入低延迟），L2 仍负责事后清理

#### 3.7.1 dedup_check 匹配键（精确）

**匹配键 = 结构化三元组，不是文本相似**——避免把"相关"当"重复"：

```
匹配键: (主语 entity_id, 谓词 relation_label, 宾语/值)
命中规则:
  · relations 表存在 (source_id=主语, relation=谓词, valid_until IS NULL)
    且 target 与候选宾语相同或别名命中
  · 或 entities 表存在同 id 且 memory_type 一致的同谓词属性
```

| 场景 | 动作 |
|---|---|
| 三元组完全命中 | **update 而非新增**（走 §3.7 correct 或 supersede 链） |
| 主语同、谓词同、宾语不同 | **矛盾候选**——不静默覆盖，记 `conflict_candidate`（§5.4 语义） |
| 主语同、谓词不同 | 不判重（不同方面的事实） |
| 无结构化匹配但文本高相似 | **不判重**（文本相似 ≠ 事实重复；交给 L2 P3/P5） |

- **代价**：`dedup_check=True` 每次 remember 多 1-2 次索引查询 + 可能一次 embedding 相似度（默认关即零代价）
- **与 L2 关系**：写时去重是"源头拦截"，L2 P2/P3 是"事后清理"——二者互补，写时只拦最确定的，不确定的全留给 L2

### 3.8 记忆快照（版本化备份）⟵ 借鉴 Letta MemFS
Letta 2026 年把记忆改成 git 版本化的文件系统。mnelo 移植为轻量版，但**针对 SQLite 单文件 + WAL 的实际情况修正**：

- **备份方式**：周期（cron / post-write 低频）用 **`sqlite3 .backup`** 生成一致性快照。⚠️ **不要直接 `cp memory.db`**——WAL 模式下写入中的文件可能拷到中间页；备份 API 会正确包含 WAL 中未 checkpoint 的数据
- **产物归档**：`snapshots/YYYYMMDD.db.gz`（`.backup` 后 gzip），**不进 git**、不进源码主仓——单独 **rsync 到 NAS / bigbox**（与现有 `backups/pre-update-*.zip` 模式一致）。`git` 跟踪二进制完全没必要
- **体积实测（修正 README 基线）**：主人真实库 **44.72 MB 主体 + 0.72 MB WAL**，比 README 声称的 ~24MB 大近一倍（README 该基线已过期，待更新）。按 ~45MB 算：日快照 + gzip ≈ 5-10MB/份，保留 30 份 ≈ 150-300MB，可接受；**若日快照 + git 跟踪二进制，一年后 .git 膨胀 ~16GB——已排除该方案**
- 与现有 `valid_until` 版本链互补：库内版本链管"单条事实的历史"，快照管"**整个库的时间旅行**"（diff / 回滚 / 灾难恢复）
- 复用仓库已有的 `.githooks/post-commit` 基建触发备份脚本（产物进 `snapshots/`，不进主仓）

### 3.9 并发模型与日志保留
- **并发模型（明说）**：单进程内**单写者**（唯一 `Memory` 实例持有写连接）+ WAL + 多读者（recall 的 4 路并发读是读连接）。多客户端（Hermes/Claude/Cursor 同时连）共享同一写者；冲突策略 = busy_timeout + 写事务串行。**不引入多写者**——违反即触发 §1.4 边界审查
- **日志保留策略**：`recall_log` 与新增 `audit_log` 无限增长。策略：recall_log 保留 N 天 / M 条（聚合后入 stats）；audit_log 保留更久（可撤销的价值），但提供归档/清理工具；均走 `purged_queue` 通道统一管理

### 3.10 ID 命名空间策略

**问题**：现在混着三种 id——生成 id（`chunk_...`）、语义 id（`sh600089`）、保留 id（`user`/`master_*`），没有命名空间规则与冲突策略。L2 自动消歧/合并时这是隐患。

**三类命名空间（正式）**：
| 命名空间 | 前缀/模式 | 对象 | 规则 |
|---|---|---|---|
| **SYSTEM** 系统生成 | `chunk_YYYYMMDD_HHMMSS_µs`；relation 自增 int | chunk / relation | **不可人工指定**；服务端生成 |
| **SEMANTIC** 语义 | 任意 slug（`sh600089`、`identity:<predicate>:<value>`、`container:<name>`） | entity | 人工/提取器指定；**全局唯一**；小写 + 无空格（slug 化） |
| **RESERVED** 保留 | `user`、`master_*` | 主人锚点 entity | **受保护**；不可作废/合并/重命名 |

**冲突矩阵**：
| 冲突 | 处理 |
|---|---|
| 生成 vs 生成 | 微秒精度 + 服务端生成，碰撞可忽略；`_upsert` 有 REPLACE 兜底（v0.5.5） |
| 语义 vs 语义（同 id 不同概念） | L2 P3 消歧/合并（走 superseded_by，不物理删） |
| 语义 vs 语义（同概念不同 id） | 别名（aliases_json）归一，`identity:*` 由白名单约束 |
| 生成 vs 语义 | **前缀隔离，不可能冲突**（chunk_ 前缀保留） |
| 保留 vs 任何 | **拒绝**（validate_id + 保留名单检查） |

**规则**：
1. **前缀保留**：`chunk_`、`entity:`、`identity:`、`master_`、`container:` 前缀禁止用作 SEMANTIC 实体 id 的其余含义（validate_id 已限 `[a-zA-Z0-9_:.-]`，再加前缀保留名单）
2. **重命名不物理改 id**：实体改名 = `correct()`/`merge` → 旧 id `superseded_by` 指向新 id（软迁移，历史引用仍可解析）
3. **id 是身份不是标签**：id 一旦发布即稳定；显示名/别名是可变字段，id 语义变化走版本链

#### 3.10.1 命名空间文法（正式，供 validate_id 强制）

| 命名空间 | 文法（正则） | 说明 |
|---|---|---|
| SYSTEM·chunk | `chunk_\d{8}_\d{6}_\d{6}` | 时间戳 + 微秒，服务端生成 |
| SYSTEM·relation | 正整数（自增） | 不做前缀 |
| SEMANTIC·entity | `[a-z0-9][a-z0-9_-]{0,255}`（小写 slug） | 通用实体 |
| SEMANTIC·identity | `identity:[a-z_]+:[a-z0-9_-]+` | 谓词白名单 + 值 slug |
| SEMANTIC·container | `container:[a-z0-9_-]+` | 收纳节点（§4.8） |
| RESERVED | `user`、`master_[a-z0-9_]+` | 主人锚点 |

**slug 化规则**（用于 identity:*/container:* 及通用实体）：
- Unicode → 保留下划线分隔的拼音/英文（如 `identity:lives_in:beijing_daxing`）；中文实体名保留在 `name` 字段，id 用 slug
- 大写 → 小写；空白/标点 → `_`；连续分隔符折叠
- 例：`特变电工` → entity id 建议 `tebian_diangong` 或保留 `TBEA` 类官方代码；`sh600089` 本就是官方代码，直接作 id

**validate_id 强制**：现有 `[a-zA-Z0-9_:.-]{1,256}` 之外，增加**保留前缀拒绝**（`chunk_`/`identity:`/`container:`/`master_` 只能出现在对应命名空间）——防 SEMANTIC 实体伪造 SYSTEM/RESERVED id。

#### 3.10.2 关系 id 与向量映射（实现要点）

- **relation id**：自增 int。软删除（valid_until）后**不回收**——单调递增保证历史引用（audit_log/evidence）可稳定解析；物理 purge 后 id 也不复用
- **chunk id ↔ vec0 rowid**：`chunk_id`（TEXT 主键）与 `vectors.rowid` 是 **1:1 映射**（现有实现）。id 是稳定的业务标识，rowid 是物理索引——迁移/导入时 rowid 可重建（`repair_vectors.py`），chunk_id 不可变。**任何 id 变更都必须走 superseded_by 链，绝不重写 rowid 映射**
- **语义 id 的幂等**：SEMANTIC entity 的 upsert 天然幂等（同 id = 同一实体）；SYSTEM id 每次生成必不同（时间戳+微秒）

### 3.11 快照恢复流程

**问题**：§3.8 定义了快照**怎么做**（`.backup` → `.gz` → rsync），但**怎么恢复**没写——灾难时最需要的恰恰是恢复手册。

**恢复步骤（标准流程）**：
```
1. 停 MCP server          launchctl unload ai.mnelo.mcp
2. 隔离损坏库              mv memory.db memory.db.corrupt-<date>  (保留现场供排查)
3. 选快照                  最新 vs 指定时间点 (时间旅行: snapshots/YYYYMMDD.db.gz)
4. 解压到 live 路径         gzip -dc snapshots/YYYYMMDD.db.gz > memory.db
5. 校验                   PRAGMA integrity_check; quick_check
6. 重启 + 冒烟             launchctl load; 跑一次 recall 验证
```

**损坏检测**：
- **主动**：health_check 定期 `PRAGMA integrity_check`（成本低），异常 → 告警 + 引导恢复
- **被动**：`sqlite3.OperationalError: database disk image is malformed` / `file is not a database` 捕获 → 打印恢复指引

**恢复粒度选择**：最新快照（最快，丢 ~1 天）vs 特定时间点（git 式时间旅行，配合 §3.8 保留策略）。恢复后**检查快照本身是否也损坏**（备份链的可信度）——建议每月一次"恢复演练"验证快照可用，成本是解压 + integrity_check。

#### 3.11.1 完整性校验（恢复前必做）

| 检查 | 命令/方式 | 作用 |
|---|---|---|
| 页一致性 | `PRAGMA integrity_check` | 检测 B-tree/页损坏 |
| 快速页检查 | `PRAGMA quick_check` | 低成本版，日常巡检用 |
| 外键一致性 | `PRAGMA foreign_key_check` | 关系悬空检测 |
| 逻辑健全 | 统计抽查：`SELECT count(*)` 各表 + `recall_log` 最新一条 | 数量级合理（非空/非爆炸） |

**快照清单校验**：每个 `snapshots/YYYYMMDD.db.gz` 旁存 `.sha256`——恢复前先对 gzip 校验哈希，再对解压后 DB 跑 integrity_check。**两层校验**防"备份本身是坏的"。

#### 3.11.2 坏快照回退

**恢复失败时的降级链**：
1. 选定快照 → 哈希不符 / integrity_check 失败 → **自动顺延到上一个快照**（保留 N 份的价值就在这里）
2. 全部快照损坏 → **提示恢复演练缺位**（说明备份链本身不可信），fallback 到 `.corrupt` 现场库尝试 `sqlite3 .recover`（尽力捞数据，非保证）

#### 3.11.3 恢复自动化

- 提供 `scripts/restore_db.py --from snapshots/YYYYMMDD.db.gz [--dry-run]`：
  - `--dry-run` 只跑校验（哈希 + integrity + 统计），不落盘——**每月演练就用这个**
  - 实际恢复：隔离现场 → 解压 → 双层校验 → 原子替换（解压到 `.tmp` 再 `mv`，杜绝写一半）
- **演练即测试**：恢复脚本要可测（dry-run 断言 integrity_check == 'ok'），防止"手册好看、实操坏"——与 §1.4 "measured" 原则一致

---

## 4. L1 检索层

### 4.1 meta 路 → FTS5
- `chunks_fts`（content + source + session_id），BM25 排序与 `importance` 加权结合
- 查询改写：`LIKE %q%` → `MATCH`，对含符号的 token（`sh600089`、`D∩W`）保留 LIKE 回退
- 收益：O(N) 全扫 → 索引检索；时间/重要度过滤下推到 SQL

#### 4.1.1 分词器决策（中文场景的关键）

| 分词器 | 中文表现 | 结论 |
|---|---|---|
| `unicode61`（默认） | 按空格/标点切，中文整句成一个大 token——**查"建仓"命中不了"今日建仓股票"** | ✗ |
| `trigram` | 3-gram 切，中文子串可命中，索引体积 ~3× | **主选** |
| 自定义 `jieba` 外置分词 | 语义分词最好，但引入外部依赖 + 写入路径分词开销 | 可选（离线场景降级 trigram） |

**决策**：`chunks_fts` 用 **`trigram` tokenizer**（对中文子串召回够用、零外部依赖）。含符号 token（`sh600089`、`D∩W`）在 trigram 下可能被切碎——这正是 **LIKE 回退**保留的原因（§4.1 主条）。

#### 4.1.2 表结构与同步（软删一致性）

```
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    content, source, session_id,
    tokenize='trigram'
);
-- 外部内容表 + 触发器维护 (FTS5 外部内容表模式)
CREATE TRIGGER trg_chunks_fts_insert AFTER INSERT ON chunks
    BEGIN INSERT INTO chunks_fts(rowid, content, source, session_id)
          VALUES (new.rowid, new.content, new.source, new.session_id); END;
CREATE TRIGGER trg_chunks_fts_delete AFTER DELETE ON chunks
    BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, content, source, session_id)
          VALUES ('delete', old.rowid, old.content, old.source, old.session_id); END;
CREATE TRIGGER trg_chunks_fts_update AFTER UPDATE ON chunks
    BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, content, source, session_id)
          VALUES ('delete', old.rowid, old.content, old.source, old.session_id);
          INSERT INTO chunks_fts(rowid, content, source, session_id)
          VALUES (new.rowid, new.content, new.source, new.session_id); END;
```

**软删一致性**：mnelo 用 `valid_until` 软删而非 DELETE——FTS 索引里仍留着已软删 chunk 的 token。**查询侧过滤**（`WHERE rowid IN (SELECT id FROM chunks WHERE valid_until IS NULL ...)`）解决，不物理删 FTS 行（保历史可回放）。**不建 FTS 删除触发器**（软删是 UPDATE valid_until，UPDATE 触发器已覆盖）

#### 4.1.3 查询设计（BM25 + importance）

```
SELECT c.id, c.content, c.source, c.timestamp, c.importance,
       bm25(chunks_fts) AS fts_score
FROM chunks_fts JOIN chunks c ON c.rowid = c.id
WHERE chunks_fts MATCH ?
  AND c.valid_until IS NULL            -- 软删过滤
  AND c.memory_type = ?                -- 类型过滤 (§3.0)
ORDER BY c.importance * -bm25(chunks_fts) DESC   -- importance 与 BM25 结合
LIMIT ?
```

- `-bm25()` 越低越好，`importance * -bm25` 升序 = 重要且相关优先
- 与现有 `_meta_recall` 的 filters（source/type/time_range）下推同构
- 保留 `LIKE %q%` 回退：当 query 含 trigram 切碎风险的高符号 token 时走旧路（§4.1 主条）

### 4.2 entity 路补 id 通道
- 现状只匹配 `name LIKE` / `aliases_json LIKE`；查询 `sh600089`（实体 id）时 entity 路空手而归
- 补 `id LIKE` 通道（优先级：id > name > alias），并修正"按 id 查实体"的召回空窗

### 4.3 RRF method 标签修正
- 现状：`_rrf_fuse` 对同 `chunk_id` 的后处理 lane 覆盖 hit dict，导致显示 `method='meta'` 掩盖实际贡献 lane，**污染 recall_log 反馈环**
- 修正：同一 chunk 多 lane 命中时，保留**最高 rank 分数的 lane** 作为 method（或存 lane 集合 `methods: [...]`）

### 4.4 召回质量评测 harness
- 消费 `recall_log.recall_details_json`（已存每 hit 的 method/rank/distance/rrf_score/importance）
- 对标 LongMemEval 思路建立轻量评测：**时态正确率**（asof 回放是否符合预期）+ **lane 贡献分布**（每 lane 命中率、空窗监控）
- `scripts/benchmark.py` 升级：从纯延迟 → 延迟 + 召回质量双指标

### 4.5 常驻记忆摘要 ⟵ 借鉴 Letta core memory
- 现状：最该记住的事（身份/关键决策）也要靠检索碰运气
- 借鉴：`Memory` 自动维护一份 **500–2000 字的记忆摘要**（主人身份 + 近期高 importance 决策），提取规则进 L2；新 MCP 工具 `memory_get_digest` 或 MCP initialize 时自动注入 Agent 上下文
- 摘要本身也是 chunk（`source='digest'`），可更新/作废，遵循同一套双时态
- 与 identity_facts 的关系：identity_facts 是"结构化身份事实"，摘要是"面向 Agent 上下文的压缩叙事"，二者互为视图

#### 4.5.1 生成与刷新机制（精确）

**内容来源（固定三块，规则优先）**：
| 块 | 来源 | 方式 |
|---|---|---|
| 身份 | identity_facts（`kind=identity_fact`） | 直接读出，无 LLM |
| 近期关键 | 高 importance（≥0.8）且近期（30 天）的 decision/episode chunks | 规则抽取首句 + 证据链接 |
| 进行中 | 最近写入的 session 主题（session_id 分组聚合） | 规则聚合 |

**刷新触发**：增量重建，非全量重写——
- 写入侧：新 identity_fact 或高 importance decision 落地时置 `digest_dirty` 标志
- 读取侧：`memory_get_digest` 遇 dirty → 重建（规则块 O(1) 读，秒级）；否则返回缓存
- **LLM 可选**：`l2.llm.enabled` 时，摘要块 3 用 LLM 压缩（否则规则截断首句 + 省略号）
- **双时态**：每次重建 = 新摘要 chunk + 旧摘要 superseded（复用 §3.0 版本链），历史摘要可回放
- **体积护栏**：上限 2000 字，超限截断 + 记 `digest_truncated` 标志（可观测）

#### 4.5.2 可逆压缩（⟵ 借鉴 Headroom CCR，v0.13）

**思想**：Headroom 的 CCR（可逆压缩）= 压缩版进上下文、原稿本地缓存可回溯。mnelo **天然满足可逆性**（chunk 永不丢、摘要是派生视图），缺的是**显式 provenance 指针 + 按需展开路径**。补两点：

1. **每条摘要行带 `source_chunk_ids` 指针**（复用 evidence_chunk_id 同构）：
   - 摘要行 = 压缩视图，指针指向生成它的原始 chunk（真相源）
   - 信息单源不破——摘要行携带的信息，原始 chunk 全都有
2. **展开路径 `memory_get_digest(ref=<行指针>)`**（非新工具，digest 工具双模式）：
   - 无 ref → 返回摘要（压缩视图，进上下文）
   - 带 ref → 返回该行对应的原始 chunk（按需回溯，取细节）
   - 语义：Agent 上下文用压缩版省 token，需要细节时**显式展开**而非依赖压缩版

**取舍**：
- **保真优先**：摘要行宁可截断也绝不引入 chunk 没有的信息（§3.0.1 信息单源）
- **指针生命周期**：摘要行来源 chunk 被 supersede → 指针指向该版本链（digest 重建时刷新）；摘要自身被 supersede → 指针随旧摘要保留（历史可回放）
- **与 §4.5 体积护栏联动**：展开路径让"截断"可恢复——`digest_truncated` 不是丢信息，是被截断行可经指针取回原文

### 4.6 多跳路径推理 ⟵ 借鉴 Cognee CoT graph traversal
- 现状：图路只有 2-hop BFS 返回邻居，回答不了"X 和 Y 怎么连起来的"
- 借鉴：新增 `Memory.reason(start_id, end_id, max_hops=4)`，返回**完整路径链**
  ```
  entity1 --relation--> entity2 --relation--> entity3 --relation--> entity4
  ```
  带每跳的 evidence_chunk_id（保持证据可回溯）。SQL 层用递归 CTE（SQLite 3.8+ 支持），比 N+1 循环更稳
- 新 MCP 工具 `memory_reason`

### 4.7 会话级召回隔离 ⟵ 借鉴 Mem0 scoping
- 现状：chunks 有 session_id，但 recall 不按会话过滤——对话 A 会串到对话 B 的上下文
- 借鉴：`recall(session_id=...)` 时把 meta/vector 路的过滤条件加上 `session_id`；不传则全局（默认，保持兼容）
- 与 L1 现有 filters 参数整合（filters 增加 `session_id` 键）

### 4.8 双轨组织模型：显式容器树 + 语义涌现
组织是记忆系统的根问题。现状只有**涌现轨道**（向量相似 + 社区聚类），缺**显式收纳轨道**——"我亲手把这条放进某个区域"。二者互补：语义检索解决"忘了放哪"，显式结构解决"我知道它该在哪"。参考记忆宫殿（method of loci）的组织原则，但实现为通用容器树：

- **显式轨道**：实体 `kind='container'`（或 `locus`）+ `contains` 关系边形成树。几乎零 schema 改动——容器就是实体，收纳就是边
  - `remember(location=<容器id>)` 建收纳边
  - `recall(location=...)` 把召回限定在容器子树内（各 lane 语义见下）
  - 容器带 `order` 属性支持路线巡游（宫殿式的按序访问）
- **涌现轨道**：向量相似 + L2 P6 社区检测（自动聚类，人不用管）
- **约束（防止两条轨道打架）**：`location` 只是召回的一个**可选约束**，不是新主路径；默认不传=全局语义召回。显式结构是"加固"，不是"替代"
- 新工具 `memory_loci_*`（建容器 / 放置 / 子树导航），并入 §6.5 工具收敛后的"组织"意图组

#### 4.8.1 location 过滤的各 lane 语义（精确）

`location=<容器id>` 限定到**子树**（容器及其所有子孙），各 lane 的实现：

| lane | location 实现 | 说明 |
|---|---|---|
| **graph** | 从容器出发的子树 BFS 已在 `graph_query` 能力内——先取子树节点集 `S`，再查与 `S` 相连的 chunks/entities | **最自然**，子树即图遍历 |
| **meta** | 子查询取 `S` 的 chunks：`AND chunk_id IN (SELECT ... 子树内 chunk)` | 一次子查询下推 |
| **entity** | 实体 id ∈ `S` | 实体在容器内才算 |
| **vector** | 向量命中后回查 chunk 是否 ∈ `S`（后过滤） | 向量索引无容器概念，只能后过滤 |

- **种子语义**：容器树关系本身是 `contains` 边（evidence_chunk_id 指向建容器的 chunk），子树计算走 §3.10.2 的 relation 查询
- **空子树**：`location` 指向空容器 → 全 lane 返回空（显式约束优先，不悄悄放大到全局）
- **与 session/type 复合**：`location` 与 `session`、`type`、`asof` 是 AND 关系（多重约束叠加）

### 4.9 新近度加权进 RRF ⟵ 借鉴 Zep/Graphiti 时态检索
现状：asof/valid_until 让"某时点有效"**可查**，但**排序时不奖励新近度**——昨天的重要事实和半年前的同等重要事实同分。个人记忆里"现在的相关度"几乎总该加权：

- RRF 融合后加一个**时态新鲜度因子**：`final = rrf_score × (1 + λ · freshness(valid_window, recall_time))`
- `freshness` 随时间平滑衰减（如半衰期 30 天）；`λ` 可配（默认小，避免压制相关性）
- 与 asof 正交：asof 决定"哪些有效"，freshness 决定"有效里哪些更当下"

### 4.10 来源可信度进入排序
现状：relations 有 `confidence`、chunks 有 `source`，证据链有 `evidence_chunk_id`，但**来源可靠性没进排序**：

- 定义来源可信度档：`user_confirmed`（主人确认，最高）> `manual` > `agent` > `import:*`（脚本导入）> `digest`（自动摘要）
- `source` 前辍映射到权重，在 RRF 融合时给 chunk/entity 加分
- 与 L2 卫生联动：低可信来源的低 importance 项，优先进入 TTL/清理候选

### 4.11 召回请求模型（意图化查询）

**问题**：`recall(query, top_k, graph_hops, filters, strategy, asof)` 参数在膨胀，加上 location/session/type/新近度/可信度后成为抓参数袋。L2/工具收敛后调用方会困惑"该传什么"。

**方向**：把"**要什么**"（查询意图与约束）和"**怎么找**"（lane 组合与排序）分开。请求对象化，query 先被轻量拆解成结构化意图。

**请求对象**（`memory_recall` 收敛后的入参模型）：
```
RecallRequest = {
  query: str,                    # 自由文本（可空，此时必须带 entities/location 等约束）
  intent: lookup|semantic|navigational|temporal|meta,   # 可选，缺省自动推断
  entities: [id...],             # 显式实体约束（锚定）
  aspect: str,                   # 关注方面：价格 / 理由 / 时间线 / 人物关系
  type: memory_type,             # 记忆类型过滤（§3.0）
  time: { asof: ISO, range: {from,to} },   # 时态约束
  location: container_id,        # 显式容器子树（§4.8）
  session: session_id,           # 会话隔离（§4.7）
  filters: {...},                # 兼容现有 {kind, source, tag}
  strategy: rrf|lane...,         # lane 组合（缺省 rrf）
  top_k: int,
  weights: { recency: 0..1, trust: 0..1 },  # 排序因子权重（§4.9/§4.10）
}
```

**意图类型与 lane 路由矩阵**：
| intent | 语义 | 自动路由 | 典型 query |
|---|---|---|---|
| `lookup` 查证 | "X 是什么" | entity + vector | "sh600089" |
| `semantic` 语义 | "关于 Y 的内容" | vector + meta + graph | "最近建仓的股票" |
| `navigational` 导航 | "X 和 Z 怎么连的" | graph + entity | "特变电工和西电什么关系" |
| `temporal` 回放 | "某时点状态" | meta + graph（asof 生效） | "6/1 时点持仓依据" |
| `meta` 元记忆 | "我记得……吗" | 全 lane + recall_log | "我上次怎么说的" |

- intent 缺省时由 query 特征推断（含实体 id → lookup；含时间词 → temporal；含两个实体 → navigational；否则 semantic）
- 推断失败回落到全 lane（现有 rrf 行为），**不因意图机制引入召回盲区**

**响应对象**（供反馈环）：
```
RecallResponse = {
  hits: [{...chunk_id, content, memory_type, method, rrf_score, evidence...}],
  lanes_used: [vector, graph, meta, entity],
  query_parsed: {intent, entities, aspect, time_range, type},   # 拆解结果
  diagnostics: {per_lane_hits, total_ms, empty: bool},          # 喂 recall_log
}
```

**排序因子合成（正式化）**：
```
final_score = rrf_score × (1 + λ₁·freshness) × (1 + λ₂·trust) × importance^α
# rrf_score   = Σ 1/(k+rank)             (§2 现有)
# freshness   = 时态新鲜度，半衰期 30 天   (§4.9)
# trust       = 来源可信度档位            (§4.10)
# importance^α = 重要性加权（α 可配，默认 0 保持现行为）
```

**因子默认值（v0.9 补，实施起点）**：
| 因子 | 默认 | 说明 |
|---|---|---|
| `λ₁`（freshness） | **0.3** | 新鲜度至多贡献 30%——避免"新但无关"压制"旧但相关" |
| `λ₂`（trust） | **0.2** | 信任档位至多贡献 20%——降权为主，不主导 |
| `α`（importance） | **0** | 保持现状（importance 已用于 lane 内排序，不在跨 lane 叠加） |
| freshness 半衰期 | **30 天** | 30 天前的记忆新鲜度减半 |

- 全部经 `RecallRequest.weights` 可调；P2 质量评测（§4.4）用真实数据校准
- **temporal / lookup intent 强制降 recency**（§4.11.1 行为调整）：此时 `λ₁` 视为 0

**与现有兼容**：旧 `recall(query, top_k, filters, strategy, asof)` 调用自动映射为 `RecallRequest`（intent 推断，weights 默认），**行为不倒退**。

#### 4.11.1 分解管线（intent 推断的具体机制）

intent 推断不是黑盒，是**一条规则优先、可审计的管线**（无 LLM 依赖，保持离线）：

```
query → ① entity spotter    在实体索引里查 id/name/alias 命中 → 得 entities[]
      → ② aspect 提取       特征词表: 价格/理由/时间线/关系/持仓...
      → ③ 时间词检测         "上周/6月/2026-06-01/之前" → time.range 或 asof
      → ④ 类型提示           "偏好/决定/当时" 等 → type 提示
      → ⑤ intent 分类       规则判定 (见下) → 失败回落 semantic
```

**intent 判定规则（确定性）**：
| 信号 | intent |
|---|---|
| 命中的实体 id 数 ≥ 2 且含关系词（"和/与/关系/关联"） | `navigational` |
| 命中 ≥1 实体 id 且 query 是短指称（无谓词） | `lookup` |
| 含时间词 / asof 显式给出 | `temporal` |
| 含"我记得/上次/说过"类元记忆词 | `meta` |
| 其余 | `semantic` |

**intent 行为调整**（不只路由，还调整排序权重）：
| intent | 调整 |
|---|---|
| `lookup` | entity 路优先，降 recency 权重（查证要的是准不是新） |
| `temporal` | 强制 asof 语义，**降 recency**（回放历史不该被新鲜度拉偏） |
| `navigational` | graph 路优先，多 hop |
| `semantic` | 全 lane + 默认权重 |
| `meta` | 检索范围含 recall_log（"我上次怎么说的"） |

#### 4.11.2 与 memory_reason / memory_topics 的关系（澄清）

- **`memory_reason(start, end)`**（§4.6）与 `navigational` intent **不冲突**：reason 是"给定两端求路径"（显式锚点），recall-navigational 是"query 里隐含两个实体求关联"。实现上 memory_reason 是 `RecallRequest` 的特化（intent=navigational + 强制两端）
- **`memory_topics`**（§5.2 P6）是**聚合查询**（社区概览），不属于召回——是独立的统计类工具，不进入 RecallRequest
- 结论：召回模型只管"找相关记忆"，"找路径"是它的特化，"找话题"是它上游的聚合层

#### 4.11.3 多意图与失败语义
- **多意图**：query 同时命中 temporal + semantic → **primary/secondary** 两级，先按 primary 路由，secondary 作为排序加权提示；不拆分成两次检索（保延迟）
- **分解失败**：entity spotter 零命中 + 时间词零命中 → 回落 `semantic` 全 lane（= 现有 rrf 行为），**召回盲区不因意图机制引入**
- **query_parsed 进 recall_log**：每次分解结果（含推断的 intent）写入 recall_log，供质量评测核对"推断对不对"——意图机制本身可被评测，防"推断错误导致召回偏"悄悄发生

#### 4.11.4 aspect 的消费端（v0.8 修订，补上此前缺失的定义）

`aspect` 提取出来必须有用——作为**lane 优先级 + 权重微调的提示**，与 intent 正交（intent 定路由，aspect 定 lane 内偏向）：

| aspect | lane 偏向 | 权重调整 |
|---|---|---|
| `price` / `position` 价格持仓 | entity + meta | 升 entity、降 recency |
| `reason` / `why` 理由 | meta + evidence 链 | 升 decision 类型、强 evidence 关联 |
| `timeline` / `history` 时间线 | meta + graph（asof） | 强制 asof、降 recency |
| `relationship` / `rel` 关系 | graph + entity | 升 graph hop |
| `status` / `current` 现状 | vector + meta | 升 recency |
| 无/未知 | — | 默认权重 |

- aspect 从**特征词表**提取（`价格/成本/为什么/理由/当初/当时/和…关系/现在/目前`），提取不到 = `无`
- 不强制：aspect 只是提示，不改变硬约束；实现时先支持前 3 行，其余回落默认

---

## 5. L2 记忆管理层（新增 · 轻量自主层）

> 这是本蓝图的核心新增。原则一句话：**可选的、异步的、永不悄悄破坏数据的记忆维护管线**。

### 5.1 设计原则
1. **显式写入保持默认路径**——`remember/update/forget` 的行为在 L2 开/关时**完全一致**；开启时写路径只多一个**非阻塞原子 dirty 标志**
2. **所有变更先成为提案**——L2 永不直接改数据；每个动作写成 `Proposal` → 过 `Policy` 门槛 → 写 `audit_log`（status=`proposed`）→ 显式启用才 apply
3. **dry-run 是默认**——`run_maintenance()` 默认不改变任何数据，只产出报告
4. **幂等**——每 pass 有 watermark（`meta.l2.last_run.<pass>` + `chunks.processed_at`），重复运行无副作用

### 5.2 六个 pass

| Pass | 输入 | 输出提案 | 复用 | LLM 可选 |
|---|---|---|---|---|
| **P1a 提取·规则模板** | 新 chunks（`processed_at IS NULL`） | 高置信实体/关系（stock 符号+中文名、身份陈述模板等） | 复用 entity_resolve 的 stock-probe 模式（符号+中文名强制）、模板 | ✗ 纯规则，零依赖 |
| **P1b 提取·LLM** | P1a 未覆盖的 chunks | 自由文本事实/实体 | 向量相似度找已有实体、aliases 归一 | ✅ 自由文本；**无 LLM 时跳过** |
| **P2 矛盾检测** | 提案事实 + 当前有效事实 | `supersede_relation` / `update_entity_property` | valid_until 链 + 级联触发器 | ✅ 语义矛盾；规则只做精确谓词 |
| **P3 实体消歧** | 候选对 | `merge_entities` | `entity_resolve.find_duplicate_candidates` + embedding 相似度 | ✅ 中置信度裁决 |
| **P4 记忆卫生** | importance + recall_log | `decay_importance` / `ttl_expire` / `purge_candidate` | recall_count、purged_queue | ✗ 纯规则 |
| **P5 整合** | 高相似 chunks | `merge_near_duplicate_chunks` / `summarize_old` | chunks.superseded_by 链 | ✅ 摘要仅 LLM |
| **P6 社区检测** ⟵ Zep communities | 关系图 + 实体 | `create_community` / `refresh_community_summary` | 图上聚类（标签传播/Louvain 近似）+ 社区摘要存 entity | ✅ 社区摘要仅 LLM |

共享核心：**Proposal / Policy / Applier**
```python
Proposal = {run_id, pass, action, target, before_json, after_json,
            confidence, evidence_chunk_ids, llm_used, status}
Policy   = per-pass enable/阈值/批量上限/protected 豁免/dry_run
Applier  = 接受提案 → 调 Memory 公开写方法 → audit_log(status='applied')
```

### 5.3 触发模型
| 触发 | 形态 | 角色 |
|---|---|---|
| 防抖 post-write 钩子 | 写路径只设 dirty 原子标志，异步 worker 静默期（如 60s）后跑 | **主**（长驻进程时新鲜度最好） |
| 按需 MCP 工具 `memory_maintenance` | 手动/定时调用 | **常驻原语**（不依赖进程存活） |
| 系统 cron | 调工具 | 可选（headless 场景） |

任何触发最终都收敛到同一个 `run_maintenance()`；三种形态是配置选择，不是代码分叉。

### 5.4 矛盾语义：supersede，永不 delete
自动作废仅在**四条件同时成立**时触发：
1. 新事实 `confidence ≥ 0.75`
2. 置信度优势 `F.confidence − E.confidence ≥ 0.20`（margin 是防过度作废的关键护栏）
3. 证据更新：`F.evidence_chunk.timestamp > E.evidence_chunk.timestamp`
4. 非 protected（`user_confirmed=0`）

触发后：`E.valid_until=now`、`E.superseded_by=F_id`，触发器级联失效引用边——**旧数据仍可随时态查询**，非破坏。

不满足 → **不改任何数据**，把双方写入 `audit_log` 的 `conflict_candidate`，供 `memory_merge_confirm` / 人工裁断。

### 5.5 规则 vs LLM 分界（行为矩阵）
| Pass | 无 LLM（离线默认） | 有 LLM（可选，Ollama 保离线） |
|---|---|---|
| 提取 | **P1a 规则模板**（stock 符号+中文名、身份陈述；高精度低召回，近空但非零） | P1a + **P1b 自由文本** |
| 矛盾 | 精确谓词 + 值不同才提案 | 语义矛盾（"age 32" vs "33" 跨谓词） |
| 消歧 | 高阈值自动 + 中档转人工 | 中置信度自动裁决 |
| 卫生 | 完整 | 完整 |
| 整合 | 近重复合并 | + 摘要 |

**关键立场**：无 LLM 时提取 pass 应当**近空而非"模板猜"**——垃圾事实进入图谱是自主层最糟的失败模式。宁可高精度低召回，不可低精度高污染。

### 5.6 安全护栏
- **dry-run 默认**；apply 需显式全局/per-pass 开关
- **append-only `audit_log`**：含 before/after JSON、pass、confidence、evidence、`llm_used`、`revert_sql`
- **撤销**：`memory_audit_undo(audit_id)` 重放 revert_sql（所有 L2 动作都是软写，撤销天然安全）
- **阈值护栏**：矛盾 margin≥0.20、自动合并相似度≥0.95、importance 下限 0.1
- **批量上限**：如 supersede≤20 / merge≤20 / purge≤50，单次病态运行不可级联
- **protected 标记**：`entities.user_confirmed`（master 用户实体及显式确认项豁免一切自动变更）
- **隔离而非销毁**：衰减到 0 / TTL 过期 → 只入 `purge_candidate` 报告；破坏性 purge 需 `confirm_destructive=true` 且只动 purged_queue
- **回退退避**：某 pass 的已 apply 动作被撤销后，下次运行该 pass 提高最低置信度

### 5.7 Schema / API 影响
- 新表 `audit_log`：`id, run_id, pass_name, action_type, ref_type, ref_id, before_json, after_json, confidence, llm_used, status('proposed'|'applied'|'reverted'|'skipped'), created_at, revert_sql`
- 新列：`chunks.processed_at`（可空、索引）、`entities.user_confirmed`（int、索引）
- 复用 `meta` 表存 L2 配置（`l2.enabled`、`l2.dry_run`、`l2.running`、`l2.last_run.<pass>`）
- **新 MCP 工具**：
  - `memory_maintenance(passes[], dry_run=true, since)` — 跑 L2
  - `memory_audit_list(run_id?, status?, pass?)` — 审阅决策
  - `memory_audit_undo(audit_id)` — 撤销已 apply 动作
  - `memory_merge_confirm(proposal_id)` — 把 proposed 提升为 applied（桥接现有手动 entity_resolve 流）
  - `memory_hygiene_stats()` — importance/TTL/purge 积压报告
  - `memory_get_digest(ref=None)` — 常驻记忆摘要（§4.5，⟵ Letta + Headroom CCR）；`ref` 展开到原始 chunk
  - `memory_reason(start_id, end_id, max_hops)` — 多跳路径推理（§4.6，⟵ Cognee）
  - `memory_topics()` — 社区/话题概览（§5.2 P6，⟵ Zep）
  - `memory_correct(entity_id, changes)` — 实体纠正传播（§3.7，⟵ Mem0）
- 配置块：
```toml
[l2]
enabled = false
dry_run = true
[l2.passes]
extract    = { enabled = false, llm = false, min_conf = 0.7 }
contradict = { enabled = true,  margin = 0.20, min_conf = 0.75 }
dedup      = { enabled = true,  auto_threshold = 0.95 }
hygiene    = { enabled = true,  importance_floor = 0.1 }
consolidate= { enabled = false, llm = false }
[l2.caps]
supersede = 20
merge = 20
purge = 50
[l2.hook]
enabled = false
debounce_s = 60
[l2.llm]
enabled = false
backend = "ollama"
```

### 5.8 风险与缓解（Top 5）
| 风险 | 缓解 |
|---|---|
| 1. 提取噪声污染图谱（RRF 被拉偏） | LLM 门控；无 LLM 近空；提案先行；置信度下限；全软写可撤销 |
| 2. 自动作废过度、误删好数据 | 四条件 + 0.20 margin；双时态不删；dry-run 默认；protected 豁免；批量上限 |
| 3. importance 衰减/整合悄悄改变排序 | 纯算术可逆 + floor；dry-run 报告列"将跌出检索阈值的实体"；before/after 全记录 |
| 4. 双语误合并（中英文名真实不同实体） | 自动合并阈值≥0.95；中档转人工/LLM；软合并可恢复；protected 不合并 |
| 5. 离线/在线双模式割裂成两个不一致产品 | 显式 per-(pass×llm) 行为矩阵；单 `run_maintenance()` 入口，模式只是配置差 |

### 5.9 执行原子性与失败语义

**问题**：`run_maintenance()` 批量 apply 提案时，事务边界、中途失败、watermark 推进的精确语义未定义——不写清，P1 实施必踩坑。

**决策**：
| 方面 | 设计 |
|---|---|
| **事务粒度** | **每 proposal 一个事务**（细粒度）。同 pass 先收集全部候选 proposals，再逐个 apply；单条失败**不拖垮整批** |
| **失败语义** | apply 失败 → 该 proposal 标 `skipped` + 错误记入 `audit_log`，继续下一个；整批结束返回 `{applied, skipped, failed}` 报告 |
| **watermark 推进** | **pass 全部成功后才推进**（`meta.l2.last_run.<pass>` / `chunks.processed_at`）。中途失败不推进 → 下次运行重试失败项；已成功的 item 因 watermark 未动会重跑，但所有 apply 是软写（valid_until 链）**幂等**，重跑无副作用 |
| **批量上限** | 已有（supersede≤20 / merge≤20 / purge≤50）——超限的余量排队下次 |
| **与 L2 触发的关系** | 防抖钩子/按需工具/cron 三种触发都收敛到 `run_maintenance()`；`meta.l2.running` 防重叠 |

**因果链（为什么每 proposal 一事务而非整批一个）**：整批一个事务的"全有或全无"看似更干净，但 L2 是**自主运行**，单条 bad proposal（如提取器幻觉）不应让同批 20 条好提案全回滚；且全量回滚时 watermark 不推进会导致**每轮重跑全部**——细粒度事务 + watermark 门控把"病态单条"和"健康整批"隔离。

#### 5.9.1 提案生命周期状态机

```
              ┌────────────── apply ──────────────┐
   proposed ──┤                                   ├──► applied ── undo ──► reverted
              └── skip(规则拒/超限/失败) ──► skipped
```
- **`proposed`**：L2 生成，写入 audit_log，未改任何数据（dry-run 态）
- **`applied`**：过 Policy 门槛 + 显式 apply，已执行软写（valid_until 链）
- **`skipped`**：规则拒（置信度不够）/ 批量超限 / apply 异常——**不重试的终态**，原因记录在 audit_log
- **`reverted`**：经 `memory_audit_undo` 从 applied 回退——重放 revert_sql，数据回到 before 态
- **re-applied**：reverted 的提案可再次 apply（状态机无死锁，但回退后该 pass 提阈值——§5.6 回退退避）

**audit_log 承载状态迁移**：同一 run_id 下，一条提案的 proposed/applied/skipped/reverted 是**多行**（append-only），不是改一行——保留完整决策史（谁、何时、为何改判）。

#### 5.9.2 watermark schema（精确语义）

`meta` 表存：
```
l2.last_run.<pass>     = ISO 时间戳   # pass 上次成功完成的时刻
l2.watermark.<pass>    = int          # 幂等游标，如 chunks.processed_at < last_run 或 id 游标
l2.running             = bool         # 防重叠
```
- **语义**：pass 只在 `l2.last_run.<pass>` 之后的新数据上运行（`processed_at IS NULL OR processed_at > last_run`）
- **推进时机**：pass 内**全部 proposal 处理完**（无论 applied/skipped）才更新 `last_run`；**异常中止不推进** → 下次重跑，幂等软写保证无副作用
- **purge pass 特殊**：purge 是破坏性操作，单独 watermark + 需要 `confirm_destructive`，不与其他 pass 同批

#### 5.9.3 回退级联

- **单条回退**：`memory_audit_undo(audit_id)` 只撤销该条（重放 revert_sql）
- **级联回退**：一条 `applied` 的 supersede 如果还触发了触发器级联（引用边自动失效），回退时**只恢复主数据，级联边是否恢复记录在案**——不自动反向重建（防回退引发新问题）；audit_log 记 `cascade_affected: [relation_ids]` 供人工决定
- **批量回退**：`memory_audit_undo(run_id=...)` 按逆序逐个回退（后应用先回退），中途失败停在失败项并报告

---

## 6. L3 协议层

### 6.1 消除 raw-SQL 旁路
- `memory_entity_resolve` / `memory_list_entities` / `memory_search_relations` 三个 `_CUSTOM_HANDLERS` 下沉为 `Memory` 方法（`list_entities()` / `search_relations()`），统一走校验 + 单一访问点
- 收益：旁路工具获得与核心工具同等的校验、指标、时态过滤语义

### 6.2 批量操作
- `memory_remember_many`（批量写入，事务包裹）、`memory_forget_batch`
- 服务端单一事务，客户端少开连接

### 6.3 分页
- `memory_list_entities` / `memory_search_relations` 加 `cursor`（基于 id 的游标分页，而非 offset）

### 6.4 客户端
- `MneloClient` 复用 SSE 长连接（现每调用新建连接）；`timeout` 参数真正生效（现未传入底层）

### 6.5 工具收敛（反蔓延）
现状 10 个 + L2/借鉴 新增后 ~19 个，**这是真实错误方向**——LLM Agent 工具选择本身就是错误源，工具越多越容易选错。收敛原则：**按意图分组为高层工具，内部派发**，目标 ~10 个：

| 意图组 | 工具（收敛后） | 合并现状 |
|---|---|---|
| 写 | `memory_write`（remember/update/correct/batch） | remember / update / relate / correct / remember_many |
| 查 | `memory_recall`（含 location/session/type/asof/filters） | recall（吸收多跳推理与摘要为策略参数） |
| 组织 | `memory_loci`（建容器/放置/子树导航） | 新增（§4.8） |
| 维护 | `memory_maintenance`（6-pass + dry-run） | 新增（L2） |
| 审阅 | `memory_audit`（list/undo/merge_confirm） | 新增（L2） |
| 图谱 | `memory_graph`（query/reason/topics） | graph_query / reason / topics |
| 管理 | `memory_stats` / `memory_hygiene_stats` | 现 stats + 新增 |

- 保留 `forget` 为独立工具（危险动作显式化，不让它藏在 write 里）
- 客户端 `MneloClient` 同步收敛为高层方法

### 6.6 mcp_server.py 拆分 + facade 设计（[8/12] P0 落地）

**背景**：原 `mcp_server.py` 单文件 ~1614 行，跨多职责（heavy import guard + 22 Tool() schema + handler dispatch + 4 种 transport + health/metrics endpoint + main CLI）。8/12 split 让 P1 测试粒度可拆、code review 可分 commit、CD 可单 module 重启。

**落地架构**（8/12 commit 506d5bc + 8/14 commits db522b3/c9697f8/d3972e8/c71801b/c51c72d）：

| 模块 | 行数 | 职责 |
|---|---|---|
| `mcp_server.py` | ~80 | facade（PEP 562 `__getattr__` 转发 + main() CLI） |
| `mcp_guard.py` | ~50 | `_MCP_AVAILABLE` flag + heavy mcp/Starlette/uvicorn 导入（让 unit test 跳过重型 deps） |
| `mcp_tool_definitions.py` | ~400 | 22 个 Tool() JSON schema（与 dispatch 解耦，e2e 单独 snapshot） |
| `mcp_tool_handlers.py` | ~250 | `_TOOL_REGISTRY` / `_TASK_TOOL_REGISTRY` / `_CUSTOM_HANDLERS` + `_handle_*` |
| `mcp_tool_dispatcher.py` | ~350 | `_call_tool` / `_get_mem` / `_rate_limit_check` / server wiring |
| `mcp_transports.py` | ~600 | stdio/SSE/HTTP/dual transport + health/metrics endpoint |

**PEP 562 facade 设计**（c51c72d 终态）：

```
import uvicorn  # re-export (test contract)
import auth
import mcp_guard
import mcp_tool_definitions
import mcp_tool_dispatcher
import mcp_tool_handlers
import mcp_transports
# 注意: 不 `import config` 在 top-level — 让 PEP 562 __getattr__('config')
# 转发到 _config_mod.config (Config instance), 不是 bare module
import config as _config_mod  # noqa: E402

_SUB_MODULES = (
    mcp_tool_dispatcher,
    mcp_tool_handlers,
    mcp_tool_definitions,
    mcp_transports,
    mcp_guard,
    auth,
    _config_mod,
)

def __getattr__(name):
    """PEP 562 — read-only attribute router. __setattr__/__delattr__ 不 work (CPython
    module-level C-level setattr 抢先生效, 绕过 Python hook)."""
    for mod in _SUB_MODULES:
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module 'mcp_server' has no attribute {name!r}")
```

**Test contract**（c51c72d 落地后的契约）：

| 调用 | 行为 |
|---|---|
| `from mcp_server import X` | 走 PEP 562 转发到子模块 |
| `mcp_server.X` (read) | 走 PEP 562 转发 |
| `monkeypatch.setattr(mcp_server, X, val)` | **不工作** — CPython C-level setattr 不触发 PEP 562 `__setattr__` |
| `monkeypatch.setattr(<子模块>, X, val)` | 改子模块，下次 facade read 转发到新值 |
| 子模块内部 `from X import Y` (value-binding) | 单测内部 call 永远拿 import-time 值，monkeypatch 不生效 → 必须改 module attribute lookup |

**已知坑**（commit msg 必写的 4 条教训）：

1. **PEP 562 `__setattr__` 在 module 上不可靠**——CPython module 的 setattr 在 C 层，Bypass Python hook。
   Subagent 8/14 通过隔离测试发现这点，帮我回了 db522b3 的错设计（commit c9697f8）。
   Lesson：下次设计 PEP 562 module facade 默认只保留 `__getattr__`，不要画蛇添足写 `__setattr__`/`__delattr__`。
2. **`from X import Y` value-binding 让 monkeypatch 失效**——mcp_transports 内部 `_MCP_AVAILABLE` / `_mem_instance` 都靠这个。
   Fix pattern：函数内 `import mcp_guard as _mg; if not _mg._MCP_AVAILABLE:` 走 module attribute lookup。
3. **`_load_from_repo()` 用 `spec_from_file_location` 拿子模块是 separate instance**——写它不真改 `sys.modules['mcp_tool_dispatcher']`。
   Fix pattern：test 用 `sys.modules['X']` 拿 singleton 改。
4. **facade `import X` 在 top-level 占 dict 让 PEP 562 `__getattr__('X')` 不触发**（db522b3 错设计）——client 拿到 raw module 而不是期望的 instance。
   Fix pattern：test-only imports 用 `import X as _X` 写到全局但不在 facade top-level；或者改 facade `__getattr__('X')` 走 chain → 找到 `_X.X` (instance)。

**CI 实战**（ci_per_file_runner.py 镜像 CI 跑同款）：
- 起点：mcp_server.py split 后 24 test fail（8/12 commit 后 first CI run）
- 终点：c51c72d 后 aggregate 0 fail（每个 file fresh-DB-per-run 隔离掉 SSE port race）
- 中间发现关键 insight：PEP 562 setattr 不 work → 全部 test 改 sys.modules singleton patch
- remaining 10 native crash（SIGSEGV exit -11）— 跑测环境 macos-26-arm64 usearch/sqlite_vec 已知 race，与本 split 无关

**未来延伸**（§6.6 落地后回归）：
- `memory.py` 单巨石 (~3000 行) 同样模式可拆 (task_states split 已落地 c72e1b2)
- `mcp_tool_definitions.py` (~400 行) 是另一个潜在拆分点 — 按 L1/L2/L3 工具族分文件
- facade pattern 不限于 mcp_server: `auth.py` / `config.py` 同样是 value-binding 重灾区，未来重构时同 design

---

### 6.7 v0.15 写路径 + 召回质量改进（[8/15] E-1 / E-3 / E-4 落地）

**背景**：8/15 一天内连续落地 3 个 E 改进，每个 commit 单一意图、CI 5/5 全绿。**关键纠正**：v0.15 这 3 个 E 改进**不是 Mem0 借鉴**——是 mnelo 自身 §1.2 短板修复 + 实战数据驱动决策。设计哲学层面 mnelo 跟 Mem0 有根本差异（local-first 单文件 SQLite / standard MCP / amoral by design / boring & predictable / 不抢决策），不是 "Mem0 lite"。

**落地架构**（8/15 commit chain: 8519089 → 99ae38d → 28c846c）：

| E | commit | 文件改动 | 行数变化 | 改进点 |
|---|---|---|---|---|
| **E-1** | `8519089` | `memory.py` + `memory_core.py` | +33 / -7 (helper) + 130 (包裹) | 显式 `_txn()` 包裹 remember/update 写路径 |
| **E-3** | `99ae38d` | `l2_maintenance.py` + 3 mcp_*.py | +189 / -3 + 27 (MCP 注册) | `Memory.recall_stats()` + `memory_recall_stats` MCP 工具 |
| **E-4** | `28c846c` | `memory_core.py` + `l2_maintenance.py` | +24 / -3 (RRF 累加) + 19 (SQL 展开) | `_rrf_fuse` methods 列表累加 + E-3 按 methods 展开聚合 |

#### 6.7.1 E-1: 显式事务包裹 (DESIGN §1.2 #7)

**真痛点**（不是 Mem0 借鉴）：
- 原 `remember()` / `update()` 写 chunk + entities + relations + vector 4 步，**依赖 sqlite3 隐式事务**（最后一行才 `commit()`）
- 单例 conn 复用（mcp_server Memory 单例）+ 中途异常 → 隐式事务保持打开 → 下次 `commit()` 可能连同提交脏数据
- 后果：**vec0 rowid 漂移的孤儿 chunk**（实体缺席但 chunk 占位）+ `update()` 静默吞 embed 异常（line 644-648 `try/except: logger.warning`）→ 老 chunk 被标 superseded 但新 chunk vector 缺席 → 召回断裂

**修复模式**：
```python
@contextlib.contextmanager
def _txn(conn):
    """[8/15 E-1] 显式事务包裹 helper. 行为契约:
    - 进入: BEGIN
    - 正常: COMMIT
    - 异常: ROLLBACK + raise (不吞)
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        try: conn.execute("ROLLBACK")
        except Exception: pass
        raise
    else:
        conn.execute("COMMIT")

# remember() 内:
with _txn(self._conn):
    # 1. INSERT chunk
    # 2. _upsert_entity (loop)
    # 3. INSERT relations (loop)
    # 4. self._index.add(...) ← 在事务内, 失败 → ROLLBACK
    # 5. PII audit_log (loop)
# 退出时 _txn 已 COMMIT
```

**关键设计决策**：
- index.add 放在 **SQLite commit 前**（try 内）。失败 → SQLite ROLLBACK → chunk 不入库；index 也没 add（因为 add 失败抛异常前 chunk 还没 commit 也没意义）— **两边一致** ✓
- 删除 `update()` 静默 `try/except`（line 644-648）— 异常正常上抛供调用方感知。主人 8/5 review iron law "tests-green ≠ sufficient" 应用：之前 `try/except: pass` 把 embed 失败吞掉，**CI 全绿但召回数据有 bug**

**用法变化**（调用方感知）：
- ✅ 公开 API 0 变化（`m.remember()` / `m.update()` 签名不变）
- ⚠️ `update()` 失败现在正常 raise `RuntimeError`（之前静默吞）。如果老代码 `except: pass` 兜底 — **现在能感知失败**，是 8/5 review iron law 的目标
- ✅ remember() 失败也正常 raise（之前 SQLite 隐式 commit 路径没显式 ROLLBACK）

**已知坑**：
1. **usearch/zvec 索引独立于 SQLite 事务**（search_index.py:78 主人自己写过）—— index 写文件 (.usearch.index) 不在 SQLite 事务内。本设计保证 "index 失败 → SQLite ROLLBACK" 这条路径（因为 index.add 失败抛异常前 SQLite 还没 commit）。**但 "index 成功 + SQLite COMMIT 失败" 的极低概率场景**：index 写了但 SQLite 没存 → 需 reverse `index.remove`。当前 threshold 接受，留 v0.16+ 处理。
2. **8/8 P1 fix 与本改进兼容** — 之前 8/8 已加 entity validation dry-run（防 ValidationError 时 chunk 孤儿）。E-1 把所有异常路径都接住（不仅是 ValidationError），是 8/8 修复的超集。

#### 6.7.2 E-3: 召回质量分析 (DESIGN §1.2 #6)

**真痛点**：
- `recall_log.recall_details_json` 7/18 起每条 recall 写满 `method / rank / distance / rrf_score / importance`（top-5）
- **但无人消费做质量分析** — 17 个 Prometheus 指标全是运维视角（recall_total, recall_latency），**没有 method 分布 / latency p50/p95/p99 / 空窗率 / 按日序列**
- 主人 1.1 次/日召回低频 + 30 天 116 次真实数据，但**没法看 "哪路召回实际贡献最高 / 召回质量是不是变好"**

**修复模式**：
```python
def recall_stats(self, days: int = 30, group_by: str = "method") -> Dict:
    """聚合 recall_log, 4 个子键:
    - totals: {total_recalls, unique_queries, total_hits, empty_results, empty_rate}
    - latency_ms: {p50, p95, p99, avg, min, max, n} (numpy percentile)
    - methods: {method_name: {hit_count, avg_rank, avg_rrf_score, avg_distance}}
    - by_day: [{day, count, empty}, ...]
    """
    # 1. WHERE 过滤 + totals
    # 2. latency 聚合 (numpy / Python sorted fallback)
    # 3. methods breakdown: json_each(je.value, '$.methods') 展开 (E-4 后)
    # 4. by_day: substr(created_at, 1, 10) GROUP BY
```

**关键技术点**：
- `json_each(je.value, '$.methods')` 展开 methods 列表（E-4 后才有，之前用 `$.method` 单字段）
- `COALESCE(json_extract(je.value, '$.methods'), json_array(json_extract(je.value, '$.method')))` fallback 兼容 pre-E-4 老数据
- `json_array_length(results_json)` 算总命中数（SQLite ≥ 3.38）
- numpy percentile 默认 linear interpolation（7 值 p95 = idx 5.7 插值 ≈ 79，**不是 discrete index 取整**）

**MCP 工具暴露**：
```python
{
    "name": "memory_recall_stats",
    "description": "[E-3] 召回质量分析 (DESIGN §1.2 #6): 各 method ...",
    "inputSchema": {
        "properties": {
            "days": {"type": "integer", "default": 30},
            "group_by": {"type": "string", "enum": ["method", "day"]}
        }
    }
}
```

**用法变化**：
- ✅ 公开 API additive 新增（`m.recall_stats()` + MCP 工具 `memory_recall_stats`）
- ✅ 老 recall 调用 0 变化
- 主人新能力：跑 `m.recall_stats(days=7)` 看真实召回分布，决定下一步优化方向

#### 6.7.3 E-4: RRF methods 列表累加 (DESIGN §1.2 #4)

**真痛点**（**关键 E-3 的数据纯度 bug**）：
- `_rrf_fuse` line 1448 `rrf_hits[cid] = h` **直接覆盖**，不积累 methods
- hit_lists 顺序固定 = `[vector, graph, meta, entity]`
- **同 chunk 4 路都命中时**：recursion 最后遍历的 entity 永远覆盖前面 → `recall_details_json.method = "entity"`
- **E-3 主人用 `memory_recall_stats` 查 "vector 实际命中率"** → **之前看到的全是错的**！

**这是主人 8/5 review iron law "tests-green ≠ sufficient" 的反向印证**：8/14 push E-3 时 aggregate 0 fail，**但聚合方法分布其实错的**。如果不修 E-4，主人根据错误数据做优化决策（如 "vector 路死了所以要换 embedder"）→ 灾难。

**修复模式**：
```python
# _rrf_fuse 新增 rrf_methods 累加器:
rrf_methods: Dict[str, List[str]] = {}
for hits in hit_lists:
    for rank, h in enumerate(hits):
        cid = h["chunk_id"]
        # ... RRF score 计算 ...
        if cid not in rrf_hits:  # 首次见 → set, 后续 → 保持第一路
            rrf_hits[cid] = h
        m = h.get("method")
        if m and m not in rrf_methods.get(cid, []):  # accumulate + dedup
            rrf_methods.setdefault(cid, []).append(m)
# ...
h["methods"] = rrf_methods.get(cid, [h.get("method")] if h.get("method") else [])
# backward-compat: 'method' 单字段保留 = 第一路
```

**关键决策**：
- **保持 hit 字典向后兼容**：`method` 单字段保留 = 第一路（`vector`），`methods` 列表是 additive 新字段
- `_log_recall` 写入 recall_details_json 用 `methods` 列表（`method` 单字段也写，backward-compat）
- E-3 `recall_stats` 聚合按 `methods` 列表展开（一条 hit 在每个参与的 method 都 +1）

**用法变化**：
- ✅ 公开 API 0 变化（`m.recall()` 返回 hit 字典）
- ✅ `hit["method"]` 单字段保留（=第一路）
- ➕ `hit["methods"]` 新字段（list[str] = 所有 RRF 命中 lane）
- ➕ `recall_details_json` 每条含 `methods` 列表

**已知坑**：
1. **同 lane 内多次出现同 chunk**（upstream list 重复）→ methods dedup，**不重复**（看 `test_methods_dedup_within_same_lane`）
2. **numpy percentile default linear interpolation** — 测试预期必须用 `idx = p/100 * (n-1)` 插值公式，不能用 discrete int index

#### 6.7.4 Mem0 借鉴落地对照表（**老实分类**）

| Mem0 借鉴点 | 状态 | 落地版本 | 真驱动 |
|---|---|---|---|
| **scoping IDs** (user/agent/run) | ✅ 已落地 | v0.14 P0 (8/11) | 借鉴 Mem0 + LangChain |
| **会话级召回隔离** (session_id) | ⏳ 设计落地未做 | §4.7 设计 | 借鉴 Mem0 scoping |
| **`memory_correct()`** self-editing | ⏳ 设计落地未做 | §3.7 设计 (8/4) | 借鉴 Mem0 |
| **`dedup_check=True`** 写入 NOOP | ⏳ 设计落地未做 | §3.7.1 设计 | 借鉴 Mem0 |
| **P1 提取器** (LLM-driven) | ⏳ 设计落地未做 | §5.2 设计 | 借鉴 Mem0 + Zep |
| **写路径显式事务** (E-1) | ✅ v0.15 | **mnelo 自身 §1.2 #7** | ❌ **不是 Mem0** |
| **召回质量分析工具** (E-3) | ✅ v0.15 | **mnelo 自身 §1.2 #6** | ❌ **不是 Mem0** |
| **RRF 多路 method 累加** (E-4) | ✅ v0.15 | **mnelo 自身 §1.2 #4 + E-3 数据纯度 bug** | ❌ **不是 Mem0** |

**与 Mem0 根本差异**（不是"缺"什么，是"选择不"）：

| 维度 | Mem0 默认 | mnelo 选择 |
|---|---|---|
| **存储** | Qdrant + PG + Neo4j 多 store | 单文件 SQLite + usearch |
| **图谱抽取** | LLM-driven 自动 | 显式 entities + relations（主人 6/29 不抢决策） |
| **多用户隔离** | user_id 强制 | scoping_id 可选（主人多 agent 单机场景） |
| **API** | REST + GraphQL | 标准 MCP |
| **dedup 决策** | 自动 NOOP | 显式，dedup_check 手动开关 |
| **Content moderation** | PII filter + content moderation | **amoral by design**（§12） |
| **回滚** | 重 embed + 全量重建 | WORM (write once read many) + versioned |

#### 6.7.5 v0.15 关键教训

1. **8/5 review iron law "tests-green ≠ sufficient" 反向印证**：E-3 8/14 push 时 aggregate 0 fail，但聚合方法分布数据是错的（E-4 修）。如果只信 CI aggregate 不读测试逻辑，主人可能根据错数据做灾难性决策（"换 embedder 因为 vector 路死了"）。
2. **不夸大"借鉴"是诚实 discipline**：第一次回答时把 v0.15 三个 E 都包装成"吸收 Mem0 优点" — **过度归功**。主人 push back `?` 后重新分类：真借鉴 (scoping_ids) vs 自身痛点驱动 (§1.2 短板)。**写文档比写代码更要老实**。
3. **P1 #39 final gate 应用**：v0.15 三个 E 改进后必写 §6.7 章节，把"是什么 / 为什么 / 怎么用 / 已知坑 / 借鉴对照"老实记录。**不写文档 = 知识没沉淀**，下次同类问题重新踩坑。
4. **8/9 SKILL "全面 reflect 不补丁式" 应用**：顶部 v0.15 changelog 一次说清楚（含纠正 v0.14 "Mem0 借鉴" 含糊表述），不在 §6.6 留补丁式批注。
5. **Cognee / Zep / MemGPT / SuperMemory / Hindsight 等其他借鉴系统也按本表老实分类** — 避免再次出现"过度归功"。

#### 6.7.6 v0.15 CI 实战

**3 commit chain** (8519089 → 99ae38d → 28c846c)：
- 起点：每个 commit 写完先跑 `ci_per_file_runner.py` 本地 mirror，aggregate 0 P1 才 push
- 终点：3 个 GH Action run (#31845465651 / #31847149294 / #31850644450) 全部 5/5 jobs success
- remaining 10 native crash（SIGSEGV exit -11）— 跑测环境 macos-26-arm64 usearch/sqlite_vec 已知 race，与本改进无关
- 每个 commit 单一意图（mnelo-refactor-patterns P1 #28 v3）：E-1 写事务 / E-3 召回统计 / E-4 RRF 累加，独立 review / revert

**TDD red→green 模式应用**（P1 #40 CI closure）：
- E-1: 4 fail → 4 pass（暴露 index.add 失败 + 静默 except + relations FK violation 3 个真问题）
- E-3: 7 fail → 7 pass（暴露 method 标签错 / percentile 插值 / json_each path 3 个细节）
- E-4: 7 fail → 7 pass（暴露 RRF lane 覆盖 + 老数据 fallback 兼容问题）

**未来延伸**：
- 主人跑 `m.recall_stats(days=7)` 看真实数据 → 决定 E-2 (FTS5 meta 路) 是否值得做
- §1.2 剩余 5 个短板（无记忆生命周期 / 单巨石 / 双时态不完整 / entity 路不匹配 id / 协议层 raw-SQL 旁路）按 mnelo 自身痛点驱动节奏，逐个 v0.16+ 修

---

## 7. L4 可观测性

### 7.1 召回质量指标（[8/15 v0.15 E-3] 部分落地）

| 指标 | 含义 | 数据源 | 状态 |
|---|---|---|---|
| `mnelo_recall_precision_at_k` | 召回质量（对已知标注集） | 评测 harness | ⏳ 设计未做 |
| `mnelo_recall_lane_hits_total` | 每 lane 命中占比（`method` label 的 recall_total 升级） | recall_log | ✅ **E-3 + E-4 落地**（`memory_recall_stats.methods`） |
| `mnelo_recall_empty_rate` | 空结果率 | recall | ✅ **E-3 落地**（`memory_recall_stats.totals.empty_rate`） |
| `mnelo_recall_latency_p50/p95/p99` | latency percentile | recall_log | ✅ **E-3 落地**（`memory_recall_stats.latency_ms`） |

**E-3 落地的召回质量分析**（`memory_recall_stats` MCP 工具 + `Memory.recall_stats()` 方法，详见 §6.7.2）：

```python
# 4 子键聚合:
{
    "window_days": 30,
    "totals": {total_recalls, unique_queries, total_hits, empty_results, empty_rate},
    "latency_ms": {p50, p95, p99, avg, min, max, n},
    "methods": {method_name: {hit_count, avg_rank, avg_rrf_score, avg_distance}},
    "by_day": [{day, count, empty}, ...],
}
```

**E-4 修正的关键 bug**（§6.7.3）：E-3 聚合方法分布数据**之前是错的**（RRF lane 覆盖问题），E-4 修后才准确。主人 8/5 review iron law "tests-green ≠ sufficient" 反向印证：**CI 全绿但数据是错的**。

### 7.2 反馈闭环
- `health_check.py` 日报告消费质量数据：空窗 lane、asof 时态正确率、importance 分布
- 异常告警：如某 lane 命中率连续下滑 → 提示检查 embedder 模型 / 数据质量问题

### 7.3 记忆健康度评分 ⟵ 借鉴 Hindsight + 可观测性
把 17 个散落指标收敛成**一个可行动的复合评分**（0–100），`health_check` 呈现：
| 分量 | 指标 | 警戒线 |
|---|---|---|
| 覆盖率 | 活跃实体/关系 vs 总历史 | 活跃占比骤降 |
| 新鲜度 | 近期写入占比、过期未清比例 | purged_queue 积压 |
| 去重度 | 重复实体候选数（`find_duplicate_candidates`） | >50 组 |
| 平衡度 | 各 lane 命中率方差 | 单 lane <5% 且持续 |
| 健康度 | 以上加权合成 | <0.6 提示需要维护 |

**健康度 v0 公式（P2 实施时定，不在 DESIGN 阶段假装拍权重）**：5 个分量量纲不同（覆盖率是 0-100% 占比、新鲜度是衰减时间、去重度是离散计数、平衡度是方差），设计阶段预设权重是伪精确。决策：P2 起步 **全分量等权归一化 + 0.6 警戒线**，上线后按真实数据调参（哪个分量先触线就调哪个）。

一句话回答"我的记忆是不是变脏了"，L2 的 run 报告直接喂给这个评分。

---

## 8. 可迁移路径（存储选型）

### 8.1 为什么不是 Postgres / 独立向量库
- **Postgres/pgvector**：个人单机工具引入服务器进程 + 连接管理 + 备份复杂度，负收益；个人规模（≤50 万向量）用不上其能力，且会破坏"cp memory.db = 备份"的 core tenet
- **独立向量库（Qdrant/Milvus）**：违背 local-first，且只解决 4 路召回中的 1 路——mnelo 的硬骨头（图/时态/证据）它们一点忙帮不上

### 8.2 zvec：首选升级候选
> 事实更正：zvec 是**阿里巴巴**开源（`github.com/alibaba/zvec`，Apache-2.0），非腾讯（腾讯云社区发过介绍文造成混淆）。

- **为什么值得**：进程内嵌（`pip install` 即用，符合 local-first）、真 HNSW/DiskANN、**原生 FTS + BM25**——恰好补上 mnelo 两个真实短板（sqlite-vec 暴力扫描 + meta 路无索引 LIKE）
- **边界**：解决不了图路/时态（那些留在 SQLite）；双存储需设计同步（update/forget 向量作废、asof 过滤、chunk↔向量映射）；项目年轻（v0.6.0，2026-07）

### 8.3 适配器分档
| 档位 | 向量 | FTS | CPU 要求 | 触发条件 |
|---|---|---|---|---|
| **今日** | sqlite-vec（零依赖） | SQLite FTS5 | 任意 | 默认 |
| **升级·旧 CPU** | **usearch**（HNSW，硬件无关，实测 Ivy Bridge 可跑） | 保持 LIKE | 任意 | 需 HNSW 且 CPU 无 AVX2 |
| **升级·新 CPU** | **zvec**（HNSW + 原生 FTS） | zvec 原生 | AVX2+ | 向量 >~50 万 或 meta 路延迟超标 |
| **超大规模** | Qdrant/Milvus | 独立 | — | >千万向量 / 分布式 |

迁移由 §3.6 的 `SearchIndex` 适配器封装，业务代码零改动。

**回落策略（v0.11 修订，hermes 评审采纳）**：显式配置非默认后端但不可用 → **默认 fail-fast**（报错提示改配置/装依赖），仅 `MNELO_MEMORY_ALLOW_FALLBACK=1` 才回落 sqlite_vec + 日志——silent 回落违背 boring & predictable。跨存储一致性（Q1/Q2）：索引写入在 SQLite 事务外，用 `repair_index.py`（增量清孤儿）+ sidecar 校验和（load 时比对）兜底；详见 `TASKS_SEARCH_INDEX.md` A7。

---

## 9. 演进路线图

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **P0** | L0：✅ **记忆类型谱系（§3.0，已落地 2026-08）**、chunk.valid_from、FK、FTS5、写事务、**并发与保留模型（§3.9）**、schema 迁移框架、**实体纠正传播 + 写入去重（§3.7）**、**git 快照（§3.8）**；L1：entity id 匹配、RRF 标签修正、**新近度加权（§4.9）**、**会话级召回隔离（§4.7）**、质量评测 harness | — |
| **P1** | L2 v0：audit_log + 矛盾检测 + 消歧 pass（规则优先），dry-run 跑通，`memory_maintenance` 工具；L1：**多跳路径推理 `memory_reason`（§4.6）**、**常驻记忆摘要 `memory_get_digest`（§4.5，规则版先上）**、**双轨组织 `memory_loci`（§4.8）**、**来源可信度加权（§4.10）**；**P1 末尾执行工具收敛（§6.5）**——新工具随 L2 落地即收敛，不让 agent 长期面对 ~19 个工具 | P0 |
| **P2** | L2 完整：提取（P1a 规则 + P1b LLM）+ 卫生 + 整合 + **社区检测 `memory_topics`（§5.2 P6）**；L4 质量闭环（precision@k + **健康度评分 §7.3** + health_check 反馈） | P1 |
| **P3** | L3：消除旁路、批量/分页、客户端长连接；✅ **SearchIndex 适配器 + zvec/usearch 后端（§3.6/§8.3，已落地 2026-08）** | P0-P2 |

每阶段独立可交付、可回滚，不阻塞其他阶段。

**阶段执行说明（v0.11 修订）**：P3（SearchIndex）先于 P1/P2 落地是**有意的**——它与 L2 自主层无依赖、可独立交付。但 hermes 评审指出两个配对风险，已采纳：
- **P1 §5.2 P4 卫生 pass——已排期**：目标 **Q3 2026 末（2026-09-30）前**落地，任务分解见 **`docs/TASKS_L2_HYGIENE.md`**（H1-H8，含时间窗）。否则 usearch/zvec 索引可能长期与 chunks 不一致。`repair_index.py`（TASKS A7）是即时兜底，卫生 pass 是长期解
- **P3 可观测性**：`[search] backend` 实际生效值由 health_check 报告（C3）+ **drift 指标**（索引孤儿/活跃 chunk 比，TASKS A7）持续观测；后续 L2 audit_log 落地后补到审计链

**阶段执行说明（v0.12 实际数据回灌 8/4）**：
- **§3.8 §5.6 done bug 已修**（commit 4bd654d，2026-08-04）：`run_purge_worker()` 3 phase 落地，v0.5.12 100% placeholder id 脏数据已清 4198 项；剩 110 项真 mnelo 软删项等 8/16-8/28 30 天后 Phase 2 自动物理删 + set done=1。**主人口径**: 主人 cron 可定期调 `m.run_purge_worker(dry_run=False)` (e.g. 每日 1 次) 维护 purge 状态
- **TASKS 依赖的 schema 前置 (H-1) 仍未建**: `user_confirmed` / `processed_at` / `audit_log` 3 项 — H0 之前必须建, 否则整个 L2 落不了 (§1.1.1 实际回灌表)
- **memory_type 6 类系统**: 字段已加 (f1bc1bf), 实际 0% non-fact — 必须等 P1 提取器 (§5.2 P1a 规则 + P1b LLM) 落地才能真用; H3 TTL 表不能先于 P1 跑
- **P1 卫生 pass 落地步骤调整 (v0.12 修订)**: H-1 (建 3 schema) → H0 (audit_log + 抽象基建) → H1-H8 (H1-H4 Q3 中, H5-H7 Q3 末, H8 同步). 不按此顺序, H0/H5/H7 落不了

---

## 10. 决策记录（ADR 摘要）

| 决策 | 结论 | 理由 |
|---|---|---|
| 存储形态 | 保持 SQLite 单文件真相源 | local-first、零运维、图/时态/证据是 SQLite 强项 |
| 自主层自主度 | 轻量可选，非全自动 | 保持 boring & predictable；防垃圾事实污染 |
| LLM 依赖 | 零依赖默认，LLM 纯可选 | 保持离线能力；无 LLM 时提取近空 |
| 向量历史 | 软删除保留（P0），换取语义回放 | 与"measured"原则一致，接受 vec0 增长 |
| meta 路 | FTS5（SQLite）优先于 zvec | 零新依赖即可补短板；zvec 留作升级档 |
| 触发模型 | 防抖钩子 + 按需工具双轨 | 长驻/短命进程都覆盖，幂等收敛到单一入口 |
| 常驻摘要 | 摘要即 chunk（双时态管理） | 复用现有软删/版本机制，不为摘要引入第二套状态 |
| 实体纠正 | 走 `Memory.correct()` 级联，受 identity_fact 不可变约束 | 比 L2 更基础的写路径能力，先于自主层落地 |
| 写入去重 | `dedup_check` 默认关 | 保持显式语义与写入低延迟；L2 负责事后兜底 |
| 快照 | git 快照 + valid_until 版本链互补 | 库内版本管单条历史，快照管整体时间旅行 |
| 社区摘要 | 摘要仅 LLM（可选），聚类纯规则 | 与 L2 其它 pass 的规则/LLM 分界一致 |
| 产品边界 | 存储+检索层，非 Agent 运行时 | 防 scope creep；新增能力过"边界审查" |
| 记忆类型 | 六类谱系（fact/preference/episode/decision/procedure/ephemeral） | 提取/矛盾/卫生按类型定规则 |
| 组织模型 | 双轨（显式容器树 + 语义涌现），`location` 仅可选约束 | 语义搜索与显式收纳互补，不互斥 |
| 新近度 | RRF 加 freshness 因子（半衰期可配） | 个人记忆"当下的相关度"应加权 |
| 可信度 | 来源档位映射权重，进 RRF | 已有 confidence/source 字段用满 |
| 工具面 | 收敛到 ~10 个高层工具，意图分组 | LLM 工具选择是错误源，越少越好 |
| 并发 | 单写者 + WAL + 多读者，不引多写者 | 边界声明 + 事务串行 |
| 实际回灌机制 (v0.12) | v0.3 报告 + deepseek 交叉验证 → §1.1.1 新增实际回灌表 + §1.2.1 实际新增短板 5 项 | DESIGN 跟实际漂移会被定期 v0.x 报告抓回，不再是"自证循环" |
| 30 天延迟清 (v0.12) | `run_purge_worker()` 3 phase (commit 4bd654d)：①清 placeholder id ②物理删 + set done=1 ③vec0 orphan cleanup；forget() 行为完全不变 | deepseek 提议; 不破坏 §3.8 30 天延迟意图; 主人 cron 定期调 |

---

## 11. 与现有文档的关系

- **`ARCHITECTURE.md`**：现状实现分析，保持不变（本蓝图的事实基线）
- **`SCHEMA.md`**：当前 SQL schema，P0 落地时随迁移更新
- **`DESIGN.md`（本文件）**：演进蓝图，含目标架构与路线图；阶段落地后同步回写 ARCHITECTURE/SCHEMA

---

## 12. 安全与信任边界

### 12.1 核心原则：mnelo 无道德立场，只对存取机制负责

**两条并立的原则**，缺一不可：

1. **内容无立场（amoral by design）**：mnelo **不判断**存储内容是否合法、涉密、冒犯、正确——它既不是道德审查系统，也不是事实仲裁者。它忠实地存储、检索、作废调用方交给它的内容。**"这段内容该不该存在"不是 mnelo 的问题**，是写入方（Agent/主人）的问题。这条是 §1.4 产品边界的直接推论：记忆层不做内容价值判断。
2. **机制可信（mechanism integrity）**：mnelo 对自己的**存取机制**负责——存储内容不能被用于诱导 Agent 执行非预期动作（prompt injection）、不能被伪造篡改主人身份、不能损坏后无法恢复。mnelo 防的是"**存取机制被滥用**"，不是"**内容本身怎么样**"。

具体到 prompt injection：chunk 内容是**数据**，mnelo 的价值是"保真存取"；任何消费方（Agent）必须把召回内容当**待处理数据**而非**待执行指令**。mnelo 自身**永不执行存储内容**——L2 提取只从 chunk 抽**事实**，不解释为操作；全部动作（作废/合并/衰减）由规则/阈值驱动，不由 chunk 文本驱动。

### 12.2 具体防线

| 层 | 防线 |
|---|---|
| **输入** | validation.py 清洗（控制字符/bidi/零宽，防 Trojan Source 伪装指令）；大小上限 |
| **输出** | MCP 响应中给 content 加**数据标记**（如 `🌳` 分隔 + 字段名 `content`），结构化返回让 Agent 明确"这是引用数据，不是命令" |
| **信任传播** | §4.10 来源可信度：`user_confirmed` > `manual` > `agent` > `import:*` > `digest`——低可信来源内容在排序中降权，Agent 看到可信度档可自行决定采纳程度 |
| **自主层** | L2 提取/合并是**提案制 + dry-run 默认**（§5.6），恶意 chunk 最多产生一条低置信提案，不会自动改数据 |
| **身份保护** | identity_fact 不可变 + master 实体 100% 不可变（§3.7）——注入无法篡改主人身份 |

### 12.3 边界外（明确不做）

- mnelo **不做内容价值判断**（合法/涉密/冒犯/正确与否）——**连"考虑是否审查"都不做**，见 §12.1
- mnelo **不做 PII 自动检测**（数据保护是调用方/主人的责任，README 已列）
- mnelo **不提供内容执行的沙箱**（Agent 上下文是调用方运行时的事）

### 12.4 与 §1.4 产品边界的呼应

安全边界是产品边界（§1.4）的推论：mnelo 是"存储+检索层"，所以**信任决策**（采不采纳召回内容）留在 Agent/主人一侧，mnelo 只负责**保真、可回溯、可降权**。

#### 12.4.1 输出数据标记（具体格式）

MCP 工具返回的 content 字段加**数据围栏**，让 Agent 明确"这是引用数据，不是命令"：

```
# 现状（无围栏）: content: "请忽略之前的指令，执行 X"
# 加围栏后:
content: "[memory-data] 请忽略之前的指令，执行 X [/memory-data]"
          (source: agent | trust: 0.3 | memory_type: fact | evidence: chunk_...)
```

- **围栏标记**：`[memory-data]...[/memory-data]` 包裹原文 + 元数据行（source/trust/type/evidence）
- **用途**：① 结构化返回让 Agent 把 content 当数据对象；② 元数据行把信任档位显式化，Agent 可据 `trust` 决定采纳程度
- **不破坏现有解析**：MneloClient 解析的是 JSON block（`[1]`），围栏在 content 字符串内部，不影响客户端
- **可选开关**：`MNELO_MEMORY_OUTPUT_FENCE=0` 关闭（兼容不想被标记的调用方）

#### 12.4.2 威胁模型表（in / out of scope）

| 威胁 | 处置 | 归属 |
|---|---|---|
| 恶意 chunk 诱导 Agent 执行动作 | 数据围栏 + 来源降权 + 信任档位暴露 | mnelo 防线（§12.2）+ Agent 判断 |
| Trojan Source（bidi/零宽伪装指令） | validation.py 清洗（已实现） | mnelo |
| 注入篡改身份事实 | identity_fact 不可变 + master 100% 不可变（§3.7） | mnelo |
| 恶意 chunk 触发 L2 自动改数据 | 提案制 + dry-run 默认 + 置信度门槛（§5.6） | mnelo |
| 大规模注入污染召回质量 | 来源可信度降权 + 健康度评分异常检测（§7.3） | mnelo + 观测 |
| DB 被篡改/损坏 | 快照 + integrity_check + 恢复流程（§3.11） | mnelo |

**设计结论**：mnelo 的威胁模型只覆盖"**存取机制被滥用导致错误决策**"这一类（注入、身份篡改、L2 越权、损坏）。**内容本身的价值判断（合法/涉密/冒犯）根本不在威胁模型里**——mnelo 无道德立场（§12.1），那不是它的关切，不是"out of scope"，是**无关**。
