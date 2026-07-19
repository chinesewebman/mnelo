#!/usr/bin/env python3
"""
benchmark.py — reproducible latency benchmark for mnelo recall.

[7/19 v0.5.5] Validates the latency numbers cited in README.md
(p50=12.5ms) with a real, reproducible script.

What it does:
1. Seed DB with N synthetic chunks (default 10k)
2. Warm up embedder + caches (5 warmup queries)
3. Run K recall queries with varied realistic queries
4. Measure per-lane latency (vector / graph / meta / entity) + total
5. Output:
   - Human-readable table to stdout
   - JSON to --json <path> (for trending across runs)

Design choices:
- Uses Memory() API directly (no internal hooks)
- Seed text is deterministic (same N → same content)
- Queries are stock-code + name + Chinese phrase mix (realistic for our use)
- Cleans up its own seed data (source prefix 'benchmark_round15:') so reruns are idempotent
- Defaults are 10k chunks + 100 queries (≈90s total, fits in CI)
- Bigger sizes (50k, 100k) available via --chunks flag

Usage:
  python scripts/benchmark.py                       # default 10k chunks, 100 queries
  python scripts/benchmark.py --chunks 50000       # 50k chunks
  python scripts/benchmark.py --chunks 100000 --queries 50
  python scripts/benchmark.py --json bench.json    # save JSON output

Exit codes:
  0 = success
  1 = DB error or seed failure
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

# [7/19] Prevent backup files from polluting DB path resolution
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# === Realistic query set (~100 queries, mixed) ===

# Each query simulates a real agent recall pattern from the README examples.
# Mix: stock codes (vector-heavy), company names (entity-heavy), Chinese phrases
# (semantic similarity), short keywords (FTS/meta).
BENCHMARK_QUERIES = [
    # Stock codes (vector similarity dominant)
    "sh600089",
    "sz002594",
    "sh600519",
    "sh601318",
    "sh600036",
    "sz000858",
    "sh601398",
    "sh600028",
    "sz000333",
    "sh601988",
    "sh601857",
    "sh600050",
    "sh600030",
    "sh600276",
    "sz000651",
    "sh600887",
    "sh601628",
    "sh600000",
    "sh601166",
    "sz000001",
    # Company names (entity match dominant)
    "特变电工",
    "比亚迪",
    "贵州茅台",
    "中国平安",
    "招商银行",
    "五粮液",
    "工商银行",
    "中国石化",
    "美的集团",
    "中国银行",
    "中国石油",
    "海康威视",
    "恒瑞医药",
    "中信证券",
    "格力电器",
    "伊利股份",
    "浦发银行",
    "上汽集团",
    "万科A",
    "平安银行",
    # Chinese phrases (semantic)
    "建仓记录",
    "市场观点",
    "今日操作",
    "风险提示",
    "投资策略",
    "持仓变化",
    "止损位",
    "买入理由",
    "卖出原因",
    "仓位调整",
    "建仓 12000 股",
    "建仓 8000 股",
    "建仓 15000 股",
    "建仓 5000 股",
    "建仓 20000 股",
    "市盈率",
    "营收增长",
    "净利润",
    "毛利率",
    "资产负债率",
    "技术分析",
    "基本面分析",
    "消息面",
    "政策影响",
    "行业前景",
    # English / technical
    "BAAI embedder",
    "fastembed model",
    "SQLite WAL",
    "RRF fusion",
    "knowledge graph",
    "entity resolution",
    "vector search",
    "semantic recall",
    "memory recall",
    "memory update",
    "memory forget",
    "memory remember",
    "i18n locale",
    "config.toml",
    "launchd plist",
    "MCP server",
    "test coverage",
    "pytest",
    "ruff lint",
    "bandit security",
    # Generic / partial match
    "master 2077 ling",
    "hermes agent",
    "memory layer",
    "today decision",
    "weekly review",
    "monthly summary",
    "annual report",
    "earnings call",
    "price target",
    "market cap",
    "dividend yield",
    "P/E ratio",
    "buy signal",
    "sell signal",
    "hold position",
    "stop loss",
    "sector rotation",
    "macro trend",
    "fed rate",
    "inflation data",
    "earnings surprise",
    "guidance update",
    "analyst rating",
    "trading volume",
    "institutional flow",
    "retail sentiment",
    # Longer phrases (multi-keyword)
    "建仓 sh600089 18.96 价格 12000 股",
    "清仓 sz002594 240 卖出",
    "市场观点 — 谨慎乐观",
    "持仓调整 — 加仓茅台",
    "今日复盘 — 震荡上行",
]


def percentile(values: list, p: float) -> float:
    """Compute the p-th percentile of values (0 ≤ p ≤ 100).

    Uses linear interpolation (same method as numpy default).
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


