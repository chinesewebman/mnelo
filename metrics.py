"""
metrics.py — mnelo in-memory metrics registry (Prometheus text format).

[7/19 v0.5.3] Lightweight Prometheus-compatible metrics:

Design:
- In-process only (no external Prometheus client lib)
- Threadsafe via threading.Lock (mnelo is single-writer but MCP serves
  concurrent reads; metrics scrape must not race with writers)
- Process-local counters/histograms (no cross-process aggregation —
  acceptable because mnelo runs as a single launchd-managed process)
- DB stats cached with TTL (10s) to avoid hammering SQLite on every scrape

Metric inventory:
- mnelo_recall_total{method="vector|graph|meta|entity"}  — counter
- mnelo_recall_latency_seconds{method, le=...}           — histogram
- mnelo_recall_hits_total{result="empty|non_empty"}      — counter
- mnelo_recall_top_k_total{k="1|3|5|10|20"}             — counter (param distribution)
- mnelo_remember_total{source="..."}                     — counter
- mnelo_forget_total{kind="chunk|entity|relation"}      — counter
- mnelo_relate_total                                     — counter
- mnelo_update_total                                     — counter
- mnelo_db_entities / chunks / relations / vectors      — gauge (TTL 10s)
- mnelo_db_size_bytes                                    — gauge (TTL 10s)
- mnelo_wal_pages_flushed_total                          — gauge
- mnelo_uptime_seconds                                   — gauge
- mnelo_process_rss_bytes                                — gauge

Latency histogram buckets (seconds, Prometheus-standard):
le="0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, +Inf"

Output format: Prometheus text exposition format
https://prometheus.io/docs/instrumenting/exposition_formats/#text-based-format
"""

import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

# Latency histogram buckets (seconds)
_LATENCY_BUCKETS: Tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)
_INF = float("inf")


class Counter:
    """Monotonic counter with optional labels.

    Usage:
        c = Counter('mnelo_recall_total', 'Total recall calls', labelnames=('method',))
        c.inc(method='vector')
        c.inc(method='graph', amount=5)
    """

    __slots__ = ("_name", "_help", "_labelnames", "_values", "_lock")

    def __init__(self, name: str, help: str, labelnames: Tuple[str, ...] = ()):
        self._name = name
        self._help = help
        self._labelnames = labelnames
        self._values: Dict[Tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def get(self, **labels) -> float:
        return self._values.get(self._key(labels), 0.0)

    def _key(self, labels: Dict[str, str]) -> Tuple[str, ...]:
        return tuple(labels.get(n, "") for n in self._labelnames)

    def render(self) -> List[str]:
        """Render in Prometheus text format."""
        lines = [
            f"# HELP {self._name} {self._help}",
            f"# TYPE {self._name} counter",
        ]
        with self._lock:
            if not self._values:
                lines.append(f"{self._name} 0")
                return lines
            for key, value in sorted(self._values.items()):
                if self._labelnames:
                    labels_str = ",".join(f'{n}="{v}"' for n, v in zip(self._labelnames, key))
                    lines.append(f"{self._name}{{{labels_str}}} {value}")
                else:
                    lines.append(f"{self._name} {value}")
        return lines


class Gauge:
    """Gauge metric with optional labels (can go up or down).

    Usage:
        g = Gauge('mnelo_uptime_seconds', 'Process uptime', labelnames=())
        g.set(123.45)
    """

    __slots__ = ("_name", "_help", "_labelnames", "_values", "_lock")

    def __init__(self, name: str, help: str, labelnames: Tuple[str, ...] = ()):
        self._name = name
        self._help = help
        self._labelnames = labelnames
        self._values: Dict[Tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, **labels) -> None:
        self.set(self.get(**labels) + amount, **labels)

    def get(self, **labels) -> float:
        return self._values.get(self._key(labels), 0.0)

    def _key(self, labels: Dict[str, str]) -> Tuple[str, ...]:
        return tuple(labels.get(n, "") for n in self._labelnames)

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self._name} {self._help}",
            f"# TYPE {self._name} gauge",
        ]
        with self._lock:
            if not self._values:
                lines.append(f"{self._name} 0")
                return lines
            for key, value in sorted(self._values.items()):
                if self._labelnames:
                    labels_str = ",".join(f'{n}="{v}"' for n, v in zip(self._labelnames, key))
                    lines.append(f"{self._name}{{{labels_str}}} {value}")
                else:
                    lines.append(f"{self._name} {value}")
        return lines


