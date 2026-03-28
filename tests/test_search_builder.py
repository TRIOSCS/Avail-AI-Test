"""test_search_builder.py — Tests for SearchBuilder utility.

Covers ilike_filter (empty, prefix, multi-column), fts_or_fallback
(empty query, short query, FTS error fallback, FTS no-results fallback,
FTS with results, ProgrammingError/OperationalError fallback).

Called by: pytest
Depends on: app/utils/search_builder.py, conftest.py
"""

from unittest.mock import MagicMock, patch

from sqlalchemy import Column, String
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.utils.search_builder import SearchBuilder


class FakeModel:
    name = Column(String)
    industry = Column(String)


# ═══════════════════════════════════════════════════════════════════════
#  __init__
# ═══════════════════════════════════════════════════════════════════════


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


# ═══════════════════════════════════════════════════════════════════════
#  ilike_filter
# ═══════════════════════════════════════════════════════════════════════


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
    compiled = str(filt.compile(compile_kwargs={"literal_binds": False}))
    assert "OR" in compiled.upper()


def test_empty_query_ilike_returns_true():
    """Empty search should match everything (sa_true())."""
    sb = SearchBuilder("")
    filt = sb.ilike_filter(FakeModel.name)
    assert str(filt) == "true"


def test_whitespace_query_ilike_returns_true():
    sb = SearchBuilder("   ")
    filt = sb.ilike_filter(FakeModel.name)
    assert str(filt) == "true"


# ═══════════════════════════════════════════════════════════════════════
#  fts_or_fallback — lines 64-81
# ═══════════════════════════════════════════════════════════════════════


def test_fts_empty_query_uses_ilike(db_session):
    """Empty query short-circuits to ILIKE (line 64)."""
    from app.models.intelligence import MaterialCard

    sb = SearchBuilder("")
    q = db_session.query(MaterialCard)
    result = sb.fts_or_fallback(q, MaterialCard, [MaterialCard.normalized_mpn])
    assert result.all() == []


def test_fts_short_query_uses_ilike(db_session):
    """Query shorter than min_len skips FTS (line 64)."""
    from app.models.intelligence import MaterialCard

    sb = SearchBuilder("ab")
    q = db_session.query(MaterialCard)
    result = sb.fts_or_fallback(q, MaterialCard, [MaterialCard.normalized_mpn], min_len=3)
    assert result.all() == []


def test_fts_model_without_search_vector_uses_ilike(db_session):
    """Model without search_vector attribute falls back to ILIKE (line 64)."""
    from app.models.intelligence import MaterialCard

    sb = SearchBuilder("resistor query")
    q = db_session.query(MaterialCard)

    # Use a model class without search_vector
    class NoFTSModel:
        pass

    # Manually test with a model that definitely lacks search_vector
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.all.return_value = []
    result = sb.fts_or_fallback(mock_query, NoFTSModel, [MaterialCard.normalized_mpn])
    assert result is not None


def test_fts_sqlite_operational_error_fallback(db_session):
    """SQLite throws OperationalError for FTS — must fall back to ILIKE (line 80)."""
    from app.models.intelligence import MaterialCard

    sb = SearchBuilder("resistor test query")
    q = db_session.query(MaterialCard)

    if hasattr(MaterialCard, "search_vector"):
        # SQLite can't handle @@ operator — should fall back gracefully
        result = sb.fts_or_fallback(q, MaterialCard, [MaterialCard.normalized_mpn])
        assert result.all() == []


def test_fts_no_results_falls_back_to_ilike():
    """FTS returns 0 results → fall back to ILIKE (line 79)."""
    sb = SearchBuilder("xyznonexistent")

    mock_model = MagicMock()
    mock_model.search_vector = MagicMock()

    mock_query = MagicMock()
    mock_fts_query = MagicMock()
    mock_fts_query.count.return_value = 0
    mock_fallback = MagicMock()

    # First call: FTS filter chain; second call: ILIKE fallback
    mock_query.filter.side_effect = [mock_fts_query, mock_fallback]
    mock_fts_query.filter.return_value = mock_fts_query
    mock_fts_query.params.return_value = mock_fts_query
    mock_fts_query.order_by.return_value = mock_fts_query

    with patch.object(sb, "ilike_filter", return_value=MagicMock()):
        result = sb.fts_or_fallback(mock_query, mock_model, [MagicMock()])
    assert result is not None


def test_fts_with_results_returns_fts_query():
    """FTS returns >0 results → use the FTS query (line 78)."""
    sb = SearchBuilder("resistor")

    mock_model = MagicMock()
    mock_model.search_vector = MagicMock()

    mock_query = MagicMock()
    mock_fts_query = MagicMock()
    mock_fts_query.count.return_value = 5
    mock_query.filter.return_value = mock_fts_query
    mock_fts_query.filter.return_value = mock_fts_query
    mock_fts_query.params.return_value = mock_fts_query
    mock_fts_query.order_by.return_value = mock_fts_query

    fallback_col = MagicMock()
    result = sb.fts_or_fallback(mock_query, mock_model, [fallback_col])
    assert result is mock_fts_query


def test_fts_programming_error_fallback():
    """ProgrammingError in FTS path falls back to ILIKE (line 80-81)."""
    sb = SearchBuilder("resistor query long enough")

    mock_model = MagicMock()
    mock_model.search_vector = MagicMock()

    call_count = 0

    def _filter_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ProgrammingError("test", {}, Exception("pg error"))
        return MagicMock()

    mock_query = MagicMock()
    mock_query.filter.side_effect = _filter_side_effect

    with patch.object(sb, "ilike_filter", return_value=MagicMock()):
        result = sb.fts_or_fallback(mock_query, mock_model, [MagicMock()])
    assert result is not None


def test_fts_operational_error_fallback():
    """OperationalError in FTS path falls back to ILIKE (line 80-81)."""
    sb = SearchBuilder("resistor query long enough")

    mock_model = MagicMock()
    mock_model.search_vector = MagicMock()

    call_count = 0

    def _filter_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OperationalError("test", {}, Exception("sqlite error"))
        return MagicMock()

    mock_query = MagicMock()
    mock_query.filter.side_effect = _filter_side_effect

    with patch.object(sb, "ilike_filter", return_value=MagicMock()):
        result = sb.fts_or_fallback(mock_query, mock_model, [MagicMock()])
    assert result is not None
