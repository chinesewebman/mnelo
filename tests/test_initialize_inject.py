import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server
from mcp_server import _list_resources, _read_resource


def _mem_with_digest(content: str, chunk_id: str = "chunk_test_digest"):
    class FakeMemory:
        def __init__(self, text):
            self._text = text

        def get_digest(self, ref=None):
            return {
                "enabled": True,
                "content": self._text,
                "chunk_id": chunk_id,
                "line_refs": {"1": [chunk_id]},
                "truncated": False,
                "built_at": "2026-08-05T00:00:00",
            }

    return FakeMemory(content)


def test_initialize_does_not_register_digest_resource_by_default(monkeypatch):
    monkeypatch.setattr(mcp_server.config, "digest_inject_on_initialize", False)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", _mem_with_digest("identity: 2077 Ling"))
    result = asyncio.run(_list_resources())
    assert "memory://session/digest" not in {str(r.uri) for r in result.root.resources}


def test_initialize_registers_digest_resource_when_enabled(monkeypatch):
    monkeypatch.setattr(mcp_server.config, "digest_inject_on_initialize", True)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", _mem_with_digest("identity: 2077 Ling"))
    result = asyncio.run(_list_resources())
    assert any(str(r.uri) == "memory://session/digest" for r in result.root.resources)


def test_digest_resource_text_returns_digest(monkeypatch):
    monkeypatch.setattr(mcp_server.config, "digest_inject_on_initialize", True)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", _mem_with_digest("identity: 2077 Ling"))
    contents = asyncio.run(_read_resource("memory://session/digest")).root.contents
    text = "\n".join(getattr(c, "text", "") or getattr(c, "blob", "") for c in contents)
    assert "2077 Ling" in text


def test_digest_resource_refuses_when_disabled(monkeypatch):
    monkeypatch.setattr(mcp_server.config, "digest_inject_on_initialize", False)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", _mem_with_digest("identity: 2077 Ling"))
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(_read_resource("memory://session/digest"))


def test_digest_resource_typed_uri_accepted(monkeypatch):
    from pydantic import AnyUrl

    monkeypatch.setattr(mcp_server.config, "digest_inject_on_initialize", True)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", _mem_with_digest("identity: 2077 Ling"))
    contents = asyncio.run(_read_resource(AnyUrl("memory://session/digest"))).root.contents
    text = "\n".join(getattr(c, "text", "") or getattr(c, "blob", "") for c in contents)
    assert "2077 Ling" in text


def test_digest_resource_swallows_init_errors(monkeypatch, caplog):
    monkeypatch.setattr(mcp_server.config, "digest_inject_on_initialize", True)

    class BoomMemory:
        def get_digest(self, ref=None):
            raise RuntimeError("db down")

    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_mem_instance", None)
    monkeypatch.setattr(sys.modules["mcp_tool_dispatcher"], "_get_mem", lambda: BoomMemory())
    contents = asyncio.run(_read_resource("memory://session/digest")).root.contents
    text = "\n".join(getattr(c, "text", "") or getattr(c, "blob", "") for c in contents)
    assert text == ""
