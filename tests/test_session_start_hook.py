"""session_start_digest.py 钩子脚本容错测试 (TASKS_L2_SESSION_STATE S3/S6).

覆盖容错保证:
- mnelo MCP 未跑 → 静默 (stdout 空 + exit 0, 不刷错误日志)
- digest 关闭 → 静默
- 自举 venv: 防无限 re-exec (当前已在 venv 时不再 exec 自己)
- 运行中 → stdout 含 [mnelo-digest] 标记 (需 server 在跑, 标记为可选)

用 subprocess 跑脚本 (真实部署形态), 隔离环境。
"""

import builtins
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "session_start_digest.py"


def _run_script(env_extra=None):
    env = os.environ.copy()
    env.pop("MNELO_AUTH_TOKEN", None)  # 不依赖外部 token
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )
    return r


class TestSessionStartHookSilent(unittest.TestCase):
    def test_01_mcp_down_is_silent(self):
        """mnelo MCP 未跑 → stdout 空 + exit 0 + 无错误日志 (容错核心)."""
        # [8/5 fix] 强制 SSE 指向死端口模拟 MCP 不可达 — 避免 live server
        # 在跑时拿到真 digest 把测试弄挂 (主人 8/5 开 inject_on_initialize=true 后
        # live 状态默认有 digest). 容错路径要真静默.
        r = _run_script({"MNELO_MEMORY_SSE_URL": "http://127.0.0.1:1/sse"})
        self.assertEqual(r.returncode, 0, f"exit 应 0, got {r.returncode}")
        self.assertEqual(r.stdout.strip(), "", f"stdout 应空, got: {r.stdout!r}")
        # stderr 不应有 mnelo_client ERROR (容错路径要真静默)
        self.assertNotIn("ERROR", r.stderr, f"stderr 不应有错误日志: {r.stderr!r}")

    def test_02_digest_disabled_is_silent(self):
        """MNELO_MEMORY_DIGEST_ENABLED=false → 静默 (即使 server 在跑)."""
        r = _run_script({"MNELO_MEMORY_DIGEST_ENABLED": "false"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


def _load_module():
    spec = importlib.util.spec_from_file_location("ssd_mod", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBootstrapVenv(unittest.TestCase):
    """_bootstrap_venv 自举逻辑 — env 标记防无限 re-exec."""

    def _load_with_mock(self, fail_mcp, marker_set):
        mod = _load_module()
        calls = []
        mod.os.execve = lambda *a: calls.append(a) or (_ for _ in ()).throw(SystemExit)

        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if fail_mcp and name == "mcp":
                raise ImportError("no mcp")
            return real_import(name, *a, **k)

        orig_import = builtins.__import__
        orig_marker = os.environ.get(mod._BOOTSTRAP_MARKER)
        had_mcp = "mcp" in sys.modules
        sys.modules.pop("mcp", None)  # 强制 `import mcp` 走 __import__ (被 fake 拦截)
        builtins.__import__ = fake_import
        if marker_set:
            os.environ[mod._BOOTSTRAP_MARKER] = "1"
        else:
            os.environ.pop(mod._BOOTSTRAP_MARKER, None)
        # [8/5 fix] mock venv path — 真实环境 (仓库) 不一定有 .venv, 但测试要验
        # "缺 mcp 时找 venv 路径并 exec" 这个逻辑. mock Path.exists 让所有路径"存在".
        real_exists = mod.Path.exists
        mod.Path.exists = lambda self: True
        try:
            result = mod._bootstrap_venv()
        except SystemExit:
            # mock execve 抛 SystemExit 模拟"进程被替换" → 标记为已尝试 exec
            result = "execve_attempted"
        finally:
            builtins.__import__ = orig_import
            mod.Path.exists = real_exists
            if orig_marker is not None:
                os.environ[mod._BOOTSTRAP_MARKER] = orig_marker
            else:
                os.environ.pop(mod._BOOTSTRAP_MARKER, None)
            if had_mcp:
                import mcp  # noqa: F401  # 恢复 (避免影响后续 import)
        return mod, calls, result

    def test_01_marker_set_no_reexec(self):
        """已自举过 (marker 置位) 且 mcp 仍缺失 (损坏 venv) → 不 exec, 防死循环."""
        _, calls, result = self._load_with_mock(fail_mcp=True, marker_set=True)
        self.assertEqual(len(calls), 0, "已自举过不应再 exec (防无限循环)")
        self.assertFalse(result)

    def test_02_mcp_present_no_reexec(self):
        """当前解释器有 mcp → 不 exec (正常路径)."""
        _, calls, result = self._load_with_mock(fail_mcp=False, marker_set=False)
        self.assertEqual(len(calls), 0, "有 mcp 不应 exec")
        self.assertFalse(result)

    def test_03_no_mcp_reexecs_to_venv(self):
        """缺 mcp 且未自举 → exec 一次到仓库 venv."""
        _, calls, result = self._load_with_mock(fail_mcp=True, marker_set=False)
        self.assertEqual(len(calls), 1, "缺 mcp 应 exec 一次到 venv")
        self.assertTrue(Path(calls[0][1][0]).name.startswith("python"), f"exec 目标应为 venv python, got {calls[0][1][0]}")
        self.assertEqual(result, "execve_attempted", "execve 被调用即进程被替换")


if __name__ == "__main__":
    unittest.main()
