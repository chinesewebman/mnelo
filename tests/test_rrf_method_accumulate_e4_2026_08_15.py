"""[8/15 E-4] RRF 融合必须 accumulate methods, 不能 lane 覆盖 (DESIGN §1.2 #4).

主人 DESIGN §1.2 #4 真痛点: _rrf_fuse line 1448 `rrf_hits[cid] = h` 直接覆盖,
不积累 methods. 同 chunk_id 在 4 路召回都命中时, recall_details_json.method
只记最后遍历的 entity 路, 污染 E-3 memory_recall_stats 的 method 分布数据.

修复: _rrf_fuse 保留第一路 hit + 维护 methods list (顺序按 hit_lists 遍历序),
返回的 hit dict 新增 'methods' 字段 (list[str]). _log_recall 写
recall_details_json 用 'methods' 替代 'method'. E-3 recall_stats 按 methods
展开 (一条 hit 计入多个 method, 与 RRF 实际行为一致).

[测试矩阵]
  1. 同 chunk 跨 4 路 → methods 列表含所有 4 个 method
  2. 同 chunk 跨 2 路 (vector+graph) → methods = [vector, graph]
  3. 单 chunk 单路 → methods = [vector] (单元素 list, 不退化)
  4. 不同 chunk 各自单路 → methods 各自正确
  5. _log_recall 写入 recall_details_json 用 methods list
  6. E-3 recall_stats 多 method hit 在聚合时各 method hit_count +1
"""

