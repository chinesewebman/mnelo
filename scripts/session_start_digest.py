#!/usr/bin/env python3
"""session_start_digest.py — Claude Code SessionStart 钩子注入 mnelo digest.

[TASKS_L2_SESSION_STATE S3] 每次会话开场, 把 mnelo 常驻摘要 (身份 + 近期关键
决策 + 进行中) 注入 Claude Code 上下文, Agent 不用主动 recall 就有"当前最重要的事".

设计 (8/5):
- **容错**: mnelo MCP 未跑 / 超时 / 任何异常 → **静默 exit 0** (不阻断会话启动,
  不刷错误日志)。Claude Code SessionStart 钩子的 stdout 会被注入上下文; 本脚本
  只在成功取到摘要时输出。
- **通用优先**: 走 MneloClient (MCP 标准), 不直接读 DB —— 任何部署形态 (server
  常驻/手动起) 都能用; 客户端默认连 127.0.0.1:8086/mcp (streamable-http), token 从
  MNELO_AUTH_TOKEN 或 ~/.config/mnelo/auth_token 读。
- **输出格式**: `[mnelo-digest] ... [/mnelo-digest]` 包裹, 便于 Agent 识别为
  引用数据而非指令 (DESIGN §12 数据围栏同思想)。

安装 (.claude/settings.json, 见仓库内示例):
  SessionStart 钩子 → `python3 <mnelo_repo>/scripts/session_start_digest.py`
"""

import logging
import os
import sys
from pathlib import Path

# 定位 mnelo 仓库根: 本脚本在 <repo>/scripts/, api/ 在 <repo>/api/
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "api"))


_BOOTSTRAP_MARKER = "MNELO_HOOK_BOOTSTRAPPED"


def _bootstrap_venv() -> bool:
    """若当前解释器缺 mcp 依赖 (如系统 python3), 用仓库 venv 重新执行自身.

    SessionStart 钩子命令常用 `python3` (PATH 解析), 但 mnelo 的 mcp/starlette
    依赖装在 venv 里 — 直接 import mnelo_client 会静默失败。这里自举:
    mcp 不可导入 → exec 仓库 .venv/bin/python 重跑本脚本。

    [audit fix] 防无限 re-exec 用 **env 标记**而非 realpath 比较 — venv python
    常是系统 python 的符号链接 (realpath 相同), realpath 守卫会误判"已在 venv"
    而跳过本该有用的 re-exec (venv 的 sys.path 含 mcp site-packages)。正确做法:
    自举前设 _BOOTSTRAP_MARKER, 子进程见标记即不再自举, 杜绝循环。
    """
    if os.environ.get(_BOOTSTRAP_MARKER):
        return False  # 已自举过一次 → 不循环
    try:
        import mcp  # noqa: F401  # 门禁依赖; mnelo_client 需要它

        return False  # 当前解释器 OK
    except ImportError:
        pass
    for vp in (_REPO_ROOT / ".venv" / "bin" / "python", _REPO_ROOT / "venv" / "bin" / "python"):
        if not vp.exists():
            continue
        env = os.environ.copy()
        env[_BOOTSTRAP_MARKER] = "1"
        os.execve(str(vp), [str(vp), str(Path(__file__).resolve())] + sys.argv[1:], env)
    return False  # 无 venv → 走主流程, 由调用方容错


# 摘要开关: MNELO_MEMORY_DIGEST_ENABLED=false 时静默 (与 config [digest] 一致)
_DIGEST_ENABLED = os.environ.get("MNELO_MEMORY_DIGEST_ENABLED", "true").lower() not in ("false", "0", "no", "off")


def main() -> int:
    if not _DIGEST_ENABLED:
        return 0
    if _bootstrap_venv():
        return 0  # 已 re-exec, 不会到这
    try:
        from mnelo_client import MneloClient, DEFAULT_MCP_URL

        # [8/8] 默认走 streamable-http /mcp (新 transport)。覆盖优先级:
        # MNELO_MEMORY_URL (新) > MNELO_MEMORY_SSE_URL (旧, 测试注入死端口用)。
        # URL 含 /sse 时 client 自动回落 sse transport (向后兼容)。
        url = os.environ.get("MNELO_MEMORY_URL") or os.environ.get("MNELO_MEMORY_SSE_URL") or DEFAULT_MCP_URL

        # 容错路径要真正静默: mnelo_client 在 import 时 setLevel(INFO)+挂 handler,
        # 必须在 import 之后压制 — mnelo 未跑时它会打 ERROR 日志, 对 SessionStart
        # 钩子是噪音 (期望失败场景不该刷错误)。
        logging.getLogger("mnelo_client").setLevel(logging.CRITICAL)
        digest = MneloClient(url=url).get_digest()  # 默认 ref=None → 摘要压缩视图
    except Exception:
        # mnelo 未跑/超时 → 静默退出, 不阻断会话
        return 0

    if not digest or not digest.get("enabled") or not digest.get("content"):
        return 0  # digest 未启用 / 无内容 → 不输出

    content = digest["content"]
    print(f"\n[mnelo-digest]\n{content}\n[/mnelo-digest]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
