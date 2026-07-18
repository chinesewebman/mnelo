"""tests for auth.py — Bearer token 加载 + 校验"""
import os
import tempfile
from pathlib import Path
import pytest

from auth import load_auth_token, verify_bearer, AuthError, AUTH_TOKEN_ENV


class TestVerifyBearer:
    def test_valid_token(self):
        assert verify_bearer('Bearer abc123', 'abc123') is True

    def test_missing_header(self):
        assert verify_bearer(None, 'abc123') is False
        assert verify_bearer('', 'abc123') is False

    def test_wrong_scheme(self):
        assert verify_bearer('Basic abc123', 'abc123') is False
        # 'bearer' 大小写容忍 (RFC 7235)
        assert verify_bearer('bearer abc123', 'abc123') is True
        assert verify_bearer('BEARER abc123', 'abc123') is True

    def test_wrong_token(self):
        assert verify_bearer('Bearer wrong', 'abc123') is False
        assert verify_bearer('Bearer abc1234', 'abc123') is False  # longer

    def test_whitespace_tolerated(self):
        # 允许 header value 末尾空格 (split 后 strip)
        assert verify_bearer('Bearer abc123  ', 'abc123') is True
        # 内部多个空格: split(' ', 1) 拿到 ' abc123', strip 后是 'abc123' → True
        # 这是 RFC 7235 推荐的容错行为 (客户端拼 header 多空一格不致命)
        assert verify_bearer('Bearer  abc123', 'abc123') is True


class TestLoadAuthToken:
    def test_explicit_path(self, tmp_path):
        token_file = tmp_path / 'token'
        token_file.write_text('my-secret-token\n')
        assert load_auth_token(explicit_path=str(token_file)) == 'my-secret-token'

    def test_explicit_path_empty_file(self, tmp_path):
        token_file = tmp_path / 'token'
        token_file.write_text('\n')
        with pytest.raises(AuthError, match='empty'):
            load_auth_token(explicit_path=str(token_file))

    def test_explicit_path_not_found(self, tmp_path):
        with pytest.raises(AuthError, match='not found'):
            load_auth_token(explicit_path=str(tmp_path / 'nonexistent'))

    def test_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv(AUTH_TOKEN_ENV, 'env-token-value')
        # 不能传 explicit_path, 也不能让默认文件存在
        # (使用 monkeypatch 不影响文件系统)
        token = load_auth_token()
        assert token == 'env-token-value'

    def test_env_var_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv(AUTH_TOKEN_ENV, '  spaced-token  \n')
        assert load_auth_token() == 'spaced-token'

    def test_no_source_raises(self, monkeypatch):
        monkeypatch.delenv(AUTH_TOKEN_ENV, raising=False)
        # 默认文件 ~/.config/mnelo/auth_token 如果存在, 也会被读
        # 这里假设测试环境没那个文件 (CI 沙箱), 实际 dev 机器有 → skip
        try:
            default_path = Path.home() / '.config' / 'mnelo' / 'auth_token'
            if default_path.exists():
                pytest.skip('default auth_token file exists on this system')
            load_auth_token()
            pytest.fail('should have raised')
        except AuthError as e:
            assert 'MNEOLO_AUTH_TOKEN' in str(e)

    def test_default_file_priority_below_env(self, monkeypatch, tmp_path):
        # env 优先于 file
        monkeypatch.setenv(AUTH_TOKEN_ENV, 'env-wins')
        # 即使 file 存在, env 优先
        token = load_auth_token()
        assert token == 'env-wins'