class Histogram:
    """Histogram with fixed bucket boundaries (Prometheus-style).

    Tracks _bucket{le="X"} + _bucket{le="+Inf"} + _sum + _count.

    Usage:
        h = Histogram('mnelo_recall_latency_seconds', 'Recall latency',
                      labelnames=('method',))
        h.observe(0.012, method='vector')
    """

    __slots__ = ("_name", "_help", "_labelnames", "_buckets", "_data", "_lock")

    def __init__(self, name: str, help: str, labelnames: Tuple[str, ...] = ()):
        self._name = name
        self._help = help
        self._labelnames = labelnames
        self._buckets = _LATENCY_BUCKETS
        # {labels_tuple: {'buckets': [count_per_le], 'sum': 0.0, 'count': 0}}
        self._data: Dict[Tuple[str, ...], Dict] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels) -> None:
        key = tuple(labels.get(n, "") for n in self._labelnames)
        with self._lock:
            if key not in self._data:
                self._data[key] = {
                    "buckets": [0] * (len(self._buckets) + 1),  # +1 for +Inf
                    "sum": 0.0,
                    "count": 0,
                }
            entry = self._data[key]
            entry["sum"] += value
            entry["count"] += 1
            for i, le in enumerate(self._buckets):
                if value <= le:
                    entry["buckets"][i] += 1
            entry["buckets"][-1] += 1  # +Inf bucket

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self._name} {self._help}",
            f"# TYPE {self._name} histogram",
        ]
        with self._lock:
            for key, entry in sorted(self._data.items()):
                label_str = ",".join(f'{n}="{v}"' for n, v in zip(self._labelnames, key)) if self._labelnames else ""
                # Render cumulative buckets
                for i, le in enumerate(self._buckets):
                    count = entry["buckets"][i]
                    if label_str:
                        lines.append(f'{self._name}_bucket{{{label_str},le="{le}"}} {count}')
                    else:
                        lines.append(f'{self._name}_bucket{{le="{le}"}} {count}')
                # +Inf bucket
                count_inf = entry["buckets"][-1]
                if label_str:
                    lines.append(f'{self._name}_bucket{{{label_str},le="+Inf"}} {count_inf}')
                else:
                    lines.append(f'{self._name}_bucket{{le="+Inf"}} {count_inf}')
                # sum + count
                if label_str:
                    lines.append(f"{self._name}_sum{{{label_str}}} {entry['sum']:.6f}")
                    lines.append(f"{self._name}_count{{{label_str}}} {entry['count']}")
                else:
                    lines.append(f"{self._name}_sum {entry['sum']:.6f}")
                    lines.append(f"{self._name}_count {entry['count']}")
        return lines


