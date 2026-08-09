#!/usr/bin/env python3
"""
mnelo_remote_client.py — Tailscale mesh 另一台机器调 mnelo MCP server 的 thin wrapper.

[8/8 Tailscale multi-agent] 主人 macbook 上 mnelo mcp_server 绑 0.0.0.0:8086,
Tailscale mesh 上其他 agent (vps nanobot / 别的 Mac) 通过这台 client 直接调
mcp_server's HTTP API. 走 streamable-http (MCP 2025-06-18 spec).

[与 mnelo_echo.py 的区别]
- mnelo_echo.py: 同机, 通过 Python API 直接调 Memory 类 (直连 SQLite).
- mnelo_remote_client.py: 跨机器, 通过 Bearer-auth HTTP 调 mcp_server.
  走 Hermes A2A pattern 类似 (nanobot-ops skill), 但目标是 mnelo 不是 nanobot.

[setup]
1. 主人 macbook Tailscale IP (e.g. 100.83.50.99) — 已经在主人 mesh
2. mnelo mcp_server --host 0.0.0.0 (commit 70f3cf4 docs §1.5 + 3e538de 接受 Tailscale CGNAT)
3. 共享 ~/.config/mnelo/auth_token (按 owner 同意分发)
4. 改 MNELO_REMOTE_URL env var 指向 macbook Tailscale IP

[5 业务函数]
- recall(query, top_k, filters)  → 命中 hits + rrf 分数
- remember(content, source, importance, entities)  → 写入 chunk_id
- forget(chunk_id)  → 软删
- get_digest()  → session digest
- stats()  → chunks / entities / vectors 计数

[tokens environment]
- MNELO_REMOTE_URL  默认 http://100.83.50.99:8086/mcp (主人 macbook Tailscale IP)
- MNELO_REMOTE_TOKEN  默认 ~/.config/mnelo/auth_token (mounted by owner)
- 显式更高优先级: --url / --token CLI args
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional, List, Dict, Any


DEFAULT_TAILSCALE_HOST = "mnelo.tail6a710.ts.net"  # [8/9 P1-yanru] 占位 fallback — 实际从 config.client_tailscale_host 读
DEFAULT_PORT = 8086
DEFAULT_TOKEN_PATH = os.path.expanduser("~/.config/mnelo/auth_token")


def _get_tailscale_host() -> str:
    """[8/9 P1-yanru] Tailscale host 解析链: env MNELO_MEMORY_CLIENT_TAILSCALE_HOST
    > config.toml [client].tailscale_host > 默认 mnelo.tail6a710.ts.net."""
    try:
        from config import config as _cfg  # 延迟 import, scripts/ 路径下也安全

        return _cfg.client_tailscale_host
    except Exception:
        return DEFAULT_TAILSCALE_HOST


class MneloRemoteError(Exception):
    """mnelo MCP server returned error or unreachable."""

    def __init__(self, message: str, http_status: int = 0, payload: Optional[dict] = None):
        super().__init__(message)
        self.http_status = http_status
        self.payload = payload or {}


class MneloRemoteClient:
    """[8/8 Tailscale multi-agent] 跨 vps 调 mnelo MCP server."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: int = 30):
        self.url = url or os.environ.get(
            "MNELO_REMOTE_URL",
            f"http://{_get_tailscale_host()}:{DEFAULT_PORT}/mcp",
        )
        token = token or os.environ.get("MNELO_REMOTE_TOKEN")
        if not token:
            if os.path.exists(DEFAULT_TOKEN_PATH):
                with open(DEFAULT_TOKEN_PATH) as f:
                    token = f.read().strip()
            else:
                raise MneloRemoteError(
                    f"No auth token. Provide via MNELO_REMOTE_TOKEN env, --token CLI arg, or mount ~/.config/mnelo/auth_token",
                )
        self.token = token
        self.timeout = timeout
        self.session_id: Optional[str] = None

    def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                # MCP streamable_http returns text/event-stream; first data: line is the JSON
                raw = r.read().decode()
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"raw": body}
            raise MneloRemoteError(
                f"HTTP {e.code} from mnelo: {payload.get('error', body)[:200]}",
                http_status=e.code,
                payload=payload,
            )
        except urllib.error.URLError as e:
            raise MneloRemoteError(f"mnelo unreachable at {self.url}: {e.reason}")

        # 解析 SSE (data: {...}) 或纯 JSON
        for line in raw.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        # 没 data: prefix — try raw
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise MneloRemoteError(f"unparseable mnelo response: {raw[:200]}")

    def initialize(self) -> Dict[str, Any]:
        """必须先 initialize 拿到 session, 后面的 tools/call 才能用."""
        return self._call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mnelo_remote_client", "version": "1.0"},
            },
        )

    def _tools_call(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """[8/8] 薄包装 tools/call, 解析 mnelo 2-block 响应 (🌳 echo + JSON)."""
        if not self.session_id:
            self.initialize()
        resp = self._call("tools/call", {"name": tool_name, "arguments": args})
        # result.content 是 list of TextContent; [0] 是 🌳 echo, [1] 是 JSON
        try:
            content = resp["result"]["content"]
            if len(content) >= 2:
                text = content[1]["text"]
                return json.loads(text)
            elif len(content) == 1:
                return json.loads(content[0]["text"])
        except (KeyError, json.JSONDecodeError, IndexError) as e:
            raise MneloRemoteError(f"mnelo returned unexpected shape: {e}")
        return resp

    # ============================================================
    # 业务函数
    # ============================================================

    def recall(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
        strategy: str = "rrf",
        asof: Optional[str] = None,
    ) -> Dict[str, Any]:
        """[8/8] 跨 vps 召回 mnelo 内容."""
        args = {"query": query, "top_k": top_k, "strategy": strategy}
        if filters:
            args["filters"] = filters
        if asof:
            args["asof"] = asof
        return self._tools_call("memory_recall", args)

    def remember(
        self,
        content: str,
        source: str,
        importance: float = 0.5,
        memory_type: Optional[str] = None,
        entities: Optional[List[Dict[str, Any]]] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """[8/8] 写入跨 vps 共享记忆. source 前缀建议 '<agent>-<host>/<channel>'."""
        args = {"content": content, "source": source, "importance": importance}
        if memory_type:
            args["memory_type"] = memory_type
        if entities:
            args["entities"] = entities
        if tags:
            args["tags"] = tags
        return self._tools_call("memory_remember", args)

    def forget(self, chunk_id: str) -> Dict[str, Any]:
        """[8/8] 软删跨 vps 记忆."""
        return self._tools_call("memory_forget", {"target_id": chunk_id})

    def get_digest(self) -> Dict[str, Any]:
        """[8/8] 拉 session 摘要."""
        return self._tools_call("memory_get_digest", {})

    def stats(self) -> Dict[str, Any]:
        """[8/8] mnelo 当前状态."""
        return self._tools_call("memory_stats", {})


# ============================================================
# CLI
# ============================================================


def main():
    parser = argparse.ArgumentParser(
        description="Tailscale mesh 跨 vps 调 mnelo MCP server (8/8 multi-agent)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    # 用法: mnelo_remote_client.py [--url X] [--token Y] [--timeout 30] <cmd> [args]
    parser.add_argument("--url", help=f"MCP URL (default: $MNELO_REMOTE_URL or http://{_get_tailscale_host()}:{DEFAULT_PORT}/mcp)")
    parser.add_argument("--token", help="Bearer token (default: $MNELO_REMOTE_TOKEN or ~/.config/mnelo/auth_token)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout (s)")

    p_recall = sub.add_parser("recall", help="recall context")
    p_recall.add_argument("query")
    p_recall.add_argument("--top-k", type=int, default=5)
    p_recall.add_argument("--source", help="filter by source (exact match)")
    p_recall.add_argument("--kind", help="filter by entity kind")
    p_recall.add_argument("--type", help="filter by memory_type")
    p_recall.add_argument("--strategy", default="rrf", choices=["rrf", "vector_only", "graph_only", "meta_only", "entity_only"])

    p_remember = sub.add_parser("remember", help="write a memory chunk")
    p_remember.add_argument("content")
    p_remember.add_argument("--source", required=True, help="source prefix (e.g. 'yanru-vps/telegram')")
    p_remember.add_argument("--importance", type=float, default=0.5)
    p_remember.add_argument("--type", choices=["fact", "preference", "episode", "decision", "procedure", "ephemeral"])
    p_remember.add_argument("--tags", nargs="+", help="tags for filtering")

    p_forget = sub.add_parser("forget", help="soft-delete a chunk")
    p_forget.add_argument("--id", required=True, dest="chunk_id")

    sub.add_parser("digest", help="session digest")
    sub.add_parser("stats", help="mnelo stats")

    args = parser.parse_args()
    client = MneloRemoteClient(url=args.url, token=args.token, timeout=args.timeout)

    if args.cmd == "recall":
        filters = {}
        if args.source:
            filters["source"] = args.source
        if args.kind:
            filters["kind"] = args.kind
        if args.type:
            filters["type"] = args.type
        result = client.recall(args.query, top_k=args.top_k, filters=filters or None, strategy=args.strategy)
        # [8/8] mnelo memory_recall 返回 list of hits (不是 dict) — 适配 shape
        hits = result if isinstance(result, list) else (result.get("hits", []) if isinstance(result, dict) else [])
        top_method = hits[0].get("method", "?") if hits else "?"
        top_rrf = hits[0].get("rrf_score", 0.0) if hits else 0.0
        print(f'🌳 mnelo    ~{len(hits)} hits  "{args.query}"  (top={top_method} rrf={top_rrf:.3f})')
        for h in hits:
            content = h.get("content", "")[:80]
            rrf = h.get("rrf", 0)
            method = h.get("method", "?")
            print(f"  - [{method} rrf={rrf:.3f}] {content}")

    elif args.cmd == "remember":
        result = client.remember(
            content=args.content,
            source=args.source,
            importance=args.importance,
            memory_type=args.type,
            tags=args.tags,
        )
        chunk_id = result.get("chunk_id", "?")
        print(f"🌳 mnelo    +{chunk_id}  (importance={args.importance}, source={args.source})")

    elif args.cmd == "forget":
        # [8/8] mcp_server tool schema: memory_forget 参数名是 target_id 不是 id
        result = client._tools_call("memory_forget", {"target_id": args.chunk_id})
        print(f"🌳 mnelo    -{args.chunk_id}  (soft_deleted)")

    elif args.cmd == "digest":
        result = client.get_digest()
        digest = result.get("digest", []) if isinstance(result, dict) else []
        print(f"🌳 mnelo    digest ({len(digest)} lines):")
        for line in digest[:10]:
            print(f"  - {line[:100]}")

    elif args.cmd == "stats":
        result = client.stats()
        chunks = result.get("chunks", {}).get("active", "?")
        entities = result.get("entities", {}).get("active", "?")
        vectors = result.get("vectors", "?")
        print(f"🌳 mnelo    stats: chunks={chunks} entities={entities} vectors={vectors}")


if __name__ == "__main__":
    try:
        main()
    except MneloRemoteError as e:
        print(f"✗ mnelo error: {e}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# HermesMneloClient — [8/9] hermes 端默认 source filter
# ============================================================
# 8/9 燕如 P5 反馈: mnelo 没有 row-level ACL. 跨 vps 客户端要自己在 wrapper 锁死
# source, 不然 recall 不带 filter 会看到其它客户端 chunk. 燕如写了 YanruMneloClient
# 锁死 source="yanru-vps". 我们 hermes 端镜像这个 pattern.
#
# 跟 macbook 本机的 hybrid path (path A via memory_recall MCP tool) 不同:
# - path A (hermes core memory tool): 已经在 ~/.hermes/config.yaml mcp_servers.mnelo
#   跑, hermes owner 通过 memory_recall('query') 返回 mnelo hits. 主人 8/9 拍板
#   hermes 端 source 默认 'hermes-gw' (AGENTS.md §1.5 决策点).
# - path B (mnelo_remote_client.py cross-vps): 同 hermes owner 调用, 默认 source
#   'hermes-gw' (Tailscale 入口). 燕如 / 别的 agent 写 'yanru-vps/*' 隔离.


class HermesMneloClient(MneloRemoteClient):
    """[8/9] hermes 端 source 默认 'hermes-gw' filter, 防跨客户端污染."""

    DEFAULT_SOURCE = "hermes-gw"

    def recall(self, query, top_k=5, filters=None, strategy="rrf", asof=None):
        # 锁死 source. 显式传 source 不用锁 (怕燕如 end 共享 hermes client 时
        # 临时切身份, 但默认 union 'hermes-gw' filter).
        merged_filters = dict(filters or {})
        merged_filters.setdefault("source", self.DEFAULT_SOURCE)
        return super().recall(query, top_k=top_k, filters=merged_filters, strategy=strategy, asof=asof)

    def remember(self, content, source=None, **kwargs):
        # 强制 source 前缀 (防裸 source / 空字符串漏掉 hermes-gw 命名)
        if not source:  # None + empty string 都走 default
            source = self.DEFAULT_SOURCE
        elif not source.startswith(self.DEFAULT_SOURCE):
            source = f"{self.DEFAULT_SOURCE}/{source}"
        return super().remember(content, source=source, **kwargs)
