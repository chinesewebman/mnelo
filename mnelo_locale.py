"""
locale.py — mnelo locale detection + i18n message resolver.

[7/18 patch F]  i18n 框架 — 支持中英双语 (more locales easy to add)
-  locale 检测: env LANG / LC_ALL > config.toml > 'en' (default)
-  fallback 链: zh → en ( zh fallback 到 en)
-  t() 函数: msg_id →  string. 支持 format args.

设计:
1.  message 不在代码里写死 string, 用 msg_id (e.g. 'db.stats.retrieved')
2.  table 在 i18n_messages.py: MESSAGES = {'msg_id': {'zh': '...', 'en': '...'}, ...}
3. t('db.stats.retrieved', current=4390) → 当前 locale string
4. 默认 locale: en (English)
5. fallback: zh → en, en → en (no fallback)
"""

import locale as _syslocale
import os
from typing import Optional

# 默认 locales (可扩展)
SUPPORTED_LOCALES = ["en", "zh", "zh-CN", "zh-TW"]


def get_locale() -> str:
    """: 返回当前 locale code.

    Priority (highest first):
    1. MNELO_MEMORY_LANG env var (e.g. 'zh', 'en-US')
    2. LANG/LC_ALL env (POSIX standard, e.g. 'zh_CN.UTF-8' → 'zh')
    3. config.toml default → 'en'

    Returns:
        'en' | 'zh' | ...
    """
    # 1.  override
    env_lang = os.environ.get("MNELO_MEMORY_LANG")
    if env_lang:
        return _normalize(env_lang)

    # 2.  POSIX
    for var in ["LC_ALL", "LANG"]:
        val = os.environ.get(var)
        if val:
            return _normalize(val)

    # 3.  Python's locale 模块
    try:
        sys_loc = _syslocale.getlocale()[0]
        if sys_loc:
            return _normalize(sys_loc)
    except Exception:
        pass

    # 4. fallback
    return "en"


def _normalize(lang: str) -> str:
    """: zh_CN.UTF-8 → zh, en-US → en, zh-TW → zh-TW ()."""
    if not lang:
        return "en"
    lang = lang.strip().replace("-", "_")
    # zh_CN.UTF-8 → zh_CN
    parts = lang.split("_")
    primary = parts[0].lower()
    # 简化: zh_CN/zh_TW 都归 'zh' (主用户 zh)
    if primary == "zh":
        return "zh"
    return primary


# 当前 locale (实践 lazy, 可 reload)
_current_locale: Optional[str] = None
_current_locale_cache: Optional[str] = None


def current_locale() -> str:
    """: 返回缓存的当前 locale ( use 之后 reload())."""
    global _current_locale, _current_locale_cache
    if _current_locale is None:
        _current_locale = get_locale()
        _current_locale_cache = _current_locale
    return _current_locale


def reload() -> None:
    """: 重新检测 locale ( MNELO_MEMORY_LANG 实测 changed 后 reload)."""
    global _current_locale, _current_locale_cache
    _current_locale = get_locale()
    _current_locale_cache = _current_locale


def t(msg_id: str, **kwargs) -> str:
    """:  message resolver.

    Args:
        msg_id: 唯一 id (e.g. 'db.stats.retrieved')
        **kwargs: format args (e.g. count=4390)

    Returns:
        当前 locale 的 string.  fallback: msg_id 未找到 → 'en' 表; 都没有 → msg_id 本身.
    """
    from i18n_messages import MESSAGES

    loc = current_locale()
    table = MESSAGES.get(msg_id, {})
    # 当前 locale, fallback 到 'en'
    text = table.get(loc) or table.get("en") or msg_id
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