import importlib.util as _ilu
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _load_from_repo(mod_name: str):
    target = str(_REPO / f"{mod_name}.py")
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "__file__", None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_validation_repo = _load_from_repo("validation")
_memory_repo = _load_from_repo("memory")
_memory_repo.ValidationError = _validation_repo.ValidationError  # type: ignore[attr-defined]


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """Fresh REPO Memory with tmp_path db + usearch backend."""
    import config as _cfg_mod

    monkeypatch.setattr(_cfg_mod.config, "search_backend", "usearch", raising=True)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(_cfg_mod.config, "db_path", db_path, raising=False)

    schema_path = _REPO / "schema.sql"
    import sqlite3 as _sqlite
    import re

    conn = _sqlite.connect(str(db_path))
    sql = schema_path.read_text()
    sql = re.sub(r"PRAGMA[^;]*;", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"INSTALL[^;]*;", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"LOAD[^;]*;", "", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"CREATE VIRTUAL TABLE[^;]*USING vec0[^)]*\)",
        "",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    try:
        conn.executescript(sql)
    except Exception as e:
        if "already exists" not in str(e):
            raise
    conn.commit()
    conn.close()

    m = _memory_repo.Memory(db_path=db_path)
    yield m
    try:
        m._conn.execute("DELETE FROM recall_log")
        m._conn.commit()
    finally:
        m.close()


class TestRRFMethodAccumulate:
    """[8/15 E-4] _rrf_fuse 必须 accumulate methods (不 lane 覆盖)."""

    def test_chunk_in_all_4_lanes_has_all_methods(self, mem):
        """[E-4.1] 同 chunk 跨 4 路 → methods = [vector, graph, meta, entity]."""
        chunk_id = "chunk_shared_4lanes"
        vector_hits = [{"chunk_id": chunk_id, "method": "vector", "distance": 0.2}]
        graph_hits = [{"chunk_id": chunk_id, "method": "graph"}]
        meta_hits = [{"chunk_id": chunk_id, "method": "meta"}]
        entity_hits = [{"chunk_id": chunk_id, "method": "entity"}]
        result = mem._rrf_fuse([vector_hits, graph_hits, meta_hits, entity_hits], top_k=5)
        assert len(result) == 1
        hit = result[0]
        # 核心: methods 列表含所有参与的 method
        assert "methods" in hit, "RRF hit 必须含 methods 列表"
        assert set(hit["methods"]) == {"vector", "graph", "meta", "entity"}, f"methods 漏了, 实际 {hit['methods']}"
        # 顺序按 hit_lists 遍历序 (vector → graph → meta → entity)
        assert hit["methods"] == ["vector", "graph", "meta", "entity"], f"methods 顺序应按 hit_lists, 实际 {hit['methods']}"
        # backward compat: 单一 method 字段保留 = 第一路 (vector)
        assert hit["method"] == "vector", f"method 字段应保留第一路, 实际 {hit.get('method')}"

    def test_chunk_in_2_lanes_has_2_methods(self, mem):
        """[E-4.2] 同 chunk 跨 2 路 → methods = [vector, graph]."""
        chunk_id = "chunk_shared_2lanes"
        vector_hits = [{"chunk_id": chunk_id, "method": "vector"}]
        graph_hits = [{"chunk_id": chunk_id, "method": "graph"}]
        result = mem._rrf_fuse([vector_hits, graph_hits], top_k=5)
        assert len(result) == 1
        hit = result[0]
        assert hit["methods"] == ["vector", "graph"]
        assert hit["method"] == "vector"

    def test_single_lane_chunk_has_single_method(self, mem):
        """[E-4.3] 单 chunk 单路 → methods = [vector] (单元素 list)."""
        chunk_id = "chunk_single_lane"
        vector_hits = [{"chunk_id": chunk_id, "method": "vector"}]
        result = mem._rrf_fuse([vector_hits], top_k=5)
        assert len(result) == 1
        hit = result[0]
        assert hit["methods"] == ["vector"]
        # 单一 method 字段也保留
        assert hit["method"] == "vector"

    def test_distinct_chunks_each_single_lane(self, mem):
        """[E-4.4] 不同 chunk 各自单路 → methods 各自正确."""
        hits = [
            [{"chunk_id": "c1", "method": "vector"}],
            [{"chunk_id": "c2", "method": "graph"}],
        ]
        result = mem._rrf_fuse(hits, top_k=5)
        assert len(result) == 2
        by_cid = {r["chunk_id"]: r for r in result}
        assert by_cid["c1"]["methods"] == ["vector"]
        assert by_cid["c2"]["methods"] == ["graph"]

    def test_methods_dedup_within_same_lane(self, mem):
        """[E-4.5] 同 lane 内多次出现同 chunk → methods 不重复."""
        chunk_id = "chunk_dup_same_lane"
        # 同 lane 出现 3 次 (unusual but possible if upstream lists dup)
        vector_hits = [
            {"chunk_id": chunk_id, "method": "vector"},
            {"chunk_id": chunk_id, "method": "vector"},
            {"chunk_id": chunk_id, "method": "vector"},
        ]
        result = mem._rrf_fuse([vector_hits], top_k=5)
        assert len(result) == 1
        # methods 不应重复
        assert result[0]["methods"] == ["vector"]

    def test_log_recall_writes_methods_list(self, mem):
        """[E-4.6] _log_recall 写入 recall_details_json 用 methods 列表."""
        # 构造一条 RRF 融合结果: chunk 在 2 路命中
        chunk_id = "chunk_for_log_e4"
        # 模拟 _log_recall 期望的入参: results 列表, 每个 hit 已有 'methods' 字段
        results = [
            {
                "chunk_id": chunk_id,
                "method": "vector",  # backward compat
                "methods": ["vector", "graph"],  # new field
                "distance": 0.3,
                "rrf_score": 0.02,
                "importance": 0.5,
                "rank": 1,
            }
        ]
        mem._log_recall(
            query="e4 test query",
            results=results,
            hops=2,
            latency_ms=15.0,
        )
        # 验证 recall_log 写入
        row = mem._conn.execute("SELECT recall_details_json FROM recall_log ORDER BY id DESC LIMIT 1").fetchone()
        details = json.loads(row["recall_details_json"])
        assert len(details) == 1
        # 关键: recall_details_json 里应是 methods 列表, 不是单一 method
        assert "methods" in details[0], "recall_details 应含 methods 列表"
        assert details[0]["methods"] == ["vector", "graph"]
        # backward compat: method 字段保留 = 第一路
        assert details[0]["method"] == "vector"


class TestRecallStatsMultiMethod:
    """[8/15 E-4] E-3 recall_stats 多 method hit 正确聚合."""

    def test_recall_stats_counts_multi_method_per_method(self, mem):
        """[E-4.7] 同 chunk 在 vector+graph 召回 → recall_stats 各 method +1."""
        # 直接构造 recall_details_json: 1 条 recall, 1 个 chunk methods=[vector, graph]
        mem._conn.execute(
            """
            INSERT INTO recall_log
                (query, results_json, graph_hops, latency_ms, created_at, recall_details_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "multi method query",
                json.dumps(["c1"]),
                2,
                10.0,
                "2026-08-15T06:00:00",
                json.dumps(
                    [
                        {
                            "rank": 1,
                            "chunk_id": "c1",
                            "method": "vector",
                            "methods": ["vector", "graph"],  # 2 路同时命中
                            "distance": 0.2,
                            "rrf_score": 0.02,
                            "importance": 0.5,
                        }
                    ]
                ),
            ),
        )
        mem._conn.commit()
        result = mem.recall_stats(days=30)
        # vector hit_count = 1 (这条 recall 的 vector 部分)
        # graph hit_count = 1 (这条 recall 的 graph 部分)
        # meta hit_count = 0
        # entity hit_count = 0
        assert result["methods"].get("vector", {}).get("hit_count") == 1
        assert result["methods"].get("graph", {}).get("hit_count") == 1
        assert "meta" not in result["methods"]
        assert "entity" not in result["methods"]
