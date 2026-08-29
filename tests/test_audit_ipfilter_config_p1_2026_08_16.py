"""[audit fix #2 2026-08-16] ipfilter config integration test.

Verify config.server_ipfilter_cidrs reads from:
  1. config.toml [server].ipfilter_cidrs
  2. env var MNELO_MEMORY_SERVER_IPFILTER (comma-separated)
  3. fallback empty list (backward compat)
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_default_ipfilter_empty_list():
    """No config + no env → server_ipfilter_cidrs = []."""
    os.environ.pop("MNELO_MEMORY_SERVER_IPFILTER", None)
    import importlib
    import config

    importlib.reload(config)
    cfg = config.Config()
    assert cfg.server_ipfilter_cidrs == []


def test_env_var_takes_priority():
    """env var MNELO_MEMORY_SERVER_IPFILTER > config.toml."""
    os.environ["MNELO_MEMORY_SERVER_IPFILTER"] = "100.64.0.0/10,127.0.0.0/8"
    import importlib
    import config

    importlib.reload(config)
    cfg = config.Config()
    assert cfg.server_ipfilter_cidrs == ["100.64.0.0/10", "127.0.0.0/8"]
    del os.environ["MNELO_MEMORY_SERVER_IPFILTER"]


def test_invalid_ipfilter_in_config_warns_and_ignores(tmp_path, capsys):
    """config.toml ipfilter_cidrs as string → WARN to stderr, ignore (empty)."""
    import importlib
    import config

    bad_config = tmp_path / "config.toml"
    bad_config.write_text('[server]\nipfilter_cidrs = "not-a-list"  # should be list\n')
    os.environ["MNELO_MEMORY_CONFIG"] = str(bad_config)
    try:
        importlib.reload(config)
        cfg = config.Config()
        assert cfg.server_ipfilter_cidrs == []
        captured = capsys.readouterr()
        assert "ipfilter_cidrs must be list" in captured.err
    finally:
        os.environ.pop("MNELO_MEMORY_CONFIG", None)
        importlib.reload(config)


def test_middleware_integration_with_config():
    """config.server_ipfilter_cidrs wires into middleware via _build_ipfilter_wrapper."""
    import ipaddress
    from mcp_transports import _build_ipfilter_wrapper, _parse_ipfilter_cidrs

    cidrs = _parse_ipfilter_cidrs(["100.64.0.0/10"])
    assert cidrs == [ipaddress.ip_network("100.64.0.0/10")]

    async def inner_app(scope, receive, send):
        pass

    wrapped = _build_ipfilter_wrapper(inner_app, cidrs)
    assert wrapped is not inner_app, "should wrap when CIDRs non-empty"

    empty_cidrs = _parse_ipfilter_cidrs([])
    unwrapped = _build_ipfilter_wrapper(inner_app, empty_cidrs)
    assert unwrapped is inner_app, "should NOT wrap when CIDRs empty"


def test_config_toml_loads_ipfilter(tmp_path):
    """Real config.toml with ipfilter_cidrs loads correctly."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[server]\nhost = \"127.0.0.1\"\nport = 8086\nipfilter_cidrs = ['100.64.0.0/10', '127.0.0.0/8']\n")
    os.environ["MNELO_MEMORY_CONFIG"] = str(cfg_file)
    try:
        import importlib
        import config

        importlib.reload(config)
        cfg = config.Config()
        assert cfg.server_ipfilter_cidrs == ["100.64.0.0/10", "127.0.0.0/8"]
    finally:
        os.environ.pop("MNELO_MEMORY_CONFIG", None)
        importlib.reload(config)
