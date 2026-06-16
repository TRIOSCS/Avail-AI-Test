"""Tests for app/utils/sql_helpers.py — escape_like utility.

Covers: percent, underscore, backslash, combined, and clean string.
"""

import pytest

from app.utils.sql_helpers import escape_like


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("100%", r"100\%", id="percent"),
        pytest.param("some_name", r"some\_name", id="underscore"),
        pytest.param(r"path\to", r"path\\to", id="backslash"),
        pytest.param(r"100%_test\end", r"100\%\_test\\end", id="combined"),
        pytest.param("normal text", "normal text", id="clean_string"),
        pytest.param("", "", id="empty_string"),
    ],
)
def test_escape_like(value, expected):
    assert escape_like(value) == expected
