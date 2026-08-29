"""
[8/6 E 路线] Tests for validation.scan_pii_warnings — advisory PII scanner.

Stance under test: returns list of hits, never blocks, never rewrites.
"""

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


# Force the in-tree validation.py (same pattern as conftest).
_load("validation")
v = sys.modules["validation"]


# --- happy paths: each category should hit on realistic input ---


def test_luhn_valid_visa_hits_credit_card():
    # Generated Luhn-valid Visa test number (well-known public test PAN)
    s = "Card: 4111 1111 1111 1111 issued 2020"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "credit_card" in cats


def test_random_16_digits_no_luhn_does_not_hit_credit_card():
    # 16 digits but Luhn-invalid (no real card number)
    s = "Order id 1234567890123456 placed"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "credit_card" not in cats


def test_email_hits():
    s = "Contact alice@example.com for details"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "email" in cats
    hit = next(h for h in hits if h["category"] == "email")
    assert hit["match"].startswith("alice")
    assert hit["offset"] > 0  # found somewhere in s


def test_cn_mobile_hits():
    s = "Call me at 13800138000 after 6pm"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "cn_mobile" in cats


def test_cn_id_card_hits_valid_shape():
    # 18-digit, plausible 1990-01-01 birth date — checksum not enforced by design.
    s = "ID: 110101199001011234 noted"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "cn_id_card" in cats


def test_secret_token_openai_prefix_hits():
    s = "Use key sk-abcdefghijklmnopqrstuv for the test"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "secret_token" in cats


def test_secret_token_jwt_hits():
    s = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    hits = v.scan_pii_warnings(s)
    cats = [h["category"] for h in hits]
    assert "secret_token" in cats


# --- stance: never blocks, never rewrites ---


def test_empty_content_returns_empty_list():
    assert v.scan_pii_warnings("") == []


def test_no_pii_returns_empty_list():
    s = "I went hiking yesterday and saw a hawk on the ridge."
    hits = v.scan_pii_warnings(s)
    assert hits == []


def test_luhn_helper_known_valid_and_invalid():
    # Standard published Visa test PAN passes Luhn
    assert v._luhn_ok("4111111111111111") is True
    # "1234567890123456" fails Luhn
    assert v._luhn_ok("1234567890123456") is False
    # Too short
    assert v._luhn_ok("411111111111") is False
    # Too long
    assert v._luhn_ok("4" * 20) is False


def test_dedup_same_category_offset():
    # If the same chunk of text matches only one category, we get one hit, not duplicates.
    s = "alice@example.com alice@example.com"  # same email twice
    hits = v.scan_pii_warnings(s)
    email_hits = [h for h in hits if h["category"] == "email"]
    assert len(email_hits) == 2  # different offsets — both legitimate
    assert email_hits[0]["offset"] != email_hits[1]["offset"]


def test_hit_dict_shape():
    s = "Email alice@example.com"
    hits = v.scan_pii_warnings(s)
    assert hits
    h = hits[0]
    assert set(h.keys()) == {"category", "match", "offset", "length"}
    assert isinstance(h["offset"], int)
    assert isinstance(h["length"], int)
    assert h["length"] > 0
