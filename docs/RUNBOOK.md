# mnelo 部署运行手册

> **mnelo** = μνήμη + λόγος (希腊语 记忆 + 推理). 5-字符缩写, 跟 Hermes Agent 平行 — Hermes 是 messenger, mnelo 是他的 memory layer.

部署约 5 分钟。**单 SQLite 文件、macOS launchd 守护, MCP 协议 via SSE**.

---

## 1. 系统要求

| 项 | 最小 | 推荐 |
|---|---|---|
| Python | 3.9+ | 3.11+ (实测) |
| OS | macOS / Linux | macOS 14+ (launchd 守护) |
| 磁盘 | 100 MB | 1 GB (留 growth 余量) |
| 内存 | 4 GB | 8 GB (bge-small-zh embedder ~200 MB RSS + Python + mcp + SQLite overhead，总进程 RSS 实测 ~270 MB) |
| 网络 | 仅首次拉模型 | 同上 |

### 已实测依赖版本 (本仓库写于 2026-07-18)

```
python        3.11.12
sqlite-vec    0.1.9
mcp           1.26.0
fastembed     0.8.0
sqlite3       built-in (≥3.35)
```

---

## 2. 安装

```bash
# 1. 创建 venv (推荐, 跟 hermes-agent 共享)
python3 -m venv ~/hermes-agent/venv
source ~/hermes-agent/venv/bin/activate

# 2. 安装依赖 (用 requirements.txt 列出真正需要的)
pip install -r requirements.txt
```

> ⚠️ **不要**装 `sentence-transformers` — 我们故意用 fastembed，体积小 ~10 倍、快 ~3 倍。

### 2.1 拉模型 (bge-small-zh)

首次运行会自动从 HuggingFace 拉 `BAAI/bge-small-zh-v1.5` (~90 MB)。要预拉可：

```bash
python3 -c "from fastembed import TextEmbedding; t = TextEmbedding('BAAI/bge-small-zh-v1.5'); print('ok')"
```

模型缓存在 `~/.cache/huggingface/`，下次启动秒级 warm-up。

---

## 3. 文件布局

```
~/.hermes/memory/
├── memory.py                 # 核心 Memory class (CRU + 4 路召回)
├── mcp_server.py             # MCP server entry (SSE)
├── config.py                 # 配置加载 (env + toml)
├── config.toml               # 默认配置
├── config.toml.example       # 配置样例
├── schema.sql                # 11 tables SQLite schema
├── embedder.py               # fastembed wrapper (singleton)
├── entity_resolve.py         # entity 合并 + alias 解析
│
├── api/
│   └── mnelo_client.py       # ★ 客户端 (MneloClient class)
│
├── scripts/
│   ├── init_db.py            # 第一次创建 db (跑过一次即可)
│   ├── health_check.py       # 每天自检 (source-of-truth)
│   ├── repair_vectors.py     # 一次性 vec0 rowid 修复 (post-import)
│   └── migrate_to_hermes_memory.py  # 从旧 Mnemosyne 迁移 (可选)
│
├── tests/
│   ├── test_memory.py        # 50 测试覆盖 CRUD/recall/bounds/clamp
│   └── test_edge_cases.py    # 18 边界 + 安全性
│
└── docs/
    ├── SCHEMA.md
    ├── ARCHITECTURE.md
    └── RUNBOOK.md            # ← 你在这文件
```

---

## 4. 初始化数据库 (首次)

```bash
cd ~/.hermes/memory
python3 scripts/init_db.py
# 输出: 初始化 db @ ~/.hermes/memory/memory.db
```

schema 自动建 11 tables: `chunks`, `entities`, `relations`, `vectors` (sqlite-vec), `recall_log`, `purged_queue`, `meta`, `vectors_*` (sqlite-vec 内部)。

---

## 5. 启动 MCP server

### 5.1 launchd 守护 (macOS 推荐)

