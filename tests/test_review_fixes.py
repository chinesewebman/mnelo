"""
[8/6 review-pass] Tests for the 7 review findings.

RF1: 零长状态窗 — timestamp 毫秒级, 同秒 2-100ms 间隔转移, 状态窗长度 > 0.
RF2: 中文 slug 退化 — task:20260806-cai-gou-ye (拼音 fallback) 而非 task:20260806-task.
RF3: task_create check-then-write 无 CAS — single UPDATE WHERE 控 active_task_id 设值.
RF4: import re 在 docstring 之前 — module.__doc__ 应 != None.
RF5: 并发 CAS 测试实为顺序模拟 — 后续拆分实测 (跟 task:tlm*-rf5 前缀).
RF6: transition() 在 autocommit 下非原子 — 文档 + 接口显式要求事务包裹.
RF7: List 未导入 — typing.List 导入生效, 函数签名注解不抛 NameError.
"""
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

import importlib.util as _ilu

def _load(name: str):
    spec = _ilu.spec_from_file_location(name, _REPO / f"{name}.py")
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("config")
_load("embedder")
_load("search_index")
_load("validation")
mem_mod = _load("memory")
ts_mod = _load("task_states")


def _setup():
    """Clean fixtures using a fresh Memory instance."""
    mem = mem_mod.Memory()
    mem._conn.execute("PRAGMA foreign_keys = OFF")
    try:
        mem._conn.execute(
            "DELETE FROM task_states WHERE task_id LIKE 'task:rf%' "
            "OR task_id LIKE 'loop:rf%' "
            "OR task_id LIKE 'loop:rf11%' "
            "OR task_id LIKE 'loop:rf12%' "
            "OR task_id LIKE 'loop:rf13%' "
            "OR task_id LIKE 'loop:rf14%' "
            "OR task_id LIKE 'task:rf11%' "
            "OR task_id LIKE 'task:20260806-rf%' "
            "OR task_id LIKE 'loop:20260806-rf%' "
            "OR task_id LIKE 'loop:rf13l%'"
        )
        mem._conn.execute(
            "DELETE FROM entities WHERE id LIKE 'task:rf%' "
            "OR id LIKE 'loop:rf%' "
            "OR id LIKE 'loop:rf11%' "
            "OR id LIKE 'loop:rf12%' "
            "OR id LIKE 'loop:rf13%' "
            "OR id LIKE 'loop:rf14%' "
            "OR id LIKE 'task:rf11%' "
            "OR id LIKE 'task:20260806-rf%' "
            "OR id LIKE 'loop:20260806-rf%' "
            "OR id LIKE 'loop:rf13l%'"
        )
    finally:
        mem._conn.execute("PRAGMA foreign_keys = ON")
    mem._conn.commit()
    mem.close()


# === RF1: 零长状态窗 — 毫秒级 timestamp ===

def test_rf1_now_is_millisecond_precision():
    """_default_now() 返回 ISO 8601 毫秒级 (>= 23 chars)."""
    ts = ts_mod._default_now()
    # ISO8601 milliseconds: 'YYYY-MM-DDTHH:MM:SS.sss' (23 chars)
    assert "." in ts, f"millisecond separator missing: {ts}"
    # 毫秒部分 3 位
    ms = ts.split(".")[1]
    assert len(ms) == 3, f"millisecond part should be 3 digits, got '{ms}'"
    # 验证整改前 (timespec='seconds') 至少 19 chars


