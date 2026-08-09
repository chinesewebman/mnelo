import json
from datetime import datetime

import config
from memory import Memory


def _cleanup(mem, ids, entity_ids=()):
    # [8/6 plan §10] 后端感知清理 (helper 先 _index.remove 再 DELETE chunks)
    from helpers import cleanup_chunks
    cleanup_chunks(mem, chunk_ids=list(set(ids)))
    for eid in entity_ids:
        mem._conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
    mem._conn.execute("DELETE FROM meta WHERE key IN ('digest_chunk_id', 'digest_dirty')")
    mem._conn.commit()


def _new_mem():
    mem = Memory()
    mem._conn.execute("DELETE FROM meta WHERE key IN ('digest_chunk_id', 'digest_dirty')")
    mem._conn.commit()
    return mem


def test_identity_fact_appears_with_entity_ref():
    mem, ids = _new_mem(), []
    eid = "test_digest_identity"
    try:
        cid = mem.remember(
            "identity source",
            source="test_digest",
            importance=1.0,
            entities=[{"id": eid, "kind": "identity_fact", "name": "Digest Tester", "summary": "Digest Tester"}],
        )
        ids.append(cid)
        result = mem.get_digest()
        ids.append(result["chunk_id"])
        ref = next(k for k, v in result["line_refs"].items() if eid in v)
        assert "身份: Digest Tester" in result["content"]
        assert mem.get_digest(ref=ref)["source_chunks"][0]["id"] == eid
    finally:
        _cleanup(mem, ids, [eid])
        mem.close()


def test_high_importance_decision_marks_dirty_then_clears():
    mem, ids = _new_mem(), []
    try:
        cid = mem.remember("digest matrix decision", source="test_digest", importance=0.9, memory_type="decision")
        ids.append(cid)
        assert mem._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()["value"] == "1"
        result = mem.get_digest()
        ids.append(result["chunk_id"])
        assert "decision: digest matrix decision" in result["content"]
        assert mem._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()["value"] == "0"
    finally:
        _cleanup(mem, ids)
        mem.close()


def test_low_importance_fact_does_not_mark_dirty():
    mem, ids = _new_mem(), []
    try:
        cid = mem.remember("digest matrix ordinary fact", source="test_digest", importance=0.5, memory_type="fact")
        ids.append(cid)
        row = mem._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()
        assert row is None or row["value"] == "0"
    finally:
        _cleanup(mem, ids)
        mem.close()


def test_consecutive_rebuild_supersedes_old_digest():
    mem, ids = _new_mem(), []
    try:
        ids.append(mem.remember("digest rebuild one", source="test_digest", importance=1.0, memory_type="decision"))
        first = mem.get_digest()
        ids.append(first["chunk_id"])
        ids.append(mem.remember("digest rebuild two", source="test_digest", importance=1.0, memory_type="decision"))
        second = mem.get_digest()
        ids.append(second["chunk_id"])
        old = mem._conn.execute("SELECT valid_until, metadata_json FROM chunks WHERE id=?", (first["chunk_id"],)).fetchone()
        assert old["valid_until"] is not None
        assert json.loads(old["metadata_json"])["superseded_by"] == second["chunk_id"]
    finally:
        _cleanup(mem, ids)
        mem.close()


def test_get_digest_ref_returns_full_source_content():
    mem, ids = _new_mem(), []
    content = "digest full source " + "x" * 120
    try:
        cid = mem.remember(content, source="test_digest", importance=1.0, memory_type="decision")
        ids.append(cid)
        result = mem.get_digest()
        ids.append(result["chunk_id"])
        ref = next(k for k, v in result["line_refs"].items() if cid in v)
        assert mem.get_digest(ref=ref)["source_chunks"][0]["content"] == content
    finally:
        _cleanup(mem, ids)
        mem.close()


def test_truncated_digest_ref_still_expands_source():
    mem, ids = _new_mem(), []
    old_max = config.config.digest_max_chars
    config.config.digest_max_chars = 24
    try:
        cid = mem.remember("digest truncation original content", source="test_digest", importance=1.0, memory_type="decision")
        ids.append(cid)
        result = mem.get_digest()
        ids.append(result["chunk_id"])
        ref = next(k for k, v in result["line_refs"].items() if cid in v)
        assert result["truncated"] is True
        assert mem.get_digest(ref=ref)["source_chunks"][0]["content"] == "digest truncation original content"
    finally:
        config.config.digest_max_chars = old_max
        _cleanup(mem, ids)
        mem.close()


