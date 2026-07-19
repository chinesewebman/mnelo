#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_identity_facts.py — 从 hermes-memory chunks 中抽取身份陈述类 fact
                            写入 entities + relations (幂等)

- 主人口中 7/18 拍板: A+B+C 三件套的"backfill 身份陈述类 fact"具体实施
- 输入: hermes-memory.db 的 chunks 表 (valid_until IS NULL)
- 输出: entities (kind=identity_fact) + relations (user --predicate--> identity:xxx)
- 幂等键:
    - entities: id = identity:{predicate}:{slug(value)} (slugify 后唯一)
    - relations: (source_id, target_id, relation, valid_until IS NULL) 存在则跳过
- source 标记: 'identity-fact-extract' (溯源用)

[7/18 ]
- 6 个 predicate: lives_in / timezone / telegram_handle / github_handle / display_name / working_lang
- 不用 LLM 抽取 (怕 token 成本 + 不一致), 用严格正则 + 白名单
"""
import sys
import re
import sqlite3
from pathlib import Path

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')

# === 严格抽取器 ===
# (regex, predicate, value_normalizer)
STRICT_EXTRACTORS = [
    (re.compile(r"(?:位于|我在|住在|居住地|home in|lives in)[^a-zA-Z]{0,8}(北京市大兴区亦庄镇|北京)"),
     "lives_in", lambda m: "北京市大兴区亦庄镇"),
    (re.compile(r"坐标[^a-zA-Z]{0,8}(北京市大兴区亦庄镇|北京)"),
     "lives_in", lambda m: "北京市大兴区亦庄镇"),
    (re.compile(r"时区[^a-zA-Z]{0,8}(GMT[+\-]\d+)"),
     "timezone", lambda m: m.group(1)),
    (re.compile(r"(GMT[+\-]8)"),
     "timezone", lambda m: m.group(1)),
    (re.compile(r"(?:Telegram|TG|电报)[:：]?\s*(@\w+)"),
     "telegram_handle", lambda m: m.group(1)),
    (re.compile(r"GitHub[:：]?\s*(chinesewebman)"),
     "github_handle", lambda m: m.group(1)),
    (re.compile(r"github\.com/(chinesewebman)"),
     "github_handle", lambda m: m.group(1)),
    (re.compile(r"(?:名字|网名)[::]?\s*(2077 Ling)"),
     "display_name", lambda m: m.group(1)),
    (re.compile(r"(?:名字|网名)[::]?\s*(主人)"),
     "display_name", lambda m: m.group(1)),
    (re.compile(r"working lang[^a-zA-Z]{0,8}(English|中文|简体中文)", re.I),
     "working_lang", lambda m: "English" if m.group(1).lower() == "english" else "中文"),
    (re.compile(r"工作语言[^a-zA-Z]{0,8}(English|中文|简体中文)", re.I),
     "working_lang", lambda m: "English" if m.group(1).lower() == "english" else "中文"),
]


def slugify(s: str) -> str:
    """中文保留, ASCII 转 _ 形式"""
    out = []
    for ch in s:
        if ch.isascii() and ch.isalnum():
            out.append(ch)
        elif '\u4e00' <= ch <= '\u9fff':
            out.append(ch)
        else:
            out.append('_')
    s2 = ''.join(out).strip('_')
    return s2 or 'unnamed'


def extract_all(con):
    """扫所有现行 chunks, 返回 { (predicate, value) : [evidence_chunks] }"""
    cur = con.execute("select id, content from chunks where valid_until IS NULL")
    extracted = {}
    for cid, content in cur:
        if not content:
            continue
        for rx, predicate, norm in STRICT_EXTRACTORS:
            for m in rx.finditer(content):
                val = norm(m)
                key = (predicate, val)
                if key not in extracted:
                    extracted[key] = []
                if cid not in extracted[key]:
                    extracted[key].append(cid)
    return extracted


def ensure_entity(con, predicate: str, value: str) -> str:
    """保证 entity 存在, 返回 entity id"""
    ent_id = f"identity:{predicate}:{slugify(value)}"
    cur = con.execute("select id from entities where id = ? and valid_until IS NULL", (ent_id,))
    if cur.fetchone():
        return ent_id
    con.execute("""
        INSERT INTO entities (id, kind, name, summary, properties_json, source,
                              valid_from, valid_until, importance)
        VALUES (?, 'identity_fact', ?, ?, ?, 'identity-fact-extract',
                datetime('now'), NULL, 0.9)
    """, (ent_id, value, value, f'{{"predicate": "{predicate}", "value": "{value}"}}'))
    return ent_id


def relation_exists(con, source_id: str, target_id: str, relation: str) -> bool:
    cur = con.execute("""
        select 1 from relations
        where source_id = ? and target_id = ? and relation = ?
          and valid_until IS NULL
        limit 1
    """, (source_id, target_id, relation))
    return cur.fetchone() is not None


def ensure_relation(con, source_id: str, target_id: str, relation: str,
                    evidence_chunk_id: str):
    """保证 (source, target, relation, valid_until=NULL) 关系存在; 已存在补 evidence"""
    if relation_exists(con, source_id, target_id, relation):
        # 已存在: 跳过 (幂等)
        return False
    con.execute("""
        INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                               valid_from, valid_until, source, confidence,
                               evidence_chunk_id)
        VALUES (?, ?, ?, 1.0, ?, datetime('now'), NULL,
                'identity-fact-extract', 1.0, ?)
    """, (source_id, target_id, relation,
          f'{{"extracted_at": "{Path(__file__).name}", "predicate": "{relation}"}}',
          evidence_chunk_id))
    return True


def main(dry_run: bool = False):
    con = sqlite3.connect(DB_PATH)
    extracted = extract_all(con)
    print(f"=== 抽取 {len(extracted)} 个独立 (predicate, value) ===")

    # user 是已存在 entity
    USER_ID = 'user'

    stats = {"entities_new": 0, "entities_existing": 0,
             "relations_new": 0, "relations_skipped": 0}

    for (pred, val), evidences in sorted(extracted.items()):
        ent_id = f"identity:{pred}:{slugify(val)}"
        cur = con.execute("select id from entities where id = ? and valid_until IS NULL", (ent_id,))
        if cur.fetchone():
            stats["entities_existing"] += 1
        else:
            stats["entities_new"] += 1
            if not dry_run:
                ensure_entity(con, pred, val)

        # 拿最新 evidence
        first_ev = evidences[0]
        if not dry_run:
            new = ensure_relation(con, USER_ID, ent_id, pred, first_ev)
            if new:
                stats["relations_new"] += 1
            else:
                stats["relations_skipped"] += 1
        print(f"  {pred:18} → {val!r}  (entity: {ent_id}, evidence: {len(evidences)} chunks)")

    if not dry_run:
        con.commit()
    con.close()
    print()
    print(f"=== {'DRY-RUN' if dry_run else '实跑'} 统计 ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    main(dry_run=dry)