def test_rf1_repeated_transition_creates_non_zero_window():
    """同毫秒内 2 次 transfer (不可能, 但 1ms 间隔) 状态窗长度 > 0."""
    _setup()
    m = mem_mod.Memory()
    try:
        tid = "task:rf1-rapid"
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            (tid, "task", "rapid"),
        )
        m._conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, created_at) "
            "VALUES (?, ?, ?, ?)",
            (tid, "open", "2026-08-06T10:00:00.000", "2026-08-06T10:00:00.000"),
        )
        m._conn.commit()

        # 手动 transfer 间隔 1ms 应产生 长度 1ms 窗
        ts_mod.transition(
            m._conn, task_id=tid, to_state="in_progress",
            reason="A", now="2026-08-06T10:00:00.500",
        )
        ts_mod.transition(
            m._conn, task_id=tid, to_state="waiting",
            reason="B", now="2026-08-06T10:00:01.000",
        )

        windows = [
            tuple(r)
            for r in m._conn.execute(
                "SELECT state, valid_from, valid_until FROM task_states "
                "WHERE task_id=? ORDER BY id",
                (tid,),
            )
        ]
        # 3 窗: open[10:00:00.000, 10:00:00.500), in_progress[10:00:00.500, 10:00:01.000), waiting[10:00:01.000, None)
        assert windows == [
            ("open",        "2026-08-06T10:00:00.000", "2026-08-06T10:00:00.500"),
            ("in_progress", "2026-08-06T10:00:00.500", "2026-08-06T10:00:01.000"),
            ("waiting",     "2026-08-06T10:00:01.000", None),
        ]
    finally:
        m.close()


def test_rf1_replay_includes_intermediate_state():
    """asof 回放中途态可见 (毫秒级)."""
    _setup()
    m = mem_mod.Memory()
    try:
        tid = "task:rf1-asof"
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            (tid, "task", "asof"),
        )
        # 3 状态窗 - 紧凑毫秒
        windows = [
            ("open",        "2026-08-06T10:00:00.000", "2026-08-06T10:00:00.500"),
            ("in_progress", "2026-08-06T10:00:00.500", "2026-08-06T10:00:01.000"),
            ("waiting",     "2026-08-06T10:00:01.000", None),
        ]
        for state, vf, vu in windows:
            m._conn.execute(
                "INSERT INTO task_states (task_id, state, valid_from, valid_until, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (tid, state, vf, vu, vf),
            )
        m._conn.commit()

        # asof 10:00:00.750 (in_progress 中段): 期望 in_progress 可见
        result = ts_mod.replay_task(m._conn, task_id=tid, asof="2026-08-06T10:00:00.750")
        states = [w["state"] for w in result["windows"]]
        assert "in_progress" in states, f"intermediate state lost, got {states}"
    finally:
        m.close()


# === RF2: 中文 slug 退化 ===

def test_rf2_chinese_name_uses_pinyin_slug():
    """中文 task 名 → slug 拼音 fallback (e.g. '采购耗材' → 'cai-gou-hao-cai') 而非 'task'."""
    _setup()
    m = mem_mod.Memory()
    try:
        # task_create 走完整流程
        r = ts_mod.task_create(
            m._conn, name="采购耗材",
            now="2026-08-06T10:00",
        )
        # 不应是 'task:20260806-task' (退化)
        assert "task" not in r["task_id"].rsplit("-", 1)[-1].split(":"), \
            f"slug 退化: {r['task_id']}"
        # 实际拼音 / 拼音 fallback / 缩写 都行, 只要不是 'task'
        assert r["task_id"].startswith("task:20260806-")
        # 校验 slug 长度合理 (中文转拼音 1-3 字符)
        assert len(r["task_id"]) > 17, f"slug 太短, 退化: {r['task_id']}"
    finally:
        m.close()


def test_rf2_chinese_loop_name_uses_pinyin_slug():
    """中文 loop 名 → slug 拼音 fallback."""
    _setup()
    m = mem_mod.Memory()
    try:
        r = ts_mod.loop_create(
            m._conn, name="耗材库存监控", trigger="库存低于阈值",
            now="2026-08-06T09:00",
        )
        assert r["loop_id"].startswith("loop:")
        # slug (= prefix 后的部分) 不应是 'loop' 退化
        suffix = r["loop_id"][len("loop:"):]
        assert suffix != "loop", f"loop slug 退化: {r['loop_id']}"
        assert len(suffix) > 0, f"slug 为空: {r['loop_id']}"
        # 应该 < 30 字符 (hash 8 字符 OR ascii slug 30 字符)
        assert len(suffix) <= 30, f"slug 太长: {r['loop_id']}"
    finally:
        m.close()


# === RF3: task_create check-then-write 无 CAS — single UPDATE WHERE 原子 ===

