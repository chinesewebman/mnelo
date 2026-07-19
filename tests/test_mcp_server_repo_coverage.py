"""Round 8 — push mcp_server.py REPO coverage 79% → 85%+.

Targets uncovered lines (REPO file):
- 280-282: _handle_simple id_field wrap (remember/relate/update)
- 394: unknown tool error path
- 530-532: AuthError propagation
- 553-555: _build_sse_app dispatch
- 559-596: main() CLI args
- 600: __main__ guard

Strategy: explicitly load REPO mcp_server via _load_from_repo so coverage
tracks the REPO file (not LIVE).
"""
import json
import sys
import time
from pathlib import Path

import pytest

import importlib.util as _ilu


_REPO = Path(__file__).resolve().parent.parent


def _load_from_repo(mod_name: str):
    """Load REPO module into sys.modules (idempotent)."""
    target_path = str(_REPO / f'{mod_name}.py')
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, '__file__', None) == target_path:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target_path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Force REPO mcp_server into sys.modules
_mcp_repo = _load_from_repo('mcp_server')


@pytest.fixture
def mem():
    m = _load_from_repo('memory').Memory()
    yield m
    m.close()


@pytest.fixture
def clean_prefix():
    return f'mcp8_{int(time.time() * 1_000_000)}'


class TestHandleSimpleAllBranches:
    """mcp_server.py:280-282 — _handle_simple with/without id_field."""

    def test_handle_simple_chunk_id_path(self, mem, clean_prefix):
        """memory_remember → id_field='chunk_id' → wrapped."""
        result = _mcp_repo._handle_simple(mem, 'memory_remember', {
            'content': f'r8_test {clean_prefix}',
            'source': 'test_cov',
        })
        data = json.loads(result)
        assert 'chunk_id' in data
        assert data.get('status') == 'ok'

    def test_handle_simple_relation_id_path(self, mem, clean_prefix):
        """memory_relate → id_field='relation_id' → wrapped."""
        a_id = f'{clean_prefix}_rel_a'
        b_id = f'{clean_prefix}_rel_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'rel_a_n', 'test_cov', ?, NULL)",
            (a_id, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'rel_b_n', 'test_cov', ?, NULL)",
            (b_id, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        result = _mcp_repo._handle_simple(mem, 'memory_relate', {
            'source_id': a_id,
            'target_id': b_id,
            'relation': 'r8_rel',
            'weight': 0.5,
        })
        data = json.loads(result)
        assert 'relation_id' in data
        assert data.get('status') == 'ok'

    def test_handle_simple_new_chunk_id_path(self, mem, clean_prefix):
        """memory_update → id_field='new_chunk_id' → wrapped."""
        cid = mem.remember(
            content=f'r8_orig {clean_prefix}',
            source='test_cov',
        )
        result = _mcp_repo._handle_simple(mem, 'memory_update', {
            'old_id': cid,
            'reason': 'test_update',
            'new_content': f'r8_updated {clean_prefix}',
        })
        data = json.loads(result)
        assert 'new_chunk_id' in data
        assert data.get('status') == 'ok'

    def test_handle_simple_recall_no_id_field(self, mem, clean_prefix):
        """memory_recall → id_field=None → direct JSON dump."""
        mem.remember(content=f'r8_recall {clean_prefix}', source='test_cov')
        result = _mcp_repo._handle_simple(mem, 'memory_recall', {
            'query': f'r8_recall {clean_prefix}',
            'top_k': 3,
        })
        data = json.loads(result)
        # No id_field wrapping — direct list
        assert isinstance(data, list)

    def test_handle_simple_stats_no_id_field(self, mem, clean_prefix):
        """memory_stats → id_field=None → direct JSON dump."""
        result = _mcp_repo._handle_simple(mem, 'memory_stats', {})
        data = json.loads(result)
        assert isinstance(data, dict)
        assert 'chunks' in data or 'entities' in data


class TestGraphQueryBranch:
    """mcp_server.py graph_query via _TOOL_REGISTRY."""

    def test_graph_query_handler(self, mem, clean_prefix):
        """memory_graph_query → _handle_simple path."""
        # Create an entity to graph_query
        eid = f'{clean_prefix}_gq_entity'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'gq_target', 'test_cov', ?, NULL)",
            (eid, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # graph_query goes through _TOOL_REGISTRY → _handle_simple (id_field=None)
        result = _mcp_repo._handle_simple(mem, 'memory_graph_query', {
            'start_node': eid,
            'max_hops': 1,
        })
        assert isinstance(result, str)
        data = json.loads(result)
        assert 'nodes' in data or 'edges' in data

    def test_graph_query_with_kind_filter(self, mem, clean_prefix):
        """graph_query with edge_types filter."""
        result = _mcp_repo._handle_simple(mem, 'memory_graph_query', {
            'start_node': 'nonexistent_id',
            'max_hops': 1,
            'edge_types': ['stock'],
        })
        assert isinstance(result, str)

    def test_graph_query_with_asof(self, mem, clean_prefix):
        """graph_query with asof timestamp."""
        result = _mcp_repo._handle_simple(mem, 'memory_graph_query', {
            'start_node': 'some_entity',
            'max_hops': 2,
            'asof': '2026-07-19T00:00:00',
        })
        assert isinstance(result, str)


class TestRateLimitBranches:
    """mcp_server.py:244-260 — rate limit bucket logic."""

    def test_rate_limit_under_threshold_passes(self, mem, clean_prefix):
        """Under rate limit → request succeeds."""
        for i in range(3):
            result = _mcp_repo._call_tool('memory_stats', {})
            data = json.loads(result)
            assert isinstance(data, dict)

    def test_rate_limit_buckets_is_dict(self, mem):
        """Verify _RATE_BUCKETS is a dict."""
        assert isinstance(_mcp_repo._RATE_BUCKETS, dict)

    def test_rate_limit_constants_defined(self):
        """Rate limit constants present."""
        assert _mcp_repo._RATE_LIMIT_MAX_REQS > 0
        assert _mcp_repo._RATE_LIMIT_WINDOW_SEC > 0


class TestResolveServerDefaults:
    """mcp_server.py:230-234 — _resolve_server_defaults fallback."""

    def test_resolve_server_defaults_returns_tuple(self):
        """Returns (host, port) tuple from config."""
        host, port = _mcp_repo._resolve_server_defaults()
        assert isinstance(host, str)
        assert isinstance(port, int)
        assert port > 0


class TestSSEBuild:
    """mcp_server.py:553-555 — _build_sse_app + BearerAuthMiddleware."""

    def test_build_sse_app_returns_app(self):
        """_build_sse_app returns a Starlette app."""
        app = _mcp_repo._build_sse_app('test_token_abc')
        assert app is not None
        # Starlette app has routes
        assert hasattr(app, 'routes')

    def test_sse_app_routes_registered(self):
        """SSE app should have /sse and /messages/ routes."""
        app = _mcp_repo._build_sse_app('test_token_xyz')
        route_paths = [r.path for r in app.routes if hasattr(r, 'path')]
        assert any('/sse' in p for p in route_paths)

    def test_sse_app_with_invalid_token(self):
        """Invalid token still produces valid app (token only checked on request)."""
        app = _mcp_repo._build_sse_app('')
        assert app is not None


class TestMainFunction:
    """mcp_server.py:559-596 — main() CLI entry point."""

    def test_main_help(self, monkeypatch, capsys):
        """Run main() with --help → argparse prints help."""
        monkeypatch.setattr(sys, 'argv', ['mcp_server', '--help'])
        with pytest.raises(SystemExit) as exc_info:
            _mcp_repo.main()
        # argparse exits with code 0 on --help
        assert exc_info.value.code == 0

    def test_main_invalid_transport(self, monkeypatch, capsys):
        """Invalid --transport value → argparse error → SystemExit."""
        monkeypatch.setattr(sys, 'argv', ['mcp_server', '--transport', 'invalid_xyz'])
        with pytest.raises(SystemExit):
            _mcp_repo.main()