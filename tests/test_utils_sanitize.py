"""tests/test_utils_sanitize.py — Tests for app/utils/sanitize.py."""

import os

os.environ["TESTING"] = "1"

from app.utils.sanitize import sanitize_dict, sanitize_text


class TestSanitizeText:
    def test_none_returns_none(self):
        assert sanitize_text(None) is None

    def test_plain_text_unchanged_entities(self):
        result = sanitize_text("Hello World")
        assert "Hello World" in result

    def test_html_tags_stripped(self):
        result = sanitize_text("<b>Bold</b>")
        assert "<b>" not in result
        assert "Bold" in result

    def test_script_tag_stripped(self):
        result = sanitize_text("<script>alert('xss')</script>Hello")
        assert "<script>" not in result
        assert "Hello" in result

    def test_javascript_uri_blocked(self):
        result = sanitize_text("Click javascript:alert(1)")
        assert "javascript:" not in result
        assert "_blocked_:" in result

    def test_data_uri_blocked(self):
        result = sanitize_text("data:text/html,<h1>test</h1>")
        assert "data:" not in result

    def test_event_handler_blocked(self):
        result = sanitize_text("text onclick=alert(1)")
        assert "onclick=" not in result
        assert "_blocked_=" in result

    def test_onmouseover_blocked(self):
        result = sanitize_text("hover onmouseover=evil()")
        assert "onmouseover=" not in result

    def test_ampersand_escaped(self):
        result = sanitize_text("AT&T")
        assert "&amp;" in result

    def test_angle_brackets_tag_stripped(self):
        # "a < b > c" is parsed as <b > (tag) so "b" is stripped
        # But the surrounding text is still there
        result = sanitize_text("before <script>bad</script> after")
        assert "before" in result
        assert "after" in result
        assert "<script>" not in result

    def test_non_string_returned_as_is(self):
        assert sanitize_text(42) == 42
        assert sanitize_text(3.14) == 3.14

    def test_empty_string_returns_empty(self):
        result = sanitize_text("")
        assert result == ""

    def test_quotes_escaped(self):
        result = sanitize_text('say "hello"')
        assert "&quot;" in result or '"' not in result


class TestSanitizeDict:
    def test_sanitizes_specified_field(self):
        data = {"name": "<script>alert(1)</script>John", "age": 30}
        result = sanitize_dict(data, ["name"])
        assert "<script>" not in result["name"]
        assert "John" in result["name"]

    def test_leaves_unspecified_fields_alone(self):
        data = {"name": "John", "notes": "<b>Bold</b>"}
        result = sanitize_dict(data, ["name"])
        assert result["notes"] == "<b>Bold</b>"

    def test_skips_missing_fields(self):
        data = {"name": "John"}
        result = sanitize_dict(data, ["name", "missing_field"])
        assert "missing_field" not in result

    def test_skips_non_string_fields(self):
        data = {"name": 123}
        result = sanitize_dict(data, ["name"])
        assert result["name"] == 123

    def test_mutates_and_returns_dict(self):
        data = {"desc": "<script>xss</script>"}
        result = sanitize_dict(data, ["desc"])
        assert result is data  # same object

    def test_empty_fields_list(self):
        data = {"name": "<b>Bold</b>"}
        result = sanitize_dict(data, [])
        assert result["name"] == "<b>Bold</b>"