`~/Library/LaunchAgents/ai.mnelo.mcp.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.mnelo.mcp</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/you/hermes-agent/venv/bin/python3</string>
        <string>/Users/you/.hermes/memory/mcp_server.py</string>
        <string>--transport</string>
        <string>sse</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <!-- 不传 --port: argparse default 走 config.server_port.
             Port 改值: plist 环境变量 MNELO_MEMORY_SERVER_PORT, 或 config.toml [server].port. -->
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MNELO_HOME</key>
        <string>/Users/you/.hermes</string>
        <key>MNELO_MEMORY_SERVER_PORT</key>
        <string>8086</string>
        <key>VIRTUAL_ENV</key>
        <string>/Users/you/hermes-agent/venv</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/mnelo-mcp.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mnelo-mcp.err</string>
</dict>
</plist>
```

加载 + 启动：

```bash
launchctl load ~/Library/LaunchAgents/ai.mnelo.mcp.plist
launchctl kickstart -k "gui/$(id -u)/ai.mnelo.mcp"
# : ~1.1s 启动 (含 Embedder warm-up)
```

**Token 配置**: 不在 plist 里 (plist 是 world-readable XML, 暴露 attack surface)。
Token 走 `~/.config/mnelo/auth_token` (mode 600) 默认; 或手动 `export MNEOLO_AUTH_TOKEN=***` override。

### 5.2 测试 SSE server

```bash
curl -sS http://127.0.0.1:8086/sse -m 1 | head
# 应该收到 MCP SSE handshake
```

---

## 6. 客户端连接 (以 hermes-agent 为例)

### 6.1 用 mnelo_client.py

```python
import sys
sys.path.insert(0, '/Users/you/.hermes/memory/api')

from mnelo_client import MneloClient

client = MneloClient()  # 默认连 127.0.0.1:8086
hits = client.recall("建仓 5symbols", top_k=5, strategy='rrf')
for h in hits:
    print(f'{h["method"]:8s} {h["chunk_id"]}  {h["content"][:80]}')
```

输出示例：
```
entity   entity:sh600089   sh600089 (特变电工)
vector   chunk_20260718_072606_654597    sh600089 建仓 12,000 @ 18.96
graph    chunk_20260718_072606_654252   ...
```

### 6.2 接到 Hermes Agent

Hermes 通过 platform connectors (Telegram / Discord / WeChat) 收发消息, 调到 MCP server via SSE。mnelo 暴露 7 个工具:

| Tool | 用途 |
|---|---|
| `memory_remember` | 写入 chunk + entities + relations + vector |
| `memory_recall` | 4 路召回 + RRF ( p95=36ms) |
| `memory_relate` | 加实体关系 |
| `memory_forget` | 软删除 (cascade=True) |
| `memory_update` | 新版本化 (valid_until 链) |
| `memory_graph_query` | BFS 遍历实体图 |
| `memory_stats` | DB / recall_log / kind 分布 |

接法 — 把 `~/.hermes/memory/api/mnelo_client.py` 软链到 hermes-agent 的 plugin 目录:

```bash
mkdir -p ~/.hermes/plugins/mnelo
ln -sf ~/.hermes/memory/api/mnelo_client.py \
       ~/.hermes/plugins/mnelo/__init__.py
```

然后在 `~/.hermes/config.yaml` 加:

```yaml
mcp_servers:
  mnelo:
    transport: sse
    url: http://127.0.0.1:8086/sse
    enabled_tools:
      - memory_recall
      - memory_remember
      - memory_forget
```

重启 hermes-agent 后, agent 会自动看到 `mnelo.recall(...)` / `mnelo.remember(...)` 这 7 工具.

### 6.3 RAG 召回例子

```python
import sys
sys.path.insert(0, '/Users/you/.hermes/memory/api')
from mnelo_client import MneloClient

c = MneloClient()

# 当用户问 "主人哪个股票昨天浮盈？" 时:
hits = c.recall("股票 浮盈 主人", top_k=3, strategy='rrf')

# 给 LLM 用作 context ( top-3 RRF 总是最有意义的)
context = "\\n".join([f"[{i+1}] {h['content']}" for i, h in enumerate(hits)])
prompt = f"""基于以下历史记忆回答用户问题:
{context}

问题: 主人哪个股票昨天浮盈？
"""
```

---

## 7. cron 自动维护

mnelo 不需要 LLM consolidation (用 valid_until 自动过期)。**每天一次** 自检, ~30 秒:

