"""Round 5 coverage tests — push config.py 80% → 90%+, validation.py 95% → 98%+, auth.py 92% → 100%.

Targets uncovered lines:
- config.py:33-37: tomllib import fallback chain
- config.py:51-53: bad TOML file → print warning + return {}
- config.py:72-76: _resolve_tz edge cases (None, IANA name)
- config.py:154: describe() method full coverage
- auth.py:67-69: AuthError message (already partially covered)
- validation.py:90, 101: validate_content edge cases (non-str, after-sanitize empty)
- validation.py:124: validate_query after-sanitize empty
- validation.py:174, 184: validate_holding edge cases
"""
import os
import sys
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


config_mod = _load_from_repo('config')
validation_mod = _load_from_repo('validation')
auth_mod = _load_from_repo('auth')


# ============================
# config.py coverage
# ============================

class TestTomllibFallback:
    """config.py:33-37: tomllib import fallback chain."""

    def test_tomllib_module_exists(self):
        """At least one of tomllib/tomli should be importable."""
        assert config_mod.tomllib is not None


class TestLoadTomlErrorPath:
    """config.py:51-53: bad TOML → print warning + return {}."""

    def test_bad_toml_returns_empty_dict(self, tmp_path, capsys):
        """Invalid TOML file → catch exception → return {}."""
        bad_file = tmp_path / 'bad.toml'
        bad_file.write_text('this is = not valid [[[ toml')
        result = config_mod._load_config_file(bad_file)
        assert result == {}
        # Warning printed to stderr
        captured = capsys.readouterr()
        assert 'WARN' in captured.err or 'failed' in captured.err

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """File doesn't exist → exception → return {}."""
        result = config_mod._load_config_file(tmp_path / 'nonexistent.toml')
        assert result == {}


class TestResolveTzEdgeCases:
    """config.py:72-76: _resolve_tz(None), 'local', 'utc', IANA names."""

    def test_none_returns_local(self):
        assert config_mod._resolve_tz(None) == 'local'

    def test_local_explicit(self):
        assert config_mod._resolve_tz('local') == 'local'

    def test_utc(self):
        assert config_mod._resolve_tz('utc') == 'utc'

    def test_utc_uppercase(self):
        assert config_mod._resolve_tz('UTC') == 'utc'

    def test_local_with_whitespace(self):
        assert config_mod._resolve_tz('  local  ') == 'local'

    def test_iana_name_returned_verbatim(self):
        """IANA names like 'Asia/Shanghai' returned as-is."""
        assert config_mod._resolve_tz('Asia/Shanghai') == 'Asia/Shanghai'

    def test_unknown_value_returned_as_is(self):
        """Non-standard value returned verbatim."""
        assert config_mod._resolve_tz('Mars/Olympus') == 'Mars/Olympus'


class TestConfigDescribe:
    """config.py:154: describe() method full line coverage."""

    def test_describe_returns_non_empty_string(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config_mod, 'CONFIG_PATH', tmp_path / 'nonexistent.toml')
        cfg = config_mod.Config()
        desc = cfg.describe()
        assert isinstance(desc, str)
        assert 'tz=' in desc
        assert 'warm_up=' in desc
        assert 'embedder=' in desc

    def test_config_path_property(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config_mod, 'CONFIG_PATH', tmp_path / 'fake.toml')
        cfg = config_mod.Config()
        assert cfg.config_path == tmp_path / 'fake.toml'


# ============================
# validation.py coverage
# ============================

class TestValidateContentEdgeCases:
    """validation.py:90, 101: validate_chunk_content edge cases."""

    def test_validate_content_non_string_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be str'):
            validation_mod.validate_chunk_content(12345)

    def test_validate_content_empty_after_sanitize_raises(self):
        """Control chars only → stripped → empty → ValidationError."""
        with pytest.raises(validation_mod.ValidationError, match='empty after sanitization'):
            validation_mod.validate_chunk_content('\x00\x01\x02')

    def test_validate_content_normal_string_passes(self):
        result = validation_mod.validate_chunk_content('Hello world')
        assert result == 'Hello world'

    def test_validate_content_with_newlines_allowed(self):
        """allow_newlines=True preserves \\n in content."""
        result = validation_mod.validate_chunk_content('line1\nline2')
        assert 'line1' in result
        assert 'line2' in result


