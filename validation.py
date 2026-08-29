"""
validation.py — input sanitization for memory MCP tool arguments.

[7/19 security audit P0-3 + P1-1 + P1-4]
集中所有 user-supplied input 的清洗 / 限制逻辑. 每个 helper 都 fail-fast
(ValueError with type name only — 不带原始 input, 防止 log 泄露).

边界设计:
- chunk content: max 8 KB ( mnelo 平均 chunk < 500 B; 8 KB 是 backup 块大小)
- query: max 1 KB (平均 50 B; 1 KB 已能容下任何 5+ token 多语种 query)
- id (chunk/entity/relation): 见 `_ID_RE` (validation.py:_ID_RE 定义) + `_ID_ALLOWED_DESC`
  + `_ID_REJECTED_DESC` (单 source of truth, 改 docstring 后保持同步)
- entity.name: max 200 chars (OCR 持仓名 + 多语种实体名都够)
- entity.summary: max 1000 chars (足够放 hold reason / position summary)
"""

import re
from typing import Any, Dict, List

# [8/9 P1-yanru] Size caps — 值从 config 读, 旧常量保留作 alias 兼容测试 import.
# 默认值等于原硬编码值, 行为不变.
from config import config

MAX_CHUNK_CONTENT_BYTES = config.validation_max_chunk_content_bytes
MAX_QUERY_BYTES = config.validation_max_query_bytes
MAX_ID_LEN = config.validation_max_id_len
MAX_ENTITY_NAME_LEN = config.validation_max_entity_name_len
MAX_ENTITY_SUMMARY_LEN = config.validation_max_entity_summary_len
MAX_HOLDING_FIELD_LEN = config.validation_max_holding_field_len

# === Character classes to strip ===
# 控制字符 (< 0x20) 除 \n \t \r 外全部拒
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")

# Trojan Source bidi override (CVE-2021-42574) + zero-width chars
# LRE/RLE/PDF/LRO/RLO + LRI/RLI/FSI/PDI isolates + LRM/RLM + ZWJ/ZWNJ/ZWS
_BIDI_ZERO_WIDTH = "".join(
    [
        "\u202a",  # LRE
        "\u202b",  # RLE
        "\u202c",  # PDF
        "\u202d",  # LRO
        "\u202e",  # RLO
        "\u2066",  # LRI
        "\u2067",  # RLI
        "\u2068",  # FSI
        "\u2069",  # PDI
        "\u200e",  # LRM
        "\u200f",  # RLM
        "\u200b",  # ZWS
        "\u200c",  # ZWNJ
        "\u200d",  # ZWJ
        "\ufeff",  # BOM / ZWNBSP
    ]
)
_BIDI_ZW_RE = re.compile(f"[{re.escape(_BIDI_ZERO_WIDTH)}]")

# ID whitelist: 字母/数字/_/:/./- (覆盖 chunk_id, entity_id, relation id 全场景)
# [8/16 patch] 扩到支持 unicode (中日韩) + / + 空格 — 主人清理 137 个 test fixture 时,
# 4 entity 撞限制 (主人 / user 2026-07-01... / comfyanonymous/ComfyUI / ltdrdata/...).
# 仍拒: 反斜杠 \\ 单引号 ' 双引号 " 分号 ; 反引号 ` NUL \n \r \t (SQL/shell injection + HTTP injection).
# [8/29 PR-D] update docstring + expose _ID_ALLOWED_DESC / _ID_REJECTED_DESC 给 error msg 单 source of truth
_ID_RE = re.compile(r"^[\w \u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af/.\-:]{1," + str(MAX_ID_LEN) + r"}$")
# [8/29 PR-D] 单 source of truth: allowed + rejected 字符描述, 避免 8/16 patch 后错误信息
# 误导 (曾 hard-code `[a-zA-Z0-9_:.\\-]`). pattern 改时这两个常量同步 update.
_ID_ALLOWED_DESC = f"word chars (a-z A-Z 0-9 _) + Unicode 4 ranges (中文 \\u4e00-\\u9fff, 日 \\u3040-\\u30ff, 韩 \\uac00-\\ud7af) + space + / + . + : + -, max {MAX_ID_LEN} chars"
_ID_REJECTED_DESC = (
    r"backslash \ single-quote ' double-quote \" semicolon ; backquote ` "
    r"NUL \0 newline \n carriage-return \r tab \t"
)


class ValidationError(ValueError):
    """Raised when user input fails sanitization.

    [P1-3] message 只含 type name + 简短 reason, 不带原始 input (防 log 泄露).
    """

    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _strip_unsafe_chars(s: str, *, allow_newlines: bool = True) -> str:
    """剥离控制字符 + bidi override + zero-width.

    保留: \\n \\t \\r (可配置); 普通 printable + 中文/日文/韩文/阿拉伯文等.
    """
    if not allow_newlines:
        s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    s = _CONTROL_CHARS_RE.sub("", s)
    s = _BIDI_ZW_RE.sub("", s)
    return s


