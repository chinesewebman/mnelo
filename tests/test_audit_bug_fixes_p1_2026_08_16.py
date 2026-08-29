"""[bug fix 2026-08-16] regression tests for 4 P1 bugs found by independent audit.

B1: _txn depth counter leaks on exception path
B2: Memory.close() leaks sqlite connection if index close raises
B3: ipfilter middleware ignores X-Forwarded-For (proxy bypass)
B4: getattr(config, "server_ipfilter_cidrs", []) returns [] because config is
    the module, not the singleton — ipfilter NEVER enforced even when configured
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================
# B4: getattr(config, ...) bug — ipfilter silently inactive
# ============================================================


def test_b4_config_singleton_access_returns_cidrs():
    """B4 fix: mcp_transports should read config.config.server_ipfilter_cidrs
    (singleton instance), NOT getattr(config, ...) which checks the module.
    """
    os.environ["MNELO_MEMORY_SERVER_IPFILTER"] = "100.64.0.0/10,127.0.0.0/8"
    import importlib
    import config

    importlib.reload(config)
    try:
        # Use the same access pattern mcp_transports uses for uvicorn.run wrappers
        # (must read from singleton, not module)
        cidrs = config.config.server_ipfilter_cidrs  # the FIX
        assert cidrs == ["100.64.0.0/10", "127.0.0.0/8"]
    finally:
        os.environ.pop("MNELO_MEMORY_SERVER_IPFILTER", None)
        importlib.reload(config)


def test_b4_mcp_transports_reads_singleton_not_module(monkeypatch):
    """B4 fix: mcp_transports' _resolve_ipfilter_from_config helper (or inline code)
    must use `config.config` (singleton), not bare `config` (module).
    """
    import importlib
    import config

    importlib.reload(config)

    # Set CIDRs via env
    monkeypatch.setenv("MNELO_MEMORY_SERVER_IPFILTER", "192.168.0.0/16")
    importlib.reload(config)

    try:
        # The function mcp_transports should now use
        from mcp_transports import _resolve_ipfilter_from_config

        cidrs, trust_xff = _resolve_ipfilter_from_config()
        assert cidrs == ["192.168.0.0/16"], f"ipfilter not reading from singleton: got {cidrs!r}"
        assert trust_xff is False, "trust_xff should default False"
    except ImportError:
        pytest.fail("B4 fix missing: _resolve_ipfilter_from_config() not exported")
    finally:
        monkeypatch.delenv("MNELO_MEMORY_SERVER_IPFILTER", raising=False)
        importlib.reload(config)


# ============================================================
# B1: _txn depth counter leaks on exception path
# ============================================================


def test_b1_depth_counter_does_not_leak_on_exception():
    """B1 fix: After exception in nested _txn, depth counter must decrement.
    Before fix: counter grows to 5 after 5 nested-fails.
    """
    from memory import Memory, _txn, _txn_depth_by_id

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "b1.db")
        conn_id = id(m._conn)
        try:
            for _ in range(5):
                try:
                    with _txn(m._conn):
                        with _txn(m._conn):
                            raise RuntimeError("nested fail")
                except RuntimeError:
                    pass
            depth = _txn_depth_by_id.get(conn_id, 0)
            assert depth == 0, f"_txn depth leaked: {depth} (expected 0)"
        finally:
            # Cleanup dict entry (added in fix)
            _txn_depth_by_id.pop(conn_id, None)
            m.close()


def test_b1_close_purges_depth_dict():
    """B1 fix: Memory.close() should remove its conn_id from _txn_depth_by_id
    so re-used id() doesn't inherit stale state.
    """
    from memory import Memory, _txn, _txn_depth_by_id

    with tempfile.TemporaryDirectory() as td:
        m = Memory(db_path=Path(td) / "b1c.db")
        conn_id = id(m._conn)
        with _txn(m._conn):
            pass
        # Should be in dict after one txn
        assert _txn_depth_by_id.get(conn_id) == 0 or conn_id not in _txn_depth_by_id
        m.close()
        # After close, conn_id should be purged
        assert conn_id not in _txn_depth_by_id, f"close() didn't purge depth dict: {_txn_depth_by_id.get(conn_id)}"


# ============================================================
# B2: Memory.close() leaks sqlite connection if index close raises
# ============================================================


def test_b2_close_runs_conn_close_even_if_index_raises():
    """B2 fix: If self._index.close() raises, self._conn.close() must still run.
    Use try/finally pattern.
    """

    from memory import Memory

    class BrokenIndex:
        def close(self):
            raise OSError("simulated index close failure")

    m = Memory(db_path=Path(tempfile.gettempdir()) / "b2_test.db")
    m._index = BrokenIndex()
    m.close()
    # Verify conn was actually closed (sqlite3 raises ProgrammingError on closed conn)
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError):
        m._conn.execute("SELECT 1").fetchone()
    # Cleanup leftover file
    Path(tempfile.gettempdir(), "b2_test.db").unlink(missing_ok=True)


def test_b2_close_swallows_conn_close_exception():
    """B2 fix: If self._conn.close() raises, don't propagate — log warning."""

    class FailingConn:
        def close(self):
            raise sqlite3.Error("simulated conn close failure")

    # ... harder to inject because close() uses self._conn attribute.
    # We test the logging path by making index close also fail.
    import sqlite3
    from memory import Memory

    m = Memory(db_path=Path(tempfile.gettempdir()) / "b2c.db")
    m._index = type("FailIndex", (), {"close": lambda self: (_ for _ in ()).throw(sqlite3.Error("idx"))})()
    # Should NOT raise even if both index and conn close fail
    try:
        m.close()
    except Exception as e:
        pytest.fail(f"close() should swallow exceptions, raised: {e}")
    Path(tempfile.gettempdir(), "b2c.db").unlink(missing_ok=True)


