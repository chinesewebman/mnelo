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

轻量化 AI agent 记忆系统。**4 个维度的记忆**：向量语义、知识图谱、元数据全文、实体身份——任何决策都能回溯到产生它的全部条件。本地优先 SQLite + 4 路 RRF 召回，零云端、零锁定。

**为什么是 4 路召回**：每条通道都补上别的通道漏掉的东西——向量漏字面（股票代码、ticker），元数据漏语义改写，图漏没挂实体的孤立 chunk，实体漏长篇散文。四路并发跑（WAL 模式下并发读，p50 = **12.5 ms** / p95 = **36 ms**），再用 RRF 按排名融合——不用做任何分数归一化，就能拿到高召回率，省掉各通道阈值调参。数学细节看 [🔀 什么是 RRF？](#-什么是-rrf)，延迟数字看 [⚡ 一瞥](#-一瞥)。

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

### 🔀 什么是 RRF？

**RRF = Reciprocal Rank Fusion**（[Cormack 等，2009](https://dl.acm.org/doi/10.1145/1571941.1572114)）。在融合异构搜索通道时，最朴素的方案反而吊打加权打分调参。

核心思路：每个通道独立排序，再把 `1 / (k + rank_i)` 跨通道累加，`k=60` 是标准阻尼常数。

```
通道 A（向量）:   doc1=1, doc2=3, doc5=2
通道 B（图）:     doc2=1, doc1=2, doc7=3
通道 C（meta）:   doc5=1, doc1=3, doc9=2

最终分数 = Σ_通道 1 / (60 + 该通道内的排名)
→ doc1: 1/61 + 1/62 + 1/63 = 0.0483   ← 胜
→ doc2: 1/63 + 1/61       = 0.0321
→ doc5: 1/62 + 1/61       = 0.0321
```

**为什么用 RRF 而不是加权打分？**

|                          | RRF                                          | 加权打分融合                                |
| ------------------------ | -------------------------------------------- | ------------------------------------------- |
| 需要分数归一化？         | **不需要** — 只看排名                        | 需要（每个通道的分数尺度必须校准）          |
| 某个通道抽风时是否鲁棒？  | **鲁棒** — 异常排名只贡献 `1/(60+rank)`      | 不鲁棒 — 单通道分数畸变即可主导整体         |
| 新增一个通道？            | 直接加进去                                   | 全部权重要重新调                            |
| 实现成本                 | ~5 行                                        | 分数校准 + 权重网格搜索                     |

mnelo 用教科书写法的 `k=60`，覆盖 4 个通道（vector / graph / meta / entity），再加上一个小小的 `0.05/sqrt(rank)` boost 用于股票代码命中——这是唯一的领域定制。除此之外全是 RRF 原版。

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
当 query 匹配股票代码（如 `sh600089`），entity hit 在 RRF 融合时获得 `0.05/sqrt(rank)` boost。****：查 "sh600089" 时股票实体永远浮顶。

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
| **avg** | 34.4 ms | 24h warm-path 平均 |
| **max** | 2980 ms | 冷启动后首次 recall（embedder warm-up） |
| **冷启动** | ~1.1 s | MCP server 启动 + embedder 模型加载 |

### 内存占用

macOS M-series（Apple Silicon）实测：单个 MCP server 进程，空闲状态：

| 组件 | RAM | 数据来源 |
|---|---|---|
| MCP server 进程（Python + mcp + SQLite + onnxruntime + embedder + bge 模型） | **~270 MB** | `ps -o rss` 实测 |
| └─ Embedder（bge-small-zh，权重 + onnx session + tokenizer + pooling） | 上面约 ~200 MB | 由 baseline 推算 |
| └─ Python 解释器 + mcp server + sqlite-vec + chunked buffer | 上面约 ~70 MB | 由 baseline 推算 |
| `memory.db` 的 OS page cache | OS 管理，macOS 上几乎免费 | — |
| **实用 RSS 总计** | **~270 MB** | 已验证 |

#### 为什么 embedder 的 RAM 占用是文件大小的 ~3 倍

bge-small-zh-v1.5 模型文件在磁盘上是 **92 MB**（`model.safetensors` 本身 **91.4 MB**；完整 snapshot 在 `~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/` 是 **92 MB**，含 tokenizer + config）。但 embedder 常驻大约 **200 MB**。这 ~110 MB 的差额是**运行时开销**，不是模型本身：

| 来源 | 大约 RAM | 说明 |
|---|---|---|
| `model.safetensors` 以 float32 加载 | ~120 MB | BGE 6 层 transformer + token embedding，权重在 RAM 里是 float32，所以比磁盘上的 .safetensors 文件大约翻倍 |
| onnxruntime session workspace | ~40-60 MB | 前向计算时给中间 tensor 预分配的内存池 |
| Tokenizer（Fast + 词表） | ~10-15 MB | XLM-R 中文 tokenizer 表，一次加载后常驻内存 |
| sentence-transformers pooling module | ~5-10 MB | Mean-pooling 包装层 + 它的 config |

**核心结论**：文件大小 ≠ RAM 成本。92 MB 的下载展开到运行时 ~200 MB；270 MB 进程 RSS 里剩下的 ~70 MB 是其他所有东西（Python + mcp + SQLite + sqlite-vec）。这个常量**不随**你存多少 chunk 增长。

### 📁 模型文件落到哪里？

fastembed 用 **HuggingFace Hub cache**，不是 mnelo 私有目录：

```
~/.cache/huggingface/hub/
└── models--BAAI--bge-small-zh-v1.5/       # 磁盘上 92 MB
    ├── blobs/                                # 真实文件（按 SHA 去重）
    │   ├── 354763...d61d5a026  ← model.safetensors (91.4 MB)
    │   ├── cdb3043...8f88747   ← tokenizer.json (429 KB)
    │   └── ...
    ├── snapshots/                             # 符号链接 → blobs，友好命名
    └── refs/main                              # 当前 commit hash
```

**自动下载**：首次调用 `mcp_server.py` / `scripts/init_db.py` / `scripts/health_check.py` 时自动拉，无需手工操作。典型下载 ~90s（取决于网速）。

**手工预下载**（离线安装、CI、air-gapped 环境）：

```bash
# 方案 A：huggingface-cli（官方）
pip install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-small-zh-v1.5 \
  --local-dir ~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/

# 方案 B：hf（新版 CLI）
hf download BAAI/bge-small-zh-v1.5 \
  --local-dir ~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/

# 然后指给 mnelo（可选，默认已经指对了）：
export HF_HOME=/path/to/your/cache
python3 scripts/init_db.py
```

**搬迁 cache**（比如 `/home` 只读、sandbox 环境）：

```bash
# 二选一 — 都行，HUGGINGFACE_HUB_CACHE 更具体
export HF_HOME=/srv/cache/huggingface
# 或
export HUGGINGFACE_HUB_CACHE=/srv/cache/huggingface/hub
```

环境变量必须在 **MCP server 启动前**设好（如果用 launchd 守护，在 plist 的 `EnvironmentVariables` 里加）。

**跟其他工具共享模型**：任何用 fastembed / sentence-transformers / HuggingFace transformers 的工具都会在默认路径找到同一份缓存，不会重复下载。

### 🌐 多语种模型

默认的 `bge-small-zh-v1.5` 是**中文原生**（C-MTEB 强），对英文和 100+ 其他语言也凑合能用——非中文文本质量会下降。如果工作负载偏另一种语言，通过 `config.toml` 切换 embedder：

```toml
[embedder]
model = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
dim = 384
```

或者用环境变量（不改 config）：

```bash
export HERMES_MEMORY_EMBEDDER_MODEL='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
export HERMES_MEMORY_EMBEDDER_DIM=384
launchctl kickstart -k gui/$(id -u)/ai.mnelo.mcp
```

**推荐模型矩阵**（都在 [fastembed-supported list](https://qdrant.github.io/fastembed/examples/Supported_Models/) 上，license 全是 MIT 或 Apache-2.0）：

| 用途 | 模型 | dim | 磁盘大小 | License |
|---|---|---|---|---|
| 中文（默认）| [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | 512 | 92 MB | MIT |
| 英文 | [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | 384 | 67 MB | MIT |
| **多语种（50+ 语种：日本語 / 한국어 / Español / Français / Deutsch / 中文 / English / …）** | [paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | 384 | 220 MB | Apache-2.0 |

**为什么是这 3 个？**

- **bge-small-zh-v1.5** —— 中文最佳（C-MTEB top-3，90 MB），英文也能凑合
- **bge-small-en-v1.5** —— 纯英文最佳，67 MB；如果根本不存中文就选它
- **paraphrase-multilingual-MiniLM-L12-v2** —— 一个模型覆盖 50+ 语种（含最常被问到的日本語 / 한국어 / Español / Français / Deutsch / 中文 / English），220 MB。在 fastembed 支持的模型里 MTEB multilingual-retrieval benchmark top-3。**语料混合或想一个模型走天下的场景，就选它。**

⚠️ **切换模型必须重新初始化数据库** —— `sqlite-vec` schema 把 `dim` 烤进表定义了。老的 embedding 跟新 dim 不兼容。

```bash
# 改完 config.toml 后:
rm ~/.hermes/memory/memory.db        # 有重要数据先备份
python3 scripts/init_db.py
launchctl kickstart -k gui/$(id -u)/ai.mnelo.mcp
```

### 测试覆盖

```
$ python3 -m pytest tests/ -q
........................................................................ [ 84%]
......................................................................   [100%]
450 passed, 1 skipped in ~16s
```

当前覆盖率（以 REPO 为准）：

| 模块 | 覆盖率 |
|---|---|
| `auth.py` | **100%** |
| `validation.py` | 99% |
| `mcp_server.py` | 98% |
| `memory.py` | 93% |
| `config.py` | 92% |
| `embedder.py` / `entity_resolve.py` | 85% |
| `mnelo_locale.py` / `i18n_messages.py` | 100% |

---

## 🔄 Repo ↔ live sync（post-commit hook）

mnelo 每份 `.py` / `.sql` 文件都有两份：

| 路径 | 角色 |
|---|---|
| `~/projects/mnelo/` | 单一真相源（git HEAD）|
| `~/.hermes/memory/` | 真正在 8086 端口跑的 MCP server |

没有 sync 机制，测试会跑 live 版 `memory.py` 但断言写的是 repo 版——false positive / false negative 一堆。repo 自带 **post-commit hook**，每次 commit 自动把改动文件同步到 live：

```bash
# 一次性安装（clone 后做一次）：
cd ~/projects/mnelo
git config core.hooksPath .githooks
```

每次 commit 它做的事：

- 比对 HEAD~1..HEAD，挑出 `.py` / `.sql` / `.sh` 文件
- 路径映射：`scripts/init_db.py` → `~/.hermes/memory/scripts/init_db.py`；`api/*.py` → `~/.hermes/memory/api/`；顶层 → `~/.hermes/memory/`
- **先备份 live 旧版**到 `~/.hermes/memory/.sync-backups/<时间戳>-<sha>/`（保留最近 5 份）
- 原子 `mv` 覆盖——半截写入也不会损坏 live
- 同步后跑 `scripts/health_check.py` 早发现回归
- 如果改了 `memory.py` / `embedder.py` / `config.py`，打印一次性重启提示（Python import 缓存失效）

**跳过某次 commit 的 sync**：commit message 末尾加 `[skip-sync]`。

**这一次不想要 sync**：`git commit -m "docs: ... [skip-sync]"`。

**不会触碰**（设计如此）：

- `memory.db` —— 你的数据，绝不自动改
- `config.toml` —— 可能有个人手动配置，不被覆盖
- `*.md` —— 文档不需要进 live
- `tests/` —— 测试不在 live 跑

**同步后重启**（改了 `memory.py` / `embedder.py` / `config.py` 时）：

```bash
launchctl kickstart -k gui/$(id -u)/ai.mnelo.mcp
```

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
│   └── mnelo_client.py             ← MneloClient（SSE 客户端，7 工具）
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
# 克隆
git clone https://github.com/chinesewebman/mnelo.git
cd mnelo

# 2a. 一键安装（推荐）—— 自动处理 venv、pip、init_db、plist、auth token
bash scripts/install.sh

# 2b. 或手动逐步：
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 初始化 db
python3 scripts/init_db.py

# 4. 启动 MCP server
launchctl load ~/Library/LaunchAgents/ai.mnelo.mcp.plist
# (或: HERMES_MEMORY_SERVER_PORT=8086 python3 mcp_server.py --transport sse)

# 5. Python 使用
python3 -c "
import sys; sys.path.insert(0, 'api')
from mnelo_client import MneloClient
c = MneloClient()
c.remember('sh600089 建仓 12000 @ 18.96', source='trading', importance=0.9)
for h in c.recall('sh600089 建仓', top_k=5):
    print(h['method'], h['chunk_id'], h['content'][:60])
"
```

`scripts/install.sh` **幂等**——升级后重跑安全。也支持 `LIVE_ROOT=~/.mnelo bash scripts/install.sh` 装到非默认路径。

中文 locale：

```bash
export HERMES_MEMORY_LANG=zh
python3 scripts/health_check.py
# mnelo daily check — 2026-07-18 18:30:00
# ✅ MCP server alive — PID 19408, 启动 1.5h
# 📈 Recall 24h — 1530 次, 空 hits 81 (5%), latency p50=12.5ms p95=36.2ms
```

完整部署运维 → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

### 🤖 一句话让 agent 装

跳过上面所有步骤——把下面这条**直接复制粘贴**给任意 AI agent（Hermes / Claude / Cursor / Codex 等）即可：

> **请从 https://github.com/chinesewebman/mnelo 帮我安装并启动 mnelo：克隆仓库、建 venv、跑 `scripts/init_db.py`、把 MCP server 启动到 8086 端口，最后跑 `scripts/health_check.py` 验证。看到日志里出现 `🟢 MCP server ready` 再回我。**

agent 会自动处理 venv 创建、`pip install -r requirements.txt`、plist 安装、健康探针。典型安装耗时 ~90s（bge-small-zh 模型下载是慢的那段，`~/.cache/huggingface/hub/` 下的 `model.safetensors` + tokenizer + config 共 **92 MB**）。

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
6. **可测量**。Benchmark 段所有数字都能从 cite 的来源复现。

---

## 🚧 已知局限

| 局限 | 依据 | workaround |
|---|---|---|
| **~50 万向量** @ 512 维，单 MacBook | [`sqlite-vec` v0.1 实测](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html#benchmarks)：vec0 在 1M × 128 维（sift1m）返回 33 ms，在 500K × 960 维（gist1m）< 100 ms。延迟按 `dim × log(n)` 缩放。512 维下 ~50 万向量可维持在 [100 ms 响应目标](https://developer.mozilla.org/en-US/docs/Web/Performance/How_long_is_too_long#responsiveness_goal) 内 | >1M 向量时换 HNSW 后端的 Qdrant / Milvus |
| 单用户（无多租户） | 单 SQLite 文件，无行级隔离 | 不要把 8086 端口暴露到内网 |
| 无 PII 自动检测 | 未实现（P1-5） | 不要存密码 / token / 信用卡 |
| bge-small-zh 是中文优化（英文也能用但次优） | C-MTEB 评测排名 | 英文为主时换 [bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) |

### 阈值的依据

阈值来自：

- **sqlite-vec v0.1.0 作者实测**（Alex Garcia, 2024 年 8 月）：<https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html#benchmarks>
- **alexgarcia 自己的 caveat**："sqlite-vec 的极限在 1M 向量时才显现"（高维下：192 维 → 192 ms，3072 维 → 8.5 秒）
- **MDN 100 ms 响应目标** 作延迟预算

### RAM 不是瓶颈

vec0 存储是 chunked 设计，默认不是 in-memory。Alex Garcia 在 vec0 设计讨论里（[Reddit r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1ehlazq/introducing_sqlitevec_v010_a_vector_search/)）说：

> "The vec0 virtual table stores vectors in **chunks** and reads those chunks one-by-one to perform KNN, so **not the entire dataset is fit into memory**."

对 mnelo 意味着：

| 组件 | 4487 vectors 时 | 1M vectors 时 |
|---|---|---|
| **vec0 chunked storage** | ~9 MB（4487 × 2 KB，落在 1 个 8192-row chunk 里） | ~2 GB 跨 122 个 chunk，但每次 query 实际只 hot 1 个 chunk |
| **SQLite page cache**（`PRAGMA cache_size`） | 64 MB（启动时设置） | 同默认；按 working set 调整 |
| **memory.db 的 OS page cache** | OS 管理，macOS 上几乎免费 | OS 管理，内存压力下会 evict |
| **Embedder**（bge-small-zh，固定的 RAM 开销） | ~200 MB | ~200 MB（常数；约 3 倍于 92 MB 文件大小，源自 float32 加载 + onnxruntime workspace + tokenizer — 见 [内存占用](#内存占用)） |

**单 MacBook 的真正瓶颈**：

1. **冷 chunk 的磁盘随机读延迟** — OS page cache 缓解，但首次访问冷 chunk 需 ~1 次 SSD seek（~100 µs）
2. **SQLite `cache_size`** — 启动时设为 `-64000`（64 MB），让 working-set 放下，不再每次从 OS page cache 重读
3. **Embedder RAM**（~200 MB）— **不随数据规模增长**，是唯一的固定 RAM 开销（约为 92 MB 文件大小的 3 倍 — 见 [内存占用](#内存占用)）

一句话：**vec0 设计就是 disk-first，不是 RAM-resident**。超过 1M vectors 后，约束是 disk-IOPS 预算，不是 RAM。只有需要（a）ANN 在 >10M vectors 时 sub-10 ms 延迟，或（b）分布式分片，才换 HNSW 后端的 Qdrant/Milvus。

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
# 预期: 450 passed, 1 skipped in ~16s
```

---

## 📜 许可

MIT. 见 [`LICENSE`](LICENSE)。

---

## 🙏 致谢

- [sqlite-vec](https://github.com/asg017/sqlite-vec) — 向量扩展
- [fastembed](https://qdrant.github.io/fastembed) — embedder wrapper
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) — 中文 embedding 模型（默认，512 维）
- [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) — 英文 embedding 模型（工作负载以英文为主时替换）
- [MCP](https://modelcontextprotocol.io) — 协议规范
- [Hermes Agent](https://nousresearch.com/hermes) — 主集成目标

---

> Hermes = 众神信使。
> mnelo = 他的记忆层。
>
> 拍板 2026-07-18 by [chinesewebman](https://github.com/chinesewebman) + [Hermes Agent](https://nousresearch.com/hermes)。