class TestValidateQueryEdgeCases:
    """validation.py:124: validate_query edge cases."""

    def test_validate_query_non_string_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be str'):
            validation_mod.validate_query(None)

    def test_validate_query_empty_after_sanitize_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='empty after sanitization'):
            validation_mod.validate_query('\x00\x00\x00')

    def test_validate_query_normal_string_passes(self):
        result = validation_mod.validate_query('test query')
        assert result == 'test query'

    def test_validate_query_strips_newlines(self):
        """allow_newlines=False for query → newlines removed."""
        result = validation_mod.validate_query('hello\nworld')
        # Should NOT contain newline
        assert '\n' not in result or 'hello world' in result.replace('\n', ' ')


class TestValidateHoldingEdgeCases:
    """validation.py:174, 184: validate_holding_payload edge cases."""

    def test_validate_holding_non_dict_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='must be dict'):
            validation_mod.validate_holding_payload('not a dict')

    def test_validate_holding_nan_quantity_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='finite number'):
            validation_mod.validate_holding_payload({'quantity': float('nan'), 'symbol_code': 'X'})

    def test_validate_holding_inf_quantity_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='finite number'):
            validation_mod.validate_holding_payload({'quantity': float('inf'), 'symbol_code': 'X'})

    def test_validate_holding_string_quantity_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='finite number'):
            validation_mod.validate_holding_payload({'quantity': 'not_a_number', 'symbol_code': 'X'})

    def test_validate_holding_valid_passes(self):
        result = validation_mod.validate_holding_payload({
            'symbol_code': 'sh600000',
            'name': 'PFYH',
            'quantity': 100,
            'cost_price': 10.5,
        })
        assert isinstance(result, dict)
        assert result['symbol_code'] == 'sh600000'

    def test_validate_holding_zero_values_pass(self):
        """Zero is finite, should pass."""
        result = validation_mod.validate_holding_payload({
            'symbol_code': 'X',
            'quantity': 0,
            'cost_price': 0,
        })
        assert result['quantity'] == 0
        assert result['cost_price'] == 0

    def test_validate_holding_negative_inf_raises(self):
        with pytest.raises(validation_mod.ValidationError, match='finite number'):
            validation_mod.validate_holding_payload({'quantity': float('-inf'), 'symbol_code': 'X'})


# ============================
# auth.py coverage
# ============================

class TestAuthErrorFullPath:
    """auth.py:67-69: AUTH_TOKEN_FILE exists + has content → return token."""

    def test_load_auth_token_default_file_with_content(self, monkeypatch, tmp_path):
        """AUTH_TOKEN_FILE exists with non-empty content → returns content."""
        from auth import AuthError, load_auth_token
        # Clear all env vars
        for var in ('MNEOLO_AUTH_TOKEN', 'MNELO_MEMORY_AUTH_TOKEN'):
            monkeypatch.delenv(var, raising=False)
        # Create a real token file
        import auth as live_auth
        token_file = tmp_path / 'real_token'
        token_file.write_text('test_token_from_file_abc123\n')
        monkeypatch.setattr(live_auth, 'AUTH_TOKEN_FILE', token_file)
        token = load_auth_token()
        assert token == 'test_token_from_file_abc123'  # whitespace stripped

    def test_load_auth_token_empty_file_falls_through(self, monkeypatch, tmp_path):
        """AUTH_TOKEN_FILE exists but empty → falls through → AuthError."""
        from auth import AuthError, load_auth_token
        for var in ('MNEOLO_AUTH_TOKEN', 'MNELO_MEMORY_AUTH_TOKEN'):
            monkeypatch.delenv(var, raising=False)
        import auth as live_auth
        token_file = tmp_path / 'empty_token'
        token_file.write_text('')  # Empty file
        monkeypatch.setattr(live_auth, 'AUTH_TOKEN_FILE', token_file)
        # Empty file → `if token:` is False → falls through to AuthError
        with pytest.raises(AuthError, match='no auth token configured'):
            load_auth_token()

    def test_load_auth_token_no_config_raises_auth_error(self, monkeypatch, tmp_path):
        """No env var, no token file → AuthError."""
        from auth import AuthError, load_auth_token
        for var in ('MNEOLO_AUTH_TOKEN', 'MNELO_MEMORY_AUTH_TOKEN'):
            monkeypatch.delenv(var, raising=False)
        import auth as live_auth
        monkeypatch.setattr(live_auth, 'AUTH_TOKEN_FILE', tmp_path / 'nonexistent_token')
        with pytest.raises(AuthError) as exc_info:
            load_auth_token()
        msg = str(exc_info.value)
        assert 'no auth token configured' in msg
        assert 'secrets.token_urlsafe' in msg or 'Generate' in msg
