"""
conftest.py — pytest 共享 fixture.

[7/19 patch] 修复测试间 state leakage:
- vec0 (vectors) 表的 rowid = chunks.rowid
- 老 tearDownClass 只 DELETE chunks → vectors 留下 → 下次 INSERT 撞 UNIQUE
- session-scoped fixture 在 session 开始前清空所有 test_* 源数据, 之后每个 test
  自己的 tearDownClass 各自清自己的 prefix — session-scoped cleanup 只兜底
  cross-class 脏数据 (如 test_edge_cases TestUpdateEdgeCases 漏删 vectors)
"""
import sys
from pathlib import Path

# [7/19 patch] 强制从 repo 本地代码 import (与 tests/test_edge_cases.py 同策略)
import importlib.util as _ilu
_REPO_ROOT = Path(__file__).resolve().parent.parent

def _load_from_repo(mod_name: str):
    spec = _ilu.spec_from_file_location(mod_name, _REPO_ROOT / f'{mod_name}.py')  # type: ignore[arg-type]
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

_load_from_repo('config')
_load_from_repo('embedder')
_load_from_repo('memory')

import pytest  # noqa: E402


@pytest.fixture(scope='session', autouse=True)
def _clean_test_data_session():
    """[7/19] session 开始前清空跨 class 残留的 test 数据.

    只清 source 含 'test' 但不在 class-prefix 保护范围的数据 (兜底 tearDownClass 漏掉的).
    各 class 自己 tearDownClass 仍然管自己的 prefix cleanup.
    """
    from memory import Memory
    mem = Memory()
    try:
        # 清 vectors (按 rowid, 避免漏 vec0 rowid)
        rows = mem._conn.execute(
            "SELECT rowid FROM chunks WHERE source LIKE '%test%' OR source LIKE '%audit%'"
        ).fetchall()
        if rows:
            rowids = [r['rowid'] for r in rows]
            placeholders = ','.join('?' * len(rowids))
            mem._conn.execute(
                f"DELETE FROM vectors WHERE rowid IN ({placeholders})", rowids
            )
        # 清 chunks / entities / relations 兜底 (按 source LIKE)
        mem._conn.execute(
            "DELETE FROM chunks WHERE source LIKE '%test%' OR source LIKE '%audit%'"
        )
        mem._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'test_%' OR source LIKE '%test%'"
        )
        mem._conn.execute(
            "DELETE FROM relations WHERE source_id LIKE 'test_%' OR target_id LIKE 'test_%' "
            "OR source LIKE '%test%'"
        )
        mem._conn.commit()
    finally:
        mem.close()
    yield