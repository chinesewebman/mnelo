# mnelo

> **mnelo** = μνήμη + λόγος (Greek: *memory* + *reason*).
> Local-first, single-file, knowledge-graph memory layer for AI agents.

> **中文用户**: [README.zh.md](README.zh.md) 提供中文版。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.26-green)](https://modelcontextprotocol.io)
[![SQLite-vec](https://img.shields.io/badge/sqlite--vec-0.1.9-orange)](https://github.com/asg017/sqlite-vec)
[![Bilingual](https://img.shields.io/badge/i18n-EN%20%2B%20中文-blueviolet)](#-i18n)
[![Local-first](https://img.shields.io/badge/local--first-100%25-brightgreen)](#-design-tenets)

A drop-in memory layer for [Hermes Agent](https://nousresearch.com/hermes), [Claude Desktop](https://claude.ai/download), [Cursor](https://cursor.sh), or any MCP client. Stores vectors, knowledge graph, and metadata in one SQLite file. Hybrid recall (vector + graph + meta + entity) with RRF fusion. **Zero cloud dependency**.

---

## ⚡ At a glance

| | |
|---|---|
| **Storage** | Single SQLite file (~24 MB @ 4500 chunks, 4600 entities) |
| **Vector index** | `sqlite-vec` (vec0) + `bge-small-zh-v1.5` (512-dim, CN-native) |
| **Graph** | Native relations table, 2-hop BFS traversal |
| **Recall** | 4-way hybrid: `vector + graph + meta + entity` → RRF fusion |
| **Protocol** | MCP over SSE (127.0.0.1:8086) |
| **Latency (warm)** | p50 = **12.5 ms**, p95 = **36 ms** (4-way concurrent) |
| **LOC** | ~3000 lines of Python (memory.py + scripts + client + tests) |
| **Dependencies** | 3 pip installs: `mcp[cli]`, `sqlite-vec`, `fastembed` |
| **i18n** | English + 中文 first-class, locale auto-detect |

---

## 🆚 Why mnelo?

The MCP-for-memory landscape (surveyed July 2026):

| Project | Stars | Vector | Graph | Local | Bilingual | RRF | Single-file |
|---|---|---|---|---|---|---|---|
| **mnelo** | new | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| vestige | 585 | ✅ | – | ✅ | – | – | ✅ |
| depthfusion | 3 | ✅ | ✅ | – | – | ✅ | – |
| memory-vault | 57 | ✅ | ✅ | ✅ | – | – | ✅ |
| mnemo | 230 | ✅ | ✅ | ✅ | – | – | – |
| graphmind | 198 | – | ✅ | ✅ | – | – | ✅ |

mnelo is the only one that combines **all seven** axes in a **single-file SQLite** + **bilingual** out of the box.

---

## ✨ Features

### 🧠 Knowledge-graph aware
Not just RAG. Every chunk can link to typed entities (`stock`, `concept`, `person`, `canonical_fact`) and the relations graph is queryable. `memory_graph_query` returns 2-hop neighbors for navigation.

### ⚡ 4-way parallel recall (concurrent)
```
                ┌─ vector (vec0 MATCH)
                ├─ graph (BFS from seed)
query → RRF ──→ ├─ meta (LIKE search)
                └─ entity (name LIKE + alias match)
```
Each lane uses its own SQLite connection (WAL mode allows concurrent reads). p95 dropped **80ms → 36ms** under load.

### 🛡️ Soft-delete via `valid_until` chain
Nothing is ever hard-deleted. Updates create new rows with `valid_until` timestamps; the cascade runs on recall. This gives you **automatic version history** with zero extra effort.

### 🧪 Stock-entity aware RRF
When a query matches a stock code (e.g. `sh600089`), the entity hit gets a `0.05/sqrt(rank)` boost in RRF fusion. **Practical**: stock entities float to the top when you ask "sh600089".

### 🌏 Bilingual out of the box
- Locale auto-detect: `HERMES_MEMORY_LANG` > `LC_ALL` > `LANG` > `en`
- 30+ user-facing strings, all in both English and 中文
- Add a new locale by appending to `i18n_messages.py` — no code change needed

### 🔌 Standard MCP, no lock-in
Exposes 7 tools (`memory_remember`, `memory_recall`, `memory_relate`, `memory_forget`, `memory_update`, `memory_graph_query`, `memory_stats`) over SSE. Works with **any** MCP-compatible client.

---

## 📊 Benchmark results

All numbers measured on a single MacBook (M-series), `memory.db` = **23.9 MB / 4,606 entities / 4,186 chunks / 15,749 relations / 4,484 vectors**.

### Latency

| Metric | Value | Notes |
|---|---|---|
| **p50** | **12.5 ms** | warm path, 4-way concurrent |
| **p95** | **36.2 ms** | warm path, 4-way concurrent |
| **avg** | 34.4 ms | 24h warm-path average |
| **max** | 2980 ms | first recall after cold start (embedder warm-up) |
| **cold start** | ~1.1 s | MCP server launch + embedder model load |

### Memory footprint

| Component | RAM |
|---|---|
| MCP server process | ~150 MB |
| Embedder (bge-small-zh, in-memory) | ~500 MB |
| SQLite + WAL buffers | ~50 MB |
| **Total** | **~700 MB** |

### Test coverage

```
$ python3 -m pytest tests/ -q
..................................................   [100%]
50 passed in ~3s
```

50 tests across:
- 30 CRUD + recall (with various top_k / filters / strategy)
- 12 edge cases (placeholder filter, special chars, FTS performance)
- 8 bounds checks (`importance ∈ [0, 1]`, `latency ≥ 0`, `valid_until` chain)

---

## 🏗 Architecture / Project structure

```
mnelo/
├── README.md                       ← you are here (English)
├── README.zh.md                    ← 中文版 (Simplified Chinese)
├── LICENSE                         ← MIT
├── .gitignore                      ← excludes memory.db, *.pyc, .env, *.bak*
│
├── memory.py                       ← core Memory class (~1100 LOC)
│                                    - remember() / recall() / relate() / forget() / update()
│                                    - 4-way concurrent recall + RRF fusion
│                                    - soft-delete via valid_until
│                                    - placeholder filter (15 ASCII + 4 single-char)
│                                    - stock-entity RRF boost
│                                    - feedback loop (recall_details_json)
│
├── mcp_server.py                   ← MCP server entry (SSE transport)
├── config.py                       ← env var > TOML > defaults loader
├── config.toml / config.toml.example
├── schema.sql                      ← 11 tables (chunks, entities, relations, vectors, recall_log, …)
├── embedder.py                     ← fastembed wrapper (bge-small-zh, singleton)
├── entity_resolve.py               ← entity merge + alias resolution
│
├── mnelo_locale.py                 ← locale auto-detect (env-based)
├── i18n_messages.py                ← 30+ bilingual msg table (EN + ZH)
│
├── api/
│   ├── mnelo_client.py             ← MneloClient (SSE client, 7 tools)
│   └── hermes_memory_client.py     ← back-compat alias
│
├── scripts/
│   ├── init_db.py                  ← one-shot DB creation
│   ├── health_check.py             ← daily 9-line summary (i18n)
│   ├── repair_vectors.py           ← vec0 rowid fix (post-import)
│   ├── migrate_to_hermes_memory.py ← from old Mnemosyne → mnelo
│   ├── import_holdings.py          ← portfolio snapshots → entities
│   └── import_identity_facts.py    ← identity facts → canonical_fact
│
├── tests/
│   ├── test_memory.py              ← 30 tests: CRUD + recall + bounds
│   └── test_edge_cases.py          ← 18 tests: edges + security
│
└── docs/
    ├── RUNBOOK.md                  ← deployment & operations
    ├── SCHEMA.md                   ← SQL schema reference
    └── ARCHITECTURE.md             ← design decisions
```

### Recall pipeline (4-way + RRF)

```
                   query
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼             ▼
    vector         graph          meta         entity
   (vec0 +      (BFS 2-hop     (LIKE fuzzy    (name LIKE +
    embed)       from seeds)    match)        alias match)
       │             │             │             │
       └─────────────┴─────────────┴─────────────┘
                     ▼
              RRF fusion
       score(d) = Σ 1 / (60 + rank_i)
       + stock entity boost: 0.05 / sqrt(rank)
                     ▼
                top-K results
                     │
                     ▼
           _log_recall (recall_details_json
            persists top-5 ranks + method +
            distance + rrf_score + importance
            for feedback analysis)
```

---

## 🚀 Quick start

```bash
# Install
git clone https://github.com/chinesewebman/mnelo
pip install -r requirements.txt

# Init DB
cd mnelo && python3 scripts/init_db.py

# Start MCP server
launchctl load ~/Library/LaunchAgents/ai.hermes-memory.mcp.plist
# (or: python3 mcp_server.py --transport sse --port 8086)

# Use from Python
python3 -c "
import sys; sys.path.insert(0, 'api')
from mnelo_client import MneloClient
c = MneloClient()
c.remember('sh600089 build at 18.96', source='trading', importance=0.9)
for h in c.recall('sh600089 建仓', top_k=5):
    print(h['method'], h['chunk_id'], h['content'][:60])
"
```

For Chinese locale:

```bash
export HERMES_MEMORY_LANG=zh
python3 scripts/health_check.py
```

Full deployment & operations → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

---

## 🌐 i18n

Add a new locale (e.g. Japanese) — 1 edit, no code change:

```python
# i18n_messages.py
'check.recall_24h': {
    'zh': '📈 Recall 24h — {count} 次...',
    'en': '📈 Recall 24h — {count} calls...',
    'ja': '📈 リコール 24h — {count} 回...',   # ← add
},
```

Set `HERMES_MEMORY_LANG=ja` to test. Locale miss falls back to `en`, then to `msg_id` (so missing strings are debuggable, not silent).

---

## 🛡 Design tenets

1. **Local first.** No cloud API calls, ever. The embedder model can be pre-downloaded, then runs offline.
2. **Single file.** SQLite. `cp memory.db` = full backup.
3. **Standard MCP, no lock-in.** Works with any MCP-compatible client.
4. **Bilingual.** English + 中文, both first-class citizens.
5. **Boring & predictable.** No magic. If something breaks, the traceback says why.
6. **Measured.** All numbers in the Benchmark section are reproducible from the cited sources.
7. **Bounded.** Soft-delete chain has a max depth; old versions are GC'd by a cron job (not implemented yet, see TODO).

---

## 🚧 Known limitations

| Limit | Basis | Workaround |
|---|---|---|
| **~500K vectors** @ 512-dim on a single MacBook | [`sqlite-vec` v0.1 benchmarks](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html#benchmarks): vec0 returns 33 ms at 1M × 128-dim (sift1m) and < 100 ms at 500K × 960-dim (gist1m). Latency scales with `dim × log(n)`. At 512-dim, ~500K vectors stays under the [100 ms responsiveness goal](https://developer.mozilla.org/en-US/docs/Web/Performance/How_long_is_too_long#responsiveness_goal) | Switch to HNSW-backed Qdrant/Milvus at >1M vectors |
| Single-user (no multi-tenant) | One SQLite file, no row-level isolation | Don't expose port 8086 to LAN |
| No PII auto-detection | Not yet implemented (P1-5) | Don't store passwords / tokens / credit cards |
| bge-small-zh is CN-tuned (works for EN but suboptimal) | C-MTEB benchmark ranking | Swap to bge-small-en-v1.5 if your workload is mostly English |

### Basis for the limits

The thresholds come from:

- **sqlite-vec v0.1.0 author benchmarks** (Alex Garcia, Aug 2024): <https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html#benchmarks>
- **alexgarcia's own caveat**: "the limits of sqlite-vec really show at 1 million vectors" for higher dimensions (192-dim → 192 ms, 3072-dim → 8.5 s)
- **MDN 100 ms responsiveness goal** as the latency budget

### RAM is NOT the bottleneck

vec0 storage is chunked, not in-memory by default. From Alex Garcia on the vec0 design ([Reddit, r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1ehlazq/introducing_sqlitevec_v010_a_vector_search/)):

> "The vec0 virtual table stores vectors in **chunks** and reads those chunks one-by-one to perform KNN, so **not the entire dataset is fit into memory**."

What this means for mnelo:

| Component | Memory at 4487 vectors | Memory at 1M vectors |
|---|---|---|
| **vec0 chunked storage** | ~9 MB (4487 × 2 KB, fits in 1 of 8192-row chunks) | ~2 GB across 122 chunks, but only ~1 chunk is hot per query |
| **SQLite page cache** (`PRAGMA cache_size`) | 64 MB (set at startup) | Same default; bump to fit working set |
| **OS page cache** for `memory.db` | OS-managed, free on macOS | OS-managed, evicted under memory pressure |
| **Embedder** (bge-small-zh, the actual RAM hog) | ~500 MB | ~500 MB (constant) |

The **real bottleneck on a single MacBook** is:

1. **Disk random-read latency** for cold chunks — mitigated by OS page cache, but first access to a cold chunk costs ~1 SSD seek (~100 µs)
2. **SQLite `cache_size`** — set to `-64000` (64 MB) at startup so the working set fits without re-fetching from OS page cache
3. **Embedder RAM** (~500 MB) — does NOT scale with data size; this is the only fixed RAM cost

In short: **vec0 is designed for disk-first storage, not RAM-resident**. Past 1M vectors, the disk-IOPS budget becomes the constraint, not RAM. Use HNSW-backed Qdrant/Milvus only if you need (a) ANN with sub-10 ms latency at >10M vectors, or (b) distributed shards.

---

## 🔗 Integrations

- **Hermes Agent** (primary) — `~/.hermes/plugins/mnelo/` symlink + `mcp_servers.mnelo.url: http://127.0.0.1:8086/sse` in config
- **Claude Desktop** — add MCP server in `claude_desktop_config.json`
- **Cursor** — Settings → MCP → Add server
- **Any MCP client** — SSE at `http://127.0.0.1:8086/sse`, 7 tools

---

## 🧪 Run tests

```bash
cd mnelo
python3 -m pytest tests/ -q
# expected: 50 passed in ~3s
```

---

## 📜 License

MIT. See [`LICENSE`](LICENSE).

---

## 🙏 Acknowledgements

- [sqlite-vec](https://github.com/asg017/sqlite-vec) — vector extension
- [fastembed](https://qdrant.github.io/fastembed) — embedder wrapper
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) — CN embedding model
- [MCP](https://modelcontextprotocol.io) — protocol spec
- [Hermes Agent](https://nousresearch.com/hermes) — primary integration target

---

> Hermes = the messenger god.
> mnelo = his memory layer.
>
> Built 2026-07-18 by [chinesewebman](https://github.com/chinesewebman) + [Hermes Agent](https://nousresearch.com/hermes).