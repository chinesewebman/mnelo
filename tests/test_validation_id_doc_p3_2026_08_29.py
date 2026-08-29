"""[bug fix 8/29 P3] validate_id() error message listed stale ASCII-only whitelist
([a-zA-Z0-9_:.\\-]) but the actual _ID_RE was expanded in 8/16 to allow unicode +
space + / (see validation.py:62-65 comment). User reads the msg and wastes time
debugging valid IDs.

Verify post-fix error message lists the actual allowed characters."""
import pytest

from validation import validate_id, ValidationError, MAX_ID_LEN


def test_validate_id_error_msg_lists_actual_allowed_chars():
    """Post-fix: error msg must mention CJK + space + / since regex allows them."""
    # Force the failure by passing a character the regex rejects (e.g. '+')
    # but which is not in the docstring's old ASCII whitelist either.
    bad_id = "id+plus"
    with pytest.raises(ValidationError) as exc_info:
        validate_id(bad_id)
    msg = str(exc_info.value)
    # The new msg lists: alnum, _, :, ., -, space, CJK, /
    for needed in ["CJK", "space", "/", "ASCII alnum"]:
        assert needed in msg, f"error msg missing required token {needed!r}: {msg!r}"
    # Must NOT contain the stale ASCII-only pattern that misled users.
    assert "[a-zA-Z0-9_:." not in msg, f"stale ASCII whitelist still in msg: {msg!r}"


def test_validate_id_accepts_unicode_and_slash():
    """Sanity: post-fix regex actually accepts unicode + / + space (regression guard)."""
    for ok in ["user/name", "我是实体", "with space", "汉字/english", "a:b.c-d_e"]:
        assert validate_id(ok) == ok, f"valid id rejected: {ok!r}"


def test_validate_id_rejects_plus_with_descriptive_msg():
    """Post-fix: '+' is correctly rejected AND the message tells user what is allowed."""
    with pytest.raises(ValidationError) as exc_info:
        validate_id("id+plus")
    msg = str(exc_info.value)
    # '+' is not in the allowed whitelist (it's a reserved shell/URL char).
    assert "format mismatch" in msg
    # The message must reference MAX_ID_LEN — important for users debugging near-limit IDs.
    assert str(MAX_ID_LEN) in msg
