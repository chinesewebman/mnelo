"""Round 17 — mcp_server.py 🌳 echo format tests.

The MCP call_tool handler now emits a 2-block response:
  [0] = 🌳 mnelo {verb} {key_fact}    (human-readable echo)
  [1] = {json result}                  (machine-readable)

These tests lock the format so future refactors don't accidentally drop or
change the echo prefix. Each test invokes the actual mcp_server.py via stdio
(same path Hermes uses) and asserts on the echoed TextContent block.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "mcp_server.py"
MNELO_HOME = os.environ.get("MNELO_HOME", "/Users/apple/.hermes")


def call_mcp(tool_name: str, arguments: dict, *, transport: str = "stdio"):
    """Send initialize + tools/call via stdio MCP, return parsed JSON-RPC responses."""
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "echo-test", "version": "1.0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    payload = json.dumps(init) + "\n" + json.dumps(initialized) + "\n" + json.dumps(call) + "\n"

    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--transport", transport],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO),
        env={**os.environ, "MNELO_HOME": MNELO_HOME},
    )
    responses = []
    for line in r.stdout.split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                responses.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return responses


def get_echo_block(responses) -> str:
    """Extract the 🌳 echo TextContent block from tools/call response."""
    for r in responses:
        if r.get("id") == 2:
            contents = r.get("result", {}).get("content", [])
            if contents:
                return str(contents[0].get("text", ""))
    return ""


def get_json_block(responses) -> str:
    """Extract the JSON result TextContent block from tools/call response."""
    for r in responses:
        if r.get("id") == 2:
            contents = r.get("result", {}).get("content", [])
            if len(contents) >= 2:
                return str(contents[1].get("text", ""))
            elif contents:
                return str(contents[0].get("text", ""))
    return ""


class TestEchoPrefix:
    """All echo lines start with '🌳 mnelo'."""

    def test_remember_echo_prefix(self):
        responses = call_mcp("memory_remember", {"content": "round17_echo_test_remember", "importance": 0.5})
        echo = get_echo_block(responses)
        assert echo, "no echo block returned"
        assert echo.startswith("🌳 mnelo"), f"missing prefix: {echo!r}"

    def test_recall_echo_prefix(self):
        responses = call_mcp("memory_recall", {"query": "round17_echo_test", "top_k": 2})
        echo = get_echo_block(responses)
        assert echo
        assert echo.startswith("🌳 mnelo")

    def test_stats_echo_prefix(self):
        responses = call_mcp("memory_stats", {})
        echo = get_echo_block(responses)
        assert echo
        assert echo.startswith("🌳 mnelo")


class TestEchoContent:
    """Each echo contains the key fact about the operation."""

    def test_remember_echo_contains_chunk_id(self):
        responses = call_mcp("memory_remember", {"content": "round17_chunkid_check", "importance": 0.7})
        echo = get_echo_block(responses)
        assert echo
        assert re.search(r"\+chunk_\d{8}_\d{6}_\d{6}", echo), f"no chunk_id: {echo!r}"
        assert "importance=0.7" in echo, f"no importance: {echo!r}"

    def test_recall_echo_contains_hits_count(self):
        responses = call_mcp("memory_recall", {"query": "round17", "top_k": 5})
        echo = get_echo_block(responses)
        assert echo
        assert "~" in echo, f"missing hit marker: {echo!r}"
        assert "hits" in echo, f"missing 'hits': {echo!r}"
        assert "top=" in echo, f"missing top method: {echo!r}"

    def test_stats_echo_contains_counts(self):
        responses = call_mcp("memory_stats", {})
        echo = get_echo_block(responses)
        assert echo
        assert "chunks=" in echo
        assert "entities=" in echo
        assert "vectors=" in echo

    def test_recall_echo_zero_or_low_hits(self):
        responses = call_mcp("memory_recall", {"query": "absolutely_unique_xyz_no_match_zzz_2077", "top_k": 1})
        echo = get_echo_block(responses)
        assert echo
        # Even with no matches, recall still returns up to top_k=1, so we
        # accept either "~0 hits" or "~1 hits" with proper format.
        if "~1 hits" in echo:
            assert "top=" in echo


class TestEchoCanBeDisabled:
    """MNELO_ECHO=0 disables echo entirely."""

    def test_echo_disabled_with_env(self):
        payload = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1.0"},
                    },
                }
            )
            + "\n"
        )
        payload += json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        payload += (
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_stats", "arguments": {}}}
            )
            + "\n"
        )
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--transport", "stdio"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO),
            env={**os.environ, "MNELO_HOME": MNELO_HOME, "MNELO_ECHO": "0"},
        )
        # Find the tools/call response — should have only 1 block (the JSON), no echo
        for line in r.stdout.split("\n"):
            line = line.strip()
            if line.startswith("{") and '"id":2' in line:
                resp = json.loads(line)
                contents = resp.get("result", {}).get("content", [])
                assert len(contents) == 1, f"echo not disabled: got {len(contents)} blocks"
                assert "🌳" not in str(contents[0].get("text", "")), "echo present despite MNELO_ECHO=0"
                return
        raise AssertionError("no tools/call response found")


class TestJsonBlockPreserved:
    """The 2nd TextContent block must remain the JSON result (no breaking change)."""

    def test_json_is_valid(self):
        responses = call_mcp("memory_stats", {})
        json_text = get_json_block(responses)
        assert json_text
        parsed = json.loads(json_text)
        # memory_stats returns dict with chunks/entities/relations/vectors
        assert "chunks" in parsed
        assert "entities" in parsed

    def test_remember_json_has_chunk_id(self):
        responses = call_mcp("memory_remember", {"content": "round17_json_check_2077", "importance": 0.5})
        json_text = get_json_block(responses)
        assert json_text
        parsed = json.loads(json_text)
        assert "chunk_id" in parsed
        assert parsed["status"] == "ok"
        assert parsed["chunk_id"].startswith("chunk_")
