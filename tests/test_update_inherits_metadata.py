"""[bug fix 2026-11-25] update() 必须继承老 chunk 的 metadata_json。

背景：家庭记忆系统接入 mnelo 后实测发现，按人召回（filters={"user_id": ...}）
对**更新过的记忆**完全失效。

根因：memory_core.update() 写新版本 chunk 时，metadata_json 写死成
    {"supersedes": old_id, "reason": reason}
不继承老 chunk 的 metadata —— agent_id / user_id / run_id / tags 全部丢失。
于是新版本对任何 scoping filter 都不匹配（SQL 侧
json_extract(metadata_json, '$.user_id') = ? 拿不到值），
记忆等于从召回里消失。

实测（修前）：
    remember(..., agent_id="family", user_id="爸爸") → metadata 完整
    update(...) → 新 chunk metadata = {"supersedes":..., "reason":"updated"}
    recall(filters={"agent_id":"family","user_id":"爸爸"}) → 0 条
    recall(filters={"agent_id":"family","user_id":"妈妈"}) → 0 条
    （两个都召不回 —— 记忆消失，且隔离语义无从谈起）

修后：继承老 metadata，再叠加 supersedes / reason。

这个 bug 之所以能活很久：update() 的 chunk 版本机制**没有任何测试覆盖**
（tests/ 里搜 update|supersede 只有 loop/task 相关的两个文件）。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def mem(tmp_path):
    """新建空 DB 的 Memory 实例。"""
    from memory import Memory

    db_path = tmp_path / "update_meta.db"
    m = Memory(db_path=db_path)
    yield m
    m.close()
    for ext in ("", ".usearch.index", ".usearch.state"):
        p = db_path.parent / (db_path.name + ext)
        if p.exists():
            p.unlink()


def test_update_inherits_scoping_metadata(mem):
    """update() 后的新 chunk 必须保留 agent_id / user_id / run_id。"""
    cid = mem.remember(
        "爸爸最近胃不太好，在吃胃药",
        source="family_test",
        agent_id="family",
        user_id="爸爸",
        run_id="run_1",
        tags=["健康"],
    )
    mem._conn.commit()

    new_id = mem.update(cid, reason="updated", new_content="爸爸最近胃好多了")

    row = mem._conn.execute(
        "SELECT metadata_json FROM chunks WHERE id = ?", (new_id,)
    ).fetchone()
    meta = json.loads(row["metadata_json"])

    # 继承的字段
    assert meta["agent_id"] == "family"
    assert meta["user_id"] == "爸爸"
    assert meta["run_id"] == "run_1"
    assert meta["tags"] == ["健康"]
    # 叠加的字段
    assert meta["supersedes"] == cid
    assert meta["reason"] == "updated"


def test_updated_chunk_still_visible_to_its_owner(mem):
    """修的核心场景：更新过的记忆，按 user_id 召回依然能看到。"""
    cid = mem.remember(
        "爸爸最近胃不太好，在吃胃药",
        source="family_test",
        agent_id="family",
        user_id="爸爸",
    )
    mem._conn.commit()
    mem.update(cid, reason="updated", new_content="爸爸最近胃好多了，不吃药了")
    mem._conn.commit()

    hits = mem.recall(
        query="胃 药",
        top_k=10,
        filters={"agent_id": "family", "user_id": "爸爸"},
    )
    contents = " | ".join(h["content"] for h in hits)
    assert "胃好多了" in contents, f"更新过的记忆对本人不可见: {contents}"


def test_updated_chunk_not_visible_to_other_user(mem):
    """同时不能破坏隔离：别人的 user_id 依然召不到。"""
    cid = mem.remember(
        "爸爸最近胃不太好，在吃胃药",
        source="family_test",
        agent_id="family",
        user_id="爸爸",
    )
    mem._conn.commit()
    mem.update(cid, reason="updated", new_content="爸爸最近胃好多了，不吃药了")
    mem._conn.commit()

    hits = mem.recall(
        query="胃 药",
        top_k=10,
        filters={"agent_id": "family", "user_id": "妈妈"},
    )
    contents = " | ".join(h["content"] for h in hits)
    assert "胃好多了" not in contents, f"更新过的记忆泄漏给其他 user: {contents}"


def test_update_without_prior_metadata_still_writes_supersedes(mem):
    """老 chunk 没有 metadata 时，update 仍要写 supersedes / reason（不崩）。"""
    cid = mem.remember("一条普通记忆，没有 scoping", source="plain")
    mem._conn.commit()
    # 手工把 metadata 清空，模拟旧数据
    mem._conn.execute("UPDATE chunks SET metadata_json = NULL WHERE id = ?", (cid,))
    mem._conn.commit()

    new_id = mem.update(cid, reason="updated", new_content="改了内容")

    row = mem._conn.execute(
        "SELECT metadata_json FROM chunks WHERE id = ?", (new_id,)
    ).fetchone()
    meta = json.loads(row["metadata_json"])
    assert meta["supersedes"] == cid
    assert meta["reason"] == "updated"
