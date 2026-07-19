"""Round 14 — i18n_messages key-level coverage.

Tests that every key in MESSAGES is accessed via t() for both zh and en,
catching accidental key removals, missing translations, and format regressions.

Approach: pytest-cov counts dict literal as 1 statement, so 100% line coverage
is already easy. The real coverage signal is "every key is reachable from code".
This test exercises every key for both locales.
"""
import os
import sys
from pathlib import Path

import pytest

# Load REPO modules (REPO source-of-truth)
sys.path.insert(0, '/Users/apple/projects/mnelo')


@pytest.fixture
def fresh_locale(monkeypatch):
    """Reload locale with cleared env."""
    for var in ('MNELO_MEMORY_LANG', 'LC_ALL', 'LANG'):
        monkeypatch.delenv(var, raising=False)
    import mnelo_locale
    # Reset cached locale without full reload (preserve coverage line numbers)
    mnelo_locale._current_locale = None
    return mnelo_locale


class TestAllKeysCoverage:
    """Every key in MESSAGES must be reachable + have zh + en."""

    def test_every_key_has_zh_and_en(self):
        """Every key has both 'zh' and 'en' translations."""
        from i18n_messages import MESSAGES
        for key, table in MESSAGES.items():
            assert 'zh' in table, f"key '{key}' missing 'zh' translation"
            assert 'en' in table, f"key '{key}' missing 'en' translation"
            # No empty strings
            assert table['zh'].strip(), f"key '{key}' has empty 'zh'"
            assert table['en'].strip(), f"key '{key}' has empty 'en'"

    def test_every_key_resolvable_via_t_zh(self, fresh_locale, monkeypatch):
        """Every key resolves to non-empty zh string when locale=zh."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'zh')
        from i18n_messages import MESSAGES
        for key in MESSAGES:
            result = fresh_locale.t(key)
            assert isinstance(result, str)
            assert result.strip(), f"key '{key}' resolved to empty string"
            # Should NOT fall back to msg_id (means missing translation)
            assert result != key, f"key '{key}' fell back to msg_id (missing translation)"

    def test_every_key_resolvable_via_t_en(self, fresh_locale, monkeypatch):
        """Every key resolves to non-empty en string when locale=en."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        from i18n_messages import MESSAGES
        for key in MESSAGES:
            result = fresh_locale.t(key)
            assert isinstance(result, str)
            assert result.strip(), f"key '{key}' resolved to empty string"
            assert result != key, f"key '{key}' fell back to msg_id (missing translation)"

    def test_every_key_count_matches(self):
        """Total key count is documented (33 keys)."""
        from i18n_messages import MESSAGES
        # If you add/remove keys, update this assertion
        assert len(MESSAGES) >= 33, f"keys dropped below 33: {len(MESSAGES)}"


class TestFormatArgsForKeys:
    """Keys with {placeholders} accept all referenced kwargs."""

    def test_startup_config_loaded_with_kwargs(self, fresh_locale, monkeypatch):
        """startup.config_loaded has tz + warm placeholders."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t('startup.config_loaded', tz='UTC', warm=True)
        assert 'UTC' in result
        assert 'True' in result

    def test_db_exists_with_path(self, fresh_locale, monkeypatch):
        """db.exists has {path} placeholder."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'zh')
        result = fresh_locale.t('db.exists', path='/tmp/foo.db')
        assert '/tmp/foo.db' in result

    def test_db_stats_with_all_args(self, fresh_locale, monkeypatch):
        """check.db_stats has many placeholders (e_a/e_t/c_a/c_t/r_a/r_t/v)."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t(
            'check.db_stats', e_a=100, e_t=200, c_a=300, c_t=400,
            r_a=500, r_t=600, v=700,
        )
        assert '100' in result and '700' in result

    def test_recall_24h_with_all_args(self, fresh_locale, monkeypatch):
        """check.recall_24h has count/empty/pct/p50/p95/avg."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t(
            'check.recall_24h', count=100, empty=5, pct=5.0,
            p50=12.5, p95=36.2, avg=33.0,
        )
        assert '100' in result

    def test_kind_top_with_kinds(self, fresh_locale, monkeypatch):
        """check.kind_top has {kinds}."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t('check.kind_top', kinds='concept=4155, stock=388')
        assert 'concept=4155' in result

    def test_recall_ok_with_args(self, fresh_locale, monkeypatch):
        """recall.ok has query/n/ms."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t('recall.ok', query='test', n=5, ms=12.5)
        assert 'test' in result
        assert '5' in result

    def test_error_out_of_range_with_args(self, fresh_locale, monkeypatch):
        """error.out_of_range has name/lo/hi/value."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t(
            'error.out_of_range', name='importance', lo=0, hi=1, value=1.5,
        )
        assert 'importance' in result

    def test_error_retry_failed_with_args(self, fresh_locale, monkeypatch):
        """error.retry_failed has n/err."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'en')
        result = fresh_locale.t('error.retry_failed', n=3, err='timeout')
        assert '3' in result
        assert 'timeout' in result


