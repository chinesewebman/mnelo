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

A memory layer for AI agents. Remembers across **4 dimensions** — vector semantics, knowledge graph, full-text metadata, and entity identity — so every decision can be traced back to the conditions that produced it. One local SQLite file, shared by every local MCP client. **Zero cloud, zero lock-in.**

**Why 4-way recall wins**: each lane catches what the others miss — vector misses literal terms (stock codes, ticker symbols), meta misses semantic paraphrases, graph misses orphaned chunks with no entity links, entity misses long-form prose. Four lanes run in parallel (WAL-mode concurrent reads, p50 = **12.5 ms** / p95 = **36 ms**), and RRF fuses their ranks without any score normalization — so you get high recall without per-lane threshold tuning. See [🔀 What is RRF?](#-what-is-rrf) below for the math, and [At a glance](#-at-a-glance) for the latency numbers.

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

### 🔀 What is RRF?

**RRF = Reciprocal Rank Fusion** ([Cormack et al., 2009](https://dl.acm.org/doi/10.1145/1571941.1572114)). The simplest recipe that actually beats weighted-score tuning when you're fusing results from heterogeneous search lanes.

The core idea: each lane ranks results independently; you then merge by summing `1 / (k + rank_i)` across lanes, where `k=60` is the standard damping constant.

```
Lane A (vector):   doc1=1, doc2=3, doc5=2
Lane B (graph):    doc2=1, doc1=2, doc7=3
Lane C (meta):     doc5=1, doc1=3, doc9=2

Final score = Σ_lanes 1 / (60 + rank_in_lane)
→ doc1: 1/61 + 1/62 + 1/63 = 0.0483   ← wins
→ doc2: 1/63 + 1/61       = 0.0321
→ doc5: 1/62 + 1/61       = 0.0321
```

**Why RRF over weighted-score fusion?**

|                          | RRF                                          | Weighted score fusion                       |
| ------------------------ | -------------------------------------------- | ------------------------------------------- |
| Needs score normalization? | **No** — rank-only                          | Yes (each lane's score scale must be calibrated) |
| Robust to one lane going wild? | **Yes** — outlier ranks only contribute `1/(60+rank)` | No — a single lane with skewed scores can dominate |
| New lane added?          | Just add it                                  | Re-tune all weights                         |
| Implementation cost      | ~5 lines                                     | Score calibration + weight grid search      |

mnelo uses the canonical `k=60` for the 4 lanes (vector / graph / meta / entity), plus a small `0.05/sqrt(rank)` boost when a stock-code entity matches — that's the only per-domain tweak. Everything else is textbook RRF.

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

Measured on macOS M-series (Apple Silicon), one MCP server process, idle:

| Component | RAM | Measured? |
|---|---|---|
| MCP server process (Python + mcp + SQLite + onnxruntime + embedder + bge model) | **~270 MB** | ✅ RSS, PID 39344 |
| └─ Embedder (bge-small-zh, weights + onnx session + tokenizer + pooling) | ~200 MB of the above | inferred from baseline |
| └─ Python interpreter + mcp server + sqlite-vec + chunked buffer | ~70 MB of the above | inferred from baseline |
| OS page cache for `memory.db` | OS-managed, free on macOS | — |
| **Total practical RSS** | **~270 MB** | ✅ |

#### Why the embedder uses ~3× its file size in RAM

The bge-small-zh-v1.5 model file is **92 MB** on disk (`model.safetensors` only is **91.4 MB**; full snapshot in `~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/` is **92 MB** including tokenizer + config). But the embedder holds roughly **200 MB** resident. The ~110 MB gap is **runtime overhead**, not the model itself:

| Source | Approx. RAM | What it is |
|---|---|---|
| `model.safetensors` loaded into float32 | ~120 MB | BGE 6-layer transformer + token embeddings, weights stored as float32 in RAM even though the .safetensors file is ~half that on disk |
| onnxruntime session workspace | ~40-60 MB | Pre-allocated memory arena for intermediate tensors during forward passes |
| Tokenizer (Fast + vocab) | ~10-15 MB | XLM-R tokenizer table for Chinese, loaded once and held in memory |
| sentence-transformers pooling module | ~5-10 MB | Mean-pooling wrapper + its config |

**Key takeaway**: file size ≠ RAM cost. The 92 MB download inflates to ~200 MB at runtime; the remaining ~70 MB of the 270 MB process RSS is everything else (Python + mcp + SQLite + sqlite-vec). This is constant — it does NOT scale with how many chunks you store.

### 📁 Where does the model live?

fastembed uses the **HuggingFace Hub cache**, not a mnelo-private directory:

```
~/.cache/huggingface/hub/
└── models--BAAI--bge-small-zh-v1.5/       # 92 MB on disk
    ├── blobs/                                # actual files (deduped by SHA)
    │   ├── 354763...d61d5a026  ← model.safetensors (91.4 MB)
    │   ├── cdb3043...8f88747   ← tokenizer.json (429 KB)
    │   └── ...
    ├── snapshots/                             # symlinks → blobs, with friendly names
    └── refs/main                              # current commit hash
```

**Auto-download** happens on first call to `mcp_server.py` / `scripts/init_db.py` / `scripts/health_check.py` — no manual step needed. Typical download: ~90s on a fast connection.

**Manual pre-download** (offline install, CI, or air-gapped environments):

```bash
# Option A: huggingface-cli (official)
pip install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-small-zh-v1.5 \
  --local-dir ~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/

# Option B: hf (newer CLI)
hf download BAAI/bge-small-zh-v1.5 \
  --local-dir ~/.cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/

# Then point mnelo at it (optional — default already points there):
export HF_HOME=/path/to/your/cache
python3 scripts/init_db.py
```

**Relocate the cache** (e.g. on a sandboxed machine with `/home` mounted read-only):

```bash
# Pick one — both work, HUGGINGFACE_HUB_CACHE is more specific
export HF_HOME=/srv/cache/huggingface
# or
export HUGGINGFACE_HUB_CACHE=/srv/cache/huggingface/hub
```

The env var must be set **before** the MCP server starts (launchd plist inherits it via `EnvironmentVariables` if you set it there).

**Sharing the model with other tools**: any other tool that uses fastembed / sentence-transformers / HuggingFace transformers will find the same cached files at the default path. No duplicate downloads.

### 🌐 Multilingual models

The default `bge-small-zh-v1.5` is **Chinese-native** (C-MTEB strong) but works for English and 100+ other languages too — just with degraded quality on non-Chinese text. For workloads heavy in another language, swap the embedder via `config.toml`:

```toml
[embedder]
model = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
dim = 384
```

Or via env var (no config edit):

```bash
export HERMES_MEMORY_EMBEDDER_MODEL='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
export HERMES_MEMORY_EMBEDDER_DIM=384
launchctl kickstart -k gui/$(id -u)/ai.mnelo.mcp
```

**Recommended model matrix** (all on the [fastembed-supported list](https://qdrant.github.io/fastembed/examples/Supported_Models/), all MIT or Apache-2.0):

| Use case | Model | dim | Size on disk | License |
|---|---|---|---|---|
| Chinese (default) | [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | 512 | 92 MB | MIT |
| English | [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | 384 | 67 MB | MIT |
| **Multilingual (50+ languages: 日本語 / 한국어 / Español / Français / Deutsch / 中文 / English / …)** | [paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | 384 | 220 MB | Apache-2.0 |

**Why these three?**

- **bge-small-zh-v1.5** — best for Chinese text (C-MTEB top-3 at 90 MB), acceptable for English
- **bge-small-en-v1.5** — best English-only at 67 MB; pick if you never store Chinese
- **paraphrase-multilingual-MiniLM-L12-v2** — covers 50+ languages including the ones most commonly requested (日本語 / 한국어 / Español / Français / Deutsch / 中文 / English) at one model, 220 MB. Top-3 in MTEB multilingual-retrieval benchmarks among fastembed-supported models. **This is the right pick if your corpus is mixed-language or you want one model for everything.**

⚠️ **Switching models requires re-initializing the database** — the `sqlite-vec` schema bakes `dim` into the table definition. Old embeddings are not portable across dims.

```bash
# After editing config.toml:
rm ~/.hermes/memory/memory.db        # backup first if you have data you care about
python3 scripts/init_db.py
launchctl kickstart -k gui/$(id -u)/ai.mnelo.mcp
```

### Test coverage

```
$ python3 -m pytest tests/ -q
........................................................................ [ 84%]
......................................................................   [100%]
429 passed, 1 skipped in ~16s
```

429 tests across 12 rounds (v0.4.0 → v0.4.11):
- mcp_server: 87% → 98% (+147 tests via `_load_from_repo` pattern for REPO source-of-truth)
- entity_resolve: 76% → 85% (+38 tests via merge/get_aliases edge cases)
- memory: 92% → 93% (+10 branch tests)
- validation: 95% → 99% (+22 tests, accept int IDs + reject bool)
- auth: 92% → 100%, config: 80% → 92%
- mnelo_locale: 0% → 100%
- i18n_messages / `__main__` blocks: tracked via `coverage run -m` subprocess tests

Cross-test pollution (REPO vs LIVE module identity) accepted as structural — coverage uses REPO source-of-truth.

---

## 🔄 Repo ↔ live sync (post-commit hook)

mnelo has two copies of every `.py` / `.sql` file:

| Location | Role |
|---|---|
| `~/projects/mnelo/` | Source of truth (git HEAD) |
| `~/.hermes/memory/` | The MCP server actually running on port 8086 |

Without a sync mechanism, tests run against `memory.py` in live but assertions are written against the repo version — false positives, false negatives, and lots of head-scratching. The repo ships a **post-commit hook** that copies edited files to live on every commit:

```bash
# One-time install (after clone):
cd ~/projects/mnelo
git config core.hooksPath .githooks
```

What it does on every commit:

- Diffs HEAD~1..HEAD, picks `.py` / `.sql` / `.sh` files
- Maps `scripts/init_db.py` → `~/.hermes/memory/scripts/init_db.py`, `api/*.py` → `~/.hermes/memory/api/`, top-level → `~/.hermes/memory/`
- **Backs up the live version** first to `~/.hermes/memory/.sync-backups/<timestamp>-<sha>/` (keeps last 5)
- Atomic `mv` overwrite — partial writes can't corrupt live
- Runs `scripts/health_check.py` after sync to catch regressions early
- Prints a one-shot hint if live server needs a restart (changing `memory.py` / `embedder.py` / `config.py` invalidates Python's import cache)

**Skip on a specific commit**: append `[skip-sync]` to the commit message.

**Don't sync on this commit**: `git commit -m "docs: ... [skip-sync]"`.

What it does **NOT** touch (by design):

- `memory.db` — your data, never auto-modified
- `config.toml` — may contain personal overrides you don't want blown away
- `*.md` — docs don't need to be in live
- `tests/` — tests don't run in live

**Restart after sync** (when memory.py / embedder.py / config.py changed):

```bash
launchctl kickstart -k gui/$(id -u)/ai.mnelo.mcp
```

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
│   └── mnelo_client.py             ← MneloClient (SSE client, 7 tools)
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
# 1. Clone
git clone https://github.com/chinesewebman/mnelo.git
cd mnelo

# 2a. One-shot install (recommended) — handles venv, pip, init_db, plist, auth token
bash scripts/install.sh

# 2b. Or manual step-by-step:
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Init DB
python3 scripts/init_db.py

# 4. Start MCP server
launchctl load ~/Library/LaunchAgents/ai.mnelo.mcp.plist
# (or: HERMES_MEMORY_SERVER_PORT=8086 python3 mcp_server.py --transport sse)

# 5. Use from Python
python3 -c "
import sys; sys.path.insert(0, 'api')
from mnelo_client import MneloClient
c = MneloClient()
c.remember('sh600089 build at 18.96', source='trading', importance=0.9)
for h in c.recall('sh600089 建仓', top_k=5):
    print(h['method'], h['chunk_id'], h['content'][:60])
"
```

`scripts/install.sh` is **idempotent** — safe to re-run after upgrades. It also accepts `LIVE_ROOT=~/.mnelo bash scripts/install.sh` to install to a non-default path.

For Chinese locale:

```bash
export HERMES_MEMORY_LANG=zh
python3 scripts/health_check.py
```

Full deployment & operations → [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

### 🤖 One-line agent install

Skip all five steps — just hand the URL to an AI agent (Hermes / Claude / Cursor / Codex / any coding agent):

> **Install and start mnelo from https://github.com/chinesewebman/mnelo — clone it, set up the venv, run `scripts/init_db.py`, launch the MCP server on port 8086, and verify with `scripts/health_check.py`. Report back when `🟢 MCP server ready` is in the log.**

The agent handles venv creation, `pip install -r requirements.txt`, plist install, and the health probe. Typical install takes ~90s (the bge-small-zh model download is the slow part — **92 MB** of `model.safetensors` + tokenizer + config in `~/.cache/huggingface/hub/`).

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
| bge-small-zh is CN-tuned (works for EN but suboptimal) | C-MTEB benchmark ranking | Swap to [bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) if your workload is mostly English |

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
| **Embedder** (bge-small-zh, the constant RAM cost) | ~200 MB | ~200 MB (constant; ~3× its 92 MB file size due to float32 load + onnxruntime workspace + tokenizer — see [Memory footprint](#-memory-footprint)) |

The **real bottleneck on a single MacBook** is:

1. **Disk random-read latency** for cold chunks — mitigated by OS page cache, but first access to a cold chunk costs ~1 SSD seek (~100 µs)
2. **SQLite `cache_size`** — set to `-64000` (64 MB) at startup so the working set fits without re-fetching from OS page cache
3. **Embedder RAM** (~200 MB) — does NOT scale with data size; this is the only fixed RAM cost (~3× its 92 MB file size — see [Memory footprint](#-memory-footprint))

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
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) — CN embedding model (default, 512-dim)
- [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) — EN embedding model (swap if your workload is mostly English)
- [MCP](https://modelcontextprotocol.io) — protocol spec
- [Hermes Agent](https://nousresearch.com/hermes) — primary integration target

---

> Hermes = the messenger god.
> mnelo = his memory layer.
>
> Built 2026-07-18 by [chinesewebman](https://github.com/chinesewebman) + [Hermes Agent](https://nousresearch.com/hermes).