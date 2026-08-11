"""benchmarks.locomo — LoCoMo-style end-to-end recall quality smoke test.

[8/11 P3-benchmarks] LoCoMo（Long Conversation Memory）是 2026 年 LLM agent 召回
质量的事实标准 benchmark 之一（mem0 时间推理 blog 引用 LoCoMo +9.1 pts）。mnelo
目前没有完整 LoCoMo 数据集接入，但提供一个可跑的 smoke harness：

  - 写入 2 段有意重叠的对话 chunks，模拟「多 session 提到同一主题」
  - 跑 recall 拿 top-k
  - 验证至少一条覆盖所有主题（粗粒度 F1-style metric）

完整 LoCoMo 数据集接入 / mnelo graph-aware scoring 留作 P3 之后的延后工作，
因为:
  1. LoCoMo 10-conversation dataset 50MB+ 直接拉取会拖慢 CI
  2. mnelo 真正的质量优势（graph relation）需要写专门 scorer
  3. 单测不是 PR 阻塞项；后续 P 优先

用法:
  python -m benchmarks locomo                     # 默认 50 chunks / 10 queries
  python -m benchmarks locomo --chunks 200 --json locomo.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import sys
import time
from pathlib import Path

logger = logging.getLogger("mnelo")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# Anchor topics are the basis for coverage metric. Each scenario is a small
# conversation about ONE topic with deliberate cross-references so recall has
# to fuse (vector + entity + meta) lanes to find all relevant chunks.
#
# Note: entity IDs must be ASCII ([a-zA-Z0-9_:.-]{1,256}); we use a slug derived
# from the topic so the relation/recall flow can still group chunks by topic.
import hashlib


def _slug(topic: str) -> str:
    """ASCII-safe slug derived from a CJK topic. Stable across runs."""
    h = hashlib.md5(topic.encode("utf-8")).hexdigest()[:8]
    return f"scn_{h}"


LOCOMO_SCENARIOS = [
    {
        "topic": "光伏装机",
        "chunks": [
            "2026 Q1 全国光伏新增装机 23.5 GW，同比增长 18%。",
            "分布式光伏占比 52%，首次超过集中式。",
            "光伏组件价格周环比 -2.3%，硅料库存仍在高位。",
        ],
        "queries": ["光伏新增装机", "分布式光伏占比", "组件价格"],
        # expected: at least 1 hit per query that mentions the topic key
        "topic_keys": ["光伏", "装机"],
    },
    {
        "topic": "美联储利率",
        "chunks": [
            "美联储 6 月按兵不动，基准利率维持 5.25-5.50%。",
            "鲍威尔暗示 2026 年可能降息 2 次。",
            "点阵图显示中位数预期 2026 末 4.875%。",
        ],
        "queries": ["美联储", "降息", "利率决议"],
        "topic_keys": ["美联储", "利率"],
    },
    {
        "topic": "比亚迪销量",
        "chunks": [
            "比亚迪 2026 年 7 月新能源车销量 32.4 万辆，同比 +12%。",
            "插混占新能源销量 48%，纯电 52%。",
            "海外出口 4.8 万辆，巴西 / 泰国市场增速最快。",
        ],
        "queries": ["比亚迪 销量", "新能源车", "海外出口"],
        "topic_keys": ["比亚迪", "新能源"],
    },
]


def _seed_scenario(memory, scenario: dict, source_prefix: str) -> None:
    """Write a scenario's chunks into memory with deliberate entity/relation links."""
    topic = scenario["topic"]
    topic_slug = _slug(topic)
    for i, content in enumerate(scenario["chunks"]):
        memory.remember(
            content=content,
            source=f"{source_prefix}{topic_slug}_{i}",
            importance=0.6,
            entities=[
                {
                    "id": f"locomo:topic:{topic_slug}",
                    "kind": "topic",
                    "name": topic,
                    "aliases": [topic, topic_slug],
                    "properties": {"scenario": topic},
                },
            ],
            tags=[topic_slug, "locomo"],
        )