### 7.1 Hermes cron 配 jobs.json

```bash
hermes cron create \
  --name 'mnelo 自检 (凌晨 02:00)' \
  --schedule '0 2 * * *' \
  --script health_check.py \
  --no-agent
```

或者手动加到 `~/.hermes/cron/jobs.json`:

```json
{
  "id": "b776af792d72",
  "name": "mnelo 自检 (凌晨 02:00)",
  "schedule": {"kind": "cron", "expr": "0 2 * * *"},
  "enabled": true,
  "script": "health_check.py",
  "no_agent": true,
  "deliver": "local"
}
```

### 7.2 daily_check 输出格式

```text
mnelo daily check — 2026-07-18 17:25:04 BJT
==================================================
✅ MCP server alive — PID 19408, uptime 0.2h
✅ WAL checkpoint — 124/124 pages flushed
📊 DB stats — entities 4566/4743, chunks 4161/4522, relations 15749/15750, vectors 4440
💾 Size — db 23.7M, WAL 5.7M, shm 32.0K, journal_mode wal
📈 Recall 24h — 1390 次, 空 hits 80 (6%), latency p50=12.4ms p95=36.2ms avg=33.0ms
🏷️  Kind TOP-3 — concept=4155, stock=388, canonical_fact=7
⚠️  concept 占 91.0% —  kind 单一化, 考虑提升其他 kind 占比
```

### 7.3 健康预警阈值

| 指标 | 正常 | 关注 | 处理 |
|---|---|---|---|
| MCP alive | ✅ | — | 不 alive → `launchctl kickstart` |
| WAL checkpoint pages | > 0 | < 0.5× db pages | passive checkpoint 自动修复 |
| p95 latency | < 50ms | 50-100ms | 检查 vec0 cold-cache |
| 空 hits rate | < 10% | 10-20% | 检查 query 表达 |
| kind concept 占比 | < 95% | > 95% | entity_resolve merge |

---

## 8. 配置 (`~/.hermes/memory/config.toml`)

```toml
[main]
timezone = "local"           # 'local' / 'utc' / 'Asia/Shanghai' 等 IANA 名
warm_up_embedder = true      # 启动时预加载 bge-small-zh (避免首次 1s 冷启动)
```

环境变量优先 (env vars > toml > 代码默认):

```bash
export MNELO_MEMORY_TIMEZONE=Asia/Shanghai   # 覆盖 timezone
export MNELO_MEMORY_WARM_UP_EMBEDDER=false   # 启动不预热 (省 500ms)
```

---

## 9. 运维

### 9.1 重启

```bash
launchctl kickstart -k "gui/$(id -u)/ai.mnelo.mcp"
sleep 3
lsof -tiTCP:8086 -sTCP:LISTEN | xargs -I{} ps -p {}
```

### 9.2 备份

`memory.db` 是单 SQLite 文件,  cp 即可 (WAL 用 `-wal` flag):

```bash
sqlite3 memory.db ".backup '/backup/memory.$(date +%Y%m%d).db'"
```

dr-backup 加 cron 后 (见 `~/.hermes/scripts/dr-backup.sh`) 自动 rsync 到 NAS.

### 9.3 修复 vec0 (一次性)

如果从老 Mnemosyne 迁移, vec0 rowid 可能 60-70% 错位. 一次性修复:

```bash
cd ~/.hermes/memory
python3 scripts/repair_vectors.py  # dry-run 默认
python3 scripts/repair_vectors.py --apply   # 写入 (1591 条历史, ~2 sec)
```

效果: vec0 rowid sync 60% → 100%; vector_only recall 从 0 → 命中.

### 9.4 监控

实时看 recall 流:

```bash
sqlite3 ~/.hermes/memory/memory.db \
  "SELECT id, query, latency_ms, created_at FROM recall_log ORDER BY id DESC LIMIT 20"
```

---

## 10. 故障排查

### MCP server 起不来

```bash
# 检查 launchd
launchctl list | grep mnelo
# 查看 stderr
cat /tmp/mnelo-mcp.err | tail -50
```

### Recall 全空

