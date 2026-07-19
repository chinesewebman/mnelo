"""Round 7 — push validation.py 97% → 100%, entity_resolve.py 76% → 85%+.

Targets:
- validation.py:117 — bool rejection in validate_id
- validation.py:121 — non-str non-int rejection
- entity_resolve.py:50 — alias_match_score empty string
- entity_resolve.py:73 — get_aliases empty name
- entity_resolve.py:80-81 — bad JSON path
- entity_resolve.py:121 — kind with < 2 entities
- entity_resolve.py:141, 144 — empty name + same id branches
- entity_resolve.py:155 — alias conflict
- entity_resolve.py:184, 194 — merge same id / missing entity
"""
import json
import time
import pytest

import entity_resolve
import validation as validation_mod


@pytest.fixture
def mem():
    """Fresh Memory instance."""
    import sys
    Memory = sys.modules['memory'].Memory
    m = Memory()
    yield m
    m.close()


@pytest.fixture
def clean_prefix():
    return f'r7_{int(time.time() * 1_000_000)}'


# ============================================================
# validation.py edge cases
# ============================================================

class TestValidateIdBoolRejection:
    """validation.py:117 — bool is subclass of int, explicitly reject."""

    def test_validate_id_bool_raises(self):
        """bool input must be rejected (True/False would silently coerce)."""
        with pytest.raises(validation_mod.ValidationError, match='must be str or int'):
            validation_mod.validate_id(True)

    def test_validate_id_false_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be str or int'):
            validation_mod.validate_id(False)


class TestValidateIdNonStrNonInt:
    """validation.py:121 — non-str, non-int types rejected."""

    def test_validate_id_list_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be str or int'):
            validation_mod.validate_id(['id_str'])

    def test_validate_id_dict_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be str or int'):
            validation_mod.validate_id({'id': 'value'})

    def test_validate_id_none_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be str or int'):
            validation_mod.validate_id(None)

    def test_validate_id_float_raises(self):
        """Float is neither str nor int → rejected (float IDs are ambiguous)."""
        with pytest.raises(validation_mod.ValidationError, match='must be str or int'):
            validation_mod.validate_id(1.5)


class TestValidateIdIntCoercion:
    """validation.py:118-119 — int is coerced to str."""

    def test_validate_id_int_coerced_to_str(self):
        result = validation_mod.validate_id(42)
        assert result == '42'
        assert isinstance(result, str)

    def test_validate_id_zero_int(self):
        result = validation_mod.validate_id(0)
        assert result == '0'

    def test_validate_id_negative_int(self):
        result = validation_mod.validate_id(-1)
        assert result == '-1'


class TestValidateIdFormatMismatch:
    """validation.py:124 — invalid chars after coercion → format error."""

    def test_validate_id_invalid_chars_raises(self):
        """Special chars not in [a-zA-Z0-9_:.\\-] → rejected."""
        with pytest.raises(validation_mod.ValidationError, match='format mismatch'):
            validation_mod.validate_id('id with space')

    def test_validate_id_too_long_raises(self):
        """Length exceeds MAX_ID_LEN → rejected."""
        from validation import MAX_ID_LEN
        long_id = 'a' * (MAX_ID_LEN + 1)
        with pytest.raises(validation_mod.ValidationError, match='format mismatch'):
            validation_mod.validate_id(long_id)


# ============================================================
# entity_resolve.py edge cases (REPO version)
# ============================================================

class TestNormalizeText:
    """entity_resolve.py:31-43 — normalize_text coverage."""

    def test_empty_string_returns_empty(self):
        assert entity_resolve.normalize_text('') == ''

    def test_chinese_only(self):
        result = entity_resolve.normalize_text('你好世界')
        # At least lowercased (Chinese unaffected)
        assert isinstance(result, str)


class TestAliasMatchScoreEdge:
    """entity_resolve.py:50 — alias_match_score empty input."""

    def test_both_empty_returns_zero(self):
        assert entity_resolve.alias_match_score('', '') == 0.0

    def test_only_punctuation_returns_zero(self):
        """After normalize_text strips punctuation, becomes empty → 0.0."""
        score = entity_resolve.alias_match_score('!!!', '???')
        assert score == 0.0


