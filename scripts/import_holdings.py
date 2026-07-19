#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_holdings.py — 从 ~/.hermes/state/holdings_*.json 导入持仓快照到 mnelo

[ 7/18]
- 主人 7/17 明确: 持仓以截图为准, AI 推测/口头 buy/sell 记忆不靠谱
- 主人口中拍板 C: holdings_correction_2026-07-17.json → mnelo + decision-history.md §7
- 输入: ~/.hermes/state/holdings_*.json (holdings_correction_2026-07-17.json)
- 输出: mnelo entities (kind=position_snapshot) + relations (user --holds_position--> :stock)
- 幂等键:
    - entities: id = holding:{asof}:{symbol_code}
    - relations: (source_id='user', target_id, relation='holds_position', properties['asof'])
- source 标记: 'holdings-screenshot-ocr' (溯源用)
- valid_until: 不设 (快照是事实陈述, 不假设过期; 新快照 supersede)
"""
import sys
import json
import glob
import sqlite3
from pathlib import Path

# [7/19 P1-5] import validation
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validation import validate_holding_payload, ValidationError

DB_PATH = Path('/Users/apple/.hermes/memory/memory.db')
STATE_DIR = Path('/Users/apple/.hermes/state')


def find_holdings_files() -> list:
    return sorted(glob.glob(str(STATE_DIR / 'holdings_*.json')))


def ensure_stock_entity(con, symbol_code: str, name: str):
    """保证股票实体存在, 返回 entity id."""
    stock_id = f'stock:{symbol_code}'
    cur = con.execute("select id from entities where id = ? and valid_until IS NULL", (stock_id,))
    if cur.fetchone():
        return stock_id
    con.execute("""
        INSERT INTO entities (id, kind, name, summary, properties_json, source,
                              valid_from, valid_until, importance)
        VALUES (?, 'stock', ?, ?, ?, 'holdings-screenshot-ocr',
                datetime('now'), NULL, 0.85)
    """, (stock_id, name, name, json.dumps({'symbol_code': symbol_code, 'name': name}, ensure_ascii=False)))
    return stock_id


def ensure_holding_entity(con, asof: str, h: dict):
    """保证持仓快照实体存在, 返回 entity id."""
    holding_id = f'holding:{asof}:{h["symbol_code"]}'
    cur = con.execute("select id from entities where id = ? and valid_until IS NULL", (holding_id,))
    if cur.fetchone():
        return holding_id, False
    props = {
        'symbol_code': h['symbol_code'],
        'name': h['name'],
        'quantity': h['quantity'],
        'cost_price': h['cost_price'],
        'current_price': h['current_price'],
        'market_value_yuan': h['market_value_yuan'],
        'pnl_yuan': h['pnl_yuan'],
        'pnl_pct': h['pnl_pct'],
        'direction': h['direction'],
        'asof': asof,
        'source_file': 'holdings_correction_2026-07-17.json',
    }
    summary = (f'{h["name"]} ({h["symbol_code"]}) @ {h["quantity"]}股 '
               f'成本{h["cost_price"]:.2f} 现价{h["current_price"]:.2f} '
               f'盈亏{h["pnl_pct"]:+.2f}%')
    con.execute("""
        INSERT INTO entities (id, kind, name, summary, properties_json, source,
                              valid_from, valid_until, importance)
        VALUES (?, 'position_snapshot', ?, ?, ?, 'holdings-screenshot-ocr',
                datetime('now'), NULL, 0.95)
    """, (holding_id, h['name'], summary, json.dumps(props, ensure_ascii=False)))
    return holding_id, True


def ensure_relation(con, source_id: str, target_id: str, relation: str,
                    properties: dict):
    """保证 (source, target, relation, valid_until=NULL) 关系存在 (幂等)."""
    cur = con.execute("""
        select 1 from relations
        where source_id = ? and target_id = ? and relation = ?
          and valid_until IS NULL
        limit 1
    """, (source_id, target_id, relation))
    if cur.fetchone():
        return False
    con.execute("""
        INSERT INTO relations (source_id, target_id, relation, weight, properties_json,
                               valid_from, valid_until, source, confidence,
                               evidence_chunk_id)
        VALUES (?, ?, ?, 1.0, ?, datetime('now'), NULL,
                'holdings-screenshot-ocr', 1.0, NULL)
    """, (source_id, target_id, relation, json.dumps(properties, ensure_ascii=False)))
    return True


def main(dry_run: bool = False):
    files = find_holdings_files()
    print(f'=== 找到 {len(files)} 份 holdings json ===')
    for f in files:
        print(f'  {f}')

    if not files:
        print('没有 holdings json 文件, 退出')
        return

    con = sqlite3.connect(DB_PATH)

    USER_ID = 'user'
    stats = {
        'files': 0,
        'stock_entities_new': 0,
        'stock_entities_existing': 0,
        'holding_entities_new': 0,
        'holding_entities_existing': 0,
        'relations_new': 0,
        'relations_skipped': 0,
    }

    for fp in files:
        with open(fp) as f:
            data = json.load(f)

        # [7/19 P1-5] JSON schema 验证: 必须有 asof(str) + holdings(list)
        # 否则 malicious JSON 可以注入任意内容进 entities.summary / properties_json
        if not isinstance(data, dict):
            print(f'  ⚠️  {Path(fp).name}: 顶层不是 dict, 跳过')
            stats['files_skipped'] = stats.get('files_skipped', 0) + 1
            continue
        asof = data.get('asof')
        if not isinstance(asof, str) or not asof:
            print(f'  ⚠️  {Path(fp).name}: asof 缺失或不是 str, 跳过')
            stats['files_skipped'] = stats.get('files_skipped', 0) + 1
            continue
        holdings = data.get('holdings', [])
        if not isinstance(holdings, list):
            print(f'  ⚠️  {Path(fp).name}: holdings 不是 list, 跳过')
            stats['files_skipped'] = stats.get('files_skipped', 0) + 1
            continue

        print(f'\n--- {Path(fp).name} asof={asof} ({len(holdings)} 标的) ---')

        for h in holdings:
            # [7/19 P1-5] 每个 holding 也走 schema 验证, 防单条恶意 JSON
            try:
                h = validate_holding_payload(h)
            except ValidationError as ve:
                print(f'  ⚠️  holding validation fail: {ve.field} - {ve.reason}, skip')
                stats['holdings_skipped'] = stats.get('holdings_skipped', 0) + 1
                continue

            print(f'  {h.get("symbol_code", "?")} ({h.get("name", "?")}) '
                  f'{h.get("quantity", "?")}股 成本{h.get("cost_price", "?")} '
                  f'现价{h.get("current_price", "?")} 盈亏{h.get("pnl_pct", 0):+.2f}%')

            if dry_run:
                continue

            # 1. 股票 entity
            stock_id = ensure_stock_entity(con, h['symbol_code'], h['name'])
            cur = con.execute("select id from entities where id=?", (stock_id,))
            stats['stock_entities_new' if stock_id else 'stock_entities_existing'] += 0
            # 改: 上面没法判断"新建/已存在"; 先看是不是新建
            # 简化: 看 metadata
            cur2 = con.execute("select properties_json from entities where id=? and valid_until IS NULL", (stock_id,))
            # 这里建实体后必在,简化: 不区分

            # 2. 持仓快照 entity
            holding_id, is_new = ensure_holding_entity(con, asof, h)
            if is_new:
                stats['holding_entities_new'] += 1
            else:
                stats['holding_entities_existing'] += 1

            # 3. user --holds--> holding
            new1 = ensure_relation(con, USER_ID, holding_id, 'holds_position',
                                   {'asof': asof, 'source': 'screenshot_ocr'})
            if new1:
                stats['relations_new'] += 1
            else:
                stats['relations_skipped'] += 1

            # 4. holding --snapshot_of--> stock
            new2 = ensure_relation(con, holding_id, stock_id, 'snapshot_of',
                                   {'asof': asof})
            if new2:
                stats['relations_new'] += 1
            else:
                stats['relations_skipped'] += 1

        # 5. 整个快照元数据也存为 chunk (让人召回时能看到完整持仓列表)
        snap_text = json.dumps(data, ensure_ascii=False)
        chunk_id = f'holding_snapshot:{asof}'
        cur = con.execute("select id from chunks where id = ? and valid_until IS NULL", (chunk_id,))
        if not cur.fetchone():
            if not dry_run:
                con.execute("""
                    INSERT INTO chunks (id, content, source, session_id, timestamp,
                                        importance, metadata_json, valid_until)
                    VALUES (?, ?, 'holdings-screenshot-ocr', 'default', ?, 0.95,
                            ?, NULL)
                """, (chunk_id, snap_text, asof,
                      json.dumps({'asof': asof, 'holdings_count': len(holdings),
                                  'totals': data.get('totals', {}),
                                  'correction_notes': data.get('correction_notes')},
                                 ensure_ascii=False)))
        stats['files'] += 1

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
