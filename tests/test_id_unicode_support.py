"""
test_id_unicode_support.py — 扩 _ID_RE 接受中文 + 路径 / + 空格.

[8/16 实战] 主人清理 137 个 test fixture 时, 4 个 entity 撞 _ID_RE 限制:
- 主人 (中文)
- user 2026-07-01 explicit "停掉 omlx" (含空格+引号)
- comfyanonymous/ComfyUI (含 /)
- ltdrdata/ComfyUI-Inspire-Pack (含 /)

[修复目标] _ID_RE 接受 unicode (CJK + 拉丁扩展) + / + 空格,
[仍拒绝] 反斜杠 \\, 单引号 ', 双引号 ", 分号 ;, 反引号 `, NUL, 控制字符, bidi override
(SQL/shell injection 攻击面)
"""

import pytest

import validation as validation_mod


class TestIdUnicodeSupport:
    """_ID_RE 扩到支持中文 + 路径 / + 空格 (主人 8/16 实战 patch)."""

    def test_chinese_id_passes(self):
        """主人: 中文 id 应该通过."""
        result = validation_mod.validate_id("主人")
        assert result == "主人"

    def test_chinese_long_id_passes(self):
        """中文长 id 走 unicode 范围."""
        result = validation_mod.validate_id("主人_2077_Ling")
        assert result == "主人_2077_Ling"

    def test_id_with_space_passes(self):
        """含空格的 id (m5 task fixture 用的 'user 2026-07-01...') 通过."""
        result = validation_mod.validate_id("user 2026-07-01 explicit")
        assert result == "user 2026-07-01 explicit"

    def test_id_with_slash_passes(self):
        """含路径分隔符 / 的 id (ComfyUI repo 引用) 通过."""
        result = validation_mod.validate_id("comfyanonymous/ComfyUI")
        assert result == "comfyanonymous/ComfyUI"

    def test_id_with_quotes_rejected(self):
        """双引号 \" 仍拒 (SQL injection / log 泄露)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id('id "with quote"')

    def test_id_with_single_quote_rejected(self):
        """单引号 ' 仍拒 (SQL injection)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id("id'with quote")

    def test_id_with_backslash_rejected(self):
        """反斜杠 \\ 仍拒 (path traversal)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id("id\\with\\backslash")

    def test_id_with_semicolon_rejected(self):
        """分号 ; 仍拒 (SQL injection)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id("id;with;semicolon")

    def test_id_with_backtick_rejected(self):
        """反引号 ` 仍拒 (shell injection)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id("id`with`backtick")

    def test_id_with_nul_rejected(self):
        """NUL \\x00 仍拒 (C string truncation)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id("id\x00with\x00nul")

    def test_id_with_newline_rejected(self):
        """换行符 \\n 仍拒 (HTTP header injection)."""
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id("id\nwith\nnewline")

    def test_korean_id_passes(self):
        """韩文 id (扩展 unicode 范围覆盖)."""
        result = validation_mod.validate_id("한글_id_테스트")
        assert result == "한글_id_테스트"

    def test_japanese_id_passes(self):
        """日文 id."""
        result = validation_mod.validate_id("テスト_id_ひらがな")
        assert result == "テスト_id_ひらがな"

    def test_mixed_latin_chinese_slash_passes(self):
        """主人实测 entity id 模式: Latin + 中文 + /."""
        result = validation_mod.validate_id("owner/主人_2077_Ling")
        assert result == "owner/主人_2077_Ling"

    def test_too_long_id_rejected(self):
        """长度超 MAX_ID_LEN 仍拒."""
        from validation import MAX_ID_LEN

        long_id = "主" * (MAX_ID_LEN + 1)
        with pytest.raises(validation_mod.ValidationError, match="format mismatch"):
            validation_mod.validate_id(long_id)
