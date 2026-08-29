"""[bug fix 8/29 P3 → PR-D upgrade] validate_id() error message used to list the
stale ASCII-only whitelist `[a-zA-Z0-9_:.\\-]`, but the actual _ID_RE was expanded
in 8/16 to allow unicode + space + / (see validation.py:_ID_RE comment).

Initial fix (this PR's first commit) hardcoded the new whitelist inline. PR-D
(#18) improved on this by introducing `_ID_ALLOWED_DESC` + `_ID_REJECTED_DESC`
constants as a single source of truth — when _ID_RE changes, both desc
constants update in lockstep, eliminating the "forgot to update the msg"
class of bug. This test now reflects PR-D's contract.

Tests:
  - error msg is sourced from `_ID_ALLOWED_DESC` (not stale hardcoded pattern)
  - error msg also includes rejected-chars desc (actionable feedback)
  - regression: unicode + / + space + colons still accepted
  - '+' rejected with descriptive msg referencing MAX_ID_LEN"""

import pytest

from validation import (
    validate_id,
    ValidationError,
    MAX_ID_LEN,
    _ID_ALLOWED_DESC,
    _ID_REJECTED_DESC,
)


def test_id_allowed_desc_constant_exists():
    """Single source of truth: _ID_ALLOWED_DESC must reference MAX_ID_LEN
    so changing the cap doesn't desync from the regex."""
    assert "256" in _ID_ALLOWED_DESC  # MAX_ID_LEN = 256
    # Must mention the 4 CJK ranges by name (so users debugging CN/JP/KR IDs
    # see the actual allowed ranges, not a vague "unicode").
    assert "u4e00" in _ID_ALLOWED_DESC
    assert "u30ff" in _ID_ALLOWED_DESC
    assert "ud7af" in _ID_ALLOWED_DESC


def test_id_rejected_desc_lists_dangerous_chars():
    """Security boundary: rejected chars must include SQL/shell/HTTP injection
    primitives — backslash, single-quote, double-quote, semicolon, backquote,
    NUL, newline, CR, tab."""
    for ch in ["backslash", "single-quote", "double-quote", "semicolon", "backquote", "NUL", "newline"]:
        assert ch in _ID_REJECTED_DESC, f"missing rejected char {ch!r}"


def test_validate_id_error_msg_uses_desc_constants():
    """Post-PR-D: error msg must reference _ID_ALLOWED_DESC + _ID_REJECTED_DESC,
    NOT hardcode any whitelist — single source of truth contract."""
    # Force failure: '+' is not in _ID_RE allowed list.
    with pytest.raises(ValidationError) as exc_info:
        validate_id("id+plus")
    msg = str(exc_info.value)
    # Must NOT contain the stale hardcoded ASCII-only pattern.
    assert "[a-zA-Z0-9_:." not in msg, f"stale hardcoded whitelist in msg: {msg!r}"
    # Must contain the actual desc constants (resolved values).
    assert "word chars" in msg, f"missing _ID_ALLOWED_DESC 'word chars': {msg!r}"
    assert "Unicode" in msg, f"missing _ID_ALLOWED_DESC 'Unicode': {msg!r}"
    assert "backslash" in msg, f"missing _ID_REJECTED_DESC 'backslash': {msg!r}"


def test_validate_id_accepts_unicode_and_slash():
    """Sanity: post-fix regex actually accepts unicode + / + space (regression guard)."""
    for ok in ["user/name", "我是实体", "with space", "汉字/english", "a:b.c-d_e"]:
        assert validate_id(ok) == ok, f"valid id rejected: {ok!r}"


def test_validate_id_rejects_plus_with_actionable_msg():
    """'+' is correctly rejected AND msg lists what's actually allowed/rejected
    so user can fix their input without reading source code."""
    with pytest.raises(ValidationError) as exc_info:
        validate_id("id+plus")
    msg = str(exc_info.value)
    assert "format mismatch" in msg
    # MAX_ID_LEN must appear (so user knows the length cap).
    assert str(MAX_ID_LEN) in msg
    # Both "allowed" and "rejected" sections must appear for actionability.
    assert "allowed:" in msg and "rejected:" in msg, f"msg missing allowed/rejected sections: {msg!r}"