class Registry:
    """Process-local metrics registry.

    All mnelo metrics live here. Use `get_registry()` to get the singleton.
    """

    def __init__(self):
        self._start_time = time.time()
        self._lock = threading.Lock()

        # Recall metrics
        self.recall_total = Counter(
            "mnelo_recall_total",
            "Total recall calls broken down by lane method",
            labelnames=("method",),
        )
        self.recall_latency = Histogram(
            "mnelo_recall_latency_seconds",
            "Recall latency distribution by lane method (seconds)",
            labelnames=("method",),
        )
        self.recall_hits = Counter(
            "mnelo_recall_hits_total",
            "Recall result hit counts (empty = no results returned)",
            labelnames=("result",),
        )
        self.recall_top_k = Counter(
            "mnelo_recall_top_k_total",
            "Recall call counts by top_k parameter",
            labelnames=("k",),
        )

        # Write metrics
        self.remember_total = Counter(
            "mnelo_remember_total",
            "Total remember() calls by source",
            labelnames=("source",),
        )
        self.forget_total = Counter(
            "mnelo_forget_total",
            "Total forget() calls by target kind",
            labelnames=("kind",),
        )
        self.relate_total = Counter(
            "mnelo_relate_total",
            "Total relate() calls",
        )
        self.update_total = Counter(
            "mnelo_update_total",
            "Total update() calls",
        )

        # DB stats gauges (cached, TTL=10s)
        self.db_entities = Gauge(
            "mnelo_db_entities",
            "Current entity count (live)",
        )
        self.db_chunks = Gauge(
            "mnelo_db_chunks",
            "Current chunk count (live)",
        )
        self.db_relations = Gauge(
            "mnelo_db_relations",
            "Current relation count (live)",
        )
        self.db_vectors = Gauge(
            "mnelo_db_vectors",
            "Current vector count (live)",
        )
        self.db_size_bytes = Gauge(
            "mnelo_db_size_bytes",
            "SQLite db file size in bytes",
        )
        self.wal_pages_flushed = Gauge(
            "mnelo_wal_pages_flushed_total",
            "Total WAL pages flushed since startup",
        )

        # Process gauges
        self.uptime_seconds = Gauge(
            "mnelo_uptime_seconds",
            "Process uptime in seconds since startup",
        )
        self.process_rss_bytes = Gauge(
            "mnelo_process_rss_bytes",
            "Process resident set size in bytes (RSS)",
        )

        # DB cache state (TTL tracking)
        self._db_cache_time: float = 0.0
        self._db_cache_ttl: float = 10.0

    def update_uptime(self) -> None:
        """Update uptime + RSS gauges (call periodically)."""
        self.uptime_seconds.set(time.time() - self._start_time)
        # RSS via psutil if available, else /proc/self/statm or fallback
        try:
            import resource  # noqa: F401 — macOS doesn't expose /proc

            # macOS uses getrusage()
            import resource as _r

            usage = _r.getrusage(_r.RUSAGE_SELF)
            # ru_maxrss is KB on macOS, bytes on Linux
            if sys.platform == "darwin":
                self.process_rss_bytes.set(usage.ru_maxrss * 1024)
            else:
                self.process_rss_bytes.set(usage.ru_maxrss)
        except Exception:
            pass

    def refresh_db_stats(self, memory_instance) -> None:
        """Refresh DB gauges with TTL caching.

        Args:
            memory_instance: Memory object (used for stats() + WAL query).
        """
        now = time.time()
        with self._lock:
            if now - self._db_cache_time < self._db_cache_ttl:
                return  # cache still warm
            self._db_cache_time = now

        try:
            stats = memory_instance.stats()
            # stats() shape: {'entities': {'total', 'active', 'deleted'}, 'chunks': {...},
            #                 'relations': {...}, 'vectors': N, 'recall_log': N}
            entities_table = stats.get("entities", {})
            chunks_table = stats.get("chunks", {})
            relations_table = stats.get("relations", {})
            self.db_entities.set(entities_table.get("total", 0))
            self.db_chunks.set(chunks_table.get("total", 0))
            self.db_relations.set(relations_table.get("total", 0))
            self.db_vectors.set(stats.get("vectors", 0))
            # No db size in stats() — try fs path
            try:
                import os
                from pathlib import Path

                # Memory doesn't expose DB_PATH publicly; use ENV MNELO_MEMORY_CONFIG path
                cfg_path = os.environ.get("MNELO_MEMORY_CONFIG", "")
                if cfg_path:
                    db_path = Path(cfg_path).parent / "memory.db"
                    if db_path.exists():
                        self.db_size_bytes.set(db_path.stat().st_size)
            except Exception:
                pass
            # WAL pages not exposed in stats() yet — leave at 0 if absent
        except Exception:
            # Don't crash /metrics endpoint if stats() fails
            pass

    def render(self) -> str:
        """Render all metrics in Prometheus text exposition format.

        Returns:
            Multi-line string with metric exposition.
        """
        self.update_uptime()
        lines: List[str] = []
        # Order matters for human readability
        lines.extend(self.recall_total.render())
        lines.extend(self.recall_latency.render())
        lines.extend(self.recall_hits.render())
        lines.extend(self.recall_top_k.render())
        lines.extend(self.remember_total.render())
        lines.extend(self.forget_total.render())
        lines.extend(self.relate_total.render())
        lines.extend(self.update_total.render())
        lines.extend(self.db_entities.render())
        lines.extend(self.db_chunks.render())
        lines.extend(self.db_relations.render())
        lines.extend(self.db_vectors.render())
        lines.extend(self.db_size_bytes.render())
        lines.extend(self.wal_pages_flushed.render())
        lines.extend(self.uptime_seconds.render())
        lines.extend(self.process_rss_bytes.render())
        # Prometheus convention: end with newline
        return "\n".join(lines) + "\n"


# === Singleton accessor ===

_REGISTRY: Optional[Registry] = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> Registry:
    """Get the process-wide metrics registry singleton."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = Registry()
        return _REGISTRY


def reset_registry() -> None:
    """Reset registry (for testing only)."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = None
