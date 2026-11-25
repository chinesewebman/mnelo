# Benchmark results

All numbers measured on a single MacBook (M-series). Current baseline
(2026-08): `memory.db` ≈ **~44.7 MB + 0.7 MB WAL / 4,498 entities / 4,343
chunks**.

## Latency

| Metric | Value | Notes |
|---|---|---|
| **p50** | **18 ms** | warm, zvec 0.6 @ 5k vectors, 4-way concurrent (8/6 measured) |
| **p95** | **24 ms** | same |
| **p99** | **25 ms** | same |
| **p50 (15k vectors)** | **73 ms** | `scripts/benchmark.py --chunks 10000` (15422 vectors after seed) |
| **p95 (15k vectors)** | **95 ms** | HNSW scales sub-linearly with chunk count |
| **p99 (15k vectors)** | **161 ms** | worst-case observed |
| **avg (24h warm)** | 10.4 ms | `recall_log` 8/6, 232 hits, incl. cold-start outliers |
| **cold start** | ~1.1 s | MCP launch + embedder load |

Reproduce (public harness — anyone can rerun):

```bash
python -m benchmarks latency --chunks 10000 --queries 100 --json bench.json
# 旧入口兼容: python scripts/benchmark.py --chunks 10000 --queries 100 --json bench.json
```

## LoCoMo (smoke)

[8/11 P3-followup] 在 chinesewebman/mnelo#11 之上加 `locomo` 子命令 — LoCoMo 风格
召回质量 smoke。3 个内置 scenario（光伏装机 / 美联储利率 / 比亚迪销量），每个 3
个 chunk + 3 个 query，测「召回覆盖率」+ 同一批 query 的延迟。

```bash
python -m benchmarks locomo
# 输出: coverage per_scenario / mean / latency p50 / mean
```

完整 10-conversation LoCoMo dataset 接入留作后续 PR — 50MB+ 数据集 + mnelo
graph-aware scorer 是单独的工作。

## Memory footprint

One MCP server process, idle (macOS M-series): **~270 MB RSS** — of which
the embedder (bge-small-zh weights + onnxruntime + tokenizer) is ~200 MB,
constant regardless of data size; the rest (~70 MB) is Python + MCP +
SQLite + zvec (or usearch). The 92 MB model file inflates to ~200 MB
resident (float32 load + ONNX arena + tokenizer) — **file size ≠ RAM
cost**. The zvec collection itself is ~30 MB on disk for 5422 512-dim
vectors.

Model lives in the HuggingFace Hub cache
(`~/.cache/huggingface/hub/`); auto-downloaded on first use, shareable
with other tools.

## Multilingual models

Default `bge-small-zh-v1.5` is CN-native; swap via `config.toml`
`[embedder]` (or env) for English (`bge-small-en-v1.5`, 384d) or
multilingual (`paraphrase-multilingual-MiniLM-L12-v2`, 384d). ⚠️
Switching models requires re-initializing the DB (vector dim is baked
into schema).

## Test coverage

```
$ python3 -m pytest tests/ -q
# 738 passed, 1 skipped (~210s)  [2026-08]
```

51 test files covering: core CRUD/recall, memory_type classifier
(双语/繁简), L2 hygiene/watermark/atomicity, audit undo, digest,
search-index backends, Claude Code hook, schema consistency.
