#!/usr/bin/env python3
"""mn.py — mnelo CLI 薄封装 (Claude Code agent 用).

Claude Code 会话里用 Bash 驱动 mnelo 的标准入口. 底层走 MneloClient (MCP
标准协议), token 自动从 ~/.config/mnelo/auth_token 读.

用法:
  mn.py digest [--ref <行号>]            # 会话摘要; 缺省压缩视图, --ref 展开某行源 chunk
  mn.py remember "文本" [--source s] [--importance 0.7] [--memory-type decision] [--tags a,b]
  mn.py recall "查询" [--top-k 5] [--filters '{"type":"decision"}']
  mn.py relate --from <entity_id> --to <entity_id> --rel <relation> [--evidence <chunk_id>]
  mn.py stats                            # chunk / entity / relation 计数

memory_type 取值 (README §操作指令): fact / preference / episode / decision / procedure / ephemeral.
不传则走 P1a 零-LLM 规则自动分类.

对 Claude Code: 需要时显式传 memory_type 和实体 kind (约定见 CLAUDE.md), 一致性是契约.
"""

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "api"))

from mnelo_client import MneloClient  # noqa: E402


def _client() -> MneloClient:
    return MneloClient()


def cmd_digest(args) -> int:
    client = _client()
    digest = client.get_digest(ref=args.ref)
    print(json.dumps(digest, ensure_ascii=False, indent=2) if isinstance(digest, dict) else digest)
    return 0


def cmd_remember(args) -> int:
    client = _client()
    call_args = {
        "content": args.content,
        "source": args.source,
        "importance": args.importance,
    }
    if args.memory_type:
        call_args["memory_type"] = args.memory_type
    if args.tags:
        call_args["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    result = client._call("memory_remember", call_args)  # noqa: SLF001 — 薄封装, 同仓库
    print(json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else result)
    return 0


def cmd_recall(args) -> int:
    client = _client()
    filters = json.loads(args.filters) if args.filters else None
    results = client.recall(args.query, top_k=args.top_k, filters=filters)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_relate(args) -> int:
    client = _client()
    call_args = {
        "source_id": args.frm,
        "target_id": args.to,
        "relation": args.rel,
    }
    if args.evidence:
        call_args["evidence_chunk_id"] = args.evidence
    result = client._call("memory_relate", call_args)  # noqa: SLF001 — 薄封装, 同仓库
    print(json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else result)
    return 0


def cmd_stats(args) -> int:
    client = _client()
    stats = client.stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mn", description="mnelo CLI (MCP/SSE)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("digest", help="会话状态摘要")
    p.add_argument("--ref", type=int, default=None, help="展开摘要第 N 行的源 chunk")
    p.set_defaults(fn=cmd_digest)

    p = sub.add_parser("remember", help="写入一条记忆")
    p.add_argument("content")
    p.add_argument("--source", default="claude-code", help="来源标签, 便于检索/清理")
    p.add_argument("--importance", type=float, default=0.5)
    p.add_argument("--memory-type", choices=["fact", "preference", "episode", "decision", "procedure", "ephemeral"], default=None, help="不传则自动分类")
    p.add_argument("--tags", default=None, help="逗号分隔 tags")
    p.set_defaults(fn=cmd_remember)

    p = sub.add_parser("recall", help="4 路 + RRF 召回")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--filters", default=None, help='JSON, 如 {"type":"decision"}')
    p.set_defaults(fn=cmd_recall)

    p = sub.add_parser("relate", help="连接两个实体")
    p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--to", dest="to", required=True)
    p.add_argument("--rel", required=True)
    p.add_argument("--evidence", default=None, help="支撑这条关系的 chunk_id")
    p.set_defaults(fn=cmd_relate)

    p = sub.add_parser("stats", help="计数")
    p.set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except Exception as e:  # noqa: BLE001 — CLI 兜底: server 掉线等统一报简洁错误
        print(f"[mn] error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
