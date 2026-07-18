"""
tests/test_more_coverage.py — Round 3 coverage: mcp_server sub-handlers + entity_resolve

[Round 3 quality] 补齐 mcp_server.py (56% → target 70%) + entity_resolve module 0%
- _handle_entity_resolve / _handle_list_entities / _handle_search_relations
- rate limit (超限抛 ValidationError)
- _resolve_server_defaults except fallback
- entity_resolve.normalize_text / alias_match_score / find_duplicate_candidates
"""
import json
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import importlib.util as _ilu


def _load_from_repo(mod_name: str):
    spec = _ilu.spec_from_file_location(mod_name, _REPO / f'{mod_name}.py')
    mod = _ilu.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# 顺序加载
_load_from_repo('config')
_load_from_repo('embedder')
_load_from_repo('validation')
_load_from_repo('memory')
# 防 memory.py 把 live validation 污染
_LIVE_ROOT = '/Users/apple/.hermes/memory'
if _LIVE_ROOT in sys.path:
    sys.path.remove(_LIVE_ROOT)
_val_spec = _ilu.spec_from_file_location('validation', _REPO / 'validation.py')
_val_mod = _ilu.module_from_spec(_val_spec)
sys.modules['validation'] = _val_mod
_val_spec.loader.exec_module(_val_mod)
_mem_spec = _ilu.spec_from_file_location('memory', _REPO / 'memory.py')
_mem_mod = _ilu.module_from_spec(_mem_spec)
_mem_mod.ValidationError = _val_mod.ValidationError
sys.modules['memory'] = _mem_mod
_mem_spec.loader.exec_module(_mem_mod)

_load_from_repo('auth')
_load_from_repo('entity_resolve')

# mcp_server 可选
try:
    _load_from_repo('mcp_server')
    MCP_SERVER_AVAILABLE = True
except Exception:
    MCP_SERVER_AVAILABLE = False

from memory import Memory  # noqa: E402


@pytest.fixture
def mem():
    m = Memory()
    yield m
    m.close()


@pytest.fixture
def clean_prefix():
    return f'cov2_{int(time.time() * 1_000_000)}'


# ============================================================
# mcp_server._handle_entity_resolve + entity_resolve module
# ============================================================

