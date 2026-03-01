"""Phase 9 — Integration tests for the full prospecting flow.

End-to-end tests covering:
A. Discovery → scoring → enrichment → claim → briefing
B. Edge cases: dedup, concurrent claims, JSONB null handling, malformed data
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.prospect_account import ProspectAccount
from app.services.prospect_claim import (
    add_prospect_manually,
    check_enrichment_status,
    claim_prospect,
    reveal_contacts,
)
from app.services.prospect_scoring import calculate_fit_score, calculate_readiness_score

# ── Helpers ──────────────────────────────────────────────────────────


def _make_user(db: Session, **kw) -> User:
    defaults = {"email": "sales@test.com", "name": "Sales Rep", "role": "buyer", "azure_id": "az-1"}
    defaults.update(kw)
    u = User(**defaults)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_prospect(db: Session, **kw) -> ProspectAccount:
    defaults = {
        "name": "Test Corp",
        "domain": "testcorp.com",
        "industry": "Aerospace & Defense",
        "region": "US",
        "fit_score": 75,
        "readiness_score": 60,
        "status": "suggested",
        "discovery_source": "explorium",
        "readiness_signals": {},
        "contacts_preview": [],
        "similar_customers": [],
    }
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_company(db: Session, **kw) -> Company:
    defaults = {"name": "Existing Corp", "domain": "existing.com", "is_active": True}
    defaults.update(kw)
    c = Company(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ══════════════════════════════════════════════════════════════════════
# A. Full Flow Integration Tests
# ══════════════════════════════════════════════════════════════════════


class TestFullProspectingFlow:
    """Discovery → score → claim → Company created → contacts revealed."""

    def test_discovery_scoring_claim_flow(self, db_session):
        """Simulate: Explorium finds a company, scoring runs, user claims it."""
        user = _make_user(db_session)

        # Step 1: Discovery creates a prospect with raw data
        prospect_data = {
            "name": "Raytheon Sensors",
            "industry": "Aerospace & Defense",
            "naics_code": "336412",
            "employee_count_range": "5001-10000",
            "region": "US",
        }

        # Step 2: Scoring runs
        fit, reasoning = calculate_fit_score(prospect_data)
        readiness, breakdown = calculate_readiness_score(
            prospect_data,
            {"intent": {"strength": "strong", "topics": ["military electronics"]}},
        )

        assert fit > 50, "Aerospace+defense should score well"
        assert readiness > 0

        # Step 3: Prospect is created with scores
        p = _make_prospect(
            db_session,
            name="Raytheon Sensors",
            domain="raytheon-sensors.com",
            industry="Aerospace & Defense",
            naics_code="336412",
            employee_count_range="5001-10000",
            region="US",
            fit_score=fit,
            readiness_score=readiness,
            readiness_signals={"intent": {"strength": "strong"}},
            enrichment_data={
                "contacts_full": [
                    {
                        "name": "Jane Buyer",
                        "title": "VP Procurement",
                        "email": "jane@raytheon-sensors.com",
                        "verified": True,
                    },
                ]
            },
        )

        # Step 4: User claims the prospect
        result = claim_prospect(p.id, user.id, db_session)

        assert result["status"] == "claimed"
        assert result["path"] == "new_company"
        assert result["company_id"] is not None

        # Step 5: Company was created
        company = db_session.get(Company, result["company_id"])
        assert company is not None
        assert company.name == "Raytheon Sensors"
        assert company.domain == "raytheon-sensors.com"
        assert company.account_owner_id == user.id
        assert company.source == "prospecting"

        # Step 6: Prospect is linked
        db_session.refresh(p)
        assert p.status == "claimed"
        assert p.company_id == company.id

    def test_claim_reveals_contacts(self, db_session):
        """After claim, reveal_contacts creates SiteContact records."""
        user = _make_user(db_session)
        p = _make_prospect(
            db_session,
            name="Contact Test Corp",
            domain="contacttest.com",
            hq_location="Austin, TX, US",
            enrichment_data={
                "contacts_full": [
                    {
                        "name": "Alice Buyer",
                        "title": "Director Procurement",
                        "email": "alice@contacttest.com",
                        "verified": True,
                        "seniority": "director",
                    },
                    {
                        "name": "Bob Supply",
                        "title": "Supply Chain Manager",
                        "email": "bob@contacttest.com",
                        "verified": True,
                        "seniority": "manager",
                    },
                ]
            },
        )

        # Claim first (creates Company)
        result = claim_prospect(p.id, user.id, db_session)
        db_session.refresh(p)

        # Reveal contacts
        contacts = reveal_contacts(p, db_session)

        assert len(contacts) == 2
        assert contacts[0]["name"] == "Alice Buyer"
        assert contacts[0]["email"] == "alice@contacttest.com"

        # Verify SiteContact records
        site = db_session.query(CustomerSite).filter_by(company_id=p.company_id).first()
        assert site is not None
        assert site.site_name == "Contact Test Corp - HQ"

        site_contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(site_contacts) == 2
        assert site_contacts[0].is_primary is True
        assert site_contacts[1].is_primary is False

    def test_sf_migrated_claim_path(self, db_session):
        """SF-migrated prospect (company_id set) transfers ownership."""
        user = _make_user(db_session)
        company = _make_company(db_session, name="SF Legacy Corp", domain="sflegacy.com")

        p = _make_prospect(
            db_session,
            name="SF Legacy Corp",
            domain="sflegacy.com",
            company_id=company.id,
            discovery_source="sf_import",
        )

        result = claim_prospect(p.id, user.id, db_session)

        assert result["path"] == "existing_company"
        assert result["company_id"] == company.id

        # Verify ownership transferred
        db_session.refresh(company)
        assert company.account_owner_id == user.id

    def test_domain_collision_on_claim(self, db_session):
        """New discovery with matching domain links to existing Company."""
        user = _make_user(db_session)
        company = _make_company(db_session, name="Existing Corp", domain="collision.com")

        p = _make_prospect(
            db_session,
            name="Collision Corp",
            domain="collision.com",
            company_id=None,  # new discovery
        )

        result = claim_prospect(p.id, user.id, db_session)

        assert result["path"] == "domain_collision"
        assert result["company_id"] == company.id
        assert "warning" in result

        # Company ownership transferred
        db_session.refresh(company)
        assert company.account_owner_id == user.id


class TestDismissFlow:
    def test_dismiss_removes_from_suggested(self, db_session):
        """Dismissed prospect no longer appears in suggested list."""
        user = _make_user(db_session)
        p = _make_prospect(db_session, name="Dismiss Me", domain="dismissme.com")

        p.status = "dismissed"
        p.dismissed_by = user.id
        p.dismissed_at = datetime.now(timezone.utc)
        p.dismiss_reason = "not a fit"
        db_session.commit()

        # Query suggested only — should not find the dismissed one
        suggested = db_session.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()
        assert all(s.id != p.id for s in suggested)

    def test_dismiss_preserves_data(self, db_session):
        """Dismissed prospect retains all enrichment data."""
        user = _make_user(db_session)
        p = _make_prospect(
            db_session,
            name="Data Keeper",
            domain="datakeeper.com",
            enrichment_data={"key": "value"},
            readiness_signals={"intent": {"strength": "strong"}},
        )

        p.status = "dismissed"
        p.dismiss_reason = "timing"
        db_session.commit()
        db_session.refresh(p)

        assert p.enrichment_data == {"key": "value"}
        assert p.readiness_signals["intent"]["strength"] == "strong"


class TestExpireResurfaceFlow:
    @pytest.mark.asyncio
    async def test_expire_then_resurface(self, db_session):
        """Prospect expires, gets fresh signals, resurfaces."""
        from app.services.prospect_scheduler import job_expire_and_resurface

        # Create an old low-readiness prospect
        p = _make_prospect(
            db_session,
            name="Cycle Corp",
            domain="cyclecorp.com",
            readiness_score=30,
            readiness_signals={},
            created_at=datetime.now(timezone.utc) - timedelta(days=120),
            last_enriched_at=datetime.now(timezone.utc) - timedelta(days=90),
        )

        # Run expire
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            await job_expire_and_resurface()

        db_session.refresh(p)
        assert p.status == "expired"

        # Simulate fresh signals arriving
        p.readiness_signals = {"intent": {"strength": "strong"}}
        p.readiness_score = 55
        p.last_enriched_at = datetime.now(timezone.utc) - timedelta(days=5)
        db_session.commit()

        # Run again — should resurface
        with patch("app.database.SessionLocal", return_value=db_session), patch.object(db_session, "close"):
            result = await job_expire_and_resurface()

        db_session.refresh(p)
        assert p.status == "suggested"
        assert result["resurfaced"] >= 1


# ══════════════════════════════════════════════════════════════════════
# B. Edge Cases
# ══════════════════════════════════════════════════════════════════════


class TestConcurrentClaims:
    def test_double_claim_rejected(self, db_session):
        """Second claim on same prospect raises ValueError."""
        user1 = _make_user(db_session, email="rep1@test.com", azure_id="az-rep1")
        user2 = _make_user(db_session, email="rep2@test.com", azure_id="az-rep2")

        p = _make_prospect(db_session, name="Race Corp", domain="racecorp.com")

        # First claim succeeds
        claim_prospect(p.id, user1.id, db_session)

        # Second claim fails
        with pytest.raises(ValueError, match="Already claimed"):
            claim_prospect(p.id, user2.id, db_session)

    def test_cannot_claim_dismissed(self, db_session):
        """Cannot claim a dismissed prospect."""
        user = _make_user(db_session)
        p = _make_prospect(db_session, name="Gone Corp", domain="gonecorp.com", status="dismissed")

        with pytest.raises(ValueError, match="Cannot claim"):
            claim_prospect(p.id, user.id, db_session)

    def test_cannot_claim_expired(self, db_session):
        """Cannot claim an expired prospect."""
        user = _make_user(db_session)
        p = _make_prospect(db_session, name="Old Corp", domain="oldcorp.com", status="expired")

        with pytest.raises(ValueError, match="Cannot claim"):
            claim_prospect(p.id, user.id, db_session)


class TestJSONBNullHandling:
    def test_null_readiness_signals(self, db_session):
        """Prospect with NULL readiness_signals doesn't crash scoring."""
        p = _make_prospect(
            db_session,
            name="Null Signals",
            domain="nullsignals.com",
            readiness_signals=None,
        )
        # Readiness calculation should handle None
        data = {"name": p.name, "industry": p.industry, "region": p.region}
        score, breakdown = calculate_readiness_score(data, None)
        assert isinstance(score, (int, float))

    def test_null_enrichment_data(self, db_session):
        """Check enrichment status with NULL enrichment_data."""
        p = _make_prospect(
            db_session,
            name="Null Enrich",
            domain="nullenrich.com",
            enrichment_data=None,
        )
        result = check_enrichment_status(p.id, db_session)
        assert result["status"] == "none"
        assert result["contacts_created"] == 0

    def test_null_contacts_preview(self, db_session):
        """Prospect with NULL contacts_preview serializes safely."""
        p = _make_prospect(
            db_session,
            name="Null Contacts",
            domain="nullcontacts.com",
            contacts_preview=None,
        )
        # Simulate serialization (from router)
        contacts = p.contacts_preview or []
        assert contacts == []

    def test_empty_similar_customers(self, db_session):
        """Empty similar_customers is handled."""
        p = _make_prospect(
            db_session,
            name="No Similar",
            domain="nosimilar.com",
            similar_customers=[],
        )
        similar = p.similar_customers or []
        assert similar == []

    def test_nested_jsonb_signals(self, db_session):
        """Complex nested JSONB signals round-trip correctly."""
        signals = {
            "intent": {"strength": "strong", "topics": ["semiconductors", "passive components"]},
            "hiring": {"type": "procurement", "count": 3},
            "events": [{"type": "funding", "date": "2026-01"}],
        }
        p = _make_prospect(
            db_session,
            name="Rich Signals",
            domain="richsignals.com",
            readiness_signals=signals,
        )
        db_session.refresh(p)
        assert p.readiness_signals["intent"]["topics"] == ["semiconductors", "passive components"]
        assert p.readiness_signals["hiring"]["count"] == 3


