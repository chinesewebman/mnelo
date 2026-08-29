"""S2 — MneloClient.get_digest() (TASKS_L2_SESSION_STATE §1.3A).

[8/5 P0-fix] 客户端包装 memory_get_digest MCP tool, 返回 dict.

[8/5 P1] 用 _load_from_repo 风格加载 mnelo_client, 跟 tests/conftest.py
对 memory.py 的策略一致 (避免其它测试对 sys.modules['api.mnelo_client']
引用错位导致 'has no attribute get_digest').
"""

import importlib.util as _ilu
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"


def _load_mnelo_client():
    """[8/5 P1] 跟 tests/conftest.py 的 _load_from_repo 一致, 隔离 sys.modules 干扰."""
    if "mnelo_client" in sys.modules and hasattr(sys.modules["mnelo_client"], "MneloClient"):
        mod = sys.modules["mnelo_client"]
        # 重置 class 方法集合 — 单测期间我们要看最新方法
        importlib = __import__("importlib")
        importlib.reload(mod)
    spec = _ilu.spec_from_file_location("mnelo_client", API_DIR / "mnelo_client.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules["mnelo_client"] = mod
    spec.loader.exec_module(mod)
    return mod


_mc = _load_mnelo_client()


def _make_client(_call_return):
    """Make MneloClient with mocked _call() that returns _call_return."""
    client = _mc.MneloClient.__new__(_mc.MneloClient)
    client._call = MagicMock(return_value=_call_return)
    return client


def test_s2_get_digest_no_ref_passes_through():
    """[S2 验收] client.get_digest() 无 ref → 调 memory_get_digest tool, 无参透传."""
    digest_dict = {
        "enabled": True,
        "content": "...",
        "chunk_id": "c1",
        "line_refs": {},
        "truncated": False,
        "built_at": "2026-08-05",
    }
    client = _make_client(digest_dict)
    result = client.get_digest()
    assert result == digest_dict
    args = client._call.call_args[0][1]
    assert args == {}, f"无 ref 应透传空 args, got {args}"


def test_s2_get_digest_with_ref_passes_through():
    """[S2 验收] client.get_digest(ref=5) → 调 memory_get_digest, ref='5' 透传."""
    digest_dict = {"source_chunks": [{"id": "x"}]}
    client = _make_client(digest_dict)
    result = client.get_digest(ref="5")
    assert result == digest_dict
    args = client._call.call_args[0][1]
    assert args == {"ref": "5"}


def test_s2_get_digest_uses_memory_get_digest_tool_name():
    """[S2 验收] tool name 必须是 'memory_get_digest' (跟 S1 注册一致)."""
    client = _make_client({})
    client.get_digest()
    tool_name = client._call.call_args[0][0]
    assert tool_name == "memory_get_digest", f"tool name 应 memory_get_digest, got {tool_name}"


def test_s2_get_digest_docstring_present():
    """[S2 验收] 客户端 get_digest 必须有 docstring."""
    assert _mc.MneloClient.get_digest.__doc__, "MneloClient.get_digest 必须有 docstring"