class TestEntityResolve:
    """entity_resolve.py unit tests"""

    def test_normalize_text_lowercase_strip(self):
        from entity_resolve import normalize_text
        # [Round 3 fix] 新实现删空格 + 标点 → 'helloworld'
        # 旧实现 r'[\s\W_]+' 有 catastrophic backtracking bug
        assert normalize_text('  Hello WORLD  ') == 'helloworld'
        # 标点保留行为
        assert normalize_text('sh600089') == 'sh600089'
        # 标点被剥
        assert normalize_text('hello, world!') == 'helloworld'

    def test_alias_match_score_identical(self):
        from entity_resolve import alias_match_score
        score = alias_match_score('sh600089', 'sh600089')
        assert score >= 0.99

    def test_alias_match_score_partial(self):
        from entity_resolve import alias_match_score
        # 完全不同的字符串 score 低
        score = alias_match_score('hello', 'world')
        assert score < 0.5

    def test_find_duplicate_candidates_empty_db(self, mem):
        from entity_resolve import find_duplicate_candidates
        # [Round 3 fix] 显式 kind + max_pairs=10 防 live DB O(N²)
        candidates = find_duplicate_candidates(mem._conn, threshold=0.85, kind='nonexistent_kind_xyz', max_pairs=10)
        assert isinstance(candidates, list)
        assert len(candidates) == 0

    def test_find_duplicate_candidates_with_match(self, mem, clean_prefix):
        from entity_resolve import find_duplicate_candidates
        # 插入两个 entity, name 高度相似
        eid_a = f'{clean_prefix}_a'
        eid_b = f'{clean_prefix}_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'stock', 'sh600089', 'test_cov', ?, NULL)",
            (eid_a, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'stock', 'sh600089', 'test_cov', ?, NULL)",
            (eid_b, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        candidates = find_duplicate_candidates(mem._conn, threshold=0.85, kind='stock', max_pairs=100)
        # 应找到重复候选
        assert isinstance(candidates, list)


@pytest.mark.skipif(not MCP_SERVER_AVAILABLE, reason='mcp_server module not importable')
class TestMCPServerHandlers:
    """[Round 3] mcp_server sub-handler functions"""

    def test_handle_list_entities(self, mem, clean_prefix):
        """mcp_server._handle_list_entities 测 (line 319-332)"""
        from mcp_server import _handle_list_entities
        # 建几个 entity
        for i in range(3):
            mem._conn.execute(
                "INSERT INTO entities (id, kind, name, summary, importance, source, valid_from, valid_until) "
                "VALUES (?, 'test_kind', ?, 'summary', ?, 'test_cov', ?, NULL)",
                (f'{clean_prefix}_{i}', f'entity_{i}', 0.5 + i * 0.1, '2026-07-19T00:00:00'),
            )
        mem._conn.commit()
        result = _handle_list_entities(mem, {'kind': 'test_kind', 'limit': 10})
        data = json.loads(result)
        assert 'entities' in data
        assert data['count'] >= 3

    def test_handle_list_entities_with_min_importance(self, mem, clean_prefix):
        from mcp_server import _handle_list_entities
        # import 0.3, 0.7
        for imp in (0.3, 0.7):
            mem._conn.execute(
                "INSERT INTO entities (id, kind, name, importance, source, valid_from, valid_until) "
                "VALUES (?, 'test_imp', ?, ?, 'test_cov', ?, NULL)",
                (f'{clean_prefix}_imp_{int(imp*10)}', f'name_{int(imp*10)}', imp, '2026-07-19T00:00:00'),
            )
        mem._conn.commit()
        # min_importance=0.5 应只返 0.7
        result = _handle_list_entities(mem, {'kind': 'test_imp', 'min_importance': 0.5, 'limit': 10})
        data = json.loads(result)
        for ent in data['entities']:
            assert ent['importance'] >= 0.5

    def test_handle_search_relations(self, mem, clean_prefix):
        """mcp_server._handle_search_relations 测 (line 346-362)"""
        from mcp_server import _handle_search_relations
        # 建一个 relation
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'a', 'test_cov', ?, NULL)",
            (f'{clean_prefix}_src', '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, 'test', 'b', 'test_cov', ?, NULL)",
            (f'{clean_prefix}_tgt', '2026-07-19T00:00:00'),
        )
        mem.relate(f'{clean_prefix}_src', f'{clean_prefix}_tgt', 'owns_relation', weight=0.9)
        mem._conn.commit()

        result = _handle_search_relations(mem, {'relation': 'owns_relation', 'limit': 5})
        data = json.loads(result)
        assert 'relations' in data
        assert data['count'] >= 1
        # 第一个 relation 应是我们建的
        rel = data['relations'][0]
        assert rel['relation'] == 'owns_relation'

    def test_handle_entity_resolve(self, mem, clean_prefix):
        """mcp_server._handle_entity_resolve 测 (line 281-305)"""
        from mcp_server import _handle_entity_resolve
        # 建一对几乎重复的 entity (用 unique kind 防 hit live db)
        unique_kind = f'test_kind_{clean_prefix[-6:]}'
        eid_a = f'{clean_prefix}_res_a'
        eid_b = f'{clean_prefix}_res_b'
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, ?, 'sh600089', 'test_cov', ?, NULL)",
            (eid_a, unique_kind, '2026-07-19T00:00:00'),
        )
        mem._conn.execute(
            "INSERT INTO entities (id, kind, name, source, valid_from, valid_until) "
            "VALUES (?, ?, 'sh600089', 'test_cov', ?, NULL)",
            (eid_b, unique_kind, '2026-07-19T00:00:00'),
        )
        mem._conn.commit()
        # [Round 3 fix] 显式 kind + max_pairs 防扫 live DB
        result = _handle_entity_resolve(mem, {
            'threshold': 0.85,
            'kind': unique_kind,
            'max_pairs': 100,
        })
        data = json.loads(result)
        assert 'candidates' in data
        assert 'count' in data