def test_update_marks_digest_dirty_and_rebuild_refreshes_pointer():
    mem, ids = _new_mem(), []
    try:
        old_id = mem.remember("digest pointer old content", source="test_digest", importance=1.0, memory_type="decision", timestamp=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        ids.append(old_id)
        first = mem.get_digest()
        ids.append(first["chunk_id"])
        old_ref = next(k for k, v in first["line_refs"].items() if old_id in v)
        new_id = mem.update(old_id, new_content="digest pointer new content")
        ids.append(new_id)
        assert mem._conn.execute("SELECT value FROM meta WHERE key='digest_dirty'").fetchone()["value"] == "1"
        second = mem.get_digest()
        ids.append(second["chunk_id"])
        assert any(new_id in refs for refs in second["line_refs"].values())
        assert all(old_id not in refs for refs in second["line_refs"].values())
        historical = mem._conn.execute("SELECT metadata_json FROM chunks WHERE id = ?", (first["chunk_id"],)).fetchone()
        assert old_id in json.loads(historical["metadata_json"])["line_refs"][old_ref]
    finally:
        _cleanup(mem, ids)
        mem.close()


def test_ttl_revert_sql_revives_chunk_and_cancels_purge():
    mem, ids = _new_mem(), []
    try:
        cid = mem.remember("digest ttl undo fixture", source="test_digest", memory_type="ephemeral")
        ids.append(cid)
        before = {"valid_until": None}
        after = {"valid_until": "2026-08-05T00:00:00"}
        revert_sql = (
            f"UPDATE chunks SET valid_until = NULL WHERE id = '{cid}'; "
            f"DELETE FROM purged_queue WHERE target_id = '{cid}' AND target_kind = 'chunk' AND done = 0"
        )
        assert mem._apply_ttl_soft_delete("test_digest_ttl", cid, "ephemeral", before, after, revert_sql, "2026-08-05T00:00:00")
        assert mem._conn.execute("SELECT COUNT(*) FROM purged_queue WHERE target_id=? AND done=0", (cid,)).fetchone()[0] == 1
        mem._conn.executescript(revert_sql)
        mem._conn.commit()
        assert mem._conn.execute("SELECT valid_until FROM chunks WHERE id=?", (cid,)).fetchone()["valid_until"] is None
        assert mem._conn.execute("SELECT COUNT(*) FROM purged_queue WHERE target_id=? AND done=0", (cid,)).fetchone()[0] == 0
    finally:
        mem._conn.execute("DELETE FROM audit_log WHERE run_id='test_digest_ttl'")
        _cleanup(mem, ids)
        mem.close()


def test_audit_undo_executes_multistatement_revert_and_appends_reverted_log():
    mem, ids = _new_mem(), []
    try:
        cid = mem.remember("undo integration fixture", source="test_digest", memory_type="ephemeral")
        ids.append(cid)
        before = {"valid_until": None}
        after = {"valid_until": "2026-08-05T00:00:00"}
        sql = f"UPDATE chunks SET valid_until = '2026-08-05T00:00:00' WHERE id = '{cid}';"
        mem._conn.execute("UPDATE chunks SET valid_until=? WHERE id=?", ("2026-08-05T00:00:00", cid))
        mem._conn.execute("INSERT INTO audit_log (run_id,pass_name,action_type,ref_type,ref_id,before_json,after_json,confidence,llm_used,status,created_at,revert_sql) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("test_undo", "hygiene", "ttl_soft_delete", "chunk", cid, json.dumps(before), json.dumps(after), 1.0, 0, "applied", "2026-08-05T00:00:00", f"UPDATE chunks SET valid_until=NULL WHERE id='{cid}'; DELETE FROM purged_queue WHERE target_id='{cid}' AND done=0;"))
        mem._conn.commit()
        result = mem.audit_undo(mem._conn.execute("SELECT max(id) FROM audit_log WHERE run_id='test_undo'").fetchone()[0])
        assert result["status"] == "reverted"
        assert mem._conn.execute("SELECT valid_until FROM chunks WHERE id=?", (cid,)).fetchone()["valid_until"] is None
        assert mem._conn.execute("SELECT count(*) FROM audit_log WHERE run_id='test_undo' AND status='reverted'").fetchone()[0] == 1
    finally:
        mem._conn.execute("DELETE FROM audit_log WHERE run_id='test_undo'")
        _cleanup(mem, ids)
        mem.close()


def test_historical_digest_content_remains_queryable_asof():
    mem, ids = _new_mem(), []
    try:
        ids.append(mem.remember("digest historical first", source="test_digest", importance=1.0, memory_type="decision"))
        first = mem.get_digest()
        ids.append(first["chunk_id"])
        ids.append(mem.remember("digest historical second", source="test_digest", importance=1.0, memory_type="decision"))
        second = mem.get_digest()
        ids.append(second["chunk_id"])
        row = mem._conn.execute(
            "SELECT content FROM chunks WHERE id=? AND timestamp <= ? AND (valid_until IS NULL OR valid_until >= ?)",
            (first["chunk_id"], first["built_at"], first["built_at"]),
        ).fetchone()
        assert row and "digest historical first" in row["content"]
        assert second["chunk_id"] != first["chunk_id"]
    finally:
        _cleanup(mem, ids)
        mem.close()
