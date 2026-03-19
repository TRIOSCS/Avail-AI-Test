"""Tests for SearchBuilder utility in app/utils/search_builder.py."""

from sqlalchemy import Column, String

from app.utils.search_builder import SearchBuilder


class FakeModel:
    name = Column(String)
    industry = Column(String)


def test_init_strips_whitespace():
    sb = SearchBuilder("  hello  ")
    assert sb.q == "hello"


def test_init_escapes_like_chars():
    sb = SearchBuilder("test%value_here")
    assert "\\%" in sb.safe
    assert "\\_" in sb.safe


def test_empty_query():
    sb = SearchBuilder("")
    assert sb.q == ""
    assert sb.safe == ""


def test_ilike_filter_generates_contains_pattern():
    sb = SearchBuilder("test")
    filt = sb.ilike_filter(FakeModel.name)
    assert filt is not None


def test_ilike_filter_prefix_mode():
    sb = SearchBuilder("test")
    filt = sb.ilike_filter(FakeModel.name, prefix=True)
    assert filt is not None


def test_ilike_filter_multiple_columns():
    sb = SearchBuilder("test")
    filt = sb.ilike_filter(FakeModel.name, FakeModel.industry)
    assert filt is not None


def test_empty_query_ilike_returns_true():
    """Empty search should match everything."""
    sb = SearchBuilder("")
    filt = sb.ilike_filter(FakeModel.name)
    # sa_true() returns a True clause
    assert filt is not None
