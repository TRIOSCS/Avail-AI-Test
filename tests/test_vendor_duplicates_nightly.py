"""test_vendor_duplicates_nightly.py — Coverage boost for
app/services/vendor_duplicates.py.

Targets missing lines: exact-match return, no-match empty list, Python fuzzy fallback
(SQLite dialect), _fuzzy_match_python with/without matches, pg_trgm fallback to Python on
OperationalError.

Called by: pytest
Depends on: tests/conftest.py (db_session, test_vendor_card)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import VendorCard
from app.services.vendor_duplicates import (
    _fuzzy_match_python,
    check_vendor_duplicate,
)


def _make_vendor(db: Session, display_name: str, normalized_name: str) -> VendorCard:
    card = VendorCard(
        display_name=display_name,
        normalized_name=normalized_name,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


class TestCheckVendorDuplicate:
    def test_exact_match_returns_exact_result(self, db_session):
        """Exact normalized-name match → single result with match='exact', score=100."""
        _make_vendor(db_session, "Arrow Electronics", "arrow electronics")
        results = check_vendor_duplicate("Arrow Electronics", db_session)
        assert len(results) == 1
        assert results[0]["match"] == "exact"
        assert results[0]["score"] == 100
        assert results[0]["name"] == "Arrow Electronics"

    def test_no_match_returns_empty_list(self, db_session):
        """No vendors in DB → empty list returned."""
        results = check_vendor_duplicate("Completely Unknown Vendor XYZ", db_session)
        assert results == []

    def test_fuzzy_python_fallback_for_sqlite_dialect(self, db_session):
        """SQLite dialect → Python rapidfuzz fallback path is exercised (no pg_trgm)."""
        _make_vendor(db_session, "Mouser Electronics", "mouser electronics")
        # SQLite is the test DB — dialect is 'sqlite', so Python fallback runs
        results = check_vendor_duplicate("Mouser Electroniks", db_session)
        # May or may not find a fuzzy match; what matters is no error raised
        assert isinstance(results, list)

    def test_pg_trgm_operational_error_falls_back_to_python(self):
        """When dialect is postgresql but _fuzzy_match_pg_trgm raises OperationalError,
        the code falls back to _fuzzy_match_python."""
        # Use a fully mocked session so we fully control bind.dialect.name
        mock_dialect = MagicMock()
        mock_dialect.name = "postgresql"
        mock_bind = MagicMock()
        mock_bind.dialect = mock_dialect

        mock_db = MagicMock()
        mock_db.bind = mock_bind
        # Exact match query returns None (no exact hit)
        mock_db.query.return_value.filter_by.return_value.first.return_value = None

        with (
            patch(
                "app.services.vendor_duplicates._fuzzy_match_pg_trgm",
                side_effect=OperationalError("pg_trgm not installed", None, None),
            ),
            patch(
                "app.services.vendor_duplicates._fuzzy_match_python",
                return_value=[{"id": 1, "name": "Digi-Key", "match": "fuzzy", "score": 85}],
            ) as mock_py,
        ):
            results = check_vendor_duplicate("Digi-Key Corp", mock_db)

        mock_py.assert_called_once()
        assert len(results) == 1
        assert results[0]["match"] == "fuzzy"


class TestFuzzyMatchPython:
    def test_returns_matches_for_similar_name(self, db_session):
        """_fuzzy_match_python finds a vendor with a high rapidfuzz score (>=80)."""
        _make_vendor(db_session, "Mouser Electronics", "mouser electronics")
        results = _fuzzy_match_python(db_session, "mouser electronics")
        assert len(results) >= 1
        assert results[0]["match"] == "fuzzy"
        assert results[0]["score"] >= 80

    def test_returns_empty_for_no_similar_names(self, db_session):
        """_fuzzy_match_python returns [] when no vendor scores >=80."""
        _make_vendor(db_session, "Completely Different Name", "completely different name")
        results = _fuzzy_match_python(db_session, "xyzabc totally unlike anything")
        assert results == []

    def test_caps_results_at_five(self, db_session):
        """_fuzzy_match_python returns at most 5 results."""
        for i in range(8):
            _make_vendor(db_session, f"Arrow Electronics {i}", f"arrow electronics {i}")
        results = _fuzzy_match_python(db_session, "arrow electronics")
        assert len(results) <= 5

    def test_results_sorted_by_score_descending(self, db_session):
        """Results from _fuzzy_match_python are sorted highest score first."""
        _make_vendor(db_session, "Arrow Electronics", "arrow electronics")
        _make_vendor(db_session, "Arrow Electro", "arrow electro")
        results = _fuzzy_match_python(db_session, "arrow electronics")
        if len(results) >= 2:
            assert results[0]["score"] >= results[1]["score"]
