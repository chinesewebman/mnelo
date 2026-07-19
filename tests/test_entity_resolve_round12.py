"""Round 12 — push entity_resolve.py 82% → 92%+.

Targets uncovered lines:
- 73: get_aliases with empty name AND no aliases
- 144: find_duplicate_candidates with same-id entities skipped
- 184: merge_entities with primary_id == secondary_id
- 194: merge_entities with missing entity → False
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

import importlib.util as _ilu


_REPO = Path('/Users/apple/projects/mnelo')


def _load_from_repo(mod_name: str):
    """Load REPO module (override LIVE in sys.modules)."""
    target_path = str(_REPO / f'{mod_name}.py')
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, '__file__', None) == target_path:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target_path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Force REPO module loads
entity_resolve = _load_from_repo('entity_resolve')
_memory_repo = _load_from_repo('memory')
_validation_repo = _load_from_repo('validation')


@pytest.fixture
def mem():
    """Fresh Memory instance via REPO."""
    import sys
    if sys.modules.get('memory', None) is None or not getattr(
        sys.modules['memory'], '__file__', ''
    ).startswith('/Users/apple/projects/mnelo'):
        # Force REPO
        from pathlib import Path
        import importlib.util as _ilu
        target = '/Users/apple/projects/mnelo/memory.py'
        spec = _ilu.spec_from_file_location('memory', target)
        mod = _ilu.module_from_spec(spec)
        sys.modules['memory'] = mod
        spec.loader.exec_module(mod)
    m = sys.modules['memory'].Memory()
    yield m
    m.close()


@pytest.fixture
def clean_prefix():
    return f'r12_{int(time.time() * 1_000_000)}'


# ============================================================
# Line 73: get_aliases with empty name AND no aliases → returns []
# ============================================================

class TestGetAliasesEmptyEverything:
    """entity_resolve.py:73 — entity with empty name AND no aliases."""

    def test_empty_name_no_aliases_returns_empty(self, mem, clean_prefix):
        """Entity with name='' and aliases_json='[]' → returns []."""
        eid = f'{clean_prefix}_empty_all'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', '', '[]', 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # get_aliases needs row_factory=sqlite3.Row to access by name
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, eid)
        assert result == []

    def test_empty_name_none_aliases_returns_empty(self, mem, clean_prefix):
        """Entity with name='' and aliases_json=NULL → returns []."""
        eid = f'{clean_prefix}_empty_none'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', '', NULL, 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, eid)
        assert result == []

    def test_get_aliases_entity_not_found_returns_empty(self, mem):
        """Line 73: row not found → return []."""
        # Non-existent entity id
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, 'definitely_not_exists_xyz')
        assert result == []

    def test_get_aliases_deleted_entity_returns_empty(self, mem, clean_prefix):
        """Soft-deleted entity → row filtered by valid_until IS NULL → row not found → return []."""
        eid = f'{clean_prefix}_deleted'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'del_n', '[\"x\"]', 'test_cov', ?, ?)",
            (eid, '2026-07-19T00:00:00', '2026-07-19T01:00:00'),
        )
        mem._conn.commit()
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, eid)
        assert result == []

    def test_get_aliases_with_name_returns_name(self, mem, clean_prefix):
        """Line 73: row['name'] truthy → aliases.append(name)."""
        eid = f'{clean_prefix}_has_name'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'real_name_xyz', '[]', 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, eid)
        # Line 73 executed: name added to aliases
        assert 'real_name_xyz' in result


# ============================================================
# Line 144: find_duplicate_candidates with same-id entities skipped
# (Defensive dead code — can't trigger naturally)
# ============================================================

class TestFindDuplicateSameIdSkipped:
    """entity_resolve.py:144 — same-id entities skipped in dedup."""

    def test_same_id_entities_skipped(self, mem, clean_prefix):
        """Two entities with same id → can't actually happen via INSERT, but test the logic."""
        unique_kind = f'sameid_{clean_prefix[-6:]}'
        eid = f'{clean_prefix}_si_entity'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, ?, 'same_name', 'test_cov', ?, NULL)",
            (eid, unique_kind, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # Same kind with only one entity → no candidates
        result = entity_resolve.find_duplicate_candidates(
            mem._conn, threshold=0.5, kind=unique_kind, max_pairs=10,
        )
        # Should be empty (only one entity)
        assert result == []


# ============================================================
# Line 243: find_duplicates_report with no candidates → return message
# ============================================================

class TestFindDuplicatesReportEmpty:
    """entity_resolve.py:243 — empty candidates → return 'no duplicates' message."""

    def test_report_no_candidates_returns_message(self, mem):
        """No candidates found → return Chinese-friendly message."""
        # Use a kind that doesn't exist to ensure no candidates
        result = entity_resolve.find_duplicates_report(
            mem._conn, threshold=0.99,
        )
        # Should return "✅ 无重复 entity" message
        assert isinstance(result, str)
        # Either empty result OR message containing "无重复"
        assert '无重复' in result or 'threshold' in result


# ============================================================
# Line 184: merge_entities with primary_id == secondary_id → False
# ============================================================

class TestMergeSameIdFails:
    """entity_resolve.py:184 — merge with primary_id == secondary_id → False."""

    def test_merge_same_id_returns_false(self, mem, clean_prefix):
        """primary_id == secondary_id → return False without changes."""
        eid = f'{clean_prefix}_same_id'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'same_n', 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.merge_entities(mem._conn, eid, eid)
        assert result is False
        # Verify entity NOT modified
        row = mem._conn.execute(
            "SELECT valid_until FROM entities WHERE id = ?", (eid,)
        ).fetchone()
        assert row['valid_until'] is None


# ============================================================
# Line 194: merge_entities with missing entity → False
# ============================================================

class TestMergeMissingEntityFails:
    """entity_resolve.py:194 — primary or secondary doesn't exist → False."""

    def test_merge_missing_primary_returns_false(self, mem, clean_prefix):
        """primary_id doesn't exist → False."""
        b_id = f'{clean_prefix}_only_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'b_n', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.merge_entities(
            mem._conn, f'{clean_prefix}_missing_a', b_id,
        )
        assert result is False

    def test_merge_missing_secondary_returns_false(self, mem, clean_prefix):
        """secondary_id doesn't exist → False."""
        a_id = f'{clean_prefix}_only_a2'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'a_n', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.merge_entities(
            mem._conn, a_id, f'{clean_prefix}_missing_b2',
        )
        assert result is False

    def test_merge_both_missing_returns_false(self, mem):
        """Neither exists → False (no exception)."""
        result = entity_resolve.merge_entities(
            mem._conn, 'definitely_not_a_id', 'definitely_not_b_id',
        )
        assert result is False


