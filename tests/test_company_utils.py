"""Tests for app/company_utils.py — normalization and dedup detection."""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest

from app.company_utils import (
    _auto_keep_rank,
    _pair_dict,
    find_company_dedup_candidates,
    normalize_company_name,
    suggest_clean_company_name,
)
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
            created_at=datetime.now(timezone.utc),
        )
        db.add(c)
        db.flush()
        for i in range(sites):
            s = CustomerSite(
                company_id=c.id,
                site_name=f"Site {i + 1}",
                created_at=datetime.now(timezone.utc),
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(c2)
        db_session.commit()

        candidates = find_company_dedup_candidates(db_session, threshold=80)
        # Should not match inactive company
        assert len(candidates) == 0


class TestSuggestCleanCompanyName:
    @pytest.mark.parametrize(
        "raw,expected_contains,expected_not_contains",
        [
            ("ACME CORP", "ACME", ["Corp", "corp"]),
            ("Mouser Electronics, Inc.", "Electronics", ["Inc", "inc"]),
            ("The Phoenix Company", "Phoenix", ["The ", "Company"]),
            ("DigiKey Corp.", "DigiKey", ["Corp"]),
            ("  Foo   Bar  ", "Foo Bar", []),
            ("Arrow Electronics, LLC", "Arrow Electronics", ["LLC"]),
        ],
    )
    def test_suggest_clean(self, raw, expected_contains, expected_not_contains):
        result = suggest_clean_company_name(raw)
        assert expected_contains in result
        for not_expected in expected_not_contains:
            assert not_expected not in result

    def test_empty_returns_empty(self):
        assert suggest_clean_company_name("") == ""

    def test_none_returns_empty(self):
        assert suggest_clean_company_name(None) == ""

    def test_preserves_case(self):
        result = suggest_clean_company_name("Arrow Electronics Inc")
        assert result[0].isupper()

    def test_strips_trailing_comma(self):
        result = suggest_clean_company_name("Acme Corp,")
        assert "," not in result

    def test_strips_leading_the(self):
        result = suggest_clean_company_name("The Phoenix Company")
        assert not result.startswith("The ")


class TestPairDictHelpers:
    def _company_dict(self, id: int, name: str, sites: int = 0, owner: bool = False, strategic: bool = False):
        return {
            "id": id,
            "name": name,
            "site_count": sites,
            "has_owner": owner,
            "is_strategic": strategic,
        }

    def test_pair_dict_shape(self):
        a = self._company_dict(1, "Alpha Corp", sites=3, owner=True)
        b = self._company_dict(2, "Alpha Corporation", sites=1)
        result = _pair_dict(a, b, 92)
        assert result["score"] == 92
        assert "company_a" in result
        assert "company_b" in result
        assert "auto_keep_id" in result

    def test_pair_dict_auto_keep_prefers_more_sites(self):
        a = self._company_dict(1, "A Corp", sites=5)
        b = self._company_dict(2, "A Corporation", sites=1)
        result = _pair_dict(a, b, 90)
        assert result["auto_keep_id"] == 1

    def test_pair_dict_auto_keep_prefers_owner(self):
        a = self._company_dict(1, "B Corp", sites=1, owner=True)
        b = self._company_dict(2, "B Corporation", sites=1, owner=False)
        result = _pair_dict(a, b, 90)
        assert result["auto_keep_id"] == 1

    def test_pair_dict_auto_keep_prefers_strategic(self):
        a = self._company_dict(1, "C Corp", sites=1, owner=False, strategic=True)
        b = self._company_dict(2, "C Corporation", sites=1, owner=False, strategic=False)
        result = _pair_dict(a, b, 85)
        assert result["auto_keep_id"] == 1

    def test_auto_keep_rank_lower_id_tiebreak(self):
        a = self._company_dict(1, "D Corp", sites=0)
        b = self._company_dict(2, "D Corporation", sites=0)
        # Equal sites/owner/strategic → lower id wins
        rank_a = _auto_keep_rank(a)
        rank_b = _auto_keep_rank(b)
        assert rank_a > rank_b


class TestFindCompanyDedupCandidatesPGPath:
    """Test the PostgreSQL code path by mocking the dialect."""

    def _pg_db(self, pair_rows=None):
        from unittest.mock import MagicMock

        mock_db = MagicMock()
        mock_db.bind = MagicMock()
        mock_db.bind.dialect.name = "postgresql"
        query = mock_db.query.return_value
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = pair_rows if pair_rows is not None else []
        query.outerjoin.return_value = query
        query.group_by.return_value = query
        return mock_db

    def test_pg_path_returns_empty_when_no_pairs(self):
        from app.company_utils import _find_company_dedup_candidates_pg

        db = self._pg_db(pair_rows=[])
        result = _find_company_dedup_candidates_pg(db, threshold=85, limit=50)
        assert result == []

    def test_pg_path_with_pairs_builds_candidates(self):
        from unittest.mock import MagicMock

        from app.company_utils import _find_company_dedup_candidates_pg

        row = MagicMock()
        row.a_id = 1
        row.a_name = "Alpha Corp"
        row.b_id = 2
        row.b_name = "Alpha Corporation"
        row.sim = 0.92

        attr_row_a = MagicMock()
        attr_row_a.id = 1
        attr_row_a.account_owner_id = None
        attr_row_a.is_strategic = False
        attr_row_a.site_count = 2

        attr_row_b = MagicMock()
        attr_row_b.id = 2
        attr_row_b.account_owner_id = 5
        attr_row_b.is_strategic = False
        attr_row_b.site_count = 1

        db = MagicMock()
        db.bind = MagicMock()
        db.bind.dialect.name = "postgresql"

        call_count = [0]

        def query_side_effect(*args):
            call_count[0] += 1
            q = MagicMock()
            q.filter.return_value = q
            q.order_by.return_value = q
            q.limit.return_value = q
            q.outerjoin.return_value = q
            q.group_by.return_value = q
            if call_count[0] == 1:
                q.all.return_value = [row]
            else:
                q.all.return_value = [attr_row_a, attr_row_b]
            return q

        db.query.side_effect = query_side_effect

        result = _find_company_dedup_candidates_pg(db, threshold=85, limit=50)
        assert len(result) == 1
        assert result[0]["score"] == 92
        assert result[0]["company_a"]["name"] == "Alpha Corp"
