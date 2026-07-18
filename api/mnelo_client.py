#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hermes_memory_client.py — 实战客户端 (SSE)

[实战]
- 主人口中 7/18 拍板 C 方案: trinity_daily.py 通过 MCP tool 调 hermes-memory
- 替代直接 import memory.py (实战更解耦, mcp server 可独立升级)
- 与 cron / 实战脚本解耦: 实战脚本只 import 客户端, 不关心 server 细节

[运行]
    from hermes_memory_client import MneloClient
    client = MneloClient()
    cid = client.remember('实战', source='cron', importance=0.9)
"""
import sys
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('hermes_memory_client')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '[%(asctime)s] %(name)s %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(handler)

# 默认 SSE endpoint
DEFAULT_SSE_URL = 'http://127.0.0.1:8086/sse'


class MneloClient:
    """实战 MCP 客户端 — 7 个工具的同步包装."""

    def __init__(self, sse_url: str = DEFAULT_SSE_URL, timeout: float = 30.0):
        self.sse_url = sse_url
        self.timeout = timeout
        self._session: Optional[Any] = None

    def _ensure_mcp(self) -> Tuple[Any, Any]:
        """实战: 检查 MCP 库可用, 返回 (ClientSession, sse_client) 类引用."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            return ClientSession, sse_client
        except ImportError:
            raise RuntimeError('MCP 客户端库不可用, 请先: pip install mcp[cli]')

    def _call(self, tool_name: str, arguments: Dict) -> Any:
        """实战: SSE 连接 + 调用 + 关闭, [P2+ #5 7/18] 加重试防 cold-start race."""
        ClientSession, sse_client = self._ensure_mcp()
        last_err = None
        # [P2+ #5] 重试 2 次: 失败后退避 0.3s, 再次尝试
        # 实战 race: MCP server 启动后 1 秒内有人调 (warm-up 时) 可能 SSE 拒绝
        for attempt in range(2):
            try:
                return asyncio.run(self._async_call(tool_name, arguments, ClientSession, sse_client))
            except Exception as e:
                last_err = e
                if attempt == 0:
                    import time as _t
                    _t.sleep(0.3)
                    logger.debug(f'MCP call {tool_name} attempt {attempt+1} failed: {e}, retrying...')
                    continue
        logger.error(f'MCP call {tool_name} failed after retries: {last_err}')
        raise last_err if last_err else RuntimeError('mcp call failed')

    async def _async_call(self, tool_name: str, arguments: Dict, ClientSession, sse_client):
        async with sse_client(self.sse_url) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                # 实战: result.content[0].text 是 JSON 字符串
                if result.content and hasattr(result.content[0], 'text'):
                    text = result.content[0].text
                    try:
                        return json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        return text
                return None

    # === 7 个工具封装 ===

    def remember(self, content: str, source: str = 'manual', importance: float = 0.5,
                 entities: List[Dict] = None, relations: List[Dict] = None,
                 tags: List[str] = None, session_id: str = 'default',
                 timestamp: str = None) -> str:
        """实战: 写入 memory. 返回 chunk_id."""
        args = {'content': content, 'source': source, 'importance': importance}
        if entities: args['entities'] = entities
        if relations: args['relations'] = relations
        if tags: args['tags'] = tags
        if session_id != 'default': args['session_id'] = session_id
        if timestamp: args['timestamp'] = timestamp
        result = self._call('memory_remember', args)
        if isinstance(result, dict) and 'chunk_id' in result:
            return result['chunk_id']
        raise RuntimeError(f'remember failed: {result}')

    def recall(self, query: str, top_k: int = 5, graph_hops: int = 2,
               filters: Dict = None, strategy: str = 'rrf', asof: str = None) -> List[Dict]:
        """实战: 3 路 + RRF 召回. 返回 list of hits."""
        args = {'query': query, 'top_k': top_k, 'graph_hops': graph_hops, 'strategy': strategy}
        if filters: args['filters'] = filters
        if asof: args['asof'] = asof
        return self._call('memory_recall', args)

    def relate(self, source_id: str, target_id: str, relation: str,
               weight: float = 1.0, valid_from: str = None, valid_until: str = None,
               evidence_chunk_id: str = None, properties: Dict = None) -> int:
        """实战: 新建关系. 返回 relation_id."""
        args = {'source_id': source_id, 'target_id': target_id, 'relation': relation, 'weight': weight}
        if valid_from: args['valid_from'] = valid_from
        if valid_until: args['valid_until'] = valid_until
        if evidence_chunk_id: args['evidence_chunk_id'] = evidence_chunk_id
        if properties: args['properties'] = properties
        result = self._call('memory_relate', args)
        if isinstance(result, dict) and 'relation_id' in result:
            return result['relation_id']
        raise RuntimeError(f'relate failed: {result}')

    def forget(self, target_id: str, target_kind: str = 'chunk',
               reason: str = 'outdated', cascade: bool = True) -> Dict:
        """实战: 软删除."""
        return self._call('memory_forget', {
            'target_id': target_id, 'target_kind': target_kind,
            'reason': reason, 'cascade': cascade,
        })

    def update(self, old_id: str, reason: str = 'updated',
               new_content: str = None, new_properties: Dict = None,
               new_importance: float = None) -> str:
        """实战: 更新 (创建新版本)."""
        args = {'old_id': old_id, 'reason': reason}
        if new_content: args['new_content'] = new_content
        if new_properties: args['new_properties'] = new_properties
        if new_importance is not None: args['new_importance'] = new_importance
        result = self._call('memory_update', args)
        if isinstance(result, dict) and 'new_chunk_id' in result:
            return result['new_chunk_id']
        raise RuntimeError(f'update failed: {result}')

    def graph_query(self, start_node: str, max_hops: int = 3,
                    edge_types: List[str] = None, asof: str = None) -> Dict:
        """实战: 图遍历."""
        args = {'start_node': start_node, 'max_hops': max_hops}
        if edge_types: args['edge_types'] = edge_types
        if asof: args['asof'] = asof
        return self._call('memory_graph_query', args)

    def stats(self) -> Dict:
        """实战: 统计."""
        return self._call('memory_stats', {})