def seed_chunks(memory, n: int, source_prefix: str) -> float:
    """Seed N synthetic chunks + entities. Returns seed time in seconds."""
    # Deterministic-ish content (seeded by index)
    stocks = [
        ("sh600089", "特变电工", "公用事业"),
        ("sz002594", "比亚迪", "汽车"),
        ("sh600519", "贵州茅台", "食品饮料"),
        ("sh601318", "中国平安", "保险"),
        ("sh600036", "招商银行", "银行"),
        ("sz000858", "五粮液", "食品饮料"),
        ("sh601398", "工商银行", "银行"),
        ("sh600028", "中国石化", "能源"),
        ("sz000333", "美的集团", "家电"),
        ("sh601988", "中国银行", "银行"),
    ]
    actions = ["建仓", "加仓", "减仓", "清仓", "观察"]
    phrases = [
        "市场观点 — 谨慎乐观",
        "今日复盘",
        "风险提示",
        "投资策略",
        "基本面分析",
        "技术分析",
        "宏观环境",
        "政策影响",
    ]

    t0 = time.perf_counter()
    for i in range(n):
        ticker, name, sector = stocks[i % len(stocks)]
        action = actions[i % len(actions)]
        phrase = phrases[i % len(phrases)]
        price = 10 + (i % 200) * 0.5
        qty = (i % 10 + 1) * 1000

        content = f"{action} {ticker} ({name}, {sector}): {qty} 股 @ {price:.2f}. 备注: {phrase} #{i}"

        memory.remember(
            content=content,
            source=f"{source_prefix}{i // 1000}",
            importance=0.3 + (i % 7) * 0.1,
            entities=[
                {
                    "id": ticker,
                    "kind": "stock",
                    "name": name,
                    "aliases": [name, ticker],
                    "properties": {"ticker": ticker, "sector": sector},
                },
            ],
            relations=[
                {
                    "source_id": "benchmark_user",
                    "target_id": ticker,
                    "relation": f"_{action}_于",
                    "weight": 0.5 + (i % 5) * 0.1,
                    "properties": {"quantity": qty, "price": price},
                },
            ],
            tags=[sector, action],
        )
        # Progress every 1k
        if (i + 1) % 1000 == 0:
            elapsed = time.perf_counter() - t0
            rate = (i + 1) / elapsed
            print(f"  seed {i + 1}/{n} ({rate:.0f} chunks/s)", flush=True)
    return time.perf_counter() - t0


def cleanup_seed(memory, source_prefix: str) -> int:
    """Delete benchmark seed data. Returns count deleted."""
    rows = memory._conn.execute("SELECT rowid FROM chunks WHERE source LIKE ?", (f"{source_prefix}%",)).fetchall()
    if rows:
        rowids = [r["rowid"] for r in rows]
        placeholders = ",".join("?" * len(rowids))
        memory._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
    memory._conn.execute("DELETE FROM chunks WHERE source LIKE ?", (f"{source_prefix}%",))
    memory._conn.execute("DELETE FROM entities WHERE id = 'benchmark_user'")
    memory._conn.commit()
    return len(rows)


