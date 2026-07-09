"""test_vendor_duplicates.py — Tests for app/services/vendor_duplicates.py.

Covers: check_vendor_duplicate (exact match, fuzzy match, empty name, pg_trgm error),
        _fuzzy_match_python (direct call with matching and non-matching rows).

Called by: pytest autodiscovery
Depends on: conftest.py db_session fixture, VendorCard model
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import VendorCard
from app.services.vendor_duplicates import (
    _fuzzy_match_pg_trgm,
    _fuzzy_match_python,
    check_vendor_duplicate,
)


def _make_vendor(db: Session, normalized: str, display: str) -> VendorCard:
    card = VendorCard(
        normalized_name=normalized,
        display_name=display,
        emails=[],
        phones=[],
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


class TestCheckVendorDuplicateExact:
    def test_exact_match_returns_single_exact_result(self, db_session: Session):
        _make_vendor(db_session, "arrow electronics", "Arrow Electronics")

        results = check_vendor_duplicate("Arrow Electronics", db_session)

        assert len(results) == 1
        assert results[0]["match"] == "exact"
        assert results[0]["score"] == 100
        assert results[0]["name"] == "Arrow Electronics"
        assert "id" in results[0]

    def test_exact_match_short_circuits_fuzzy(self, db_session: Session):
        """Exact match must return immediately without running fuzzy."""
        _make_vendor(db_session, "avnet", "Avnet")

        with patch("app.services.vendor_duplicates._fuzzy_match_python") as mock_fuzzy:
            results = check_vendor_duplicate("Avnet", db_session)

        mock_fuzzy.assert_not_called()
        assert results[0]["match"] == "exact"

    def test_exact_match_normalized_suffix_stripped(self, db_session: Session):
        """'Inc.' suffix is stripped before matching."""
        _make_vendor(db_session, "mouser", "Mouser")

        results = check_vendor_duplicate("Mouser Inc.", db_session)

        assert len(results) == 1
        assert results[0]["match"] == "exact"


class TestCheckVendorDuplicateEmpty:
    def test_empty_name_returns_empty_list(self, db_session: Session):
        _make_vendor(db_session, "arrow electronics", "Arrow Electronics")
        results = check_vendor_duplicate("", db_session)
        assert results == []

    def test_whitespace_only_name_no_crash(self, db_session: Session):
        _make_vendor(db_session, "arrow electronics", "Arrow Electronics")
        results = check_vendor_duplicate("   ", db_session)
        assert results == []


class TestCheckVendorDuplicateFuzzyPythonPath:
    def test_sqlite_uses_python_fuzzy_path(self, db_session: Session):
        """SQLite dialect triggers _fuzzy_match_python, not pg_trgm."""
        _make_vendor(db_session, "digikey", "Digi-Key Electronics")

        with patch("app.services.vendor_duplicates._fuzzy_match_python", return_value=[]) as mock_py:
            check_vendor_duplicate("Totally New Vendor XYZ", db_session)

        mock_py.assert_called_once()

    def test_no_match_returns_empty_list(self, db_session: Session):
        _make_vendor(db_session, "arrow electronics", "Arrow Electronics")

        results = check_vendor_duplicate("ZZZZUNKNOWNXXX9999", db_session)
        assert results == []

    def test_fuzzy_match_returns_candidates(self, db_session: Session):
        """Names close enough (≥80 token_sort_ratio) are returned as fuzzy matches."""
        _make_vendor(db_session, "future electronics", "Future Electronics")

        results = check_vendor_duplicate("Future Electron", db_session)

        # Should find the close match
        assert any(r["match"] == "fuzzy" for r in results)

    def test_results_capped_at_five(self, db_session: Session):
        """Regardless of how many fuzzy matches exist, at most 5 are returned."""
        for i in range(8):
            _make_vendor(db_session, f"acme corp variant {i}", f"Acme Corp Variant {i}")

        with patch("app.services.vendor_duplicates._fuzzy_match_python") as mock_py:
            mock_py.return_value = [
                {"id": i, "name": f"Vendor {i}", "match": "fuzzy", "score": 90 - i} for i in range(8)
            ]
            results = check_vendor_duplicate("acme corp", db_session)

        assert len(results) <= 5


class TestCheckVendorDuplicatePgTrgmFallback:
    def test_pg_trgm_operational_error_triggers_python_fallback_via_mock_session(self, db_session: Session):
        """Verify the OperationalError branch by mocking the whole session object."""
        pg_error = OperationalError("pg_trgm not installed", None, None)

        mock_session = MagicMock()
        mock_session.bind.dialect.name = "postgresql"
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        with patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm", side_effect=pg_error):
            with patch("app.services.vendor_duplicates._fuzzy_match_python", return_value=[]) as mock_py:
                results = check_vendor_duplicate("NoMatch999XYZ", mock_session)

        mock_py.assert_called_once()
        assert results == []

    def test_pg_trgm_programming_error_falls_back_to_python(self, db_session: Session):
        """ProgrammingError (missing extension) also triggers the Python fallback."""
        from sqlalchemy.exc import ProgrammingError

        pg_error = ProgrammingError("function similarity does not exist", None, None)

        mock_session = MagicMock()
        mock_session.bind.dialect.name = "postgresql"
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        fallback_result = [{"id": 1, "name": "X", "match": "fuzzy", "score": 85}]
        with patch("app.services.vendor_duplicates._fuzzy_match_pg_trgm", side_effect=pg_error):
            with patch("app.services.vendor_duplicates._fuzzy_match_python", return_value=fallback_result):
                results = check_vendor_duplicate("SomeVendor", mock_session)

        assert results == fallback_result


class TestFuzzyMatchPgTrgmDirect:
    def test_pg_trgm_with_mocked_session_returns_list(self):
        """Call _fuzzy_match_pg_trgm with a mock session to cover the function body."""
        mock_row = MagicMock()
        mock_row.id = 99
        mock_row.display_name = "Texas Instruments"
        mock_row.score = 0.85

        mock_session = MagicMock()
        (
            mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value
        ) = [mock_row]

        results = _fuzzy_match_pg_trgm(mock_session, "texas instruments")

        assert len(results) == 1
        assert results[0]["id"] == 99
        assert results[0]["name"] == "Texas Instruments"
        assert results[0]["match"] == "fuzzy"
        assert results[0]["score"] == 85

    def test_pg_trgm_empty_result(self):
        mock_session = MagicMock()
        (
            mock_session.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value
        ) = []

        results = _fuzzy_match_pg_trgm(mock_session, "no match")

        assert results == []


class TestFuzzyMatchPythonDirect:
    def test_high_score_row_included(self, db_session: Session):
        """Rows with token_sort_ratio ≥ 80 appear in results."""
        _make_vendor(db_session, "texas instruments", "Texas Instruments")

        matches = _fuzzy_match_python(db_session, "texas instruments")

        assert len(matches) == 1
        assert matches[0]["match"] == "fuzzy"
        assert matches[0]["score"] >= 80

    def test_low_score_row_excluded(self, db_session: Session):
        """Rows below the 80-score threshold are excluded."""
        _make_vendor(db_session, "completely unrelated company name zzz", "Unrelated Co")

        matches = _fuzzy_match_python(db_session, "arrow")

        assert matches == []

    def test_results_sorted_descending_by_score(self, db_session: Session):
        """Results are ordered highest score first."""
        _make_vendor(db_session, "arrow electronics", "Arrow Electronics")
        _make_vendor(db_session, "arrow", "Arrow")

        matches = _fuzzy_match_python(db_session, "arrow electronics")

        scores = [m["score"] for m in matches]
        assert scores == sorted(scores, reverse=True)

    def test_capped_at_five_results(self, db_session: Session):
        """At most 5 candidates returned regardless of how many match."""
        for i in range(10):
            _make_vendor(db_session, f"arrow electronics variant {i}", f"Arrow Electronics Variant {i}")

        matches = _fuzzy_match_python(db_session, "arrow electronics")

        assert len(matches) <= 5

    def test_empty_db_returns_empty_list(self, db_session: Session):
        matches = _fuzzy_match_python(db_session, "any name")
        assert matches == []
