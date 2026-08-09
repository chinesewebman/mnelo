#!/usr/bin/env python3
"""
backup_db.py — mnelo DB 快照备份 (DESIGN §3.8 + TASKS_BACKUP_RESTORE A1).

包装 SQLite backup() API (不用 cp — WAL 模式 cp 拷到中间页) →
snapshots/YYYY-MM-DD-HHMMSS.db → gzip → .sha256.

读 config [backup] 段:
  enabled = true
  snapshot_dir = "~/.hermes/memory/snapshots"
  schedule = "wed+sun"  # 仅文档, 调度由 plist 负责
  retention = 30        # 保留最近 N 份, 多出按 mtime 删

用法:
  python scripts/backup_db.py                 # 按 config
  python scripts/backup_db.py --dry-run       # 只统计不写
  python scripts/backup_db.py --force         # 覆盖日内已有的同名快照
  python scripts/backup_db.py --snapshot-dir /tmp/foo  # 覆盖 config
"""

import argparse
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 强制从仓库本地 import
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from config import config as _config  # noqa: E402


def _expand(p):
    """Expand ~ and env vars in path strings."""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _default_snapshot_dir():
    """Default snapshot dir if config has none."""
    return _expand(_config.db_path).parent / "snapshots"


def _read_backup_config():
    """Read [backup] section from config (with defaults)."""
    snap_dir = _expand(getattr(_config, "backup_snapshot_dir", None) or _default_snapshot_dir())
    retention = int(getattr(_config, "backup_retention", 30) or 30)
    return {
        "snapshot_dir": snap_dir,
        "retention": retention,
    }


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_snapshot_paths(snapshot_dir: Path, ts: str):
    """Return (db_path, gz_path, sha_path)."""
    db_path = snapshot_dir / f"{ts}.db"
    gz_path = snapshot_dir / f"{ts}.db.gz"
    sha_path = snapshot_dir / f"{ts}.db.gz.sha256"
    return db_path, gz_path, sha_path


def _today_snapshot_exists(snapshot_dir: Path) -> Path | None:
    """Return existing snapshot for today (YYYY-MM-DD-*) if any."""
    if not snapshot_dir.exists():
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for gz in snapshot_dir.glob(f"{today}-*.db.gz"):
        return gz
    return None


def _prune_old(snapshot_dir: Path, retention: int) -> int:
    """Delete oldest snapshots beyond retention. Returns count deleted."""
    if not snapshot_dir.exists():
        return 0
    gz_files = sorted(snapshot_dir.glob("*.db.gz"), key=lambda p: p.stat().st_mtime)
    excess = len(gz_files) - retention
    if excess <= 0:
        return 0
    pruned = 0
    for old in gz_files[:excess]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            continue
        # 也清配套 .sha256 — Path.with_suffix(".db.gz.sha256") 只换最后后缀,
        # 会变 ".db.gz.db.gz.sha256" 错. 显式 append.
        sha = old.parent / (old.name + ".sha256")
        try:
            sha.unlink(missing_ok=True)
        except OSError:
            pass
        pruned += 1
    return pruned


def backup(snapshot_dir: Path, retention: int, dry_run: bool = False, force: bool = False, db_path: Path | None = None) -> dict:
    """Run one backup. Returns stats dict.

    db_path: 源 SQLite 路径 (默认 config.db_path). 测试可传 tmp db.
    """
    started = time.time()
    db_path = _expand(db_path) if db_path else _expand(_config.db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"源数据库不存在: {db_path}")

    if not dry_run:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        # [8/5 fix] 快照含 KG/PII — 目录收紧到 0700 (仓库 P0-1 政策)
        os.chmod(snapshot_dir, 0o700)

    # force 模式下, 如果同 ts 已有快照, 加秒后缀强制唯一
    base_ts = _timestamp()
    ts = base_ts
    if force:
        suffix = 1
        while (snapshot_dir / f"{ts}.db").exists() or (snapshot_dir / f"{ts}.db.gz").exists():
            ts = f"{base_ts}-{suffix:02d}"
            suffix += 1
            if suffix > 99:
                break
    raw_path, gz_path, sha_path = _make_snapshot_paths(snapshot_dir, ts)

    # 日内去重 (DESIGN §3.11 演练: 一次/日足够)
    if not force:
        existing = _today_snapshot_exists(snapshot_dir)
        if existing is not None:
            return {
                "skipped": True,
                "reason": f"今已有快照: {existing.name}",
                "duration_sec": round(time.time() - started, 3),
            }

    if dry_run:
        return {
            "skipped": True,
            "dry_run": True,
            "would_write": str(gz_path),
            "duration_sec": round(time.time() - started, 3),
        }

    # 1. SQLite backup() API → raw .db
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(raw_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # 2. gzip
    with raw_path.open("rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1 << 20)
    raw_path.unlink()  # 留 .gz 即可

    # 3. SHA256
    sha = _sha256_file(gz_path)
    sha_path.write_text(f"{sha}  {gz_path.name}\n")

    # [8/5 fix] 快照文件收紧到 0600 — 仓库 P0-1 政策 (KG/PII 不该对本地其他 user 可读)
    os.chmod(gz_path, 0o600)
    os.chmod(sha_path, 0o600)

    # 4. Retention
    pruned = _prune_old(snapshot_dir, retention)

    # 5. Stats
    size_mb = round(gz_path.stat().st_size / 1024 / 1024, 3)
    return {
        "path": str(gz_path),
        "sha256": sha,
        "size_mb": size_mb,
        "duration_sec": round(time.time() - started, 3),
        "pruned": pruned,
        "kept": len(list(snapshot_dir.glob("*.db.gz"))),
    }


def main():
    ap = argparse.ArgumentParser(description="mnelo DB 快照备份")
    ap.add_argument("--dry-run", action="store_true", help="只统计, 不真正备份")
    ap.add_argument("--force", action="store_true", help="无视日内去重, 强写新快照")
    ap.add_argument("--snapshot-dir", type=Path, default=None, help="覆盖 config [backup] snapshot_dir")
    ap.add_argument("--retention", type=int, default=None, help="覆盖 config [backup] retention (保 N 份)")
    ap.add_argument("--scheduled", action="store_true", help="调度调用 (cron/plist) — 尊重 [backup] enabled, disabled 时跳过")
    args = ap.parse_args()

    # [8/5 fix] scheduled 调用尊重 enabled: [backup] enabled=false 或 env=false → 跳过.
    # 手动调用 (不带 --scheduled) 不受限 — 随时可做一次性备份.
    if args.scheduled and not _config.backup_enabled:
        print("backup disabled — [backup] enabled 未开启 (scheduled run 跳过). 设 MNELO_MEMORY_BACKUP_ENABLED=true, 或去掉 --scheduled 手动备份.")
        return 0

    cfg = _read_backup_config()
    snap_dir = Path(args.snapshot_dir) if args.snapshot_dir else cfg["snapshot_dir"]
    ret = args.retention if args.retention is not None else cfg["retention"]

    result = backup(snap_dir, ret, dry_run=args.dry_run, force=args.force)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
