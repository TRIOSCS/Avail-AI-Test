"""Tests for app/company_utils.py — normalization and dedup detection."""

from datetime import UTC, datetime

import pytest

from app.company_utils import find_company_dedup_candidates, normalize_company_name
from app.models import Company, CustomerSite


class TestNormalizeCompanyName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("ACME CORP", "acme"),
            ("Mouser Electronics, Inc.", "mouser electronics"),
            ("Acme Solutions LLC", "acme solutions"),
            ("DigiKey Corp.", "digikey"),
            ("The Phoenix Company", "phoenix"),
            ("  Foo   Bar   ", "foo bar"),
        ],
        ids=[
            "empty_string",
            "lowercase",
            "strip_inc",
            "strip_llc",
            "strip_corp",
            "leading_the",
            "collapse_whitespace",
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize_company_name(raw) == expected

    def test_no_false_strip(self):
        """Should not strip 'inc' from middle of word like 'Incipio'."""
        result = normalize_company_name("Incipio Technologies")
        assert "incip" in result


class TestFindCompanyDedupCandidates:
    def _make_company(self, db, name, sites=0, owner_id=None, is_strategic=False):
        c = Company(
            name=name,
            is_active=True,
            account_owner_id=owner_id,
            is_strategic=is_strategic,
            created_at=datetime.now(UTC),
        )
        db.add(c)
        db.flush()
        for i in range(sites):
            s = CustomerSite(
                company_id=c.id,
                site_name=f"Site {i + 1}",
                created_at=datetime.now(UTC),
            )
            db.add(s)
        db.flush()
        return c

    def test_finds_similar_names(self, db_session):
        self._make_company(db_session, "Arrow Electronics")
        self._make_company(db_session, "Arrow Electronic")
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=80)
        assert len(candidates) >= 1
        names = {candidates[0]["company_a"]["name"], candidates[0]["company_b"]["name"]}
        assert "Arrow Electronics" in names
        assert "Arrow Electronic" in names

    def test_ignores_distinct_names(self, db_session):
        self._make_company(db_session, "Acme Corporation")
        self._make_company(db_session, "Zeta Industries")
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=85)
        assert len(candidates) == 0

    def test_respects_limit(self, db_session):
        # Create 4 very similar names to generate multiple pairs
        for i in range(4):
            self._make_company(db_session, f"TestCo Variant {i}")
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=70, limit=2)
        assert len(candidates) <= 2

    def test_empty_db(self, db_session):
        candidates = find_company_dedup_candidates(db_session)
        assert candidates == []

    def test_sorted_by_score(self, db_session):
        self._make_company(db_session, "Alpha Industries Inc")
        self._make_company(db_session, "Alpha Industries")
        self._make_company(db_session, "Alpha Ind")
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=70)
        if len(candidates) >= 2:
            assert candidates[0]["score"] >= candidates[1]["score"]

    def test_auto_keep_prefers_more_sites(self, db_session):
        c1 = self._make_company(db_session, "Beta Corp", sites=3)
        c2 = self._make_company(db_session, "Beta Corporation", sites=1)
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=80)
        assert len(candidates) >= 1
        assert candidates[0]["auto_keep_id"] == c1.id

    def test_inactive_excluded(self, db_session):
        c1 = self._make_company(db_session, "Gamma LLC")
        c2 = Company(
            name="Gamma",
            is_active=False,
            created_at=datetime.now(UTC),
        )
        db_session.add(c2)
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=80)
        # Should not match inactive company
        assert len(candidates) == 0


# ── suggest_clean_company_name ───────────────────────────────────────────────


class TestSuggestCleanCompanyName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", ""),
            ("Arrow Electronics, Inc.", "Arrow Electronics"),
            ("The Phoenix Company LLC", "Phoenix"),
            ("Acme Corp.", "Acme"),
            ("  DigiKey   ", "DigiKey"),
            ("Solutions, LLC,", "Solutions"),
            ("X Corp--", "X"),
        ],
    )
    def test_suggest(self, raw, expected):
        from app.company_utils import suggest_clean_company_name

        assert suggest_clean_company_name(raw) == expected

    def test_pure_none_returns_empty(self):
        from app.company_utils import suggest_clean_company_name

        assert suggest_clean_company_name("") == ""


# ── pg path / dispatcher ─────────────────────────────────────────────────────


def test_find_company_dedup_candidates_uses_pg_path():
    """Dispatcher routes to PG path when dialect is postgresql."""
    from unittest.mock import MagicMock, patch

    from app.company_utils import find_company_dedup_candidates

    mock_db = MagicMock()
    mock_db.bind = MagicMock()
    mock_db.bind.dialect.name = "postgresql"

    with patch("app.company_utils._find_company_dedup_candidates_pg", return_value=[]) as mock_pg:
        find_company_dedup_candidates(mock_db)

    mock_pg.assert_called_once_with(mock_db, 85, 50)


def test_find_company_dedup_candidates_pg_empty_pairs():
    """PG path returns [] when no similar pairs are found."""
    from unittest.mock import MagicMock

    from app.company_utils import _find_company_dedup_candidates_pg

    mock_db = MagicMock()
    mock_q = mock_db.query.return_value
    mock_q.filter.return_value = mock_q
    mock_q.order_by.return_value = mock_q
    mock_q.limit.return_value = mock_q
    mock_q.all.return_value = []

    result = _find_company_dedup_candidates_pg(mock_db, 85, 50)

    assert result == []