class TestFallbackBehavior:
    """Locale miss → 'en' → msg_id fallback chain."""

    def test_unknown_msg_id_returns_msg_id(self, fresh_locale):
        """Unknown msg_id → returns the msg_id itself."""
        result = fresh_locale.t('totally_unknown_msg_id_xyz')
        assert result == 'totally_unknown_msg_id_xyz'

    def test_invalid_locale_falls_back_to_en(self, fresh_locale, monkeypatch):
        """Set locale to 'ja' (unsupported) → fallback to en."""
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'ja')
        result = fresh_locale.t('startup.banner')
        # Should fall back to en
        assert 'startup' in result.lower() or '━━━' in result

    def test_missing_zh_falls_back_to_en(self, fresh_locale, monkeypatch):
        """If 'zh' is missing for a key → fallback to en."""
        from i18n_messages import MESSAGES
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'zh')
        monkeypatch.setitem(MESSAGES, 'fake_msg_only_en', {
            # 'zh' intentionally missing
            'en': 'only english here',
        })
        try:
            result = fresh_locale.t('fake_msg_only_en')
            assert result == 'only english here', f"expected en fallback, got: {result}"
        finally:
            MESSAGES.pop('fake_msg_only_en', None)

    def test_missing_both_falls_back_to_msg_id(self, fresh_locale, monkeypatch):
        """If both 'zh' and 'en' are missing → return msg_id itself."""
        from i18n_messages import MESSAGES
        monkeypatch.setenv('MNELO_MEMORY_LANG', 'zh')
        monkeypatch.setitem(MESSAGES, 'fake_msg_no_translations', {
            # Both 'zh' and 'en' intentionally missing
            'fr': 'juste français',
        })
        try:
            result = fresh_locale.t('fake_msg_no_translations')
            assert result == 'fake_msg_no_translations', f"expected msg_id fallback, got: {result}"
        finally:
            MESSAGES.pop('fake_msg_no_translations', None)


class TestKeyDomains:
    """Keys grouped by domain — sanity check structure."""

    def test_startup_keys_present(self):
        from i18n_messages import MESSAGES
        startup_keys = [k for k in MESSAGES if k.startswith('startup.')]
        assert len(startup_keys) >= 5, f"startup. keys: {startup_keys}"

    def test_db_keys_present(self):
        from i18n_messages import MESSAGES
        db_keys = [k for k in MESSAGES if k.startswith('db.')]
        assert len(db_keys) >= 5, f"db. keys: {db_keys}"

    def test_check_keys_present(self):
        from i18n_messages import MESSAGES
        check_keys = [k for k in MESSAGES if k.startswith('check.')]
        assert len(check_keys) >= 8, f"check. keys: {check_keys}"

    def test_recall_keys_present(self):
        from i18n_messages import MESSAGES
        recall_keys = [k for k in MESSAGES if k.startswith('recall.')]
        assert len(recall_keys) >= 4, f"recall. keys: {recall_keys}"

    def test_error_keys_present(self):
        from i18n_messages import MESSAGES
        error_keys = [k for k in MESSAGES if k.startswith('error.')]
        assert len(error_keys) >= 3, f"error. keys: {error_keys}"