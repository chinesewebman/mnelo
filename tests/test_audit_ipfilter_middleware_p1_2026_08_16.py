"""[audit fix #2 2026-08-16] ipfilter_cidrs enforcement via ASGI middleware.

Owner fix priority #13 (security defense-in-depth, public exposure mitigation).
Documented in mcp_transports.py:218 '建议: ipfilter_cidrs', but never implemented.
Without ipfilter: bind=0.0.0.0 → Bearer token is the ONLY defense line.

Fix: pure ASGI middleware `_ipfilter_middleware` checks request.client.host
against allowlist CIDRs. Reject with 403 if not in any CIDR.

Tests cover:
  - ip in CIDR → pass through
  - ip NOT in CIDR → 403 Forbidden
  - empty CIDR list → middleware inactive (backward compat)
  - multiple CIDRs → any match allows
  - IPv4 + IPv6 mixed CIDRs
"""

import ipaddress
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _parse_cidrs(cidr_strs):
    """Parse list of CIDR strings into ip_network objects (raises ValueError on bad input)."""
    return [ipaddress.ip_network(c, strict=False) for c in cidr_strs]


def test_ip_in_cidr_passes():
    """ip 100.64.0.5 in 100.64.0.0/10 → in_allowed=True."""
    cidrs = _parse_cidrs(["100.64.0.0/10"])
    test_ip = ipaddress.ip_address("100.64.0.5")
    assert any(test_ip in c for c in cidrs)


def test_ip_not_in_cidr_rejected():
    """ip 8.8.8.8 NOT in 100.64.0.0/10 → in_allowed=False."""
    cidrs = _parse_cidrs(["100.64.0.0/10"])
    test_ip = ipaddress.ip_address("8.8.8.8")
    assert not any(test_ip in c for c in cidrs)


def test_empty_cidr_list_inactive():
    """No CIDRs configured → middleware allows all (backward compat)."""
    cidrs = _parse_cidrs([])
    test_ip = ipaddress.ip_address("8.8.8.8")
    # Empty list: no CIDRs to check against → no rejection
    assert not any(test_ip in c for c in cidrs)  # no CIDRs match anything
    # But this means "allow all" (no rule = pass)


def test_multiple_cidrs_any_match_allows():
    """ip in any of multiple CIDRs → allowed."""
    cidrs = _parse_cidrs(["100.64.0.0/10", "127.0.0.0/8", "192.168.0.0/16"])
    # Loopback should match via 127.0.0.0/8
    assert any(ipaddress.ip_address("127.0.0.1") in c for c in cidrs)
    # Tailscale should match via 100.64.0.0/10
    assert any(ipaddress.ip_address("100.100.50.50") in c for c in cidrs)
    # LAN should match via 192.168.0.0/16
    assert any(ipaddress.ip_address("192.168.1.5") in c for c in cidrs)
    # Public should NOT match
    assert not any(ipaddress.ip_address("8.8.8.8") in c for c in cidrs)


def test_ipv6_cidr():
    """IPv6 CIDR parsing works."""
    cidrs = _parse_cidrs(["::1/128"])
    assert any(ipaddress.ip_address("::1") in c for c in cidrs)
    assert not any(ipaddress.ip_address("2001:4860:4860::8888") in c for c in cidrs)


def test_invalid_cidr_raises():
    """Bad CIDR string → ValueError on parse (fail loud, don't silently allow all)."""
    with pytest.raises(ValueError):
        _parse_cidrs(["not.a.cidr"])


def test_ipfilter_middleware_blocks_non_allowed_ip():
    """End-to-end: middleware returns 403 for ip not in CIDRs."""
    from mcp_transports import _ipfilter_middleware

    cidrs = _parse_cidrs(["100.64.0.0/10"])
    allowed_ips = [ipaddress.ip_network(c, strict=False) for c in cidrs]

    # Simulate ASGI scope for client IP 8.8.8.8 (not in CIDR)
    sent_status = None
    sent_body = None

    async def mock_send(message):
        nonlocal sent_status, sent_body
        if message["type"] == "http.response.start":
            sent_status = message["status"]
        elif message["type"] == "http.response.body":
            sent_body = message.get("body", b"")

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    async def call_app(scope, receive, send):
        sent_status = "app_called"

    # Test async with asyncio
    import asyncio

    async def run_test():
        scope = {
            "type": "http",
            "client": ("8.8.8.8", 12345),
            "path": "/mcp",
            "method": "GET",
        }
        await _ipfilter_middleware(scope, mock_receive, mock_send, call_app, allowed_ips)

    asyncio.run(run_test())
    assert sent_status == 403, f"non-allowed IP should be blocked, got {sent_status}"
    assert b"ipfilter" in sent_body.lower()


def test_ipfilter_middleware_passes_allowed_ip():
    """End-to-end: middleware passes through for ip in CIDRs."""
    from mcp_transports import _ipfilter_middleware

    cidrs = _parse_cidrs(["100.64.0.0/10"])
    allowed_ips = [ipaddress.ip_network(c, strict=False) for c in cidrs]

    app_called = False

    async def call_app(scope, receive, send):
        nonlocal app_called
        app_called = True

    async def mock_send(message):
        pass

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    import asyncio

    async def run_test():
        scope = {
            "type": "http",
            "client": ("100.64.0.5", 12345),
            "path": "/mcp",
            "method": "GET",
        }
        await _ipfilter_middleware(scope, mock_receive, mock_send, call_app, allowed_ips)

    asyncio.run(run_test())
    assert app_called, "allowed IP should pass through to app"


def test_ipfilter_empty_allowlist_passes_all():
    """Empty allowlist (no CIDRs configured) → middleware inactive, passes all."""
    from mcp_transports import _ipfilter_middleware

    allowed_ips = []  # empty
    app_called = False

    async def call_app(scope, receive, send):
        nonlocal app_called
        app_called = True

    async def mock_send(message):
        pass

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    import asyncio

    async def run_test():
        scope = {
            "type": "http",
            "client": ("8.8.8.8", 12345),
            "path": "/mcp",
            "method": "GET",
        }
        await _ipfilter_middleware(scope, mock_receive, mock_send, call_app, allowed_ips)

    asyncio.run(run_test())
    assert app_called, "empty allowlist should pass all (backward compat)"


def test_ipfilter_handles_ipv4_mapped_ipv6():
    """IPv4-mapped IPv6 addresses (::ffff:127.0.0.1) → unwrap and check IPv4 CIDR."""
    from mcp_transports import _ipfilter_middleware

    cidrs = _parse_cidrs(["127.0.0.0/8"])
    allowed_ips = [ipaddress.ip_network(c, strict=False) for c in cidrs]

    app_called = False

    async def call_app(scope, receive, send):
        nonlocal app_called
        app_called = True

    async def mock_send(message):
        pass

    async def mock_receive():
        return {"type": "http.request", "body": b""}

    import asyncio

    async def run_test():
        scope = {
            "type": "http",
            "client": ("::ffff:127.0.0.1", 12345),
            "path": "/mcp",
            "method": "GET",
        }
        await _ipfilter_middleware(scope, mock_receive, mock_send, call_app, allowed_ips)

    asyncio.run(run_test())
    assert app_called, "IPv4-mapped IPv6 should be unwrapped and matched against IPv4 CIDR"