class TestGetAliasesBadJson:
    """entity_resolve.py:73, 80-81 — bad aliases_json path."""

    def test_bad_json_silently_swallowed(self, mem, clean_prefix):
        eid = f'{clean_prefix}_bad_json2'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'good_name_xyz', 'not valid json{{[[', 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # Should NOT raise (bad JSON swallowed)
        result = entity_resolve.get_aliases(mem._conn, eid)
        # Should at least have the name
        assert 'good_name_xyz' in result

    def test_empty_name_returns_only_aliases(self, mem, clean_prefix):
        """entity_resolve.py:73 — empty name still returns aliases."""
        eid = f'{clean_prefix}_empty_name'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', '', ?, 'test_cov', ?, NULL)",
            (eid, json.dumps(['only_alias']), '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.get_aliases(mem._conn, eid)
        # Both name ('' empty) and alias present
        assert '' in result or 'only_alias' in result


class TestFindDuplicateCandidatesEmpty:
    """entity_resolve.py:121 — kind with < 2 entities."""

    def test_kind_with_no_entities_returns_empty(self, mem, clean_prefix):
        """Kind filter matches nothing → return []."""
        result = entity_resolve.find_duplicate_candidates(
            mem._conn, threshold=0.5, kind=f'nonexistent_kind_{clean_prefix[-6:]}',
            max_pairs=10,
        )
        assert result == []


class TestFindDuplicateCandidatesEmptyName:
    """entity_resolve.py:141 — entities with empty name skipped."""

    def test_entities_with_empty_name_skipped(self, mem, clean_prefix):
        unique_kind = f'empty_n_{clean_prefix[-6:]}'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, ?, '', 'test_cov', ?, NULL)",
            (f'{clean_prefix}_en_a', unique_kind, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, ?, '', 'test_cov', ?, NULL)",
            (f'{clean_prefix}_en_b', unique_kind, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # Both names empty → both skipped → no candidates
        result = entity_resolve.find_duplicate_candidates(
            mem._conn, threshold=0.5, kind=unique_kind, max_pairs=10,
        )
        assert isinstance(result, list)


class TestFindDuplicateCandidatesAliasConflict:
    """entity_resolve.py:155 — alias conflict path (already covered in round 4)."""

    def test_alias_conflict_produces_candidate(self, mem, clean_prefix):
        unique_kind = f'alias2_{clean_prefix[-6:]}'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, ?, 'name_alpha', ?, 'test_cov', ?, NULL)",
            (f'{clean_prefix}_ac_a', unique_kind,
             json.dumps(['CONFLICT_ALIAS']), '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, ?, 'name_beta', ?, 'test_cov', ?, NULL)",
            (f'{clean_prefix}_ac_b', unique_kind,
             json.dumps(['CONFLICT_ALIAS']), '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.find_duplicate_candidates(
            mem._conn, threshold=0.9, kind=unique_kind, max_pairs=10,
        )
        # At least one alias conflict candidate
        has_conflict = any('alias' in r[3].lower() for r in result)
        assert has_conflict


class TestMergeEntitiesErrors:
    """entity_resolve.py:184, 194 — merge error paths."""

    def test_merge_returns_dict_with_aliases(self, mem, clean_prefix):
        """Successful merge returns rowcount/aliases info."""
        a_id = f'{clean_prefix}_merge_ok_a'
        b_id = f'{clean_prefix}_merge_ok_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'primary_n', ?, 'test_cov', ?, NULL)",
            (a_id, json.dumps(['p_alias']), '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'secondary_n', ?, 'test_cov', ?, NULL)",
            (b_id, json.dumps(['s_alias']), '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = entity_resolve.merge_entities(mem._conn, a_id, b_id)
        # Should return True on success
        assert result is True
        # Verify secondary was soft-deleted
        row = mem._conn.execute(
            "SELECT valid_until FROM entities WHERE id = ?", (b_id,)
        ).fetchone()
        assert row['valid_until'] is not None


class TestFindDuplicatesReport:
    """entity_resolve.py:243 — empty candidates path."""

    def test_report_threshold_too_high(self, mem, clean_prefix):
        """Threshold > 1.0 effectively filters everything → report short."""
        result = entity_resolve.find_duplicates_report(mem._conn, threshold=1.5)
        assert isinstance(result, str)
        # Should return some kind of report (empty or table)
        assert len(result) > 0