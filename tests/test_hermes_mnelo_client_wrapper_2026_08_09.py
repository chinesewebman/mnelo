"""[8/9] HermesMneloClient wrapper 锁死 source filter 测试.

燕如 P5 实证: mnelo 没有 row-level ACL. 跨 vps 客户端必须 wrapper 锁死 source
不然 recall 看到其它客户端 chunk. 燕如写 YanruMneloClient 自锁.
我们 hermes 端镜像: HermesMneloClient 默认 source='hermes-gw', 防 hermes 端
recall 看到燕如写入.

[测试矩阵]
  1. recall 没 filter → 默认 source='hermes-gw' 自动加
  2. recall 显式 source='foo' → 保留 (信任 caller)
  3. recall 传 filters={kind:'stock'} → 跟 source 合并
  4. remember 没 source → 默认 'hermes-gw'
  5. remember 显式 source='foo' → 强制前缀 'hermes-gw/foo'
  6. remember 已是 'hermes-gw/...' → 保留
"""

import importlib.util as _ilu
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
# mnelo_remote_client.py 在 scripts/ 下, 不在 repo root
_TARGET = _REPO / "scripts" / "mnelo_remote_client.py"
sys.path.insert(0, str(_REPO))


def _load_from_repo(mod_name: str):
    target = str(_TARGET if (mod_name == "mnelo_remote_client") else _REPO / f"{mod_name}.py")
    existing = sys.modules.get(mod_name)
    if existing is not None and getattr(existing, "__file__", None) == target:
        return existing
    spec = _ilu.spec_from_file_location(mod_name, target)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mcp_repo = _load_from_repo("mnelo_remote_client")


class TestHermesMneloClient:
    """[8/9] hermes 端默认 source filter 测试."""

    def _make_client(self):
        """构造一个 fake 客户端 — 只测 wrapper 逻辑, 不真发 HTTP."""
        c = _mcp_repo.HermesMneloClient.__new__(_mcp_repo.HermesMneloClient)
        # 父类 init 跳过 — 不需 URL/token, 直接测 wrapper 逻辑
        c.token = "fake"
        c.url = "http://fake"
        c.timeout = 30
        c.session_id = None
        # 记录 _tools_call 收到的 args
        c.last_recall_args = None
        c.last_remember_args = None

        # mock _tools_call 截 args
        def fake_tools_call(name, args):
            if name == "memory_recall":
                c.last_recall_args = args
            elif name == "memory_remember":
                c.last_remember_args = args
            return {"hits": []}

        c._tools_call = fake_tools_call
        return c

    # ============================================================
    # recall 锁死 source
    # ============================================================

    def test_recall_no_filter_uses_default_source(self):
        """没 filter → 默认 source='hermes-gw' 自动加."""
        c = self._make_client()
        c.recall("query")
        assert c.last_recall_args["filters"]["source"] == "hermes-gw"

    def test_recall_explicit_source_preserved(self):
        """显式 source='foo' → 保留 (信任 caller, 不覆盖)."""
        c = self._make_client()
        c.recall("query", filters={"source": "foo"})
        assert c.last_recall_args["filters"]["source"] == "foo"

    def test_recall_filters_merged_with_source(self):
        """filters={kind:'stock'} → 跟 source 合并, 不丢其它字段."""
        c = self._make_client()
        c.recall("query", filters={"kind": "stock"})
        args = c.last_recall_args
        assert args["filters"]["source"] == "hermes-gw"
        assert args["filters"]["kind"] == "stock"

    def test_recall_filters_none_uses_default(self):
        """filters=None → 默认 source 自动加."""
        c = self._make_client()
        c.recall("query", filters=None)
        assert c.last_recall_args["filters"]["source"] == "hermes-gw"

    def test_recall_passes_top_k_and_strategy(self):
        """recall 其它参数 (top_k, strategy) 透传."""
        c = self._make_client()
        c.recall("query", top_k=10, strategy="vector_only")
        assert c.last_recall_args["top_k"] == 10
        assert c.last_recall_args["strategy"] == "vector_only"

    # ============================================================
    # remember 强制 source 前缀
    # ============================================================

    def test_remember_no_source_uses_default(self):
        """没 source → 默认 'hermes-gw'."""
        c = self._make_client()
        c.remember("content")
        assert c.last_remember_args["source"] == "hermes-gw"

    def test_remember_bare_source_gets_prefix(self):
        """显式 source='foo' → 强制前缀 'hermes-gw/foo'."""
        c = self._make_client()
        c.remember("content", source="foo")
        assert c.last_remember_args["source"] == "hermes-gw/foo"

    def test_remember_already_prefixed_preserved(self):
        """已是 'hermes-gw/...' → 保留不动."""
        c = self._make_client()
        c.remember("content", source="hermes-gw/telegram")
        assert c.last_remember_args["source"] == "hermes-gw/telegram"

    def test_remember_empty_string_uses_default(self):
        """source='' 空字符串 → 默认 'hermes-gw'."""
        c = self._make_client()
        c.remember("content", source="")
        assert c.last_remember_args["source"] == "hermes-gw"

    # ============================================================
    # class default
    # ============================================================

    def test_default_source_constant(self):
        """DEFAULT_SOURCE = 'hermes-gw' (命名空间约定)."""
        assert _mcp_repo.HermesMneloClient.DEFAULT_SOURCE == "hermes-gw"

    def test_subclass_overridable(self):
        """子类可 override DEFAULT_SOURCE (e.g. 测试场景)."""

        class TestClient(_mcp_repo.HermesMneloClient):
            DEFAULT_SOURCE = "test-source"

        c = TestClient.__new__(TestClient)
        c.token = "fake"
        c.url = "http://fake"
        c.timeout = 30
        c.session_id = None
        c.last_recall_args = None

        def fake_tools_call(name, args):
            if name == "memory_recall":
                c.last_recall_args = args
            return {"hits": []}

        c._tools_call = fake_tools_call
        c.recall("query")
        assert c.last_recall_args["filters"]["source"] == "test-source"