def _cleanup_scenario(memory, source_prefix: str) -> int:
    """Remove all locomo-prefixed chunks. Returns count deleted."""
    rows = memory._conn.execute(
        "SELECT rowid FROM chunks WHERE source LIKE ?", (f"{source_prefix}%",)
    ).fetchall()
    if rows:
        rowids = [r["rowid"] for r in rows]
        placeholders = ",".join("?" * len(rowids))
        try:
            memory._conn.execute(f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids)
        except sqlite3.OperationalError:
            pass  # vec0 不可用
        memory._conn.execute(f"DELETE FROM chunks WHERE rowid IN ({placeholders})", rowids)
        memory._conn.execute("DELETE FROM entities WHERE id LIKE 'locomo:topic:%'")
    memory._conn.commit()
    return len(rows)


def recall_coverage(memory, queries: list[str], topic_keys: list[str], top_k: int = 5) -> float:
    """Fraction of queries that produce at least one top-k hit mentioning any topic_key.

    Returns 0.0–1.0. Used as a coarse coverage metric.
    """
    if not queries:
        return 0.0
    hits = 0
    for q in queries:
        results = memory.recall(q, top_k=top_k)
        if any(any(key in (r.get("content") or "") for key in topic_keys) for r in results):
            hits += 1
    return hits / len(queries)


def run_locomo(args) -> dict:
    """Run the locomo smoke benchmark."""
    from memory import Memory

    print("=== mnelo locomo (smoke) ===")
    print(f"  scenarios: {len(LOCOMO_SCENARIOS)}")
    print(f"  top_k: {args.top_k}")
    print(f"  json: {args.json or '(none)'}")
    print()

    source_prefix = "locomo_round15:"
    memory = Memory()
    try:
        _cleanup_scenario(memory, source_prefix)
        print("  pre-cleanup done")

        # 1. Seed all scenarios
        print(f"\n[1/3] seeding {len(LOCOMO_SCENARIOS)} scenarios...")
        t0 = time.perf_counter()
        for scenario in LOCOMO_SCENARIOS:
            _seed_scenario(memory, scenario, source_prefix)
        seed_time = time.perf_counter() - t0
        n_chunks = sum(len(s["chunks"]) for s in LOCOMO_SCENARIOS)
        print(f"  ✓ seeded {n_chunks} chunks in {seed_time:.2f}s")

        # 2. Coverage per scenario
        print("\n[2/3] measuring recall coverage per scenario...")
        per_scenario = {}
        for scenario in LOCOMO_SCENARIOS:
            cov = recall_coverage(
                memory, scenario["queries"], scenario["topic_keys"], top_k=args.top_k
            )
            per_scenario[scenario["topic"]] = round(cov, 4)
            print(f"    {scenario['topic']}: coverage={cov:.2f}")

        mean_coverage = statistics.mean(per_scenario.values()) if per_scenario else 0.0
        print(f"  ✓ mean coverage: {mean_coverage:.2f}")

        # 3. Latency (cold)
        print("\n[3/3] measuring per-query latency (cold, per scenario)...")
        latencies = []
        for scenario in LOCOMO_SCENARIOS:
            for q in scenario["queries"]:
                t0 = time.perf_counter()
                memory.recall(q, top_k=args.top_k)
                latencies.append((time.perf_counter() - t0) * 1000)

        p50 = statistics.median(latencies) if latencies else 0.0
        mean = statistics.mean(latencies) if latencies else 0.0

        final_stats = memory.stats()

        results = {
            "config": {
                "n_scenarios": len(LOCOMO_SCENARIOS),
                "n_chunks": n_chunks,
                "top_k": args.top_k,
            },
            "coverage": {
                "per_scenario": per_scenario,
                "mean": round(mean_coverage, 4),
            },
            "latency_ms": {
                "p50": round(p50, 2),
                "mean": round(mean, 2),
                "n": len(latencies),
            },
            "final_db_stats": {
                "entities": final_stats["entities"]["total"],
                "chunks": final_stats["chunks"]["total"],
                "relations": final_stats["relations"]["total"],
                "vectors": final_stats["vectors"],
            },
        }

        print("\n=== Results ===")
        print(f"  Mean coverage: {mean_coverage:.2f}")
        print(f"  Latency p50:   {p50:.2f} ms")
        print(f"  Latency mean:  {mean:.2f} ms")
        return results
    finally:
        print("\n  cleaning up locomo data...")
        deleted = _cleanup_scenario(memory, source_prefix)
        print(f"  ✓ deleted {deleted} chunks")
        memory.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks locomo",
        description="LoCoMo-style end-to-end recall coverage + latency smoke",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        results = run_locomo(args)
    except Exception as e:
        print(f"\n✗ locomo benchmark failed: {e}", file=sys.stderr)
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