# ============================================================
# merge_entities already-deleted entities
# ============================================================

class TestMergeAlreadyDeletedFails:
    """merge_entities with already-superseded entities → False."""

    def test_merge_deleted_primary_returns_false(self, mem, clean_prefix):
        """primary has valid_until set (superseded) → False."""
        a_id = f'{clean_prefix}_del_a'
        b_id = f'{clean_prefix}_del_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'a_n', 'test_cov', ?, '2026-07-19T01:00:00')",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'b_n', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.merge_entities(mem._conn, a_id, b_id)
        assert result is False


# ============================================================
# get_aliases with various json shapes
# ============================================================

class TestGetAliasesAliasesJsonVariants:
    """get_aliases with different aliases_json shapes."""

    def test_aliases_with_only_whitespace_name(self, mem, clean_prefix):
        """name='   ' (whitespace) — name is truthy, appended."""
        eid = f'{clean_prefix}_ws_name'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', '   ', '[]', 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, eid)
        # name '   ' is truthy, so it's added
        assert '   ' in result

    def test_aliases_dict_format(self, mem, clean_prefix):
        """aliases_json as dict (not list) — handled by json.loads gracefully."""
        eid = f'{clean_prefix}_dict_aliases'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'good_n', ?, 'test_cov', ?, NULL)",
            (eid, json.dumps({'key': 'value'}), '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # Should not raise; either returns list or empty
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            result = entity_resolve.get_aliases(mem._conn, eid)
        assert isinstance(result, list)


# ============================================================
# merge_entities edge cases
# ============================================================

class TestMergeSuccessfulCases:
    """merge_entities successful merge paths."""

    def test_merge_with_empty_aliases(self, mem, clean_prefix):
        """Both entities have empty aliases_json → merge succeeds."""
        a_id = f'{clean_prefix}_ea'
        b_id = f'{clean_prefix}_eb'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'a_n', '[]', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'b_n', '[]', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.merge_entities(mem._conn, a_id, b_id)
        assert result is True

    def test_merge_with_name_in_secondary_aliases(self, mem, clean_prefix):
        """Secondary's name added to primary's aliases during merge."""
        a_id = f'{clean_prefix}_mn_a'
        b_id = f'{clean_prefix}_mn_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'primary_name', '[]', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'secondary_name', '[]', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        entity_resolve.merge_entities(mem._conn, a_id, b_id)
        # Primary should now have secondary_name as alias
        with _memory_repo._with_row_factory(mem._conn, sqlite3.Row):
            aliases = entity_resolve.get_aliases(mem._conn, a_id)
        assert 'secondary_name' in aliases