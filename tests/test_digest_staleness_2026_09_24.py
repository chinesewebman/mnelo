"""[2026-09-24] digest 缓存 TTL 兜底测试.

背景: live 库的 digest 从 2026-08-11 冻结到 09-24 未重建 (期间新增 140 chunk).
根因: get_digest 只在 meta.digest_dirty=1 或 digest_chunk_id 缺失时重建, 而
_mark_digest_dirty 只在 identity_fact 或 (decision|episode 且 importance >= 0.8) 时触发;
日常写入 importance 恒为 0.5 → 缓存无限期存活.

本测试验证 _digest_expired 的 TTL 兜底: 超过 digest_max_age_hours 的缓存必须被重建.
"""
import json
from datetime import datetime, timedelta

import config
from memory import Memory


def _new_mem():
    mem = Memory()
    mem._conn.execute("DELETE FROM meta WHERE key IN ('digest_chunk_id', 'digest_dirty')")
    mem._conn.commit()
    return mem


def _backdate(mem, chunk_id, hours):
    """把 digest chunk 的 metadata.built_at 往前挪 hours 小时."""
    row = mem._conn.execute("SELECT metadata_json FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    meta = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    meta["built_at"] = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    mem._conn.execute(
        "UPDATE chunks SET metadata_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), chunk_id),
    )
    mem._conn.commit()


def test_fresh_digest_is_not_rebuilt():
    """未过期 + 不脏 → 返回同一 chunk (TTL 不能变成每次都重建)."""
    mem = _new_mem()
    try:
        first = mem.get_digest()
        assert first["chunk_id"]
        assert mem.get_digest()["chunk_id"] == first["chunk_id"]
    finally:
        mem.close()


def test_stale_digest_is_rebuilt_even_when_not_dirty():
    """核心回归: dirty=0 但 built_at 超过 TTL → 必须重建出新 chunk."""
    mem = _new_mem()
    try:
        first = mem.get_digest()
        cid = first["chunk_id"]
        assert mem._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()["value"] == "0"

        _backdate(mem, cid, config.config.digest_max_age_hours + 1)

        second = mem.get_digest()
        assert second["chunk_id"] != cid, "超过 TTL 的缓存未被重建"
        # 旧 chunk 应被标 superseded
        old = mem._conn.execute("SELECT valid_until FROM chunks WHERE id = ?", (cid,)).fetchone()
        assert old["valid_until"] is not None
    finally:
        mem.close()


def test_ttl_zero_disables_staleness_check():
    """>0 未设时退化为纯 dirty 驱动 (max_age_hours<=0 = 关闭)."""
    mem = _new_mem()
    original = config.config.digest_max_age_hours
    try:
        first = mem.get_digest()
        cid = first["chunk_id"]
        _backdate(mem, cid, 24 * 365)

        config.config.digest_max_age_hours = 0
        assert mem.get_digest()["chunk_id"] == cid, "TTL=0 时不应因过期而重建"
    finally:
        config.config.digest_max_age_hours = original
        mem.close()


def test_unreadable_built_at_fails_open_to_rebuild():
    """built_at 缺失/损坏 → 当作已过期 (宁可多重建一次, 不给陈旧数据)."""
    mem = _new_mem()
    try:
        cid = mem.get_digest()["chunk_id"]
        mem._conn.execute(
            "UPDATE chunks SET metadata_json = ? WHERE id = ?",
            (json.dumps({"line_refs": {}}, ensure_ascii=False), cid),
        )
        mem._conn.commit()
        assert mem.get_digest()["chunk_id"] != cid
    finally:
        mem.close()
