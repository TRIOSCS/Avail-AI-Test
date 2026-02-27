"""Tests for app/utils/sql_helpers.py — escape_like utility.

Covers: percent, underscore, backslash, combined, and clean string.
"""

from app.utils.sql_helpers import escape_like


def test_escape_percent():
    assert escape_like("100%") == r"100\%"


def test_escape_underscore():
    assert escape_like("some_name") == r"some\_name"


def test_escape_backslash():
    assert escape_like(r"path\to") == r"path\\to"


def test_escape_combined():
    assert escape_like(r"100%_test\end") == r"100\%\_test\\end"


def test_clean_string():
    assert escape_like("normal text") == "normal text"


def test_empty_string():
    assert escape_like("") == ""
