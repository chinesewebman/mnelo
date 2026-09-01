#!/usr/bin/env python3
"""月度 purge worker (cron): 物理删到期软删项 + 清孤儿 purged_queue 行 + 孤儿向量.

设计语义 (memory.py run_purge_worker):
  - Phase 1: 清 purged_queue 中 target 已不在主表的孤儿行 (placeholder/脏数据)
  - Phase 2: 物理删 valid_until 非空 且 purged_at(计划删除时间) < 今天 的主表行
             (purged_queue.purged_at 存的是「入队时间 + 30 天」的计划物理删除时间)
  - Phase 3: 清 vec0 孤儿向量
结果写 MNELO_CRON_OUTPUT_DIR/cron-output/ 供审计. 幂等, 可安全重复跑.

注意: 本脚本不依赖 L2 自主层 (l2.enabled 可保持 false).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path('/root/work/mnelo')
sys.path.insert(0, str(REPO))

DATA_DIR = Path('/root/work/mnelo-data')
DB_PATH = DATA_DIR / 'memory.db'

from memory import Memory  # noqa: E402


def main() -> int:
    m = Memory(db_path=DB_PATH)
    r = m.run_purge_worker(dry_run=False, clean_orphan_target_ids=True)

    summary = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": r,
    }
    out = json.dumps(summary, ensure_ascii=False, indent=2)

    out_dir = DATA_DIR / 'cron-output'
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"purge_worker_{stamp}.json"
    log_path.write_text(out, encoding="utf-8")

    print(out)
    print(f"[purge_worker] log → {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
