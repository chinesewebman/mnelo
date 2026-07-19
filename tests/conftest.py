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

# [Round 3 fix] conftest 也 force repo validation rebind — 防
# test_more_coverage 把 sys.modules['validation'] 覆盖后,
# test_coverage_gaps 后续 import 继承 live validation 造成类 identity 不一致
import importlib.util as _ilu
_LIVE_ROOT = '/Users/apple/.hermes/memory'
if _LIVE_ROOT in sys.path:
    sys.path.remove(_LIVE_ROOT)


def _force_repo_validation():
    """[Round 3 fix] 强制把 sys.modules['validation'] 绑回 repo 版本, 同时
    rebind 'memory' module 的 ValidationError attr 指向新 class."""
    spec = _ilu.spec_from_file_location('validation', _REPO_ROOT / 'validation.py')
    mod = _ilu.module_from_spec(spec)
    sys.modules['validation'] = mod
    spec.loader.exec_module(mod)
    # 关键: rebind memory module 的 ValidationError 引用 (它 'from validation import')
    if 'memory' in sys.modules:
        sys.modules['memory'].ValidationError = mod.ValidationError
    return mod


_force_repo_validation()


import pytest  # noqa: E402


def pytest_collection_finish(session):
    """[Round 3 fix] collection 完后强制 rebind 每个 test 模块的 ValidationError attr.

    Why: pytest collection 时 test file 顶部 'from validation import ValidationError'
    会捕获 class reference. 如果彼时 sys.modules['validation'] 是 LIVE, 那 test 后续用的
    ValidationError 永远指 LIVE (function captures are fine, but class identity matters).
    这里 rebind test module namespace 的 ValidationError attr 到 repo.
    """
    repo_validation = sys.modules.get('validation')
    if not repo_validation:
        return
    repo_ve = repo_validation.ValidationError
    for name, mod in list(sys.modules.items()):
        if not name.startswith('tests.test_'):
            continue
        if hasattr(mod, 'ValidationError') and mod.ValidationError is not repo_ve:
            mod.ValidationError = repo_ve


@pytest.fixture(autouse=True)
def _rebind_test_validation_error(request):
    """[Round 4 fix] before each test, rebind test module's ValidationError attr to
    current sys.modules['validation'].ValidationError + rebind ValidationError
    on sys.modules['validation'].__dict__ itself.

    Why: pytest_collection_finish rebinds ONCE at collection end. But test body may
    do `from validation import validate_id` which captures CURRENT validate_id. If
    the validate_id function's __globals__ is OLD validation module (from earlier
    _load_from_repo), its __dict__['ValidationError'] is also OLD, even though test
    module's ValidationError attr is the new one. So pytest.raises fails.

    Fix: also mutate the OLD validation module's __dict__['ValidationError'] to repo_ve.
    Find it by walking sys.modules' validation module objects + functions with
    __globals__['__name__'] == 'validation'.
    """
    repo_validation = sys.modules.get('validation')
    if not repo_validation:
        yield
        return
    repo_ve = repo_validation.ValidationError
    # Rebind test module's ValidationError attr
    test_mod = sys.modules.get(request.module.__name__)
    if test_mod is not None and hasattr(test_mod, 'ValidationError'):
        test_mod.ValidationError = repo_ve
    # Rebind sys.modules['validation'].__dict__['ValidationError']
    repo_validation.ValidationError = repo_ve
    # Also find and rebind any OTHER module dicts held by function __globals__
    # (e.g., OLD memory module dicts whose functions raise ValidationError)
    # Use gc to find ALL function objects, even those held only by class methods.
    seen_dicts = set()
    import gc as _gc
    for obj in _gc.get_objects():
        try:
            if not (callable(obj) and hasattr(obj, '__globals__')):
                continue
            globs = obj.__globals__
            # Only process actual dicts (not descriptors)
            if not isinstance(globs, dict):
                continue
            mod_name = globs.get('__name__', '')
            if (mod_name in ('validation', 'memory')
                    and id(globs) not in seen_dicts):
                seen_dicts.add(id(globs))
                if globs.get('ValidationError') is not repo_ve:
                    globs['ValidationError'] = repo_ve
        except Exception:
            continue
    yield


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
            "DELETE FROM entities WHERE id LIKE 'test_%' OR id LIKE 'covgap_%' "
            "OR source LIKE '%test%'"
        )
        mem._conn.execute(
            "DELETE FROM relations WHERE source_id LIKE 'test_%' "
            "OR target_id LIKE 'test_%' OR source_id LIKE 'covgap_%' "
            "OR target_id LIKE 'covgap_%' OR source LIKE '%test%'"
        )
        mem._conn.commit()
    finally:
        mem.close()
    yield