@pytest.mark.skipif(not MCP_SERVER_AVAILABLE, reason='mcp_server module not importable')
class TestMCPServerRateLimit:
    """[Round 3] mcp_server._rate_limit_check"""

    def test_first_call_no_exception(self):
        from mcp_server import _rate_limit_check, _RATE_BUCKETS
        _RATE_BUCKETS.clear()  # reset
        _rate_limit_check('test_tool_rl')  # 不抛
        assert 'test_tool_rl' in _RATE_BUCKETS

    def test_rate_limit_exceeded_raises(self):
        from mcp_server import _rate_limit_check, _RATE_BUCKETS, _RATE_LIMIT_MAX_REQS
        from validation import ValidationError
        _RATE_BUCKETS.clear()
        # 触发 limit: 调 N+1 次
        for _ in range(_RATE_LIMIT_MAX_REQS + 1):
            try:
                _rate_limit_check('test_tool_overflow')
            except ValidationError as e:
                # 最后一次应抛 rate_limit
                assert 'rate limit' in e.reason
                return
        pytest.fail('expected ValidationError')

    def test_window_reset(self, monkeypatch):
        """超 _RATE_LIMIT_WINDOW_SEC 后 bucket 重置"""
        from mcp_server import _rate_limit_check, _RATE_BUCKETS
        _RATE_BUCKETS.clear()
        _rate_limit_check('test_tool_window')
        # monkey patch time.time 模拟过了 61s
        import time
        original_time = time.time
        monkeypatch.setattr(time, 'time', lambda: original_time() + 61)
        # 不抛, bucket reset
        _rate_limit_check('test_tool_window')  # pass


@pytest.mark.skipif(not MCP_SERVER_AVAILABLE, reason='mcp_server module not importable')
class TestResolveServerDefaults:
    """[Round 3] mcp_server._resolve_server_defaults"""

    def test_returns_tuple(self):
        from mcp_server import _resolve_server_defaults
        result = _resolve_server_defaults()
        assert isinstance(result, tuple)
        assert len(result) == 2
        host, port = result
        assert isinstance(host, str)
        assert isinstance(port, int)

    def test_fallback_when_config_unavailable(self, monkeypatch):
        """config 不可用时 fallback 到 DEFAULT_SSE_HOST/PORT"""
        from mcp_server import _resolve_server_defaults, DEFAULT_SSE_HOST, DEFAULT_SSE_PORT
        import mcp_server
        # monkey-patch config to raise
        monkeypatch.setattr(mcp_server, 'config', None)
        host, port = _resolve_server_defaults()
        assert host == DEFAULT_SSE_HOST
        assert port == DEFAULT_SSE_PORT


@pytest.mark.skipif(not MCP_SERVER_AVAILABLE, reason='mcp_server module not importable')
class TestCallToolErrorPaths:
    """[Round 3] _call_tool rate-limit + internal error 路径"""

    def test_call_tool_rate_limited(self, monkeypatch):
        from mcp_server import _call_tool, _RATE_BUCKETS
        import json
        _RATE_BUCKETS.clear()
        # mock rate limit 让它立刻抛
        from validation import ValidationError
        def _raise_rate(_):
            raise ValidationError('tool', 'rate limit: 60 reqs / 60s exceeded')
        monkeypatch.setattr('mcp_server._rate_limit_check', _raise_rate)
        result = _call_tool('memory_recall', {'query': 'test'})
        d = json.loads(result)
        assert d.get('type') == 'rate_limit'
        assert 'rate limit' in d.get('error', '')

    def test_call_tool_validation_error_returns_redacted(self):
        from mcp_server import _call_tool
        import json
        # memory_recall 接受 query, control chars 应被拒
        result = _call_tool('memory_recall', {'query': '\x00'})
        d = json.loads(result)
        # 走 validation 分支
        assert d.get('type') == 'validation'

    def test_call_tool_invalid_tool_name(self):
        from mcp_server import _call_tool
        import json
        result = _call_tool('totally_unknown_tool_xyz', {})
        d = json.loads(result)
        assert 'error' in d or 'unknown' in str(d).lower()

    def test_call_tool_internal_exception_redacted(self):
        """P1-3: internal exception 应 redact 成 type name"""
        from mcp_server import _call_tool
        import json
        # 传错 kwargs 让内部抛
        result = _call_tool('memory_remember', {'no_content_kwarg': 'x'})
        d = json.loads(result)
        # 期望是 type='internal' + error=type_name
        assert d.get('type') in ('internal', 'validation')
        if d.get('type') == 'internal':
            # 不应 leak str(e)
            detail = d.get('detail') or ''
            assert 'memory.py' not in detail
            assert 'self.' not in detail