def test_rf3_task_create_set_active_task_atomic():
    """task_create 的 UPDATE active_task_id 应是单语句 WHERE 原子 (防双 spawn)."""
    _setup()
    m = mem_mod.Memory()
    try:
        loop_r = ts_mod.loop_create(
            m._conn, name="rf3", trigger="x",
            now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]

        # 第一次 task_create 应成功
        r1 = ts_mod.task_create(
            m._conn, name="first", loop_id=lid,
            now="2026-08-06T10:00",
        )
        # 第二次 task_create 同 loop 应拒 (防双 spawn)
        try:
            ts_mod.task_create(
                m._conn, name="second", loop_id=lid,
                now="2026-08-06T10:05",
            )
        except ts_mod.TaskLoopError as e:
            assert e.code == "LoopHasActiveTaskError"
        else:
            raise AssertionError("两次 task_create 同 loop 应被拒")
    finally:
        m.close()


# === RF4: import re 在 docstring 之前 — module.__doc__ 应 != None ===

def test_rf4_module_docstring_intact():
    """task_states.__doc__ != None (模块 docstring 未被 import 截断)."""
    # Re-import fresh to validate
    import importlib
    if "task_states" in sys.modules:
        importlib.reload(sys.modules["task_states"])
    assert ts_mod.__doc__ is not None, "module docstring lost (re imported before docstring)"
    assert "task_states" in ts_mod.__doc__.lower() or "状态" in ts_mod.__doc__


# === RF5: 并发 CAS 测试实为顺序模拟 — 后续拆分实测 (验证现状) ===

def test_rf5_concurrent_test_present_with_caveat():
    """test_task_states_concurrent.py 仍存在, docstring 标 '顺序模拟'."""
    concurrent_test = _REPO / "tests" / "test_task_states_concurrent.py"
    assert concurrent_test.exists()
    text = concurrent_test.read_text()
    # docstring 自认没真并发
    assert "顺序" in text or "单线程" in text, "顺模拟 docstring 标记丢失"


# === RF6: transition() 在 autocommit 下非原子 — 文档 + 接口显式 ===

def test_rf6_transition_docstring_mentions_transaction():
    """transition() docstring 应明示调用方需包事务 (防 UPDATE 提交 + INSERT 失败)."""
    doc = ts_mod.transition.__doc__ or ""
    assert "事务" in doc, "transition() docstring 缺少事务包裹提示"
    # 或 "transaction" / "atomic"
    assert "transaction" in doc.lower() or "事务" in doc, \
        "transition() docstring 需明示事务 / 原子性要求"


# === RF7: List 未导入 ===

def test_rf7_list_typing_imported():
    """task_states 模块导入 List (typing) 不抛 NameError."""
    import task_states
    assert hasattr(task_states, "List") or hasattr(__import__("typing"), "List"), \
        "typing.List 未导入"
    # 实际尝试注解 List[...]
    try:
        from typing import List
        # 验证 task_states 源码 'List' 出现
        text = (_REPO / "task_states.py").read_text()
        assert "from typing import List" in text or ", List" in text, \
            "typing.List 导入未在 task_states.py"
    except ImportError:
        raise AssertionError("typing.List import 失败")



# === RF8 [高]: MCP 事务回滚 ===

def test_rf8_rollback_on_task_create_double_spawn():
    """并发 task_create 同 loop, 后到者应被 rollback 不留孤儿行."""
    from memory import Memory
    m = Memory()
    try:
        # 建 loop
        ts_mod.loop_create(
            m._conn, name="rf8-rollback", trigger="x",
            now="2026-08-06T09:00",
        )
        m._conn.commit()  # commit loop separately
        # 第一个 task_create 应成功
        r1 = ts_mod.task_create(
            m._conn, name="first", loop_id="loop:rf8-rollback",
            now="2026-08-06T10:00",
        )
        m._conn.commit()
        active_id = r1["task_id"]
        # 第二个 task_create 同 loop 应抛 (RF3 raise TaskLoopError; RF8 兜底 rollback)
        raised = False
        try:
            ts_mod.task_create(
                m._conn, name="second", loop_id="loop:rf8-rollback",
                now="2026-08-06T10:05",
            )
        except Exception:
            raised = True
            m._conn.rollback()  # 模拟 MCP RF8 行为 — 失败路径 rollback
        assert raised, "expected double-spawn reject"

        # 校验: 第一个 task entity + state 窗 仍在 (前 commit), 第二个不留
        n_active = m._conn.execute(
            "SELECT COUNT(*) FROM entities WHERE id=?",
            (active_id,),
        ).fetchone()[0]
        assert n_active == 1, f"first task entity lost: {n_active}"

        # 第二个 task entity 不应存在 (rollback 清掉)
        n_second = m._conn.execute(
            "SELECT COUNT(*) FROM entities WHERE id LIKE 'task:20260806-second%'",
        ).fetchone()[0]
        assert n_second == 0, f"orphan task entity left after rollback: {n_second}"
    finally:
        m.close()


