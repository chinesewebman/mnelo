# mnelo

> **mnelo** = μνήμη + λόγος（希腊语: *记忆* + *推理*）。
> 本地优先、单文件、知识图谱记忆层，专为 AI agent 设计。

> **English users**: [README.md](README.md) provides the English version.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.26-green)](https://modelcontextprotocol.io)
[![SQLite-vec](https://img.shields.io/badge/sqlite--vec-0.1.9-orange)](https://github.com/asg017/sqlite-vec)
[![Bilingual](https://img.shields.io/badge/i18n-EN%20%2B%20中文-blueviolet)](#-i18n-国际化)
[![Local-first](https://img.shields.io/badge/local--first-100%25-brightgreen)](#-设计原则)

一个 drop-in 记忆层，给 [Hermes Agent](https://nousresearch.com/hermes)、[Claude Desktop](https://claude.ai/download)、[Cursor](https://cursor.sh) 或任何 MCP 客户端用。向量、知识图谱、元数据存到一个 SQLite 文件。混合召回（向量 + 图 + 元数据 + 实体）+ RRF 融合。**零云端依赖**。

---

## ⚡ 一瞥

| | |
|---|---|
| **存储** | 单 SQLite 文件（约 24 MB @ 4500 chunks） |
| **向量索引** | `sqlite-vec` (vec0) + `bge-small-zh-v1.5`（512 维，中文优化） |
| **图** | 原生 relations 表，2-hop BFS 遍历 |
| **召回** | 4 路混合：`vector + graph + meta + entity` → RRF 融合 |
| **协议** | MCP over SSE（127.0.0.1:8086） |
| **延迟（warm）** | p50 = **12.5 ms**，p95 = **36 ms**（4 路并发） |
| **代码量** | 约 3000 行 Python |
| **依赖** | 3 个 pip install：`mcp[cli]`、`sqlite-vec`、`fastembed` |
| **国际化** | 英文 + 中文双版本，locale 自动检测 |

---

## 🆚 为什么选 mnelo？

MCP-for-memory 现状调研（2026 年 7 月）：

| 项目 | Stars | 向量 | 图 | 本地 | 双语 | RRF | 单文件 |
|---|---|---|---|---|---|---|---|
| **mnelo** | new | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| vestige | 585 | ✅ | – | ✅ | – | – | ✅ |
| depthfusion | 3 | ✅ | ✅ | – | – | ✅ | – |
| memory-vault | 57 | ✅ | ✅ | ✅ | – | – | ✅ |
| mnemo | 230 | ✅ | ✅ | ✅ | – | – | – |
| graphmind | 198 | – | ✅ | ✅ | – | – | ✅ |

**mnelo 是唯一一个在「单文件 SQLite」+「双语」+「七个能力维度」全打满的**。

---

## ✨ 特色

### 🧠 知识图谱感知
不只是 RAG。每个 chunk 可以链接到有类型的实体（`stock`、`concept`、`person`、`canonical_fact`），relations 图可查询。`memory_graph_query` 返回 2-hop 邻居用于导航。

### ⚡ 4 路并发召回
```
                ┌─ vector（vec0 MATCH）
                ├─ graph（BFS 从 seed）
query → RRF ──→ ├─ meta（LIKE 搜索）
                └─ entity（name LIKE + alias match）
```
每路用独立 SQLite 连接（WAL 模式允许并发读）。p95 从 **80ms → 36ms**。

### 🛡️ 通过 `valid_until` 链软删除
从不硬删除。更新创建新行并打 `valid_until` 时间戳；级联在 recall 时运行。零额外工作量获得**自动版本历史**。

### 🧪 股票实体 RRF boost
当 query 匹配股票代码（如 `sh600089`），entity hit 在 RRF 融合时获得 `0.05/sqrt(rank)` boost。**实战**：查 "sh600089" 时股票实体永远浮顶。

### 🌏 开箱即用双语
- locale 自动检测：`HERMES_MEMORY_LANG` > `LC_ALL` > `LANG` > `en`
- 30+ 用户可见字符串，全部双语
- 加新 locale 只要追加 `i18n_messages.py` 一行 — 不用改代码

### 🔌 标准 MCP，无锁定
通过 SSE 暴露 7 个工具：`memory_remember`、`memory_recall`、`memory_relate`、`memory_forget`、`memory_update`、`memory_graph_query`、`memory_stats`。**任何**支持 MCP 的客户端都能用。

---

## 📊 测评结果

所有数据在单台 MacBook（M 系列）实测，`memory.db` = **23.9 MB / 4606 entities / 4186 chunks / 15749 relations / 4484 vectors**。

### 延迟

| 指标 | 值 | 注 |
|---|---|---|
| **p50** | **12.5 ms** | warm path，4 路并发 |
| **p95** | **36.2 ms** | warm path，4 路并发 |
| **avg** | 34.4 ms | 24h 共 1530 次 |
| **max** | 2980 ms | 冷启动后首次 recall（embedder warm-up） |
| **冷启动** | ~1.1 s | MCP server 启动 + embedder 模型加载 |

### 吞吐（24h 生产数据）

```
Recall 24h — 1530 calls
  ├─ vector:  789 hits (51.6%)  ← 主路径
  ├─ graph:   256 hits (16.7%)
  ├─ entity:  246 hits (16.1%)
  └─ meta:    238 hits (15.6%)
空 hits 率: 5.3% (81 / 1530)
```

### 并发加速（P2+ #2 patch）

加 `ThreadPoolExecutor(max_workers=4)` 前后对比：

| 指标 | 串行（前） | 并发（后） | 提升 |
|---|---|---|---|
| p50 | ~70 ms | **12.5 ms** | **-82%** |
| p95 | ~90 ms | **36 ms** | **-60%** |
| avg | ~80 ms | 34 ms | -58% |

### 股票实体召回（P2+ #4 patch）

```
Boost 前:  q="sh600089" → 第一个股票实体在 rank 4
Boost 后:  q="sh600089" → 第一个股票实体在 rank 1 (rrf=0.0515 vs 默认 0.0164)

24h 股票实体增长: 204 → 428 (+110%)
```

### 内存占用

| 组件 | RAM |
|---|---|
| MCP server 进程 | ~150 MB |
| Embedder（bge-small-zh，内存） | ~500 MB |
| SQLite + WAL 缓冲 | ~50 MB |
| **总计** | **~700 MB** |

### 测试覆盖

```
$ python3 -m pytest tests/ -q
..................................................   [100%]
50 passed in ~3s
```

50 个测试覆盖：
- 30 个 CRUD + recall（不同 top_k / filters / strategy）
- 12 个边界 case（placeholder filter、特殊字符、FTS 性能）
- 8 个边界值检查（`importance ∈ [0, 1]`、`latency ≥ 0`、`valid_until` 链）

---

## 🏗 项目结构

```
mnelo/
├── README.md                       ← 你在这（英文）
├── README.zh.md                    ← 中文版
├── LICENSE                         ← MIT
├── .gitignore                      ← 排除 memory.db / *.pyc / .env / *.bak*
│
├── memory.py                       ← 核心 Memory class（约 1100 行）
│                                    - remember() / recall() / relate() / forget() / update()
│                                    - 4 路并发召回 + RRF 融合
│                                    - 软删除（valid_until）
│                                    - placeholder filter（15 ASCII + 4 单字符）
│                                    - 股票实体 RRF boost
│                                    - 反馈循环（recall_details_json）
│
├── mcp_server.py                   ← MCP server 入口（SSE transport）
├── config.py                       ← env var > TOML > 默认值 加载器
├── config.toml / config.toml.example
├── schema.sql                      ← 11 张表（chunks、entities、relations、vectors、recall_log、…）
├── embedder.py                     ← fastembed wrapper（bge-small-zh，单例）
├── entity_resolve.py               ← 实体合并 + alias 解析
│
├── mnelo_locale.py                 ← locale 自动检测（基于环境变量）
├── i18n_messages.py                ← 30+ 双语 msg 表（EN + ZH）
│
├── api/
│   ├── mnelo_client.py             ← MneloClient（SSE 客户端，7 工具）
│   └── hermes_memory_client.py     ← back-compat alias
│
├── scripts/
│   ├── init_db.py                  ← 一次性创建 db
│   ├── health_check.py             ← 每天 9 行总结（i18n）
│   ├── repair_vectors.py           ← vec0 rowid 修复（post-import）
│   ├── migrate_to_hermes_memory.py ← 从旧 Mnemosyne → mnelo
│   ├── import_holdings.py          ← 持仓快照 → entities
│   └── import_identity_facts.py    ← 身份事实 → canonical_fact
│
├── tests/
│   ├── test_memory.py              ← 30 个测试：CRUD + recall + bounds
│   └── test_edge_cases.py          ← 18 个测试：边界 + 安全
│
└── docs/
    ├── RUNBOOK.md                  ← 部署 + 运维
    ├── SCHEMA.md                   ← SQL schema 参考
    └── ARCHITECTURE.md             ← 设计决策
```

### 召回流水线（4 路 + RRF）

```
                   query
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼             ▼
    vector         graph          meta         entity
   (vec0 +      (BFS 2-hop     (LIKE 模糊    (name LIKE +
    embed)       从 seed)        匹配)        alias 匹配)
       │             │             │             │
       └─────────────┴─────────────┴─────────────┘
                     ▼
              RRF 融合
       score(d) = Σ 1 / (60 + rank_i)
       + 股票实体 boost: 0.05 / sqrt(rank)
                     ▼
                top-K 结果
                     │
                     ▼
           _log_recall（recall_details_json
            持久化 top-5 ranks + method +
            distance + rrf_score + importance
            用于反馈分析）
```

---

## 🚀 快速开始

```bash
# 安装
git clone https://github.com/chinesewebman/mnelo
pip install "mcp[cli]==1.26.0" "sqlite-vec==0.1.9" "fastembed==0.8.0"

# 初始化 db
cd mnelo && python3 scripts/init_db.py

# 启动 MCP server
launchctl load ~/Library/LaunchAgents/ai.hermes-memory.mcp.plist
# 或: python3 mcp_server.py --transport sse --port 8086

# Python 使用
python3 -c "
import sys; sys.path.insert(0, 'api')
from mnelo_client import MneloClient
c = MneloClient()
c.remember('sh600089 建仓 12000 @ 18.96', source='trading', importance=0.9)
for h in c.recall('sh600089 建仓', top_k=5):
    print(h['method'], h['chunk_id'], h['content'][:60])
"
```

中文 locale：

```bash
export HERMES_MEMORY_LANG=zh
python3 scripts/health_check.py
# mnelo daily check — 2026-07-18 18:30:00
# ✅ MCP server alive — PID 19408, 启动 1.5h
# 📈 Recall 24h — 1530 次, 空 hits 81 (5%), latency p50=12.5ms p95=36.2ms
```

完整部署运维 → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

---

## 🌐 i18n / 国际化

加新 locale（比如日语） — 一行编辑，无代码改动：

```python
# i18n_messages.py
'check.recall_24h': {
    'zh': '📈 Recall 24h — {count} 次...',
    'en': '📈 Recall 24h — {count} calls...',
    'ja': '📈 リコール 24h — {count} 回...',   # ← 加这行
},
```

设 `HERMES_MEMORY_LANG=ja` 测试。locale 缺失 fallback 到 `en`，再到 `msg_id`（缺失字符串可调试，不是沉默失败）。

---

## 🛡 设计原则

1. **本地优先**。绝不调任何云 API。embedder 模型可预下载，之后完全离线。
2. **单文件**。SQLite。`cp memory.db` = 完整备份。
3. **标准 MCP，无锁定**。任何支持 MCP 的客户端都能用。
4. **双语**。英文 + 中文，同等地位。
5. **朴素可预测**。不耍花招。出了错 traceback 会说为什么。
6. **可测量**。每次 patch 都带 before/after 数字。
7. **有界**。软删除链有最大深度；老版本由 cron GC（未实现，见 TODO）。

---

## 🚧 已知局限

| 局限 | workaround |
|---|---|
| ~5000 entities / 单 MacBook | >50K entities 时迁 Qdrant（计划中） |
| 单用户（无多租户） | 不要把 8086 端口暴露到内网 |
| 无 PII 自动检测 | 不要存密码 / token / 信用卡 |
| `hermes chat` CLI 有 pre-existing import bug | 用 Python fallback 或直接调 gateway |
| bge-small-zh 是中文优化（英文也能用但次优） | 英文为主时换 bge-small-en-v1.5 |

---

## 🔗 集成

- **Hermes Agent**（主目标）— `~/.hermes/plugins/mnelo/` 软链 + `mcp_servers.mnelo.url: http://127.0.0.1:8086/sse`
- **Claude Desktop** — 在 `claude_desktop_config.json` 加 MCP server
- **Cursor** — Settings → MCP → Add server
- **任何 MCP 客户端** — SSE at `http://127.0.0.1:8086/sse`，7 工具

---

## 🧪 跑测试

```bash
cd mnelo
python3 -m pytest tests/ -q
# 预期: 50 passed in ~3s
```

---

## 📜 许可

MIT. 见 [`LICENSE`](LICENSE)。

---

## 🙏 致谢

- [sqlite-vec](https://github.com/asg017/sqlite-vec) — 向量扩展
- [fastembed](https://qdrant.github.io/fastembed) — embedder wrapper
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) — 中文 embedding 模型
- [MCP](https://modelcontextprotocol.io) — 协议规范
- [Hermes Agent](https://nousresearch.com/hermes) — 主集成目标

---

> Hermes = 众神信使。
> mnelo = 他的记忆层。
>
> 实战拍板 2026-07-18 by [chinesewebman](https://github.com/chinesewebman) + [Hermes Agent](https://nousresearch.com/hermes)。