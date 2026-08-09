#!/usr/bin/env python3
"""
restore_db.py — mnelo DB 快照恢复 (DESIGN §3.11 + TASKS_BACKUP_RESTORE A2).

选择快照 → 校验 sha256 → PRAGMA integrity_check → 隔离当前 db →
原子替换 live db. dry-run 只跑校验不落盘.

用法:
  python scripts/restore_db.py --list
  python scripts/restore_db.py --latest --dry-run
  python scripts/restore_db.py --from 2026-08-05-140429 --dry-run
  python scripts/restore_db.py --latest              # 实际恢复
  python scripts/restore_db.py --from YYYY-MM-DD-HHMMSS --target /tmp/foo.db
"""

import argparse
import datetime as dt
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from config import config as _config  # noqa: E402


def _expand(p):
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _default_snapshot_dir():
    return _expand(_config.db_path).parent / "snapshots"


def _read_backup_config():
    snap_dir = _expand(getattr(_config, "backup_snapshot_dir", None) or _default_snapshot_dir())
    return snap_dir


def _server_running(port: int | None = None) -> bool:
    """MCP server 是否在监听 127.0.0.1:port.

    [8/5 fix] restore 到 live db 前必须确认 server 已停 — server 持有旧 inode,
    运行中替换会把它后续写入导向已删文件, 静默丢失.
    """
    port = port if port is not None else int(getattr(_config, "server_port", 8086) or 8086)
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _verify_sha256(gz_path: Path) -> tuple[bool, str]:
    """Return (ok, sha256_hex)."""
    # backup_db.py 写的是 <ts>.db.gz.sha256 (gzip + 显式 .sha256 后缀),
    # Path.with_suffix 只换最后一个后缀, 不能用. 显式 append.
    sha_path = gz_path.parent / (gz_path.name + ".sha256")
    if not sha_path.exists():
        return False, "no sha256 sidebar"
    expected = sha_path.read_text().split()[0]
    h = hashlib.sha256()
    with gz_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    return actual == expected, actual


def _integrity_check(db_path: Path) -> dict:
    """Run PRAGMA integrity_check + quick_check + foreign_key_check. Returns dict."""
    con = sqlite3.connect(str(db_path))
    try:
        out = {}
        # integrity_check 返 'ok' 列表, length == 1 时正常
        rows = con.execute("PRAGMA integrity_check").fetchall()
        out["integrity_check"] = rows[0][0] if rows else "no-result"
        rows = con.execute("PRAGMA quick_check").fetchall()
        out["quick_check"] = rows[0][0] if rows else "no-result"
        rows = con.execute("PRAGMA foreign_key_check").fetchall()
        # foreign_key_check 异常时返 (table, rowid, parent) tuples; 空 = ok
        out["foreign_key_check"] = "ok" if not rows else f"violations: {len(rows)}"
        # 统计
        stats = {}
        for tbl in ("chunks", "entities", "relations", "audit_log"):
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                stats[tbl] = n
            except sqlite3.OperationalError:
                stats[tbl] = None
        out["row_counts"] = stats
        return out
    finally:
        con.close()


def _list_snapshots(snapshot_dir: Path) -> list[dict]:
    if not snapshot_dir.exists():
        return []
    out = []
    for gz in sorted(snapshot_dir.glob("*.db.gz"), reverse=True):
        sha_ok, sha = _verify_sha256(gz)
        out.append(
            {
                "name": gz.name,
                "path": str(gz),
                "size_mb": round(gz.stat().st_size / 1024 / 1024, 3),
                "mtime": gz.stat().st_mtime,
                "sha256_ok": sha_ok,
                "sha256": sha,
            }
        )
    return out