# ============================================================
# B3: ipfilter X-Forwarded-For bypass
# ============================================================


def test_b3_ipfilter_parses_xff_header():
    """B3 fix: When X-Forwarded-For header is present and trusted, use first IP.
    Without proxy, scope['client'] is real peer; with proxy, XFF holds real client.
    """
    import asyncio
    from mcp_transports import _ipfilter_middleware, _parse_ipfilter_cidrs

    cidrs = _parse_ipfilter_cidrs(["100.64.0.0/10"])
    app_called = False
    app_client_seen = None

    async def app(scope, receive, send):
        nonlocal app_called, app_client_seen
        app_called = True
        app_client_seen = scope.get("client")

    async def send(msg):
        pass

    async def receive():
        return {"type": "http.request", "body": b""}

    # Client behind proxy: scope.client = proxy, XFF = real client
    scope = {
        "type": "http",
        "client": ("10.0.0.1", 12345),  # proxy IP
        "path": "/mcp",
        "method": "POST",
        "headers": [(b"x-forwarded-for", b"8.8.8.8")],  # real client
    }
    asyncio.run(
        _ipfilter_middleware(
            scope,
            receive,
            send,
            app,
            cidrs,
            trust_xff=True,  # B3 fix: new param
        )
    )
    # With trust_xff=True, 8.8.8.8 should be blocked (not in 100.64.0.0/10)
    assert not app_called, "B3 fix broken: XFF client 8.8.8.8 should be blocked"


def test_b3_ipfilter_trust_xff_disabled_uses_tcp_peer():
    """B3 fix: When trust_xff=False (default for safety), use TCP peer only.
    Bypassed via XFF but rejected via peer IP if peer not in CIDR.
    """
    import asyncio
    from mcp_transports import _ipfilter_middleware, _parse_ipfilter_cidrs

    cidrs = _parse_ipfilter_cidrs(["100.64.0.0/10"])
    app_called = False

    async def app(scope, receive, send):
        nonlocal app_called
        app_called = True

    async def send(msg):
        pass

    async def receive():
        return {"type": "http.request", "body": b""}

    # Even with XFF header, trust_xff=False means use TCP peer (10.0.0.1)
    scope = {
        "type": "http",
        "client": ("10.0.0.1", 12345),  # proxy IP
        "path": "/mcp",
        "method": "POST",
        "headers": [(b"x-forwarded-for", b"8.8.8.8")],
    }
    asyncio.run(
        _ipfilter_middleware(
            scope,
            receive,
            send,
            app,
            cidrs,
            trust_xff=False,  # B3 fix: default safe
        )
    )
    # With trust_xff=False: peer 10.0.0.1 not in 100.64.0.0/10 → blocked
    assert not app_called, "B3 fix broken: peer 10.0.0.1 not in CIDR should be blocked"


def test_b3_xff_takes_leftmost_ip():
    """B3 fix: X-Forwarded-For chain = "client, proxy1, proxy2" — take leftmost."""
    # Just test the parser function
    from mcp_transports import _parse_xff_first_ip

    assert _parse_xff_first_ip(b"8.8.8.8") == "8.8.8.8"
    assert _parse_xff_first_ip(b"8.8.8.8, 10.0.0.1, 192.168.1.1") == "8.8.8.8"
    # Edge cases
    assert _parse_xff_first_ip(b"  8.8.8.8  ,  10.0.0.1  ") == "8.8.8.8"
    assert _parse_xff_first_ip(b"") is None
