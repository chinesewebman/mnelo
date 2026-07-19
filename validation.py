"""
validation.py — input sanitization for memory MCP tool arguments.

[7/19 security audit P0-3 + P1-1 + P1-4]
集中所有 user-supplied input 的清洗 / 限制逻辑. 每个 helper 都 fail-fast
(ValueError with type name only — 不带原始 input, 防止 log 泄露).

边界设计:
- chunk content: max 8 KB ( mnelo 平均 chunk < 500 B; 8 KB 是 backup 块大小)
- query: max 1 KB (平均 50 B; 1 KB 已能容下任何 5+ token 多语种 query)
- id (chunk/entity/relation): ^[a-zA-Z0-9_:.-]{1,256}$ (允许 . _ : -, 禁 / 反斜杠 单引号 双引号 分号 NUL 等)
- entity.name: max 200 chars (OCR 持仓名 + 多语种实体名都够)
- entity.summary: max 1000 chars (足够放 hold reason / position summary)
"""

import re
from typing import Any, Dict

# === Size caps ===
MAX_CHUNK_CONTENT_BYTES = 8 * 1024  # 8 KB
MAX_QUERY_BYTES = 1024  # 1 KB
MAX_ID_LEN = 256
MAX_ENTITY_NAME_LEN = 200
MAX_ENTITY_SUMMARY_LEN = 1000
MAX_HOLDING_FIELD_LEN = 200

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
_ID_RE = re.compile(r"^[a-zA-Z0-9_:\.\-]{1," + str(MAX_ID_LEN) + r"}$")


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
        raise ValidationError(field, f"format mismatch (allowed: [a-zA-Z0-9_:.\\-]{{1,{MAX_ID_LEN}}})")
    return value


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

    return {
        "id": eid,
        "kind": kind,
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
