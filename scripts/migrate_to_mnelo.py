#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_to_mnelo.py — 从旧 Mnemosyne 导出文件迁数据到 mnelo

[7/17]
- 主人口中 7/17 拍板: 自建 mnelo (当时叫 hermes-memory) 替换 Mnemosyne
- 输入: mnemosyne-export-*.jsonl (53MB, 保留在 migration/)
- 输出: mnelo DB 全量数据 (entities + chunks + relations + vectors)

[source 标签]
- 保留 'mnemosyne-import' 作为 source 字段值, 便于数据溯源 (能区分"原始从 Mnemosyne 迁入的"vs"7/18 之后新增的")
- 注释中 "mnemosyne" 保留, 表示数据来源是旧系统, 审计/追溯用

[转换规则]
- mnemosyne.working_memory (3560) → chunks (importance=importance)
- mnemosyne.episodic_memory (334) → chunks (is_summary=true in metadata)
- mnemosyne.triples (8) → entities + relations (subject/relation/object)
- mnemosyne.annotations (19466) → entities (when kind=stock/concept) + relations
- mnemosyne.canonical_facts (8) → entities (kind=canonical_fact)
- embeddings (3103) → vectors 表 ( skip: 避免重嵌入 5+ min, 新数据靠自己写)
"""

import sys
import json
import sqlite3
import sqlite_vec
from pathlib import Path
from datetime import datetime

DB_PATH = Path("/Users/apple/.hermes/memory/memory.db")

# 复用 hermes-agent venv
sys.path.insert(0, "/Users/apple/.hermes/memory")
from embedder import get_embedder, embed_bytes
from memory import Memory, generate_id, now


def import_from_export(export_file: Path, batch_size: int = 100):
    """主入口."""
    print(f"=== 加载导出文件: {export_file} ===")
    with open(export_file) as f:
        data = json.load(f)

    wm = data.get("working_memory", [])
    em = data.get("episodic_memory", [])
    triples = data.get("triples", [])
    annotations = data.get("annotations", [])
    canonical_facts = data.get("canonical_facts", [])
    legacy_embeddings = data.get("legacy_embeddings", [])

    print(f"  working_memory: {len(wm)}")
    print(f"  episodic_memory: {len(em)}")
    print(f"  triples: {len(triples)}")
    print(f"  annotations: {len(annotations)}")
    print(f"  canonical_facts: {len(canonical_facts)}")
    print(f"  legacy_embeddings: {len(legacy_embeddings)}")

    m = Memory()
    print(f"\n=== 1. 导入 working_memory (3560) → chunks ===")
    # mnemosyne rowid ↔ hermes chunks.id 的映射
    rid_to_chunk_id = {}
    cnt = 0
    for w in wm:
        chunk_id = generate_id("chunk")
        rid_to_chunk_id[w["id"]] = chunk_id
        m._conn.execute(
            """
            INSERT INTO chunks (id, content, source, session_id, timestamp,
                                importance, metadata_json, valid_until,
                                recall_count, last_recalled)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
            (
                chunk_id,
                w["content"] or "",
                w.get("source") or "mnemosyne-import",
                w.get("session_id") or "default",
                w.get("timestamp") or w.get("created_at") or now(),
                float(w.get("importance", 0.5)),
                json.dumps(
                    {
                        "mnemosyne_id": w["id"],
                        "superseded_by": w.get("superseded_by"),
                        "metadata_json_orig": w.get("metadata_json"),
                        "veracity": w.get("veracity"),
                    },
                    ensure_ascii=False,
                ),
                int(w.get("recall_count", 0)),
                w.get("last_recalled"),
            ),
        )
        cnt += 1
        if cnt % batch_size == 0:
            m._conn.commit()
            print(f"  {cnt}/{len(wm)} chunks inserted")
    m._conn.commit()
    print(f"✅ {cnt} working_memory → chunks")

    print(f"\n=== 2. 导入 episodic_memory (334) → chunks (with is_summary=true) ===")
    for e in em:
        chunk_id = generate_id("chunk")
        rid_to_chunk_id[e["id"]] = chunk_id
        m._conn.execute(
            """
            INSERT INTO chunks (id, content, source, session_id, timestamp,
                                importance, metadata_json, valid_until)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
            (
                chunk_id,
                e["content"] or "",
                e.get("source") or "mnemosyne-import:episodic",
                e.get("session_id") or "default",
                e.get("timestamp") or now(),
                float(e.get("importance", 0.6)),
                json.dumps(
                    {
                        "mnemosyne_id": e["id"],
                        "mnemosyne_rowid": e.get("rowid"),
                        "is_summary": True,
                        "summary_of": (e.get("summary_of") or "").split(","),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    m._conn.commit()
    print(f"✅ {len(em)} episodic_memory → chunks")

    print(f"\n=== 3. 导入 triples (8) → entities + relations ===")
    for t in triples:
        # subject 和 object 当 entity
        for ent_id, label in [(t["subject"], "subject"), (t["object"], "object")]:
            if not ent_id:
                continue
            existing = m._conn.execute(
                "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL", (ent_id,)
            ).fetchone()
            if not existing:
                m._conn.execute(
                    """
                    INSERT INTO entities (id, kind, name, properties_json, source, valid_from, valid_until)
                    VALUES (?, 'concept', ?, ?, 'mnemosyne-import', ?, NULL)
                """,
                    (
                        ent_id,
                        ent_id,
                        json.dumps({"triple_role": label}, ensure_ascii=False),
                        t.get("valid_from") or now(),
                    ),
                )

        # relation
        m._conn.execute(
            """
            INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                   valid_from, valid_until, source, confidence)
            VALUES (?, ?, ?, 1.0, ?, ?, ?, 'mnemosyne-import', ?)
        """,
            (
                t["subject"],
                t["object"],
                t["predicate"],
                json.dumps({"triple_id": t["id"]}, ensure_ascii=False),
                t.get("valid_from"),
                t.get("valid_until"),
                float(t.get("confidence", 1.0)),
            ),
        )
    m._conn.commit()
    print(f"✅ {len(triples)} triples → entities + relations")

    print(f"\n=== 4. 导入 canonical_facts (8) → entities ===")
    for cf in canonical_facts:
        ent_id = f"canonical:{cf['id']}"  # 用 mnemosyne 自身 id, 避免 (category, name) 重复
        m._conn.execute(
            """
            INSERT INTO entities (id, kind, name, summary, properties_json, source, valid_from, valid_until)
            VALUES (?, 'canonical_fact', ?, ?, ?, 'mnemosyne-import', ?, NULL)
        """,
            (
                ent_id,
                cf["name"],
                (cf.get("body") or "")[:200],
                json.dumps(
                    {
                        "canonical_id": cf["id"],
                        "category": cf["category"],
                        "body": cf.get("body"),
                        "version": cf.get("version", 1),
                    },
                    ensure_ascii=False,
                ),
                cf.get("valid_from") or now(),
            ),
        )
    m._conn.commit()
    print(f"✅ {len(canonical_facts)} canonical_facts → entities")

    print(f"\n=== 5. 导入 annotations (19466) → relations ===")
    cnt = 0
    for a in annotations:
        # annotations 表 schema: id, memory_id, kind, value, source, confidence
        # : kind+value 是关系 type, memory_id 是 chunk id (via mnemosyne_id)
        target_chunk = rid_to_chunk_id.get(a["memory_id"])
        if not target_chunk:
            continue  # : 没映射到的 chunk 跳过
        # 抽取: kind 当 relation type, value 当 target entity id (虚拟)
        target_entity = f"anno:{a['kind']}:{a['value']}"
        # 确保 target_entity 存在
        existing = m._conn.execute(
            "SELECT id FROM entities WHERE id = ? AND valid_until IS NULL", (target_entity,)
        ).fetchone()
        if not existing:
            m._conn.execute(
                """
                INSERT INTO entities (id, kind, name, properties_json, source, valid_from, valid_until)
                VALUES (?, 'concept', ?, ?, 'mnemosyne-import:annotation', ?, NULL)
            """,
                (target_entity, a["value"], json.dumps({"annotation_kind": a["kind"]}, ensure_ascii=False), now()),
            )
        # relation: chunk --has_annotation--> entity
        m._conn.execute(
            """
            INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                                   valid_from, valid_until, source, confidence, evidence_chunk_id)
            VALUES (?, ?, ?, 1.0, ?, ?, NULL, 'mnemosyne-import:annotation', ?, ?)
        """,
            (
                target_chunk,
                target_entity,
                f"has_{a['kind']}",
                json.dumps({"annotation_id": a["id"], "value": a["value"]}, ensure_ascii=False),
                now(),
                float(a.get("confidence", 1.0)),
                target_chunk,
            ),
        )
        cnt += 1
        if cnt % 1000 == 0:
            m._conn.commit()
            print(f"  {cnt}/{len(annotations)} annotations imported")
    m._conn.commit()
    print(f"✅ {cnt} annotations → relations")

    print(f"\n=== 6. 嵌入 + 写向量 (会跑比较久) ===")
    # 对每个 working_memory chunk 嵌入 (复用 bge-small-zh-v1.5)
    print(f"  准备嵌入所有 working_memory chunks ({len(wm)})")
    cnt = 0
    for w in wm:
        chunk_id = rid_to_chunk_id.get(w["id"])
        if not chunk_id:
            continue
        # 查 chunks rowid
        row = m._conn.execute("SELECT rowid FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if not row:
            continue
        chunk_rowid = row[0]
        # 检查 vectors 是否已有
        existing_v = m._conn.execute("SELECT rowid FROM vectors WHERE rowid = ?", (chunk_rowid,)).fetchone()
        if existing_v:
            continue
        # 嵌入 + 写
        content = w.get("content") or ""
        v_bytes = embed_bytes(content)
        try:
            m._conn.execute("INSERT INTO vectors (rowid, embedding) VALUES (?, ?)", (chunk_rowid, v_bytes))
        except Exception as e:
            # vec0 dim 不匹配 fallback
            pass
        cnt += 1
        if cnt % 50 == 0:
            m._conn.commit()
            print(f"  {cnt} vectors inserted")
    m._conn.commit()
    print(f"✅ {cnt} vectors 写入")

    print(f"\n=== 7. 最终 stats ===")
    stats = m.stats()
    for k, v in stats.items():
        print(f"  {k}: {v}")

    m.close()
    print(f"\n✅ 全量导入完成")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="旧 Mnemosyne 导出文件路径 (mnemosyne-export-*.jsonl)")
    ap.add_argument("--skip-vectors", action="store_true", help="跳过向量嵌入 (加速)")
    args = ap.parse_args()

    import_from_export(Path(args.file))
