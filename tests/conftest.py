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
import warnings

# [G7] mcp SDK v1.26 still uses deprecated `content_item.content` access on
# TextResourceContents (field is `text`); str-returning read_resource is the
# only non-crashing path until upstream is fixed. Register at conftest
# module-import time AND inside every pytest_runtest_setup so the filter
# survives pytest's per-test warnings.filters reset.
warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="Returning str or bytes from read_resource is deprecated.*",
)


def pytest_runtest_setup(item):
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message="Returning str or bytes from read_resource is deprecated.*",
    )


# [7/19 patch] 强制从 repo 本地代码 import (与 tests/test_edge_cases.py 同策略)
import importlib.util as _ilu
_REPO_ROOT = Path(__file__).resolve().parent.parent
# [8/6 fix] conftest 缺 sys.path.insert 让 test 文件能 import repo 模块 (search_index, memory, etc).
# 主人 65c5723 改了硬编码 mac 路径, 但没补 sys.path. pytest 启动时 cwd 不一定是 repo root.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

def _load_from_repo(mod_name: str):
    spec = _ilu.spec_from_file_location(mod_name, _REPO_ROOT / f'{mod_name}.py')  # type: ignore[arg-type]
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

_load_from_repo('config')
_load_from_repo('embedder')
_load_from_repo('search_index')
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


# [8/6 plan §9] 让 from helpers import cleanup_chunks 可用
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import pytest  # noqa: E402


# [8/29 PR-A Bundle 1] iso_now / iso_now_offset SQL functions 注册 helper.
# 背景: schema.sql 用 `DEFAULT (iso_now())` / `DEFAULT (iso_now_offset(N))`,
# 但 Memory.__init__ (memory_core.py:180-181) 才在 self._conn 上注册这两个函数.
# 24 个 fail test (m29/m33/m34/m35/m36_*) 直接 sqlite3.connect(memory.DB_PATH)
# 绕过 Memory(), conn 上没 iso_now → 任何 INSERT 触发 `unknown function: iso_now()`.
# 修法: conftest 加 register_iso_now(conn) helper + session-scope autouse fixture
# monkey-patch sqlite3.connect 让 raw connect 自动注册. pytest 跑完 patch 撤销, 不污染 prod.
# [8/29 PR-A subagent review 修 P1-2] 用 memory.now("local") / now() 替代 datetime.now(),
# 跟 memory_core.py:171-172/201-202 一致, 避免 MNELO_MEMORY_TIMEZONE 覆盖时 8h tz skew.
def register_iso_now(conn) -> None:
    """注册 iso_now / iso_now_offset SQL functions 到给定 sqlite3 conn.

    Last-wins overwrite: 重复调用 sqlite3.create_function 会覆盖之前 impl
    (不是 no-op, last-wins). 跟 memory_core.py:171-181 / 200-209 注册逻辑保持
    一致 (memory.now + timedelta). 跨 timezone 安全: 跟随 prod memory.now() 解析.
    """
    from datetime import datetime, timedelta
    from memory import now as _now

    def _iso_now_local() -> str:
        return _now("local")

    def _iso_now_offset(days: int) -> str:
        base = datetime.fromisoformat(_now("local"))
        return (base + timedelta(days=days)).isoformat(timespec="seconds")

    conn.create_function("iso_now", 0, _iso_now_local)
    conn.create_function("iso_now_offset", 1, _iso_now_offset)


@pytest.fixture(scope='session', autouse=True)
def _patch_sqlite3_connect_for_iso_now():
    """[8/29 PR-A Bundle 1] session 内 monkey-patch sqlite3.connect 让 raw connect
    自动注册 iso_now. pytest session 跑完自动撤销 (session-scope fixture teardown).

    Why autouse: 24 个 fail test 各自手动 sqlite3.connect(str(memory.DB_PATH)),
    改每个 call site ~30 处量大易漏; autouse fixture session 内一次 patch 兜底.

    Risk: 范围限定 session 内, pytest fixture teardown 自动 unpatch sqlite3.connect.
    不会影响 live mnelo server / live DB / 任何 prod 代码 path.
    """
    import sqlite3 as _sqlite3
    _orig_connect = _sqlite3.connect

    def _patched_connect(*args, **kwargs):
        conn = _orig_connect(*args, **kwargs)
        # 默认 auto-register; last-wins overwrite
        try:
            register_iso_now(conn)
        except _sqlite3.Error as exc:
            # 注册失败给 warning, 避免下游 unknown function 错误信息淹没根因
            import warnings
            warnings.warn(f"register_iso_now failed: {exc}", RuntimeWarning, stacklevel=2)
        return conn

    _sqlite3.connect = _patched_connect
    try:
        yield
    finally:
        _sqlite3.connect = _orig_connect


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

    [8/9 issue-#3] zvec LOCK 抢不到时 (mnelo 服务在跑) 静默 skip — 该 fixture
    主要是清理 test 残留, LIVE 服务自带维护, 不抢 LOCK 也不影响后续 test (依赖 zvec
    的 test 自己会被 mnelo 服务挡). 纯文本 / schema test 不受任何影响.
    """
    from memory import Memory
    try:
        mem = Memory()
    except Exception as e:
        # [8/9] zvec lock 冲突 / sqlite busy / mnelo 服务占用 → 静默 skip
        # 留 warning 给开发者, 不让整个 test session 死.
        import warnings as _w
        _w.warn(
            f"[conftest] _clean_test_data_session skip: Memory() init 失败 "
            f"({type(e).__name__}: {e}). 跑纯文本/schema test 不影响; "
            f"依赖 zvec 的 test 会被后续 Memory() 错误自然 fail.",
            RuntimeWarning,
            stacklevel=2,
        )
        yield
        return
    try:
        # [8/9 P1 follow-up] 先清 task_states 防 FK 约束 (task_states.evidence_chunk_id
        # → chunks.id). 之前顺序 chunks → entities → relations 在 e2e test 留下
        # task_states → chunk 时, DELETE chunks 抛 IntegrityError.
        mem._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'task:%' "
            "OR task_id LIKE 'loop:%'"
        )
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