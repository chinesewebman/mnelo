"""
auth.py — Bearer token auth for SSE transport (P0-2 fix).

[7/19 P0-2] SSE 端口 8086 之前 0 auth, 任何能连 127.0.0.1:8086 的本地进程都能读写 KG.
现在加 Bearer token 校验:

1. Token 来源 (优先级):
   - 环境变量 MNEOLO_AUTH_TOKEN (注入 plist)
   - 文件 ~/.config/mnelo/auth_token (mode 600)
   - CLI 参数 --auth-token-file <path>
   - 都没有 → fail-fast 拒绝启动 SSE transport

2. Token 校验:
   - /sse + /messages/ 路由都需 Authorization: Bearer <token>
   - 用 hmac.compare_digest 防 timing attack
   - 错误返 401 + WWW-Authenticate: Bearer header

3. Token 生成:
   - python -c "import secrets; print(secrets.token_urlsafe(32))"
   - 存 ~/.config/mnelo/auth_token (mode 600)

4. DNS rebinding 防护 (额外):
   - enable_dns_rebinding_protection=True
   - allowed_hosts = ['127.0.0.1:8086', 'localhost:8086']
   - allowed_origins = ['http://127.0.0.1:8086', 'http://localhost:8086']

5. stdio transport 不需要 auth (同进程, MCP SDK 已认证 client)
"""

import hmac
import os
from pathlib import Path
from typing import Optional

# 通用 token env 名 — 避开 HERMES/MCP 避免冲突, 但用 mnelo_ 前缀保持项目自识别
AUTH_TOKEN_ENV = "MNEOLO_AUTH_TOKEN"
AUTH_TOKEN_FILE = Path.home() / ".config" / "mnelo" / "auth_token"


class AuthError(Exception):
    """Token 缺失 / 错误时抛. 不带 token 字面值 (防 log 泄露)."""

    pass


def load_auth_token(explicit_path: Optional[str] = None) -> str:
    """从 env / file / explicit_path 加载 token. 三处都无则 fail-fast.

    Args:
        explicit_path: CLI --auth-token-file 指定路径, 优先级最高
    """
    # 1. explicit CLI 参数
    if explicit_path:
        p = Path(explicit_path)
        if not p.exists():
            raise AuthError(f"--auth-token-file not found: {p}")
        token = p.read_text().strip()
        if not token:
            raise AuthError(f"--auth-token-file is empty: {p}")
        return token

    # 2. 环境变量
    env_token = os.environ.get(AUTH_TOKEN_ENV, "").strip()
    if env_token:
        return env_token

    # 3. 默认文件
    if AUTH_TOKEN_FILE.exists():
        token = AUTH_TOKEN_FILE.read_text().strip()
        if token:
            return token

    raise AuthError(
        f"no auth token configured. Set {AUTH_TOKEN_ENV} env var, "
        f"create {AUTH_TOKEN_FILE} (mode 600), "
        f"or pass --auth-token-file <path>. "
        f'Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )


def verify_bearer(authorization_header: Optional[str], expected_token: str) -> bool:
    """校验 Authorization: Bearer <token> header.

    用 hmac.compare_digest 防 timing attack (不能直接用 == ).
    """
    if not authorization_header:
        return False
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    # 比较时 strip (允许 header value 末尾 whitespace)
    return hmac.compare_digest(parts[1].strip(), expected_token)


def setup_auth_token_file(token: Optional[str] = None) -> Path:
    """生成新 token (或用给定) 写到默认路径, mode 600. 返回路径.

    用于首次部署 / 初始化. 不打印 token 值到 stdout (防 shoulder-surf).
    """
    import secrets

    AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if token is None:
        token = secrets.token_urlsafe(32)
    AUTH_TOKEN_FILE.write_text(token + "\n")
    AUTH_TOKEN_FILE.chmod(0o600)
    return AUTH_TOKEN_FILE