def _check_size(s: str, max_bytes: int, field: str) -> None:
    raw_len = len(s.encode("utf-8"))
    if raw_len > max_bytes:
        raise ValidationError(field, f"exceeds {max_bytes} bytes (got {raw_len})")


def validate_chunk_content(content: str) -> str:
    """[P0-3] 清洗 + 大小限制 + 控制字符剥离.

    Returns sanitized content (原文为空时返 None 抛错).
    """
    if not isinstance(content, str):
        raise ValidationError("content", "must be str")
    _check_size(content, MAX_CHUNK_CONTENT_BYTES, "content")
    cleaned = _strip_unsafe_chars(content, allow_newlines=True)
    if not cleaned.strip():
        raise ValidationError("content", "empty after sanitization")
    return cleaned


def validate_query(query: str) -> str:
    """[P1-4] recall query 验证 (跟 content 类似, 但不允许换行)."""
    if not isinstance(query, str):
        raise ValidationError("query", "must be str")
    _check_size(query, MAX_QUERY_BYTES, "query")
    cleaned = _strip_unsafe_chars(query, allow_newlines=False)
    if not cleaned.strip():
        raise ValidationError("query", "empty after sanitization")
    return cleaned


def validate_id(value: Any, field: str = "id") -> str:
    """[P1-1] chunk/entity/relation/start_node/target_id/old_id 等所有 id 字段.

    Accepts str (chunk_id, entity_id) OR int (relation_id from `Memory.relate()`).
    Numeric IDs are coerced to str so downstream SQL/JSON serialization stays uniform.
    """
    if isinstance(value, bool):
        # bool is subclass of int — reject to avoid silent True/False → 'True'/'False' IDs
        raise ValidationError(field, "must be str or int")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ValidationError(field, "must be str or int")
    if not _ID_RE.match(value):
        # [8/29 PR-D] 错误信息跟 _ID_RE pattern 同步, 不再 hard-code 过期的
        # `[a-zA-Z0-9_:.\\-]` — 用户 hit 实际拒字符时拿到 actionable 信息.
        # Constants `_ID_ALLOWED_DESC` / `_ID_REJECTED_DESC` (lines 70-78) are
        # the single source of truth — if _ID_RE changes, update both desc
        # constants in lockstep to avoid re-introducing a stale error msg.
        raise ValidationError(
            field,
            f"format mismatch (allowed: {_ID_ALLOWED_DESC}; rejected: {_ID_REJECTED_DESC})",
        )
    return value


# [P0 §3.0] 记忆类型谱系 — 单一事实源 (memory.py 的 norm_memory_type 复用)
MEMORY_TYPES = frozenset({"fact", "preference", "episode", "decision", "procedure", "ephemeral"})


def validate_entity_payload(ent: Dict) -> Dict:
    """[P1-2 + P1-5] entity dict 字段清洗 (id/kind/name/summary/aliases/properties).

    Returns sanitized dict (新对象, 不修改原 ent).
    """
    if not isinstance(ent, dict):
        raise ValidationError("entity", "must be dict")
    eid = validate_id(ent.get("id", ""), "entity.id")

    kind = ent.get("kind", "")
    if not isinstance(kind, str) or not kind:
        raise ValidationError("entity.kind", "must be non-empty str")
    kind = _strip_unsafe_chars(kind, allow_newlines=False)
    if len(kind) > 64:
        raise ValidationError("entity.kind", "exceeds 64 chars")

    name = ent.get("name")
    if name is not None:
        name = _strip_unsafe_chars(str(name), allow_newlines=True)
        if len(name) > MAX_ENTITY_NAME_LEN:
            raise ValidationError("entity.name", f"exceeds {MAX_ENTITY_NAME_LEN} chars")
    summary = ent.get("summary")
    if summary is not None:
        summary = _strip_unsafe_chars(str(summary), allow_newlines=True)
        if len(summary) > MAX_ENTITY_SUMMARY_LEN:
            raise ValidationError("entity.summary", f"exceeds {MAX_ENTITY_SUMMARY_LEN} chars")

    importance_raw = ent.get("importance")
    if importance_raw is None:
        importance = 0.5  # 默认值 — 跟 _upsert_entity 老行为一致
    elif isinstance(importance_raw, bool) or not isinstance(importance_raw, (int, float)):
        raise ValidationError("entity.importance", f"must be numeric, got {type(importance_raw).__name__}")
    elif importance_raw != importance_raw:  # NaN
        raise ValidationError("entity.importance", "must not be NaN")
    else:
        importance = max(0.0, min(1.0, float(importance_raw)))

    # [P0 §3.0] memory_type — 校验 + 默认 'fact'
    memory_type = ent.get("memory_type")
    if memory_type is None:
        memory_type = "fact"
    else:
        memory_type = str(memory_type).strip().lower()
        if memory_type not in MEMORY_TYPES:
            raise ValidationError("entity.memory_type", f"unknown memory_type {memory_type!r}")

    return {
        "id": eid,
        "kind": kind,
        "memory_type": memory_type,
        "name": name,
        "summary": summary,
        "aliases": ent.get("aliases"),
        "properties": ent.get("properties"),
        "source": ent.get("source"),
        "importance": importance,  # 总是有效 float, 不再 None
    }


