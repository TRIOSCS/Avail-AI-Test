"""Tests for app/company_utils.py — normalization and dedup detection."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.company_utils import find_company_dedup_candidates, normalize_company_name
from app.models import Company, CustomerSite


class TestNormalizeCompanyName:
    def test_empty_string(self):
        assert normalize_company_name("") == ""

    def test_lowercase(self):
        assert normalize_company_name("ACME CORP") == "acme"

    def test_strip_inc(self):
        assert normalize_company_name("Mouser Electronics, Inc.") == "mouser electronics"

    def test_strip_llc(self):
        assert normalize_company_name("Acme Solutions LLC") == "acme solutions"

    def test_strip_corp(self):
        assert normalize_company_name("DigiKey Corp.") == "digikey"

    def test_leading_the(self):
        assert normalize_company_name("The Phoenix Company") == "phoenix"

    def test_collapse_whitespace(self):
        assert normalize_company_name("  Foo   Bar   ") == "foo bar"

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
                site_name=f"Site {i+1}",
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