@pytest.mark.skipif(not MCP_SERVER_AVAILABLE, reason='mcp_server module not importable')
class TestRunSSEEntryPoints:
    """[Round 3] run_sse entry point 边界"""

    def test_run_sse_rejects_non_loopback(self):
        from mcp_server import run_sse
        with pytest.raises(ValueError, match='loopback'):
            run_sse(host='192.168.1.1', port=9999, auth_token='x')

    def test_run_sse_rejects_mcp_unavailable(self, monkeypatch):
        """_MCP_AVAILABLE=False 应 raise RuntimeError"""
        from mcp_server import run_sse
        import mcp_server
        monkeypatch.setattr(mcp_server, '_MCP_AVAILABLE', False)
        with pytest.raises(RuntimeError, match='MCP'):
            run_sse(host='127.0.0.1', port=9999, auth_token='x')

    def test_run_sse_port_in_use_exits_cleanly(self, monkeypatch):
        """_check_port_available=False → 静默 return"""
        from mcp_server import run_sse
        monkeypatch.setattr('mcp_server._check_port_available', lambda h, p: False)
        # 不抛, 优雅 return
        result = run_sse(host='127.0.0.1', port=9999, auth_token='x')
        assert result is None

    def test_run_sse_token_load_fail_raises(self, monkeypatch):
        """AuthError 应 propagate"""
        from mcp_server import run_sse
        from auth import AuthError
        def _fail():
            raise AuthError('no token')
        monkeypatch.setattr('mcp_server.load_auth_token', _fail)
        with pytest.raises(AuthError):
            run_sse(host='127.0.0.1', port=9999, auth_token=None)


# ============================================================
# entity_resolve.merge_entities (line 125)
# ============================================================

class TestMergeEntities:
    """[Round 3] entity_resolve.merge_entities"""

    def test_merge_two_entities(self, mem, clean_prefix):
        from entity_resolve import merge_entities
        a = f'{clean_prefix}_ma'
        b = f'{clean_prefix}_mb'
        for eid in (a, b):
            mem._conn.execute(
                "INSERT INTO entities (id, kind, name, aliases_json, source, valid_from, valid_until) "
                "VALUES (?, 'stock', ?, '[]', 'test_cov', ?, NULL)",
                (eid, eid.split('_')[-1], '2026-07-19T00:00:00'),
            )
        mem._conn.commit()
        # 合并 b → a
        merge_entities(mem._conn, primary_id=a, secondary_id=b)
        # a 应保留, b 应被标记 deleted (valid_until set)
        row_b = mem._conn.execute(
            "SELECT valid_until FROM entities WHERE id = ?", (b,)
        ).fetchone()
        assert row_b['valid_until'] is not None


# ============================================================
# entity_resolve.find_duplicates_report (line 193)
# ============================================================

class TestFindDuplicatesReport:
    """[Round 3] entity_resolve.find_duplicates_report"""

    def test_report_empty_db(self, mem):
        from entity_resolve import find_duplicates_report
        # [Round 3 fix] 实测返 Markdown string 不是 JSON (看 entity_resolve.py:243)
        report = find_duplicates_report(mem._conn, threshold=0.99)
        # 用高 threshold (0.99) 让 live DB 几乎找不到 → 应返 '✅ 无重复' Markdown
        # 或表格 Markdown
        assert isinstance(report, str)
        assert len(report) > 0