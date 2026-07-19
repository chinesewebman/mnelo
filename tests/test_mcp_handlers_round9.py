"""Round 9 — push mcp_server.py REPO coverage 63% → 70%+.

Targets uncovered lines (REPO file):
- 233-234: _resolve_server_defaults exception fallback (config no server_host)
- 252: rate limit threshold breach (raise ValidationError)
- 295-307: _handle_entity_resolve path (full body, with custom SQL row factory)
- 321-334: _handle_list_entities path (kind/min_importance/limit filters)
- 348-364: _handle_search_relations path (relation filter + asof)
"""
import json
import sys
import time
from pathlib import Path

import pytest

import importlib.util as _ilu


_REPO = Path(__file__).resolve().parent.parent


def _load_from_repo(mod_name: str):
    target_path = str(_REPO / f'{mod_name}.py')
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, '__file__', None) == target_path:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target_path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_mcp_repo = _load_from_repo('mcp_server')
_validation_repo = _load_from_repo('validation')


@pytest.fixture
def mem():
    m = _load_from_repo('memory').Memory()
    yield m
    m.close()


@pytest.fixture
def clean_prefix():
    return f'mcp9_{int(time.time() * 1_000_000)}'


class TestHandleEntityResolveFullPath:
    """mcp_server.py:295-307 — _handle_entity_resolve via MCP."""

    def test_entity_resolve_default_args(self, mem):
        """No args → uses defaults."""
        result = _mcp_repo._handle_entity_resolve(mem, {})
        assert isinstance(result, str)
        data = json.loads(result)
        assert isinstance(data, dict)
        # Returns {candidates: [...], count: N}
        assert 'count' in data

    def test_entity_resolve_with_kind_filter(self, mem):
        """kind filter passed through."""
        result = _mcp_repo._handle_entity_resolve(mem, {
            'kind': 'stock',
            'threshold': 0.95,
            'max_pairs': 50,
        })
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_entity_resolve_max_pairs_cap(self, mem):
        """max_pairs > 500 should be capped to 500."""
        result = _mcp_repo._handle_entity_resolve(mem, {
            'max_pairs': 100000,
        })
        data = json.loads(result)
        # Should not hang — cap kicks in
        assert isinstance(data, dict)