# === 便捷 singleton ===
_client_instance: Optional[MneloClient] = None


def get_client() -> MneloClient:
    """实战: 复用单例 client (SSE 短连接, 单次 7ms)."""
    global _client_instance
    if _client_instance is None:
        _client_instance = MneloClient()
    return _client_instance


# === 自测 ===
if __name__ == '__main__':
    print('=== hermes-memory MCP 客户端自测 ===')
    client = MneloClient()

    # 1. stats
    stats = client.stats()
    print(f'✅ stats: total_chunks={stats["chunks"]["total"]} total_entities={stats["entities"]["total"]}')

    # 2. remember
    cid = client.remember(
        content='hermes_memory_client 自测: 实战 MCP 客户端可用',
        source='client-self-test',
        importance=0.7,
    )
    print(f'✅ remember → {cid}')

    # 3. recall
    results = client.recall('hermes_memory_client 自测', top_k=2)
    print(f'✅ recall → {len(results)} hits')
    for r in results[:1]:
        print(f"  [{r.get('method', '?')}] {r.get('content', '')[:80]}")

    # 4. graph_query
    g = client.graph_query('master_2077_ling', max_hops=1)
    print(f'✅ graph_query → {len(g["nodes"])} nodes, {len(g["edges"])} edges')

    # 5. forget (cleanup)
    res = client.forget(cid, target_kind='chunk', reason='client-self-test-cleanup')
    print(f'✅ forget → {res}')

    print()
    print('✅ 自测完成 — 客户端可用')


# ============== Back-compat alias ==============
# 旧代码 (hermes_memory_client.HermesMemoryClient) 仍 work
HermesMemoryClient = MneloClient