def _select_snapshot(snapshot_dir: Path, ts: str | None) -> Path:
    """Resolve --from / --latest to a concrete .db.gz path."""
    if not snapshot_dir.exists():
        raise FileNotFoundError(f"快照目录不存在: {snapshot_dir}")
    if ts is None:
        # latest
        gzs = sorted(snapshot_dir.glob("*.db.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not gzs:
            raise FileNotFoundError(f"无快照: {snapshot_dir}")
        return gzs[0]
    # explicit timestamp
    target = snapshot_dir / f"{ts}.db.gz"
    if not target.exists():
        # 尝试 prefix 匹配
        matches = list(snapshot_dir.glob(f"{ts}*.db.gz"))
        if not matches:
            raise FileNotFoundError(f"无快照匹配 '{ts}': {snapshot_dir}")
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:5])
            raise ValueError(f"'{ts}' 匹配多个快照 ({len(matches)}): {names}...")
        return matches[0]


def _atomic_replace(src: Path, target: Path) -> None:
    """Decompress src to <target>.tmp, then mv to target atomically."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    with gzip.open(src, "rb") as f_in, tmp.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1 << 20)
    # mv 是 atomic on same filesystem
    os.replace(tmp, target)


def _rebuild_index(target: Path) -> dict:
    """[8/6 plan §7] 恢复后重建 search index (usearch.index / search_index.zv).

    索引文件不在 SQLite 事务内, DB-only 备份索引天然滞后; 恢复后跑 rebuild fresh=True
    按 SQLite 真源重建. 失败不 throw — DB 已是真源, warning 让用户手动补跑.
    """
    # [8/6 plan §7 + review P1-1] 三层 fallback: scripts pkg / cwd-relative / abs path
    # - cwd=tests/: "from scripts" 成功 (scripts 是 cwd 子目录, 无 __init__.py 也行)
    # - cwd=repo/: 两者都成功
    # - cwd=其它: 绝对路径 importlib 兜底
    _ri = None
    try:
        from scripts import rebuild_index as _ri_mod  # type: ignore

        _ri = _ri_mod
    except ImportError:
        pass
    if _ri is None:
        try:
            import rebuild_index as _ri_mod  # type: ignore

            _ri = _ri_mod
        except ImportError:
            pass
    if _ri is None:
        # 绝对路径兜底
        try:
            import importlib.util as _ilu

            _spec = _ilu.spec_from_file_location("rebuild_index", _REPO_ROOT / "scripts" / "rebuild_index.py")
            _ri = _ilu.module_from_spec(_spec)  # type: ignore
            _spec.loader.exec_module(_ri)  # type: ignore
        except Exception as e:
            return {"error": f"rebuild_index import failed: {e}"}
    backend = getattr(_config, "search_backend", "auto") or "auto"
    try:
        return _ri.rebuild(backend, target, dry_run=False, fresh=True)
    except Exception as e:
        return {"error": f"rebuild failed: {e}"}


def _isolate(target: Path) -> Path:
    """Move current live db → memory.db.corrupt-<date>. Returns the corrupt path."""
    if not target.exists():
        return None
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    corrupt = target.parent / f"{target.name}.corrupt-{ts}"
    shutil.move(str(target), str(corrupt))
    return corrupt


def restore(
    snapshot_dir: Path,
    ts: str | None,
    target: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    rebuild: bool = True,
) -> dict:
    """Run one restore. Returns stats dict.

    force: MCP server 运行时也强制替换 live db (恢复后必须重启 server).
    rebuild: [8/6 plan §7] 恢复后自动重建 search index (默认 True).
             False 只换 DB, 索引保持旧 (可能错位).
    """
    target = Path(target) if target else _expand(_config.db_path)
    gz_path = _select_snapshot(snapshot_dir, ts)
    report = {"selected": str(gz_path), "target": str(target), "dry_run": dry_run}

    # 1. sha256
    sha_ok, sha = _verify_sha256(gz_path)
    report["sha256_ok"] = sha_ok
    report["sha256"] = sha
    if not sha_ok:
        report["error"] = "sha256 校验失败"
        return report

    # 2. 解压到 tmp, 跑 integrity_check
    tmp = target.parent / f"{target.name}.{ts}.validate.tmp"
    if tmp.exists():
        tmp.unlink()
    with gzip.open(gz_path, "rb") as f_in, tmp.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out, length=1 << 20)
    # [8/5 fix] tmp 也是 KG 数据 — 0600; os.replace 后目标继承此权限
    os.chmod(tmp, 0o600)

    try:
        check = _integrity_check(tmp)
        report["integrity_check"] = check
        if check["integrity_check"] != "ok":
            report["error"] = f"integrity_check failed: {check['integrity_check']}"
            return report
        if check["quick_check"] != "ok":
            report["error"] = f"quick_check failed: {check['quick_check']}"
            return report
        if check["foreign_key_check"] != "ok":
            report["error"] = f"foreign_key_check failed: {check['foreign_key_check']}"
            return report

        if dry_run:
            return report

        # [8/5 fix] live db 恢复前检查 MCP server — 运行中替换会把 server 的
        # 后续写入导向已删 inode, 静默丢失.
        if target == _expand(_config.db_path) and _server_running():
            if not force:
                port = int(getattr(_config, "server_port", 8086) or 8086)
                report["error"] = f"MCP server 仍在运行 (127.0.0.1:{port}) — 直接替换 live db 会丢失 server 的后续写入。请先停止 server 再恢复, 或 --force 明确覆盖 (恢复后必须重启 server)。"
                return report
            report["warning"] = "MCP server 仍在运行 — 恢复后必须重启 server 才能生效"

        # 3. 隔离当前 db
        corrupt = _isolate(target)
        if corrupt:
            report["isolated_to"] = str(corrupt)
        else:
            report["isolated_to"] = None

        # 4. 原子替换 (从 tmp → target)
        try:
            os.replace(tmp, target)
            # [8/5 fix] 清掉属于旧 db 的 WAL/SHM — 残留的 -wal 会被新 db 误应用
            for suffix in ("-wal", "-shm"):
                stale = target.parent / (target.name + suffix)
                if stale.exists():
                    stale.unlink(missing_ok=True)
            report["restored"] = str(target)
        except Exception:
            # 失败: 从 corrupt 恢复
            if corrupt:
                shutil.move(str(corrupt), str(target))
            raise

        # 5. [8/6 plan §7] 重建 search index (DB 已真源; 索引文件需按新 chunks 重建)
        if rebuild:
            report["index_rebuilt"] = _rebuild_index(target)
            if report["index_rebuilt"].get("error"):
                report["index_error"] = report["index_rebuilt"]["error"]

        return report
    finally:
        if tmp.exists():
            tmp.unlink()


def main():
    ap = argparse.ArgumentParser(description="mnelo DB 快照恢复")
    ap.add_argument("--list", action="store_true", help="列出所有快照 + 校验状态")
    ap.add_argument("--from", dest="ts", default=None, help="指定快照 timestamp (YYYY-MM-DD-HHMMSS)")
    ap.add_argument("--latest", action="store_true", help="选最新快照")
    ap.add_argument("--dry-run", action="store_true", help="只校验不恢复")
    ap.add_argument("--snapshot-dir", type=Path, default=None, help="覆盖 config [backup] snapshot_dir")
    ap.add_argument("--target", type=Path, default=None, help="恢复目标路径 (默认 live db 路径)")
    ap.add_argument("--force", action="store_true", help="MCP server 运行时也强制恢复 (恢复后必须重启 server)")
    ap.add_argument("--skip-rebuild", action="store_true", help="恢复后跳过 search index 重建 (默认: 自动重建)")
    args = ap.parse_args()

    snap_dir = Path(args.snapshot_dir) if args.snapshot_dir else _read_backup_config()

    if args.list:
        snaps = _list_snapshots(snap_dir)
        for s in snaps:
            print(s)
        if not snaps:
            print(f"(no snapshots in {snap_dir})")
        return 0

    # --from / --latest 必须二选一, 默认走 latest
    if args.ts and args.latest:
        print("ERROR: --from 和 --latest 互斥", file=sys.stderr)
        return 2
    ts = args.ts if args.ts else None

    report = restore(snap_dir, ts, target=args.target, dry_run=args.dry_run, force=args.force, rebuild=not args.skip_rebuild)
    print(report)
    if report.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