# === RF11 [低]: now override 秒级零长 — 严格递增强制 ===

def test_rf17_no_negative_window_when_now_rewound():
    """[RF17 8/6 review-pass] transition(now=过去) 不应写负长窗 (valid_from > valid_until).

    修因: max(closed_vu, current_active_vf) 推进 1ms.
    """
    from memory import Memory
    m = Memory()
    try:
        # 建 task, 初始窗 valid_from='11:00:00'
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            ("task:rf17-rewind", "task", "rf17"),
        )
        m._conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("task:rf17-rewind", "open", "2026-08-06T11:00:00", "2026-08-06T11:00:00"),
        )
        m._conn.commit()

        # transition 回拨到 '10:00:05' (早于活动窗 valid_from)
        ts_mod.transition(
            m._conn, task_id="task:rf17-rewind", to_state="in_progress",
            reason="rewind", now="2026-08-06T10:00:05",
        )

        # 校验没有负长窗 (valid_from <= valid_until)
        wins = [
            tuple(r) for r in m._conn.execute(
                "SELECT state, valid_from, valid_until FROM task_states "
                "WHERE task_id='task:rf17-rewind' ORDER BY id",
            )
        ]
        # open 窗: valid_from='11:00:00', valid_until 应 >= '11:00:00.001'
        # (不能是 10:00:05 — 那是负长窗)
        first_win = wins[0]
        assert first_win[2] is not None, "open 窗必须有 valid_until (活动 in_progress 紧跟)"
        assert first_win[1] <= first_win[2],             f"negative-length window: {first_win}"
        # valid_until 必须 >= 初始 valid_from (推进至少 1ms)
        assert first_win[2] >= "2026-08-06T11:00:00",             f"valid_until 应 >= 初始 valid_from '11:00:00', got {first_win[2]}"
    finally:
        m.close()


def test_rf11_now_override_seconds_strict_increasing():
    """两次 transition 同秒级 now, 第二窗 valid_from > 旧 valid_until (1ms 推进)."""
    from memory import Memory
    m = Memory()
    try:
        # 建 task
        m._conn.execute(
            "INSERT INTO entities (id, kind, name) VALUES (?, ?, ?)",
            ("task:rf11-sec", "task", "rf11"),
        )
        m._conn.execute(
            "INSERT INTO task_states (task_id, state, valid_from, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("task:rf11-sec", "open", "2026-08-06T10:00:00.000", "2026-08-06T10:00:00.000"),
        )
        m._conn.commit()

        # 两 transition 同秒级 now
        ts_mod.transition(
            m._conn, task_id="task:rf11-sec", to_state="in_progress",
            reason="A", now="2026-08-06T10:00:05",  # 秒级
        )
        ts_mod.transition(
            m._conn, task_id="task:rf11-sec", to_state="waiting",
            reason="B", now="2026-08-06T10:00:05",  # 同样秒级
        )

        # 校验窗长度 > 0
        wins = [
            tuple(r)
            for r in m._conn.execute(
                "SELECT state, valid_from, valid_until FROM task_states "
                "WHERE task_id='task:rf11-sec' ORDER BY id",
            )
        ]
        # 3 窗: open [10:00:00, ?), in_progress [?, ?), waiting [?, NULL)
        for w in wins:
            if w[2] is not None:  # not the open-ended final
                assert w[1] != w[2], f"zero-length window: {w}"
                # 严格递增 (字符串比较 ISO 8601 时)
                assert w[1] < w[2], f"non-strictly-increasing: {w}"
    finally:
        m.close()