class TestManualProspectEdgeCases:
    def test_add_manual_deduplicates(self, db_session):
        """Adding a domain that already exists returns existing record."""
        user = _make_user(db_session)
        p = _make_prospect(db_session, name="Exists Corp", domain="exists.com")

        result = add_prospect_manually("exists.com", user.id, db_session)
        assert result["is_new"] is False
        assert result["prospect_id"] == p.id

    def test_add_manual_creates_new(self, db_session):
        """Adding a new domain creates a prospect."""
        user = _make_user(db_session)

        result = add_prospect_manually("brandnew.com", user.id, db_session)
        assert result["is_new"] is True
        assert result["domain"] == "brandnew.com"

        p = db_session.get(ProspectAccount, result["prospect_id"])
        assert p.discovery_source == "manual"
        assert p.status == "suggested"

    def test_add_manual_empty_domain_raises(self, db_session):
        """Empty domain raises ValueError."""
        user = _make_user(db_session)
        with pytest.raises(ValueError, match="Domain is required"):
            add_prospect_manually("", user.id, db_session)

    def test_add_manual_normalizes_domain(self, db_session):
        """Domain is lowercased and stripped."""
        user = _make_user(db_session)
        result = add_prospect_manually("  BIG-CORP.COM  ", user.id, db_session)
        assert result["domain"] == "big-corp.com"