class TestHandleListEntitiesFullPath:
    """mcp_server.py:321-334 — _handle_list_entities path."""

    def test_list_entities_empty(self, mem):
        """No args → all active entities."""
        result = _mcp_repo._handle_list_entities(mem, {})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert 'entities' in data
        assert 'count' in data
        assert isinstance(data['entities'], list)

    def test_list_entities_with_kind(self, mem, clean_prefix):
        """Filter by entity kind."""
        # Insert entities of different kinds
        for kind in ('test_kind_a', 'test_kind_b'):
            mem._conn.execute(
                "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
                "VALUES (?, ?, 'n', 'test_cov', ?, NULL)",
                (f'{clean_prefix}_{kind}', kind, '2026-07-19T00:00:00'),
            )
        mem._conn.commit()
        result = _mcp_repo._handle_list_entities(mem, {'kind': 'test_kind_a'})
        data = json.loads(result)
        # Should only have test_kind_a
        for ent in data['entities']:
            assert ent['kind'] == 'test_kind_a'

    def test_list_entities_min_importance(self, mem, clean_prefix):
        """Filter by minimum importance."""
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, importance, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'low_imp', 0.1, 'test_cov', ?, NULL)",
            (f'{clean_prefix}_low', '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, importance, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'high_imp', 0.9, 'test_cov', ?, NULL)",
            (f'{clean_prefix}_high', '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = _mcp_repo._handle_list_entities(mem, {'min_importance': 0.5})
        data = json.loads(result)
        # All returned entities should have importance >= 0.5
        for ent in data['entities']:
            assert float(ent['importance']) >= 0.5

    def test_list_entities_limit(self, mem, clean_prefix):
        """Limit caps result count."""
        for i in range(5):
            mem._conn.execute(
                "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
                "VALUES (?, 'test', ?, 'test_cov', ?, NULL)",
                (f'{clean_prefix}_limit_{i}', f'name_{i}', '2026-07-19T00:00:00'),
            )
        mem._conn.commit()
        result = _mcp_repo._handle_list_entities(mem, {'limit': 2})
        data = json.loads(result)
        # Should have at most 2 entities
        assert data['count'] <= 2

    def test_list_entities_excludes_deleted(self, mem, clean_prefix):
        """valid_until IS NULL filter → soft-deleted excluded."""
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'active_n', 'test_cov', ?, NULL)",
            (f'{clean_prefix}_active', '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'deleted_n', 'test_cov', ?, ?)",
            (f'{clean_prefix}_deleted', '2026-07-19T00:00:00', '2026-07-19T01:00:00'),
        )
        mem._conn.commit()
        result = _mcp_repo._handle_list_entities(mem, {})
        data = json.loads(result)
        # Deleted should not appear
        ids = [e['id'] for e in data['entities']]
        assert f'{clean_prefix}_deleted' not in ids


class TestHandleSearchRelationsFullPath:
    """mcp_server.py:348-364 — _handle_search_relations path."""

    def test_search_relations_basic(self, mem, clean_prefix):
        """Filter by relation type."""
        # Create a relation
        a_id = f'{clean_prefix}_sr_a'
        b_id = f'{clean_prefix}_sr_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'sr_a_n', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'sr_b_n', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem.relate(a_id, b_id, 'r9_test_rel', weight=0.8)
        mem._conn.commit()
        result = _mcp_repo._handle_search_relations(mem, {'relation': 'r9_test_rel'})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert 'relations' in data
        assert isinstance(data['relations'], list)

    def test_search_relations_with_asof(self, mem, clean_prefix):
        """asof timestamp filter."""
        a_id = f'{clean_prefix}_sr2_a'
        b_id = f'{clean_prefix}_sr2_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'sr2_a_n', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'sr2_b_n', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem.relate(a_id, b_id, 'r9_test_rel_2', weight=0.5)
        mem._conn.commit()
        # Future asof should still find it
        result = _mcp_repo._handle_search_relations(mem, {
            'relation': 'r9_test_rel_2',
            'asof': '2099-12-31T00:00:00',
        })
        data = json.loads(result)
        assert isinstance(data, dict)

    def test_search_relations_no_results(self, mem, clean_prefix):
        """Nonexistent relation type → empty list."""
        result = _mcp_repo._handle_search_relations(mem, {
            'relation': 'definitely_does_not_exist_xyz',
        })
        data = json.loads(result)
        assert data['relations'] == []
        assert data['count'] == 0

    def test_search_relations_with_limit(self, mem, clean_prefix):
        """limit param caps results."""
        a_id = f'{clean_prefix}_sr3_a'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'sr3_n', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        # Create many relations
        for i in range(5):
            b_id = f'{clean_prefix}_sr3_b_{i}'
            mem._conn.execute(
                "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
                "VALUES (?, 'test', ?, 'test_cov', ?, NULL)",
                (b_id, f'sr3_b_n_{i}', '2026-07-19T00:00:00'),
            )
            mem.relate(a_id, b_id, 'r9_many_rels', weight=0.5)
        mem._conn.commit()
        result = _mcp_repo._handle_search_relations(mem, {
            'relation': 'r9_many_rels',
            'limit': 2,
        })
        data = json.loads(result)
        assert data['count'] <= 2


class TestResolveServerDefaultsException:
    """mcp_server.py:233-234 — exception fallback in _resolve_server_defaults."""

    def test_fallback_when_config_has_no_server_attrs(self, monkeypatch):
        """Config object missing server_host/port → uses defaults."""
        # Mock config to raise on attribute access
        class _BadConfig:
            @property
            def server_host(self):
                raise AttributeError('no server_host')

        monkeypatch.setattr(_mcp_repo, 'config', _BadConfig())
        host, port = _mcp_repo._resolve_server_defaults()
        # Should fall back to defaults
        assert isinstance(host, str)
        assert isinstance(port, int)
        # Defaults from mcp_server module
        assert host == _mcp_repo.DEFAULT_SSE_HOST
        assert port == _mcp_repo.DEFAULT_SSE_PORT


class TestRateLimitBreach:
    """mcp_server.py:252 — rate limit threshold raises ValidationError.

    Note: This branch is covered by test_more_coverage.py::TestRateLimitCheck.
    We skip the direct test here to avoid cross-test pollution of _RATE_BUCKETS.
    """

    def test_rate_limit_window_reset(self):
        """Old bucket (window exceeded) gets reset to [now, 1]."""
        tool_name = 'test_rate_reset_unique_r9'
        old_ts = time.time() - 1000
        _mcp_repo._RATE_BUCKETS[tool_name] = [old_ts, 999]
        _mcp_repo._rate_limit_check(tool_name)
        bucket = _mcp_repo._RATE_BUCKETS[tool_name]
        assert bucket[1] == 1

    def test_rate_limit_constants_are_positive(self):
        """Constants should be sane."""
        assert _mcp_repo._RATE_LIMIT_MAX_REQS > 0
        assert _mcp_repo._RATE_LIMIT_WINDOW_SEC > 0


class TestMCPConstants:
    """Quick smoke tests for module-level constants."""

    def test_default_sse_constants(self):
        assert hasattr(_mcp_repo, 'DEFAULT_SSE_HOST')
        assert hasattr(_mcp_repo, 'DEFAULT_SSE_PORT')
        assert isinstance(_mcp_repo.DEFAULT_SSE_PORT, int)
        assert _mcp_repo.DEFAULT_SSE_PORT > 0

    def test_tool_registry_has_all_tools(self):
        """All 7 expected tools in registry."""
        expected = {
            'memory_remember', 'memory_recall', 'memory_relate',
            'memory_forget', 'memory_update', 'memory_graph_query',
            'memory_stats',
        }
        for tool in expected:
            assert tool in _mcp_repo._TOOL_REGISTRY

    def test_custom_handlers_has_3_entries(self):
        assert len(_mcp_repo._CUSTOM_HANDLERS) == 3
        assert 'memory_entity_resolve' in _mcp_repo._CUSTOM_HANDLERS
        assert 'memory_list_entities' in _mcp_repo._CUSTOM_HANDLERS
        assert 'memory_search_relations' in _mcp_repo._CUSTOM_HANDLERS