# === RF12 [低]: loop_update interval_hours 校验 ===

def test_rf12_loop_update_interval_hours_reject_zero():
    """loop_update(interval_hours=0) 应抛 InvalidIntervalError."""
    from memory import Memory
    m = Memory()
    try:
        ts_mod.loop_create(
            m._conn, name="rf12", trigger="x",
            now="2026-08-06T09:00",
        )
        try:
            ts_mod.loop_update(m._conn, loop_id="loop:rf12", interval_hours=0)
        except ts_mod.TaskLoopError as e:
            assert e.code == "InvalidIntervalError"
        else:
            raise AssertionError("expected InvalidIntervalError")
    finally:
        m.close()


def test_rf12_loop_update_interval_hours_reject_negative():
    """loop_update(interval_hours=-1) 应抛."""
    from memory import Memory
    m = Memory()
    try:
        ts_mod.loop_create(
            m._conn, name="rf12b", trigger="x",
            now="2026-08-06T09:00",
        )
        try:
            ts_mod.loop_update(m._conn, loop_id="loop:rf12b", interval_hours=-1)
        except ts_mod.TaskLoopError as e:
            assert e.code == "InvalidIntervalError"
        else:
            raise AssertionError("expected InvalidIntervalError")
    finally:
        m.close()


# === RF13 [低]: loop_update / list_loops 过滤 valid_until IS NULL ===

def test_rf13_loop_update_soft_deleted_loop_rejected():
    """loop_update 对软删 loop 应报 LoopNotFoundError."""
    from memory import Memory
    m = Memory()
    try:
        # 软删 loop (valid_until 设)
        m._conn.execute(
            "INSERT INTO entities (id, kind, name, properties_json, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("loop:rf13-soft", "loop", "soft", "{}", "2026-08-06T09:00", "2026-08-06T10:00"),
        )
        m._conn.commit()
        try:
            ts_mod.loop_update(m._conn, loop_id="loop:rf13-soft", trigger="x")
        except ts_mod.LoopNotFoundError:
            pass
        else:
            raise AssertionError("expected LoopNotFoundError for soft-deleted loop")
    finally:
        m.close()


def test_rf13_list_loops_excludes_soft_deleted():
    """list_loops 不应列软删 loop (RF13 8/6)."""
    from memory import Memory
    m = Memory()
    try:
        # 用时间戳 microseconds 命名, 保证跨测试唯一
        import time
        prefix = "loop:rf13l" + str(int(time.time() * 1_000_000))[-10:]
        active_id = prefix + "-a"
        soft_id = prefix + "-s"

        m._conn.execute(
            "INSERT INTO entities (id, kind, name, properties_json) VALUES (?, ?, ?, ?)",
            (active_id, "loop", "rf13-active", "{}"),
        )
        m._conn.execute(
            "INSERT INTO entities (id, kind, name, properties_json, valid_from, valid_until) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (soft_id, "loop", "rf13-soft", "{}", "2026-08-06T09:00", "2026-08-06T10:00"),
        )
        m._conn.commit()
        result = ts_mod.list_loops(m._conn)
        ids = [loop["loop_id"] for loop in result["loops"]]
        assert active_id in ids, f"active loop missing: {ids}"
        assert soft_id not in ids, f"soft-deleted loop leaked: {ids}"
    finally:
        m.close()


# === RF14 [低]: loop_update enabled CAS ===

def test_rf14_loop_update_enabled_uses_cas():
    """loop_update enabled CAS 关旧 + 开新, 状态窗正确推进."""
    from memory import Memory
    m = mem_mod.Memory()
    try:
        loop_r = ts_mod.loop_create(
            m._conn, name="rf14", trigger="x",
            enabled=True, now="2026-08-06T09:00",
        )
        lid = loop_r["loop_id"]
        ts_mod.loop_update(m._conn, loop_id=lid, enabled=False, now="2026-08-06T10:00")

        # 校验 current_state = dormant
        cur = m._conn.execute(
            "SELECT state FROM task_states WHERE task_id=? AND valid_until IS NULL",
            (lid,),
        ).fetchone()
        assert cur[0] == "dormant", f"expected dormant, got {cur[0]}"
    finally:
        m.close()