def validate_holding_payload(h: Dict) -> Dict:
    """[P1-5] import_holdings.py 的 holding dict 字段清洗.

     holdings JSON shape: {symbol_code, name, quantity, cost_price, ...}
    严控 free-form text 字段 (name, direction, notes), 避免恶意 JSON 注入.
    """
    if not isinstance(h, dict):
        raise ValidationError("holding", "must be dict")

    out = {}
    # 数字字段: clamp + reject NaN/inf
    for k in ("quantity", "cost_price", "current_price", "market_value", "weight"):
        if k in h and h[k] is not None:
            try:
                v = float(h[k])
                if v != v or v == float("inf") or v == float("-inf"):  # NaN / inf
                    raise ValueError
                out[k] = v
            except (TypeError, ValueError):
                raise ValidationError(f"holding.{k}", "must be finite number")

    # 字符串字段: 剥离控制 + 长度限制
    for k in ("symbol_code", "name", "direction", "notes"):
        if k in h and h[k] is not None:
            v = _strip_unsafe_chars(str(h[k]), allow_newlines=(k == "notes"))
            if len(v) > MAX_HOLDING_FIELD_LEN:
                raise ValidationError(f"holding.{k}", f"exceeds {MAX_HOLDING_FIELD_LEN} chars")
            out[k] = v

    return out


# === [8/6 E 路线] PII scanner — advisory only, never blocks or rewrites ===
#
# Stance: mnelo 不读内容、不加密、不主动 block; 命中只写 audit_log + /health 计数.
# 5 类高置信 PII + 1 类 secret 风格 token; 命中返回 list[dict], 不动 content.
# 误报可接受: a hit means "look at this", not "this is bad".

_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CN_MOBILE_RE = re.compile(r"\b1[3-9]\d{9}\b")
_CN_ID_RE = re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")
_SECRET_PREFIX_RE = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"ghp_[A-Za-z0-9]{20,}|"
    r"gho_[A-Za-z0-9]{20,}|"
    r"xox[abposr]-[A-Za-z0-9-]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIzaSy[A-Za-z0-9_-]{30,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")\b"
)


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum. Returns True on mod-10 valid 13–19 digit string."""
    s = [int(c) for c in digits if c.isdigit()]
    if len(s) < 13 or len(s) > 19:
        return False
    checksum = 0
    parity = (len(s) - 2) % 2
    for i, d in enumerate(s[:-1]):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    checksum += s[-1]
    return checksum % 10 == 0


def _hit(category: str, span_text: str, start: int, end: int) -> Dict:
    return {
        "category": category,
        "match": span_text if len(span_text) <= 64 else span_text[:32] + "…",
        "offset": start,
        "length": end - start,
    }


def scan_pii_warnings(content: str) -> List[Dict]:
    """[8/6 E 路线] Surface possible-PII categories. Advisory; never blocks.

    Categories: credit_card (Luhn-valid 13–19 digits), email (RFC-lite),
    cn_mobile (11-digit Chinese mobile), cn_id_card (GB 11643 shape),
    secret_token (known-prefix API key / JWT / PAT).

    Returns list of {category, match, offset, length} dicts. Empty input
    returns empty list. Multiple hits in one string yield multiple dicts.
    Caller decides whether to log, redact, or ignore.
    """
    if not content:
        return []
    hits: List[Dict] = []
    seen: set = set()  # dedup by (category, offset)

    for m in _CC_RE.finditer(content):
        digits = m.group()
        if _luhn_ok(digits):
            key = ("credit_card", m.start())
            if key not in seen:
                hits.append(_hit("credit_card", digits, m.start(), m.end()))
                seen.add(key)

    for m in _EMAIL_RE.finditer(content):
        key = ("email", m.start())
        if key not in seen:
            hits.append(_hit("email", m.group(), m.start(), m.end()))
            seen.add(key)

    for m in _CN_MOBILE_RE.finditer(content):
        key = ("cn_mobile", m.start())
        if key not in seen:
            hits.append(_hit("cn_mobile", m.group(), m.start(), m.end()))
            seen.add(key)

    for m in _CN_ID_RE.finditer(content):
        key = ("cn_id_card", m.start())
        if key not in seen:
            hits.append(_hit("cn_id_card", m.group(), m.start(), m.end()))
            seen.add(key)

    for m in _SECRET_PREFIX_RE.finditer(content):
        key = ("secret_token", m.start())
        if key not in seen:
            hits.append(_hit("secret_token", m.group(), m.start(), m.end()))
            seen.add(key)

    return hits
