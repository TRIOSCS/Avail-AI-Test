"""tests/test_vendor_duplicates.py — Unit tests for app/services/vendor_duplicates.py.

Covers: _fuzzy_match_pg_trgm (pg-path formatting), check_vendor_duplicate
exact-match short-circuit, postgresql-dialect routing, pg_trgm error fallback,
and SQLite (Python-side) fuzzy path.
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.models import VendorCard
from app.services.vendor_duplicates import (
    _fuzzy_match_pg_trgm,
    _fuzzy_match_python,
    check_vendor_duplicate,
)

# ── _fuzzy_match_pg_trgm (lines 31-39) ──────────────────────────────────────


def test_fuzzy_match_pg_trgm_formats_rows_correctly():
    """_fuzzy_match_pg_trgm converts raw query rows to the expected dict shape."""
    mock_row = MagicMock()
    mock_row.id = 42
    mock_row.display_name = "Arrow Electronics"
    mock_row.score = 0.85

    mock_db = MagicMock()
    (mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value) = [
        mock_row
    ]

    result = _fuzzy_match_pg_trgm(mock_db, "arrow electronics")

    assert result == [{"id": 42, "name": "Arrow Electronics", "match": "fuzzy", "score": 85}]


def test_fuzzy_match_pg_trgm_returns_empty_when_no_rows():
    mock_db = MagicMock()
    (mock_db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value) = []

    result = _fuzzy_match_pg_trgm(mock_db, "unknown vendor")
    assert result == []


# ── _fuzzy_match_python (SQLite path, already partially covered) ─────────────


def test_fuzzy_match_python_returns_matches_above_threshold(db_session: Session, test_vendor_card: VendorCard):
    """Python-side rapidfuzz path returns matches with score >= 80."""
    db_session.flush()
    result = _fuzzy_match_python(db_session, "arrow electronics")
    # Arrow Electronics has normalized_name "arrow electronics" — perfect match
    assert any(r["match"] == "fuzzy" and r["score"] >= 80 for r in result)


def test_fuzzy_match_python_returns_empty_for_very_different_name(db_session: Session, test_vendor_card: VendorCard):
    db_session.flush()
    result = _fuzzy_match_python(db_session, "xyzzy quantum photonics")
    assert result == []


# ── check_vendor_duplicate: exact match ─────────────────────────────────────


def test_check_vendor_duplicate_exact_match_short_circuits(db_session: Session, test_vendor_card: VendorCard):
    """Exact normalized-name match returns score=100 and skips fuzzy."""
    result = check_vendor_duplicate("Arrow Electronics", db_session)

    assert len(result) == 1
    assert result[0]["match"] == "exact"
    assert result[0]["score"] == 100
    assert result[0]["name"] == "Arrow Electronics"


# ── check_vendor_duplicate: postgresql dialect path (lines 102-108) ──────────


def test_check_vendor_duplicate_uses_pg_trgm_on_postgresql_dialect():
    """When db.bind.dialect.name == 'postgresql', _fuzzy_match_pg_trgm is called."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    # No exact match
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm") as mock_trgm:
        mock_trgm.return_value = [{"id": 7, "name": "Acme Corp", "match": "fuzzy", "score": 88}]
        result = check_vendor_duplicate("Acme Corp", mock_db)

    mock_trgm.assert_called_once()
    assert result == [{"id": 7, "name": "Acme Corp", "match": "fuzzy", "score": 88}]


def test_check_vendor_duplicate_falls_back_to_python_on_operational_error():
    """OperationalError from pg_trgm triggers rollback + Python-side fallback."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with (
        patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm") as mock_trgm,
        patch("app.services.vendor_duplicates._fuzzy_match_python") as mock_python,
    ):
        mock_trgm.side_effect = OperationalError("no such function: similarity", None, None)
        mock_python.return_value = [{"id": 3, "name": "Approx Corp", "match": "fuzzy", "score": 82}]

        result = check_vendor_duplicate("Approx Corp", mock_db)

    mock_db.rollback.assert_called_once()
    mock_python.assert_called_once()
    assert result[0]["name"] == "Approx Corp"


def test_check_vendor_duplicate_falls_back_to_python_on_programming_error():
    """ProgrammingError (pg_trgm extension not installed) also falls back."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "postgresql"
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with (
        patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm") as mock_trgm,
        patch("app.services.vendor_duplicates._fuzzy_match_python") as mock_python,
    ):
        mock_trgm.side_effect = ProgrammingError("pg_trgm not installed", None, None)
        mock_python.return_value = []

        result = check_vendor_duplicate("Whatever", mock_db)

    mock_db.rollback.assert_called_once()
    assert result == []


def test_check_vendor_duplicate_no_bind_uses_python_fallback():
    """When db.bind is None, dialect check returns '' and Python path is used."""
    mock_db = MagicMock()
    mock_db.bind = None
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    with patch("app.services.vendor_duplicates._fuzzy_match_python") as mock_python:
        mock_python.return_value = []
        result = check_vendor_duplicate("No Bind Vendor", mock_db)

    mock_python.assert_called_once()
    assert result == []


def test_check_vendor_duplicate_caps_at_five_results(db_session: Session):
    """Result list is capped at 5 even if the fuzzy path would return more."""
    mock_db = MagicMock()
    mock_db.bind.dialect.name = "sqlite"
    mock_db.query.return_value.filter_by.return_value.first.return_value = None

    many = [{"id": i, "name": f"Corp {i}", "match": "fuzzy", "score": 80} for i in range(10)]
    with patch("app.services.vendor_duplicates._fuzzy_match_python", return_value=many):
        result = check_vendor_duplicate("Corp", mock_db)

    assert len(result) == 5
