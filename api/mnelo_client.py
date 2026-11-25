#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mnelo_client.py — 客户端 (SSE / streamable-http 双 transport)

- 主人口中 7/18 拍板 C 方案: trinity_daily.py 通过 MCP tool 调 mnelo
- 替代直接 import memory.py (更解耦, mcp server 可独立升级)
- 与 cron / 脚本解耦: 脚本只 import 客户端, 不关心 server 细节

[运行]
    from mnelo_client import MneloClient
    client = MneloClient()
    cid = client.remember('hello world', source='cron', importance=0.9)
"""

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("mnelo_client")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

# 默认 endpoint: streamable-http (MCP 2025-03-26 spec) — /mcp 路径
DEFAULT_MCP_URL = "http://127.0.0.1:8086/mcp"
# legacy SSE endpoint (back-compat; URL 含 /sse 时自动用 sse transport)
DEFAULT_SSE_URL = "http://127.0.0.1:8086/sse"


def _http_client_factory():
    """返回 (module, AsyncClient_cls, Timeout_cls) — 造 MCP http_client 用。

    [bug fix 2026-11-25] mcp 2.3.0 的 SDK 内部用的是 **httpx2**（2.16.2），
    而 httpx v0（0.28.1）是另一个独立的包。SDK 的
    `streamable_http_client(url, http_client=...)` 会把传进来的 client 当成
    httpx2.AsyncClient 用：
      - `client.send(request, stream=True, follow_redirects=False)`
      - `mcp.shared._httpx_utils.stream_within_origin` 里用 httpx2.URL / Response
      - `from httpx2 import EventSource, ServerSentEvent` 解析 SSE
    传一个 httpx v0 的 client 进去 → SDK 走到 httpx 独有的代码路径 →
    服务端明明返回 200，客户端却抛 `httpx.ReadError` / TaskGroup 里的
    `ExceptionGroup: unhandled errors in a TaskGroup`，最终被 mcp 包装成
    `MCPError: Server returned an error response`（-32603）。

    实测：换成 httpx2.AsyncClient 后同一条 recall 立刻正常返回 3 条 hits。

    所以这里按「装了哪个用哪个」的顺序取，并且**优先 httpx2** —— 因为装了
    mcp 2.x 的环境必然有 httpx2，而 httpx v0 只是历史遗留。

    另外强制 **trust_env=False**（见调用点）：httpx 默认 trust_env=True，会
    读 HTTP_PROXY / HTTPS_PROXY 环境变量**以及 macOS 系统代理设置**。
    macOS 的 `scutil --proxy` 例外列表（ExceptionsList）里虽然有 127.0.0.1，
    但 httpx 的例外匹配规则跟它对不上 —— 实测 uvicorn 进程里
    trust_env=True → 走代理 → 502 / ReadError；trust_env=False → 200 正常。
    mnelo 的 MCP 端点默认在 127.0.0.1，永远不该走代理。
    """
    try:
        import httpx2  # mcp 2.x SDK 用的就是这个

        return httpx2, httpx2.AsyncClient, httpx2.Timeout
    except ImportError:
        import httpx  # 老 mcp / 老环境

        return httpx, httpx.AsyncClient, httpx.Timeout


class _LoopRunner:
    """在一条专用线程里跑一个**持久**的 event loop。

    [bug fix 2026-11-25] 之前用 ThreadPoolExecutor + asyncio.run(coro)：
    每次调用建一个新 loop、跑完就关。mcp 2.x 的 streamable_http_client 用
    anyio TaskGroup 管理后台任务，在「协程在 A 线程创建、在 B 线程的新 loop
    里跑」这种情况下会抛
        ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
    —— 同样的调用在纯脚本里有时能过、有时炸，服务进程里必炸。

    改成一个常驻线程 + 一个常驻 loop：协程通过 run_coroutine_threadsafe
    投递过去，loop 一直活着，anyio 的后台任务有稳定的宿主。
    这是「同步 API 包 async 实现」的标准做法。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="mnelo-mcp-loop", daemon=True
        )
        self._started = threading.Event()
        self._thread.start()
        self._started.wait(timeout=5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()


_LOOP_RUNNER: Optional[_LoopRunner] = None
_LOOP_LOCK = threading.Lock()


def _get_loop_runner() -> _LoopRunner:
    global _LOOP_RUNNER
    with _LOOP_LOCK:
        if _LOOP_RUNNER is None:
            _LOOP_RUNNER = _LoopRunner()
        return _LOOP_RUNNER


def _run_coroutine_sync(coro):
    """在专用线程的持久 event loop 里跑协程并返回结果。

    [bug fix 2026-11-25] 原来这里直接 asyncio.run(coro)。但 asyncio.run 在
    已有 running loop 的线程里会抛
        RuntimeError: asyncio.run() cannot be called from a running event loop
    —— 只要调用方在 async 上下文里就行（FastAPI 端点、asyncio 任务、任何
    嵌在 async 框架里的同步调用）。这让这个客户端在 async 场景下完全不可用。

    现在统一走专用线程的持久 loop：不管调用方是不是在 async 上下文里，
    都能安全调用，也不会踩 mcp anyio TaskGroup 的跨线程问题。
    """
    return _get_loop_runner().run(coro)


class MneloClient:
    """MCP 客户端 — 7 个工具的同步包装 (SSE / streamable-http)."""

    def __init__(self, sse_url: Optional[str] = None, url: Optional[str] = None, transport: Optional[str] = None, timeout: float = 30.0, auth_token: Optional[str] = None):
        """URL 优先级: url(新) > sse_url(旧别名) > env MNELO_MEMORY_URL > DEFAULT_MCP_URL.

        transport 未显式指定 → 按 URL 自动判断 (/sse → sse, 其余 → streamable-http).
        """
        if url is None:
            url = sse_url
        if url is None:
            url = os.environ.get("MNELO_MEMORY_URL", DEFAULT_MCP_URL)
        if transport is None:
            transport = "sse" if "/sse" in url else "streamable-http"
        if transport not in ("sse", "streamable-http"):
            raise ValueError(f"transport must be 'sse' or 'streamable-http', got {transport!r}")
        self.url = url
        self.transport = transport
        self.timeout = timeout
        self._session: Optional[Any] = None
        # [2026-07-22 P0-fix] Bearer token auth (matches server load_auth_token).
        # Priority: explicit kwarg -> MNELO_AUTH_TOKEN env -> ~/.config/mnelo/auth_token
        # Bug history: missing token caused server 401 and silent recall failures.
        self._auth_token = auth_token or os.environ.get("MNELO_AUTH_TOKEN") or self._read_token_file()

    @property
    def sse_url(self) -> str:
        """Back-compat alias for .url — 旧代码直接读写 .sse_url 属性仍有效."""
        return self.url

    @sse_url.setter
    def sse_url(self, value: str) -> None:
        self.url = value
        if "/sse" in value:
            self.transport = "sse"

    def _read_token_file(self) -> Optional[str]:
        """~/.config/mnelo/auth_token 文件读取, mode 0600 ownership-preserving."""
        token_path = Path.home() / ".config" / "mnelo" / "auth_token"
        try:
            if token_path.is_file():
                return token_path.read_text().strip()
        except Exception as e:
            logger.debug(f"Auth token file unreadable: {e}")
        return None

    def _ensure_mcp(self) -> Tuple[Any, Any]:
        """: 检查 MCP 库可用, 返回 (ClientSession, client_factory). transport 决定 sse / streamable-http."""
        try:
            from mcp import ClientSession

            if self.transport == "sse":
                from mcp.client.sse import sse_client

                return ClientSession, sse_client
            # mcp 2.x: streamable_http_client (新名; 旧名 streamablehttp_client 已弃用)
            from mcp.client.streamable_http import streamable_http_client

            return ClientSession, streamable_http_client
        except ImportError as e:
            raise RuntimeError(f"MCP 客户端库不可用: {e}; 请先: pip install mcp[cli]")

    def _call(self, tool_name: str, arguments: Dict) -> Any:
        """MCP 连接 + 调用 + 关闭, [P2+ #5 7/18] 加重试防 cold-start race."""
        ClientSession, client_factory = self._ensure_mcp()
        last_err = None
        # [P2+ #5] 重试 2 次: 失败后退避 0.3s, 再次尝试
        #  race: MCP server 启动后 1 秒内有人调 (warm-up 时) 可能拒绝
        for attempt in range(2):
            try:
                return _run_coroutine_sync(self._async_call(tool_name, arguments, ClientSession, client_factory))
            except Exception as e:
                last_err = e
                if attempt == 0:
                    import time as _t

                    _t.sleep(0.3)
                    logger.debug(f"MCP call {tool_name} attempt {attempt + 1} failed: {e}, retrying...")
                    continue
        logger.error(f"MCP call {tool_name} failed after retries: {last_err}")
        raise last_err if last_err else RuntimeError("mcp call failed")

    async def _async_call(self, tool_name: str, arguments: Dict, ClientSession, client_factory):
        # [2026-07-22 P0-fix] Inject Bearer token in MCP handshake headers
        headers = {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else None
        if self.transport == "streamable-http":
            # 新 SDK API: streamable_http_client(url, http_client=...) — 不收 headers/timeout,
            # 需传配置好的 AsyncClient (Bearer + timeout 注入).
            # [bug fix 2026-11-25] 必须用 SDK 同款的 httpx2 —— 见 _http_client_factory().
            _mod, _AsyncClient, _Timeout = _http_client_factory()

            async with _AsyncClient(
                headers=headers, timeout=_Timeout(self.timeout), trust_env=False
            ) as http_client:
                # [v0.81.7 P2-4 review fix] 新 streamable_http_client 严格 yield 2 元组
                # (read_stream, write_stream); 旧版本 yield 3 含 session_id_fn.
                # 不需要 star-unpack 防御.
                async with client_factory(self.url, http_client=http_client) as (r, w):
                    return await self._run_session(ClientSession, r, w, tool_name, arguments)
        # legacy SSE (和旧 streamablehttp_client) — 直接收 headers/timeout.
        # [v0.81.7 P2-4 review fix v2] sse_client 也 yield 2 元组 (read, write),
        # 不是 3 元组. 不需要 star-unpack 防御.
        async with client_factory(self.url, headers=headers or None, timeout=self.timeout) as (r, w):
            return await self._run_session(ClientSession, r, w, tool_name, arguments)

    async def _run_session(self, ClientSession, r, w, tool_name: str, arguments: Dict) -> Any:
        """ClientSession initialize + call_tool + 解析 TextContent 块 (两 transport 共享)."""
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            # [2026-07-22 fix] Server returns 2 TextContent blocks:
            # - [0] = 🌳 echo summary (human-readable one-liner)
            # - [1] = canonical JSON (machine-parseable)
            # Old client only read [0] and got an echo string instead of JSON.
            # Try every block, prefer the one that parses as JSON.
            if not result.content:
                return None
            last_parsed = None
            raw_text = None
            for block in result.content:
                if not hasattr(block, "text"):
                    continue
                text = block.text
                raw_text = text
                try:
                    last_parsed = json.loads(text)
                    # Prefer parsed JSON over raw text
                    if isinstance(last_parsed, (dict, list)):
                        return last_parsed
                except (json.JSONDecodeError, TypeError):
                    continue
            # No JSON block — return raw text
            return raw_text

    # === 7 个工具封装 ===

    def remember(
        self,
        content: str,
        source: str = "manual",
        importance: float = 0.5,
        entities: List[Dict] = None,
        relations: List[Dict] = None,
        tags: List[str] = None,
        session_id: str = "default",
        timestamp: str = None,
        memory_type: Optional[str] = None,
        # [P0 2026-11-25] scoping IDs — 服务端 memory_core.remember() 早就支持
        # 这三个字段（写进 chunks.metadata_json），但这个封装一直没透传，导致
        # 按人/按 agent 隔离的记忆在客户端做不到（爸爸和妈妈的记忆混在一起）。
        # 语义与 memory_core 一致：None = 不指定（不写入，兼容旧数据）；
        # 空串 = 显式选择 "no scoping"。
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """写入 memory. 返回 chunk_id."""
        args = {"content": content, "source": source, "importance": importance}
        if entities:
            args["entities"] = entities
        if relations:
            args["relations"] = relations
        if tags:
            args["tags"] = tags
        if session_id != "default":
            args["session_id"] = session_id
        if timestamp:
            args["timestamp"] = timestamp
        if memory_type:
            args["memory_type"] = memory_type
        if agent_id is not None:
            args["agent_id"] = agent_id
        if user_id is not None:
            args["user_id"] = user_id
        if run_id is not None:
            args["run_id"] = run_id
        result = self._call("memory_remember", args)
        if isinstance(result, dict) and "chunk_id" in result:
            return result["chunk_id"]
        raise RuntimeError(f"remember failed: {result}")

    def recall(self, query: str, top_k: int = 5, graph_hops: int = 2, filters: Dict = None, strategy: str = "rrf", asof: str = None) -> List[Dict]:
        """4 路 + RRF 召回. 返回 list of hits.

        Each hit dict includes:
          - chunk_id, content, source, timestamp, importance
          - method: 'vector' | 'graph' | 'entity' | 'meta'
          - rrf_score: real RRF fusion score (sum of 1/(k+rank) per lane)
          - score: alias of rrf_score for back-compat (added 2026-07-28)

        Background: server returns `rrf_score`, not `score`. Callers using
        `hit.get('score')` previously got 0.0 (default) because the key didn't
        exist, silently producing empty/zero rankings. Alias added so both
        field names return the real value.
        """
        args = {"query": query, "top_k": top_k, "graph_hops": graph_hops, "strategy": strategy}
        if filters:
            args["filters"] = filters
        if asof:
            args["asof"] = asof
        result = self._call("memory_recall", args)
        # Alias rrf_score -> score so callers using .get('score') get real values
        if isinstance(result, list):
            for hit in result:
                if isinstance(hit, dict) and "rrf_score" in hit and "score" not in hit:
                    hit["score"] = hit["rrf_score"]
        return result

    def relate(
        self, source_id: str, target_id: str, relation: str, weight: float = 1.0, valid_from: str = None, valid_until: str = None, evidence_chunk_id: str = None, properties: Dict = None
    ) -> int:
        """: 新建关系. 返回 relation_id."""
        args = {"source_id": source_id, "target_id": target_id, "relation": relation, "weight": weight}
        if valid_from:
            args["valid_from"] = valid_from
        if valid_until:
            args["valid_until"] = valid_until
        if evidence_chunk_id:
            args["evidence_chunk_id"] = evidence_chunk_id
        if properties:
            args["properties"] = properties
        result = self._call("memory_relate", args)
        if isinstance(result, dict) and "relation_id" in result:
            return result["relation_id"]
        raise RuntimeError(f"relate failed: {result}")

    def forget(self, target_id: str, target_kind: str = "chunk", reason: str = "outdated", cascade: bool = True) -> Dict:
        """: 软删除."""
        return self._call(
            "memory_forget",
            {
                "target_id": target_id,
                "target_kind": target_kind,
                "reason": reason,
                "cascade": cascade,
            },
        )

    def update(self, old_id: str, reason: str = "updated", new_content: str = None, new_properties: Dict = None, new_importance: float = None) -> str:
        """: 更新 (创建新版本)."""
        args = {"old_id": old_id, "reason": reason}
        if new_content:
            args["new_content"] = new_content
        if new_properties:
            args["new_properties"] = new_properties
        if new_importance is not None:
            args["new_importance"] = new_importance
        result = self._call("memory_update", args)
        if isinstance(result, dict) and "new_chunk_id" in result:
            return result["new_chunk_id"]
        raise RuntimeError(f"update failed: {result}")

    def graph_query(self, start_node: str, max_hops: int = 3, edge_types: List[str] = None, asof: str = None) -> Dict:
        """: 图遍历."""
        args = {"start_node": start_node, "max_hops": max_hops}
        if edge_types:
            args["edge_types"] = edge_types
        if asof:
            args["asof"] = asof
        return self._call("memory_graph_query", args)

    def stats(self) -> Dict:
        """: 统计."""
        return self._call("memory_stats", {})

    def health(self) -> Dict:
        """: GET /health — public endpoint, 无 MCP 调用."""
        import urllib.request
        base = self.url.split("/mcp")[0] if self.url.endswith("/mcp") else self.url.split("/sse")[0]
        with urllib.request.urlopen(f"{base}/health", timeout=self.timeout) as r:
            return json.loads(r.read())

    def metrics(self) -> str:
        """: GET /metrics — Prometheus text, 无 MCP 调用."""
        import urllib.request
        base = self.url.split("/mcp")[0] if self.url.endswith("/mcp") else self.url.split("/sse")[0]
        with urllib.request.urlopen(f"{base}/metrics", timeout=self.timeout) as r:
            return r.read().decode()

    def list_tools(self) -> List[Dict]:
        """: MCP tools/list → [{name, description, inputSchema}, ...]."""
        ClientSession, client_factory = self._ensure_mcp()
        # [bug fix 2026-11-25] 不能用裸 asyncio.run —— 调用方在 async 上下文里
        # 会抛 RuntimeError: asyncio.run() cannot be called from a running event loop
        return _run_coroutine_sync(self._async_list_tools(ClientSession, client_factory))

    async def _async_list_tools(self, ClientSession, client_factory):
        headers = {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else None
        if self.transport == "streamable-http":
            # mcp 2.x streamable_http_client(url, http_client=...) — 不收 headers kwarg, 走 http_client.
            # [bug fix 2026-11-25] 必须用 SDK 同款的 httpx2 —— 见 _http_client_factory().
            _mod, _AsyncClient, _Timeout = _http_client_factory()
            async with _AsyncClient(
                headers=headers, timeout=_Timeout(self.timeout), trust_env=False
            ) as http_client:
                async with client_factory(self.url, http_client=http_client) as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        return [self._tool_to_dict(t) for t in result.tools]
        # legacy SSE 路径
        async with client_factory(self.url, headers=headers or None, timeout=self.timeout) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.list_tools()
                return [self._tool_to_dict(t) for t in result.tools]

    @staticmethod
    def _tool_to_dict(t) -> Dict:
        """mcp 2.x SDK Tool 对象 → dict. 兼容 input_schema (snake, 新) / inputSchema (camel, 旧)."""
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
        return {"name": t.name, "description": t.description, "inputSchema": schema}

    def call_tool(self, name: str, arguments: Optional[Dict] = None) -> Any:
        """: 通用 MCP 工具调用 — CLI wrapper / 调试用.

        薄 alias 转发到 _call(), 由 _run_session() 解析 TextContent blocks (返回 dict/list, 失败时返原始字符串).

        业务代码请优先用专用方法 (stats/recall/get_digest/...), 避免魔法字符串.
        """
        return self._call(name, arguments or {})

    def get_digest(self, ref: Optional[str] = None) -> Dict:
        """[S2 8/5] TASKS_L2_SESSION_STATE §1.3A — 常驻摘要双模式.

        ref=None (缺省) → 摘要压缩视图 (content + line_refs + chunk_id + truncated + built_at).
        ref=<行号> → 展开该行源 chunk (source_chunks) 或 {error: ...}.

        Returns:
            dict — 与 MCP 工具 memory_get_digest 返回一致.
        """
        args: Dict[str, Any] = {}
        if ref is not None:
            args["ref"] = ref
        return self._call("memory_get_digest", args)


# === 便捷 singleton ===
_client_instance: Optional[MneloClient] = None


def get_client() -> MneloClient:
    """: 复用单例 client (短连接, 单次 ~7ms)."""
    global _client_instance
    if _client_instance is None:
        _client_instance = MneloClient()
    return _client_instance


# === 自测 ===
if __name__ == "__main__":
    print("=== mnelo MCP 客户端自测 ===")
    client = MneloClient()

    # 1. stats
    stats = client.stats()
    print(f"✅ stats: total_chunks={stats['chunks']['total']} total_entities={stats['entities']['total']}")

    # 2. remember
    cid = client.remember(
        content="mnelo_client 自测:  MCP 客户端可用",
        source="client-self-test",
        importance=0.7,
    )
    print(f"✅ remember → {cid}")

    # 3. recall
    results = client.recall("mnelo_client 自测", top_k=2)
    print(f"✅ recall → {len(results)} hits")
    for r in results[:1]:
        print(f"  [{r.get('method', '?')}] {r.get('content', '')[:80]}")

    # 4. graph_query
    g = client.graph_query("master_2077_ling", max_hops=1)
    print(f"✅ graph_query → {len(g['nodes'])} nodes, {len(g['edges'])} edges")

    # 5. forget (cleanup)
    res = client.forget(cid, target_kind="chunk", reason="client-self-test-cleanup")
    print(f"✅ forget → {res}")

    print()
    print("✅ 自测完成 — 客户端可用")


# Back-compat alias: HermesMemoryClient → MneloClient
# 旧代码 (hermes_memory_client.HermesMemoryClient) 仍 work
HermesMemoryClient = MneloClient
