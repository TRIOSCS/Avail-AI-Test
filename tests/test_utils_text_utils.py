"""tests/test_utils_text_utils.py — Tests for app/utils/text_utils.py."""

import os

os.environ["TESTING"] = "1"

from app.utils.text_utils import clean_email_body


class TestCleanEmailBody:
    def test_empty_string_returns_empty(self):
        assert clean_email_body("") == ""

    def test_none_returns_empty(self):
        assert clean_email_body(None) == ""

    def test_plain_text_unchanged(self):
        result = clean_email_body("Hello World")
        assert result == "Hello World"

    def test_strips_html_tags(self):
        result = clean_email_body("<b>Hello</b> <i>World</i>")
        assert "<b>" not in result
        assert "<i>" not in result
        assert "Hello" in result
        assert "World" in result

    def test_br_tag_becomes_newline(self):
        result = clean_email_body("Line1<br>Line2")
        assert "\n" in result
        assert "Line1" in result
        assert "Line2" in result

    def test_br_self_closing_becomes_newline(self):
        result = clean_email_body("Line1<br/>Line2")
        assert "\n" in result

    def test_p_close_tag_becomes_newline(self):
        result = clean_email_body("<p>Para1</p><p>Para2</p>")
        assert "\n" in result
        assert "Para1" in result
        assert "Para2" in result

    def test_disclaimer_stripped(self):
        body = "Important message.\n\nThis email and any attachments are confidential and may be privileged.\n\nEnd."
        result = clean_email_body(body)
        assert "Important message" in result
        assert "confidential" not in result.lower() or "End" in result

    def test_confidentiality_notice_stripped(self):
        body = "Real content.\n\nConfidentiality notice: This email is private.\n\nThanks"
        result = clean_email_body(body)
        assert "Real content" in result

    def test_disclaimer_keyword_stripped(self):
        body = "Order details here.\n\nDISCLAIMER: The contents of this email are private.\n\nRegards"
        result = clean_email_body(body)
        assert "Order details" in result

    def test_excessive_blank_lines_collapsed(self):
        result = clean_email_body("Line1\n\n\n\n\nLine2")
        assert "\n\n\n" not in result
        assert "Line1" in result
        assert "Line2" in result

    def test_tr_close_tag_becomes_newline(self):
        # </tr> becomes newline; <tr>, <td>, </td> collapse to spaces
        result = clean_email_body("Row1</tr>Row2</tr>")
        assert "\n" in result
        assert "Row1" in result
        assert "Row2" in result

    def test_li_close_tag_becomes_newline(self):
        result = clean_email_body("<li>Item1</li><li>Item2</li>")
        assert "\n" in result
        assert "Item1" in result

    def test_strips_script_tags(self):
        result = clean_email_body("<script>alert('xss')</script>Hello")
        assert "<script>" not in result
        assert "Hello" in result

    def test_whitespace_normalized(self):
        result = clean_email_body("Hello   World")
        assert "Hello" in result
        assert "World" in result
        assert "  " not in result

    def test_result_stripped(self):
        result = clean_email_body("  Hello World  ")
        assert not result.startswith(" ")
        assert not result.endswith(" ")