class TestRevealContactsEdgeCases:
    def test_reveal_no_company_id(self, db_session):
        """Reveal contacts with no company_id returns empty."""
        p = _make_prospect(db_session, name="No Company", domain="nocompany.com", company_id=None)
        result = reveal_contacts(p, db_session)
        assert result == []

    def test_reveal_no_contacts_data(self, db_session):
        """Reveal contacts with empty enrichment_data returns empty."""
        user = _make_user(db_session)
        p = _make_prospect(db_session, name="Empty Data", domain="emptydata.com")
        claim_prospect(p.id, user.id, db_session)
        db_session.refresh(p)

        result = reveal_contacts(p, db_session)
        assert result == []

    def test_reveal_skips_duplicate_emails(self, db_session):
        """Reveal doesn't create duplicate SiteContact records."""
        user = _make_user(db_session)
        p = _make_prospect(
            db_session,
            name="Dedup Contacts",
            domain="dedupcontacts.com",
            enrichment_data={
                "contacts_full": [
                    {"name": "Alice", "title": "VP", "email": "alice@dedupcontacts.com"},
                    {"name": "Alice Dup", "title": "VP", "email": "alice@dedupcontacts.com"},  # duplicate
                ]
            },
        )
        claim_prospect(p.id, user.id, db_session)
        db_session.refresh(p)

        contacts = reveal_contacts(p, db_session)
        assert len(contacts) == 1  # deduped

    def test_reveal_idempotent(self, db_session):
        """Running reveal twice doesn't create duplicate contacts."""
        user = _make_user(db_session)
        p = _make_prospect(
            db_session,
            name="Idempotent Corp",
            domain="idempotent.com",
            enrichment_data={
                "contacts_full": [
                    {"name": "Jane", "title": "Buyer", "email": "jane@idempotent.com"},
                ]
            },
        )
        claim_prospect(p.id, user.id, db_session)
        db_session.refresh(p)

        contacts1 = reveal_contacts(p, db_session)
        contacts2 = reveal_contacts(p, db_session)

        assert len(contacts1) == 1
        assert len(contacts2) == 0  # already created


class TestScoringEdgeCases:
    def test_score_with_all_none_fields(self):
        """Scoring handles completely empty prospect data."""
        data = {"name": None, "industry": None, "naics_code": None, "employee_count_range": None, "region": None}
        fit, reasoning = calculate_fit_score(data)
        assert isinstance(fit, (int, float))
        assert fit >= 0

    def test_readiness_with_empty_signals(self):
        """Readiness scoring handles empty signal dict."""
        data = {"name": "Test", "industry": "Electronics", "region": "US"}
        score, breakdown = calculate_readiness_score(data, {})
        assert isinstance(score, (int, float))
        assert score >= 0
