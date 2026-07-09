"""tests/test_text_utils.py — Tests for app/utils/text_utils.py.

Covers clean_email_body: HTML stripping, whitespace normalisation, and
email disclaimer removal.

Called by: pytest
Depends on: app.utils.text_utils
"""

import os

os.environ["TESTING"] = "1"


from app.utils.text_utils import clean_email_body


class TestCleanEmailBodyEdgeCases:
    def test_empty_string_returns_empty(self):
        assert clean_email_body("") == ""

    def test_none_returns_empty(self):
        assert clean_email_body(None) == ""

    def test_plain_text_unchanged(self):
        result = clean_email_body("Hello world")
        assert result == "Hello world"

    def test_html_tags_stripped(self):
        result = clean_email_body("<p>Hello <b>world</b></p>")
        assert "<" not in result
        assert "Hello" in result
        assert "world" in result

    def test_br_becomes_newline(self):
        result = clean_email_body("line1<br>line2")
        assert "\n" in result

    def test_br_self_closing_becomes_newline(self):
        result = clean_email_body("line1<br/>line2")
        assert "\n" in result

    def test_closing_p_becomes_newline(self):
        result = clean_email_body("<p>para1</p><p>para2</p>")
        assert "\n" in result

    def test_closing_tr_becomes_newline(self):
        result = clean_email_body("row1</tr>row2")
        assert "\n" in result

    def test_closing_li_becomes_newline(self):
        result = clean_email_body("item1</li>item2")
        assert "\n" in result

    def test_multiple_blank_lines_collapsed(self):
        result = clean_email_body("a\n\n\n\nb")
        assert "\n\n\n" not in result
        assert "a" in result
        assert "b" in result

    def test_leading_trailing_whitespace_stripped(self):
        result = clean_email_body("  hello  ")
        assert result == "hello"

    def test_inline_whitespace_collapsed(self):
        result = clean_email_body("hello   world")
        assert "  " not in result
        assert "hello" in result
        assert "world" in result

    def test_disclaimer_removed(self):
        body = "Quote info here\n\nThis email and any attachments are confidential and may be privileged.\n\nRegards"
        result = clean_email_body(body)
        assert "This email" not in result
        assert "Quote info here" in result

    def test_confidentiality_notice_removed(self):
        body = "Here is the quote\n\nConfidentiality Notice: This message is for the named person only.\n\nThank you"
        result = clean_email_body(body)
        assert "Confidentiality Notice" not in result
        assert "Here is the quote" in result

    def test_disclaimer_keyword_removed(self):
        body = "Content here\n\nDisclaimer: This information is for guidance only.\n\nEnd"
        result = clean_email_body(body)
        assert "Disclaimer" not in result

    def test_tabs_collapsed_to_space(self):
        result = clean_email_body("col1\tcol2")
        assert "\t" not in result

    def test_complex_html_table(self):
        html = "<table><tr><td>Item</td><td>Qty</td></tr><tr><td>ABC</td><td>100</td></tr></table>"
        result = clean_email_body(html)
        assert "<" not in result
        assert "Item" in result or "ABC" in result
