"""[8/8 Tailscale multi-agent] host 白名单扩展 — 接受 Tailscale CGNAT.

[8/8 决策] 主人拍板 mnelo 改成 multi-agent 远程调用. P2-1 单机 loopback
限制解除, 改接受:
  - 127.0.0.0/8 (loopback, 单机本地)
  - 100.64.0.0/10 (Tailscale CGNAT, 跨 vps mesh)
  - localhost (DNS alias)

拒绝:
  - 192.168.x.x (LAN, 同 WiFi 攻击面)
  - IPv6 link-local (fe80::)
  - 0.0.0.0/:: (绑定用, 不验证来源)

[测试矩阵]
  1. loopback 接受: 127.0.0.1, 127.0.0.53, localhost
  2. Tailscale CGNAT 接受: 100.83.50.99, 100.64.0.1, 100.127.255.254
  3. LAN 拒绝: 192.168.3.91, 10.0.0.1
  4. 公网拒绝: 8.8.8.8, 1.1.1.1 (无 Tailscale 也不能直连)
  5. IPv6 拒绝: fe80::1, ::1 (loopback IPv6 主动拒绝, Tailscale 是 IPv4)
  6. 绑定地址 0.0.0.0 单独路径 (不验证)
"""

import importlib.util as _ilu
import ipaddress
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


def _load_from_repo(mod_name: str):
    target = str(_REPO / f"{mod_name}.py")
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "__file__", None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mcp_repo = _load_from_repo("mcp_server")


# ============================================================
# 黑名单 — 必须抛 ValueError
# ============================================================


class TestHostValidation:
    """[8/8] host 白名单 validator 测试."""

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",  # loopback
            "127.0.0.53",  # macOS resolver loopback
            "127.255.255.254",  # upper bound loopback
            "localhost",  # DNS alias
        ],
    )
    def test_loopback_accepted(self, host):
        """loopback 接受 (单机本地 backward compat)."""
        _mcp_repo._validate_loopback_host(host)  # 不抛 = pass

    @pytest.mark.parametrize(
        "host",
        [
            "100.83.50.99",  # macbook Tailscale IP (主人 SOUL 已知)
            "100.64.0.1",  # Tailscale CGNAT lower bound
            "100.127.255.254",  # Tailscale CGNAT upper bound
            "100.100.100.100",  # typical Tailscale assignment
        ],
    )
    def test_tailscale_cgnat_accepted(self, host):
        """Tailscale CGNAT (100.64.0.0/10) 接受 — 跨 vps mesh."""
        _mcp_repo._validate_loopback_host(host)

    @pytest.mark.parametrize(
        "host",
        [
            "192.168.3.91",  # 主人 LAN IP (macbook)
            "192.168.1.1",  # 常见家用 router
            "10.0.0.1",  # 10.0.0.0/8 私网
            "172.16.0.1",  # 172.16.0.0/12 私网
            "172.30.219.254",  # 主人 macbook 看到的 Docker bridge IP
        ],
    )
    def test_lan_rejected(self, host):
        """LAN 私网拒绝 — 同 WiFi 攻击面太大, 需走 Tailscale VPN."""
        with pytest.raises(ValueError) as exc_info:
            _mcp_repo._validate_loopback_host(host)
        assert "not allowed" in str(exc_info.value)

    @pytest.mark.parametrize(
        "host",
        [
            "8.8.8.8",  # Google DNS
            "1.1.1.1",  # Cloudflare DNS
            "208.67.222.222",  # OpenDNS
            "209.54.106.233",  # nanobot 公网 IP (skill 已知)
        ],
    )
    def test_public_ip_rejected(self, host):
        """公网 IP 拒绝 — 暴露公网 = 攻击面, 必须走 Tailscale VPN."""
        with pytest.raises(ValueError):
            _mcp_repo._validate_loopback_host(host)

    @pytest.mark.parametrize(
        "host",
        [
            "fe80::1",  # IPv6 link-local
            "fe80::dead:beef",  # IPv6 link-local
            "::1",  # IPv6 loopback (Tailscale 主要走 IPv4)
            "2001:db8::1",  # IPv6 documentation
        ],
    )
    def test_ipv6_rejected(self, host):
        """IPv6 拒绝 (Tailscale multi-agent 现阶段只走 IPv4)."""
        with pytest.raises(ValueError):
            _mcp_repo._validate_loopback_host(host)

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",  # bind 任意 (--host 0.0.0.0 启动时)
            "::",  # IPv6 bind 任意
        ],
    )
    def test_bind_any_passes(self, host):
        """bind 任意地址单独路径 — _validate_loopback_host 不挡启动."""
        # 0.0.0.0 / :: 是 bind 用途, 不验证. 启动后通过 ipfilter 限制来源.
        _mcp_repo._validate_loopback_host(host)


class TestTailscaleCIDRHelper:
    """[8/8] 独立 helper ip_in_tailscale_cgnat 测试 — 未来其他模块复用."""

    @pytest.mark.parametrize(
        "ip",
        [
            "100.64.0.0",
            "100.64.0.1",
            "100.83.50.99",
            "100.127.255.254",
        ],
    )
    def test_in_tailscale_cgnat(self, ip):
        """100.64.0.0/10 范围内返回 True."""
        assert _mcp_repo._ip_in_tailscale_cgnat(ip) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "100.63.255.255",  # lower bound - 1
            "100.128.0.0",  # upper bound + 1
            "127.0.0.1",  # loopback
            "192.168.1.1",  # LAN
            "8.8.8.8",  # 公网
        ],
    )
    def test_not_in_tailscale_cgnat(self, ip):
        """100.64.0.0/10 范围外返回 False."""
        assert _mcp_repo._ip_in_tailscale_cgnat(ip) is False

    def test_invalid_ip_returns_false(self):
        """无效 IP 字符串返回 False (不抛)."""
        assert _mcp_repo._ip_in_tailscale_cgnat("not-an-ip") is False
        assert _mcp_repo._ip_in_tailscale_cgnat("") is False
        assert _mcp_repo._ip_in_tailscale_cgnat("999.999.999.999") is False