`empty hits rate > 20%`: 大多因 query 是 placeholder / 中文单字。请升级 `MneloClient` (含占位符短路 + ASCII 单字符过滤).

### Latency spike

- **首次 recall 慢 1s+** → `warm_up_embedder=false` 启的。设 `warm_up_embedder=true`.
- **p95 > 100ms** → vec0 cold-cache。restart MCP server.
- **p95 > 500ms** → SQLite WAL bloat. `sqlite3 memory.db 'PRAGMA wal_checkpoint(TRUNCATE)'`.

### WAL 文件过大

```bash
# 手动 PASSIVE checkpoint (非阻塞, ~1 sec)
sqlite3 ~/.hermes/memory/memory.db 'PRAGMA wal_checkpoint(PASSIVE);'
# 后 WAL 从 5.7M 缩回 <1M
```

---

## 11. 性能基准 (2026-07-18 实测)

| 指标 | 数据 | 注 |
|---|---|---|
| **Latency p50** | 12.4 ms | warm path |
| **Latency p95** | 36.2 ms | 4 路 RRF 并发 |
| **Latency avg** | 33.0 ms | |
| **Recall 24h** | 1,390 次 | |
| **Empty hits rate** | 6% | placeholder 过滤前 8% |
| **MCP startup** | ~1.1s | 含 Embedder warm-up |
| **DB size** | 23.7 MB | 4500 entities + 4500 chunks |
| **WAL size** | 5.7 MB | 1 天数据 |
| **Memory RAM** | ~270 MB RSS | mcp + embedder（实测 PID 39344，macOS M-series） |

性能上限：单一 macbook + SQLite WAL mode + sqlite-vec，**~50 万向量 (512 维) 之内可保持在 100 ms 响应目标内**。依据是 [sqlite-vec v0.1.0 作者实测](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html#benchmarks)：1M × 128 维 33 ms、500K × 960 维 < 100 ms，延迟按 `dim × log(n)` 缩放。超过 50 万向量后建议迁 Qdrant / Milvus（带 HNSW 索引）。详见 [README.md §Known limitations](../../README.md#-known-limitations)。

---

## 12. 安全 / 多用户

⚠️ 当前版本**单用户本地优先**. 多租户隔离还没建. 风险:

1. **MCP server 监听 127.0.0.1** — 不暴露对外.
2. **schema 用 valid_until 软删除** — 安全敏感数据记得 **手动 `forget()`**.
3. **PII 清理** (P1-5 待开发) — 不要存密码 / token / 信用卡.

✅ **承诺**:
- 数据完全本地 (SQLite 文件), 不调任何云 API
- Embedder 模型 (bge-small-zh) 可选预下载
- 可装在小机器 (8 GB RAM 的 mini PC 都够)

---

## 13. 安装验证

跑这个 command 一行验证所有:

```bash
cd ~/.hermes/memory && \
/Users/you/hermes-agent/venv/bin/python3 -m pytest tests/ -q
# 预期: 50 passed in ~3s
```

---

## 14. 升级

升级 mnelo 自身:

```bash
# 1. 备份 db
sqlite3 ~/.hermes/memory/memory.db ".backup ~/memory.backup.db"

# 2. 拉新代码 (git)
cd ~/.hermes/memory && git pull

# 3. 跑迁移 (如果有 schema 变)
python3 scripts/migrate.py latest

# 4. 重启 MCP
launchctl kickstart -k "gui/$(id -u)/ai.mnelo.mcp"

# 5. 跑测试
python3 -m pytest tests/ -q
```

---

## 15. 链接

- **MCP**: <https://modelcontextprotocol.io>
- **sqlite-vec**: <https://github.com/asg017/sqlite-vec>
- **fastembed**: <https://qdrant.github.io/fastembed>
- **bge-small-zh-v1.5**: <https://huggingface.co/BAAI/bge-small-zh-v1.5>
- **Hermes Agent (主入口)**: <https://nousresearch.com/hermes>

---

> mnelo = μνήμη + λόγος. 5 字符, 0 占用, PyPI / npm 都 clean.
> Hermes 是 messenger 神, mnelo 是他的 memory layer.
> 拍板 2026-07-18 BJT.