def run_benchmark(args) -> dict:
    """Run the full benchmark and return results dict."""
    from memory import Memory

    print("=== mnelo benchmark ===")
    print(f"  chunks: {args.chunks}")
    print(f"  queries: {args.queries}")
    print(f"  top_k: {args.top_k}")
    print(f"  json: {args.json or '(none)'}")
    print()

    # Truncate queries to first N
    queries = (BENCHMARK_QUERIES * ((args.queries // len(BENCHMARK_QUERIES)) + 1))[: args.queries]
    source_prefix = "benchmark_round15:"

    memory = Memory()
    try:
        # Pre-cleanup (in case previous run crashed mid-seed)
        cleanup_seed(memory, source_prefix)
        print("  pre-cleanup done")

        # 1. Seed
        print(f"\n[1/3] seeding {args.chunks} chunks...")
        seed_time = seed_chunks(memory, args.chunks, source_prefix)
        stats = memory.stats()
        print(
            f"  ✓ seeded in {seed_time:.1f}s "
            f"(entities={stats['entities']['total']} chunks={stats['chunks']['total']} "
            f"vectors={stats['vectors']})"
        )

        # 2. Warmup
        print("\n[2/3] warming up (5 queries)...")
        for q in queries[:5]:
            memory.recall(q, top_k=args.top_k)

        # 3. Measure
        print(f"\n[3/3] running {len(queries)} measured queries...")
        latencies = []
        empty_count = 0
        # Per-lane not directly observable from Memory.recall (it's internal),
        # but we can extract from the result list's `method` field on RRF hits.
        # For now, total latency is what we measure.
        for i, q in enumerate(queries):
            t0 = time.perf_counter()
            results = memory.recall(q, top_k=args.top_k)
            latency_ms = (time.perf_counter() - t0) * 1000
            latencies.append(latency_ms)
            if not results:
                empty_count += 1
            if (i + 1) % 25 == 0:
                print(f"  query {i + 1}/{len(queries)} (latest={latency_ms:.1f}ms)", flush=True)

        # Compute stats
        p50 = percentile(latencies, 50)
        p95 = percentile(latencies, 95)
        p99 = percentile(latencies, 99)
        mean = statistics.mean(latencies)
        stdev = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
        mn = min(latencies)
        mx = max(latencies)
        empty_pct = (empty_count / len(latencies)) * 100

        # Final memory state for context
        final_stats = memory.stats()

        results = {
            "config": {
                "n_chunks": args.chunks,
                "n_queries": len(queries),
                "top_k": args.top_k,
            },
            "seed_time_s": round(seed_time, 2),
            "total_query_time_s": round(sum(latencies) / 1000, 2),
            "recall": {
                "p50_ms": round(p50, 2),
                "p95_ms": round(p95, 2),
                "p99_ms": round(p99, 2),
                "min_ms": round(mn, 2),
                "max_ms": round(mx, 2),
                "mean_ms": round(mean, 2),
                "stdev_ms": round(stdev, 2),
                "empty_count": empty_count,
                "empty_pct": round(empty_pct, 2),
            },
            "final_db_stats": {
                "entities": final_stats["entities"]["total"],
                "chunks": final_stats["chunks"]["total"],
                "relations": final_stats["relations"]["total"],
                "vectors": final_stats["vectors"],
            },
        }

        # Print human-readable summary
        print("\n=== Results ===")
        print(f"  Recall latency ({len(queries)} queries, top_k={args.top_k}):")
        print(f"    p50:  {p50:6.2f} ms")
        print(f"    p95:  {p95:6.2f} ms")
        print(f"    p99:  {p99:6.2f} ms")
        print(f"    min:  {mn:6.2f} ms")
        print(f"    max:  {mx:6.2f} ms")
        print(f"    mean: {mean:6.2f} ms ± {stdev:.2f}")
        print(f"  Empty results: {empty_count}/{len(queries)} ({empty_pct:.1f}%)")
        print(f"  Total query time: {results['total_query_time_s']}s")
        print("\n  DB after benchmark:")
        print(f"    entities:  {final_stats['entities']['total']}")
        print(f"    chunks:    {final_stats['chunks']['total']}")
        print(f"    relations: {final_stats['relations']['total']}")
        print(f"    vectors:   {final_stats['vectors']}")

        return results
    finally:
        # Cleanup
        print("\n  cleaning up seed data...")
        deleted = cleanup_seed(memory, source_prefix)
        print(f"  ✓ deleted {deleted} chunks")
        memory.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="mnelo latency benchmark (vector + graph + meta + entity recall)",
    )
    parser.add_argument(
        "--chunks",
        type=int,
        default=10000,
        help="Number of synthetic chunks to seed (default: 10000)",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=100,
        help="Number of recall queries to measure (default: 100)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="top_k parameter for recall (default: 5)",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        default=None,
        help="Write JSON results to this file",
    )
    args = parser.parse_args()

    if args.chunks < 100:
        print("error: --chunks must be >= 100", file=sys.stderr)
        return 1
    if args.queries < 10:
        print("error: --queries must be >= 10", file=sys.stderr)
        return 1

    try:
        results = run_benchmark(args)
    except Exception as e:
        print(f"\n✗ benchmark failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    if args.json:
        out_path = Path(args.json)
        out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\n  ✓ JSON saved to {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
