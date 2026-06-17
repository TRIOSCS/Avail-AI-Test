"""tests/test_vendor_duplicates.py — Tests for app/services/vendor_duplicates.py.

Covers: _fuzzy_match_pg_trgm (lines 31-39), PostgreSQL dialect path (lines 102-108),
        exact-match short-circuit, and no-bind fallback.

Called by: pytest autodiscovery
Depends on: conftest.db_session, app.models.VendorCard, app.services.vendor_duplicates
"""

import os
from unittest.mock import MagicMock, patch

os.environ["TESTING"] = "1"

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import VendorCard
from app.services.vendor_duplicates import _fuzzy_match_pg_trgm, check_vendor_duplicate


class TestFuzzyMatchPgTrgm:
    """Tests for _fuzzy_match_pg_trgm — lines 31-39."""

    def test_returns_formatted_match_list(self):
        """Mock the db.query chain to exercise lines 31-39."""
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.display_name = "Arrow Electronics"
        mock_row.score = 0.85

        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = [mock_row]

        mock_db = MagicMock(spec=Session)
        mock_db.query.return_value = mock_query

        result = _fuzzy_match_pg_trgm(mock_db, "arrow")

        assert result == [{"id": 1, "name": "Arrow Electronics", "match": "fuzzy", "score": 85}]

    def test_empty_result_when_no_rows(self):
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        mock_db = MagicMock(spec=Session)
        mock_db.query.return_value = mock_query

        result = _fuzzy_match_pg_trgm(mock_db, "unknown")
        assert result == []


class TestCheckVendorDuplicatePostgresDialect:
    """Tests for the PostgreSQL dialect branch — lines 102-108."""

    def _make_mock_db(self, dialect_name: str) -> MagicMock:
        """Return a MagicMock Session whose .bind.dialect.name is set and whose
        .query(...).filter_by(...).first() returns None (no exact match)."""
        mock_db = MagicMock(spec=Session)
        mock_bind = MagicMock()
        mock_bind.dialect.name = dialect_name
        type(mock_db).bind = property(lambda self: mock_bind)  # type: ignore[assignment]
        # exact-match query chain → None (no exact match)
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        return mock_db

    def test_postgresql_dialect_calls_pg_trgm(self):
        """When dialect is postgresql and no exception, _fuzzy_match_pg_trgm is used."""
        mock_db = self._make_mock_db("postgresql")
        expected = [{"id": 1, "name": "Foo", "match": "fuzzy", "score": 82}]
        with patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm", return_value=expected) as mock_pg:
            result = check_vendor_duplicate("bar inc", mock_db)

        mock_pg.assert_called_once()
        assert result == expected

    def test_postgresql_dialect_falls_back_on_operational_error(self):
        """OperationalError from pg_trgm triggers rollback + Python fallback (lines 105-108)."""
        mock_db = self._make_mock_db("postgresql")
        err = OperationalError("pg_trgm not installed", None, None)

        with patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm", side_effect=err):
            with patch("app.services.vendor_duplicates._fuzzy_match_python", return_value=[]) as mock_py:
                result = check_vendor_duplicate("bar inc", mock_db)

        mock_py.assert_called_once()
        assert result == []


class TestCheckVendorDuplicateExactMatch:
    """Exact-match short-circuit returns immediately with score 100."""

    def test_exact_match_returns_single_result(self, db_session: Session):
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)

        result = check_vendor_duplicate("Arrow Electronics", db_session)

        assert len(result) == 1
        assert result[0]["name"] == "Arrow Electronics"
        assert result[0]["match"] == "exact"
        assert result[0]["score"] == 100
        assert result[0]["id"] == card.id


class TestCheckVendorDuplicateNoBind:
    """When db.bind is None the dialect is '' and _fuzzy_match_python is used."""

    def test_no_bind_returns_list(self):
        """Use a fully mocked session with bind=None so dialect resolves to ''."""
        mock_db = MagicMock(spec=Session)
        type(mock_db).bind = property(lambda self: None)  # type: ignore[assignment]
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.services.vendor_duplicates._fuzzy_match_python", return_value=[]) as mock_py:
            result = check_vendor_duplicate("unknown vendor xyz", mock_db)

        mock_py.assert_called_once()
        assert isinstance(result, list)
