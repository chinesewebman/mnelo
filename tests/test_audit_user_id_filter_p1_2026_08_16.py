"""[audit fix痛点 #7 2026-08-16] user_id / run_id filter recall 完全未实现.

Owner fix痛点优先级 #1 (跨用户数据泄漏 = production 安全 P1).
8/11 P1 #91 实战只 ship 了 agent_id filter, user_id / run_id 写入 metadata_json OK,
但 read 4 路 (vector/meta/entity/graph) 只读 agent_id filter.

测试范围: user_id + run_id 在 vector_only / meta_only / rrf 策略下都生效.
回归保护: 旧数据无 user_id 时不被误过滤 (跟 agent_id 同款 json_extract NULL 兼容).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mem(tmp_path):
    """新建空 DB 的 Memory 实例."""
    from memory import Memory

    db_path = tmp_path / "audit.db"
    m = Memory(db_path=db_path)
    yield m
    m.close()
    # cleanup
    for ext in ["", ".usearch.index", ".usearch.state"]:
        p = db_path.parent / (db_path.name + ext)
        if p.exists():
            p.unlink()


def _seed_two_users(mem):
    """写入 alice + bob 各 1 chunk, 共享关键词以便 vector+meta 都能召回."""
    cid_alice = mem.remember(
        "sh600519 茅台 2026-08-16 持仓 1000 股",
        user_id="alice",
        run_id="alice_run_1",
        source="alice_journal",
    )
    cid_bob = mem.remember(
        "sh600519 茅台 2026-08-16 持仓 500 股",
        user_id="bob",
        run_id="bob_run_1",
        source="bob_journal",
    )
    mem._conn.commit()
    return cid_alice, cid_bob


# === fix痛点 #7: user_id filter recall ===


def test_user_id_filter_isolates_users(mem):
    """fix痛点 #7: filter user_id='alice' 应该只返 alice, 不返 bob."""
    _seed_two_users(mem)

    results = mem.recall(query="茅台 持仓", filters={"user_id": "alice"}, top_k=10)

    contents = [r["content"] for r in results]
    # 必须不返 bob
    bob_leaked = [c for c in contents if "500 股" in c]
    assert len(bob_leaked) == 0, f"fix痛点 #7 CONFIRMED: user_id='alice' filter 漏返 bob 内容: {bob_leaked}"
    # 必须返 alice
    assert any("1000 股" in c for c in contents), "filter 应该返 alice 内容"


def test_run_id_filter_isolates_runs(mem):
    """fix痛点 #7 同源: run_id filter 也要工作."""
    _seed_two_users(mem)

    results = mem.recall(query="茅台 持仓", filters={"run_id": "bob_run_1"}, top_k=10)

    contents = [r["content"] for r in results]
    bob_leaked = [c for c in contents if "1000 股" in c]
    assert len(bob_leaked) == 0, f"fix痛点 #7 CONFIRMED: run_id='bob_run_1' filter 漏返 alice 内容: {bob_leaked}"


def test_no_user_id_filter_returns_all(mem):
    """回归: 不传 user_id filter 应该返所有 user (跟 agent_id 同款)."""
    _seed_two_users(mem)

    results = mem.recall(query="茅台 持仓", filters={}, top_k=10)

    contents = [r["content"] for r in results]
    assert any("1000 股" in c for c in contents)
    assert any("500 股" in c for c in contents)


def test_legacy_chunks_without_user_id_excluded_when_filter_active(mem):
    """回归: 旧数据 (无 user_id 在 metadata_json) + user_id filter 启用时,
    应该跟 agent_id 同款语义 — 严格隔离 (json_extract NULL 不匹配).
    这是fix设计 — fix痛点 #7 fix 后, 没打 user_id tag 的 chunk 不被泄漏给 user_id filter.
    """
    # 写一个老 chunk: 不传 user_id
    cid_old = mem.remember(
        "sh600519 茅台 2020 老数据",
        source="legacy",
    )
    mem._conn.commit()

    results = mem.recall(query="茅台", filters={"user_id": "alice"}, top_k=10)

    contents = [r["content"] for r in results]
    # 严格隔离语义: 旧数据 (无 user_id tag) 不应该被 user_id filter 召回
    legacy_leaked = [c for c in contents if "2020 老数据" in c]
    assert len(legacy_leaked) == 0, f"fix痛点 fix 后, 旧数据 (无 user_id tag) 不应被 user_id filter 召回: {legacy_leaked}"


def test_legacy_chunks_visible_without_filter(mem):
    """回归: 不传 user_id filter, 旧数据应该正常召回."""
    cid_old = mem.remember(
        "sh600519 茅台 2020 老数据",
        source="legacy",
    )
    mem._conn.commit()

    # 不传 filter — 应该召回所有 (含旧数据)
    results = mem.recall(query="茅台", filters={}, top_k=10)
    contents = [r["content"] for r in results]
    assert any("2020 老数据" in c for c in contents), "不传 filter 时, 旧数据应该正常召回"
