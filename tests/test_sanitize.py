"""test_sanitize.py — Tests for input sanitization utilities.

Called by: pytest
Depends on: app.utils.sanitize
"""

import pytest

from app.utils.sanitize import sanitize_dict, sanitize_text


class TestSanitizeText:
    """Tests for sanitize_text()."""

    def test_none_returns_none(self):
        assert sanitize_text(None) is None

    def test_non_string_returned_as_is(self):
        assert sanitize_text(42) == 42
        assert sanitize_text(3.14) == 3.14
        assert sanitize_text(True) is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("hello world", "hello world", id="plain_text"),
            pytest.param("a & b", "a &amp; b", id="ampersand"),
            pytest.param("1 < 2 > 0", "1  0", id="tag_like_angle_brackets_stripped"),
            pytest.param("a < b", "a &lt; b", id="lone_angle_bracket"),
            pytest.param('say "hello"', "say &quot;hello&quot;", id="double_quotes"),
            pytest.param("it's", "it&#x27;s", id="single_quote"),
            pytest.param("", "", id="empty_string"),
            pytest.param("   ", "   ", id="whitespace_only"),
            pytest.param("12345", "12345", id="numeric_string"),
        ],
    )
    def test_text_escaping(self, value, expected):
        assert sanitize_text(value) == expected

    def test_script_tag_stripped(self):
        result = sanitize_text("<script>alert(1)</script>")
        assert "<script>" not in result
        assert "alert(1)" in result

    def test_bold_tag_stripped(self):
        result = sanitize_text("<b>bold</b>")
        assert "<b>" not in result
        assert "bold" in result

    def test_img_tag_with_onerror_stripped(self):
        result = sanitize_text("<img src=x onerror=alert(1)>")
        assert "<img" not in result
        assert "onerror" not in result

    def test_nested_tags_stripped(self):
        result = sanitize_text("<div><span>text</span></div>")
        assert "<div>" not in result
        assert "<span>" not in result
        assert "text" in result

    @pytest.mark.parametrize(
        ("value", "absent"),
        [
            pytest.param("javascript:alert(1)", "javascript:", id="javascript_uri"),
            pytest.param("JavaScript:void(0)", None, id="javascript_uri_case_insensitive"),
            pytest.param("javascript :alert(1)", None, id="javascript_uri_with_spaces"),
            pytest.param("DATA:image/png;base64,abc", None, id="data_uri_case_insensitive"),
        ],
    )
    def test_uri_scheme_neutralized(self, value, absent):
        result = sanitize_text(value)
        if absent is not None:
            assert absent not in result
        assert "_blocked_:" in result

    def test_data_uri_neutralized(self):
        result = sanitize_text("data:text/html,<h1>XSS</h1>")
        assert "data:" not in result.lower() or "_blocked_:" in result

    @pytest.mark.parametrize(
        ("value", "absent"),
        [
            pytest.param("onclick=alert(1)", "onclick=", id="onclick"),
            pytest.param("onmouseover=doStuff()", "onmouseover=", id="onmouseover"),
            pytest.param("onerror=hack()", "onerror=", id="onerror"),
            pytest.param("ONCLICK=bad()", None, id="event_handler_case_insensitive"),
        ],
    )
    def test_event_handler_neutralized(self, value, absent):
        result = sanitize_text(value)
        if absent is not None:
            assert absent not in result
        assert "_blocked_=" in result

    def test_mixed_attack_tag_plus_event_plus_uri(self):
        payload = '<a href="javascript:alert(1)" onclick=steal()>click</a>'
        result = sanitize_text(payload)
        assert "<a" not in result
        assert "javascript:" not in result
        assert "onclick=" not in result


class TestSanitizeDict:
    """Tests for sanitize_dict()."""

    def test_sanitizes_listed_fields(self):
        data = {"name": "<b>Test</b>", "desc": "<script>x</script>"}
        result = sanitize_dict(data, ["name", "desc"])
        assert "<b>" not in result["name"]
        assert "<script>" not in result["desc"]

    def test_ignores_unlisted_fields(self):
        data = {"name": "<b>Test</b>", "other": "<script>x</script>"}
        result = sanitize_dict(data, ["name"])
        assert "<script>x</script>" == result["other"]

    def test_ignores_missing_fields(self):
        data = {"name": "safe"}
        result = sanitize_dict(data, ["name", "nonexistent"])
        assert result == {"name": "safe"}

    def test_ignores_non_string_values(self):
        data = {"count": 42, "active": True, "name": "<b>X</b>"}
        result = sanitize_dict(data, ["count", "active", "name"])
        assert result["count"] == 42
        assert result["active"] is True
        assert "<b>" not in result["name"]

    def test_mutates_in_place(self):
        data = {"name": "<b>Test</b>"}
        result = sanitize_dict(data, ["name"])
        assert result is data

    def test_empty_fields_list(self):
        data = {"name": "<script>x</script>"}
        result = sanitize_dict(data, [])
        assert result["name"] == "<script>x</script>"

    def test_empty_dict(self):
        data = {}
        result = sanitize_dict(data, ["name"])
        assert result == {}

    def test_none_value_in_dict(self):
        data = {"name": None}
        result = sanitize_dict(data, ["name"])
        assert result["name"] is None
