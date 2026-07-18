#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
entity_resolve.py — 实体消歧 (实战: 7/18 主人口中拍板方案 1 实战需求)

[实战目的]
- 实战中常出现 "sh600089" / "特变电工" / "TBEA" / "特变电工股份" 同指一只股
- 主人口中实战: 报告用 "sh600089 (特变电工)" 双标, 但其他报告用 "特变"
- 自动合并 alias 相同的 entity, 避免 kg 节点重复

[设计]
- 1) 别名匹配: aliases_json 数组, 直接匹配 → 合并
- 2) 相似度合并: 名字相似度 ≥ 0.85 → 合并 (实战: 0.85 = 同股票不同名变体)
- 3) 实战 review API: find_duplicates() 列出所有疑似重复, 实战人工 review
"""
import json
import re
import sqlite3
import difflib
import sys
from pathlib import Path
from typing import List, Tuple, Optional

# [P2 审计] 复用 memory.now() 而非自己 datetime.now()
sys.path.insert(0, str(Path(__file__).parent))
from memory import now

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')


def normalize_text(s: str) -> str:
    """实战: 标准化字符串 — 去空格 + 去标点 + 小写."""
    return re.sub(r'[\s\W_]+', '', s.lower()).strip()


def alias_match_score(a: str, b: str) -> float:
    """实战相似度: 同时考虑完全匹配 + difflib 比率."""
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    # difflib.SequenceMatcher 实战中较准
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


def get_aliases(conn: sqlite3.Connection, entity_id: str) -> List[str]:
    """Get all aliases for an entity: its name + aliases_json entries.

    Args:
        conn: sqlite3.Connection (with row_factory=sqlite3.Row)
        entity_id: entity id to look up

    Returns:
        List of alias strings. Returns [] if entity not found, soft-deleted,
        or has no name/aliases_json. JSON parse errors are silently swallowed.
    """
    row = conn.execute(
        "SELECT name, aliases_json FROM entities WHERE id = ? AND valid_until IS NULL",
        (entity_id,)
    ).fetchone()
    if not row:
        return []
    aliases = []
    if row['name']:
        aliases.append(row['name'])
    if row['aliases_json']:
        try:
            aliases.extend(json.loads(row['aliases_json']))
        except (json.JSONDecodeError, TypeError):
            pass
    return aliases


def find_duplicate_candidates(
    conn: sqlite3.Connection,
    threshold: float = 0.85,
    kind: Optional[str] = None,
) -> List[Tuple[str, str, float, str]]:
    """实战: 找出所有疑似重复的 entity 对.

    Returns:
      [(entity_a_id, entity_b_id, score, reason), ...]
    """
    sql = "SELECT id, kind, name FROM entities WHERE valid_until IS NULL"
    params = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    rows = conn.execute(sql, params).fetchall()

    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r['kind'], []).append(r)

    candidates = []
    for kind_name, ents in by_kind.items():
        if len(ents) < 2:
            continue
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a_id, a_name = ents[i]['id'], (ents[i]['name'] or '')
                b_id, b_name = ents[j]['id'], (ents[j]['name'] or '')
                if not a_name or not b_name:
                    continue
                # 实战: 跳过已 supersede / 完全相同 id
                if a_id == b_id:
                    continue
                score = alias_match_score(a_name, b_name)
                if score >= threshold:
                    candidates.append((a_id, b_id, score, f'name: "{a_name}" vs "{b_name}"'))

                # 实战: aliases 匹配
                a_aliases = get_aliases(conn, a_id)
                b_aliases = get_aliases(conn, b_id)
                for al in a_aliases:
                    for bl in b_aliases:
                        if al == bl and al != a_name and al != b_name:
                            candidates.append((a_id, b_id, 1.0, f'alias 冲突: "{al}"'))

    return candidates


def merge_entities(
    conn: sqlite3.Connection,
    primary_id: str,
    secondary_id: str,
    reason: str = 'auto-merge',
) -> bool:
    """Merge secondary entity into primary (idempotent reverse direction is a no-op).

    操作:
    1. primary 的 aliases += secondary 的 aliases + name (dedup via dict.fromkeys)
    2. secondary 的所有 relations 重指向 primary (1 个 SQL 同时处理 src + tgt)
    3. secondary soft delete (valid_until = now())
    4. secondary 的 chunks/embeddings 不动 (保留原 content, 实战审计)

    Args:
        conn: sqlite3.Connection (should be the same Memory._conn)
        primary_id: 保留的 entity id (接收 aliases + relations)
        secondary_id: 被合并的 entity id (将 soft delete)
        reason: audit reason (写进 secondary 的 superseded 记录)

    Returns:
        True 成功, False 失败 (id 相同 / 任一 id 不存在 / 已 supersede)
    """
    if primary_id == secondary_id:
        return False
    primary = conn.execute(
        "SELECT id, name, aliases_json FROM entities WHERE id = ? AND valid_until IS NULL",
        (primary_id,)
    ).fetchone()
    secondary = conn.execute(
        "SELECT id, name, aliases_json FROM entities WHERE id = ? AND valid_until IS NULL",
        (secondary_id,)
    ).fetchone()
    if not primary or not secondary:
        return False

    # 1. 合并 aliases (复用 get_aliases 的 JSON 解析逻辑, 避免重复)
    primary_aliases = get_aliases(conn, primary_id)
    secondary_aliases = get_aliases(conn, secondary_id)
    merged = list(dict.fromkeys(primary_aliases + secondary_aliases))
    merged_str = json.dumps(merged, ensure_ascii=False)

    # 2. primary aliases 更新
    conn.execute("""
        UPDATE entities SET aliases_json = ?
        WHERE id = ? AND valid_until IS NULL
    """, (merged_str, primary_id))

    # 3. secondary 的入边 / 出边重指向 primary (1 个 SQL, 同时处理 src + tgt)
    conn.execute("""
        UPDATE relations
        SET target_id = CASE WHEN target_id = ? THEN ? ELSE target_id END,
            source_id = CASE WHEN source_id = ? THEN ? ELSE source_id END
        WHERE (target_id = ? OR source_id = ?) AND valid_until IS NULL
          AND NOT (source_id = ? AND target_id = ?)  -- 排除自环
    """, (secondary_id, primary_id, secondary_id, primary_id,
          secondary_id, secondary_id, primary_id, primary_id))

    # 4. secondary soft delete
    conn.execute("""
        UPDATE entities SET valid_until = ?, superseded_by = ?
        WHERE id = ? AND valid_until IS NULL
    """, (now(), primary_id, secondary_id))

    conn.commit()
    return True


def find_duplicates_report(conn: sqlite3.Connection, threshold: float = 0.85) -> str:
    """Generate Markdown report of duplicate entity candidates.

    Args:
        conn: sqlite3.Connection
        threshold: similarity threshold [0.0, 1.0], default 0.85

    Returns:
        Markdown string. Empty if no duplicates:
            "✅ 无重复 entity (threshold=X)"
        Otherwise: table with columns A / B / 相似度 / 原因
    """
    candidates = find_duplicate_candidates(conn, threshold)
    if not candidates:
        return "✅ 无重复 entity (threshold={})".format(threshold)

    lines = [f"# 重复 entity 候选 (threshold={threshold})", ""]
    lines.append(f"共 {len(candidates)} 组疑似重复")
    lines.append("")
    lines.append("| A | B | 相似度 | 原因 |")
    lines.append("|---|---|---|---|")
    for a, b, score, reason in candidates:
        lines.append(f"| `{a}` | `{b}` | {score:.3f} | {reason} |")
    return '\n'.join(lines)


# === 自测 ===
if __name__ == '__main__':
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    print('=== Entity 数量 ===')
    for kind in ['stock', 'concept', 'person', 'canonical_fact']:
        n = conn.execute(
            "SELECT count(*) FROM entities WHERE kind = ? AND valid_until IS NULL",
            (kind,)
        ).fetchone()[0]
        print(f'  {kind}: {n}')

    print()
    print('=== 重复候选 (stock) ===')
    cands = find_duplicate_candidates(conn, threshold=0.7, kind='stock')
    for a, b, score, reason in cands[:20]:
        print(f'  {a} --{score:.3f}--> {b} ({reason})')

    print()
    print('=== 重复候选 (canonical_fact) ===')
    cands = find_duplicate_candidates(conn, threshold=0.6, kind='canonical_fact')
    for a, b, score, reason in cands[:20]:
        print(f'  {a} --{score:.3f}--> {b} ({reason})')

    conn.close()
