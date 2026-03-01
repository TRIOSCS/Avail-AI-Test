"""Tests for the prospect_suggested router — Suggested Accounts (Phase 6+7).

Covers: list_suggested, suggested_stats, get_suggested_detail,
        claim_suggested (enhanced), dismiss_suggested,
        enrichment_status, add_prospect, list_batches,
        prospect_claim service (claim, reveal_contacts, briefing, manual add).
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.discovery_batch import DiscoveryBatch
from app.models.prospect_account import ProspectAccount
from app.services.prospect_claim import (
    add_prospect_manually,
    check_enrichment_status,
    claim_prospect,
    reveal_contacts,
    _template_briefing,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_prospect(db: Session, **overrides) -> ProspectAccount:
    """Create a ProspectAccount with sensible defaults."""
    defaults = {
        "name": "Test Corp",
        "domain": "testcorp.com",
        "industry": "Electronics",
        "region": "US",
        "fit_score": 60,
        "readiness_score": 50,
        "status": "suggested",
        "discovery_source": "explorium",
        "readiness_signals": {},
        "contacts_preview": [],
        "similar_customers": [],
    }
    defaults.update(overrides)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ── list_suggested ───────────────────────────────────────────────────────

class TestListSuggested:

    def test_empty_list(self, client, db_session):
        resp = client.get("/api/prospects/suggested")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1

    def test_returns_suggested_only(self, client, db_session):
        _make_prospect(db_session, name="Sugg", domain="sugg.com", status="suggested")
        _make_prospect(db_session, name="Claimed", domain="claimed.com", status="claimed")
        _make_prospect(db_session, name="Dismissed", domain="dismissed.com", status="dismissed")
        resp = client.get("/api/prospects/suggested")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Sugg"

    def test_search_by_name(self, client, db_session):
        _make_prospect(db_session, name="Acme Widgets", domain="acme.com")
        _make_prospect(db_session, name="Beta Chips", domain="beta.com")
        resp = client.get("/api/prospects/suggested?search=acme")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Acme Widgets"

    def test_search_by_domain(self, client, db_session):
        _make_prospect(db_session, name="Foo", domain="foo-electronics.com")
        _make_prospect(db_session, name="Bar", domain="bar-tech.com")
        resp = client.get("/api/prospects/suggested?search=foo-elec")
        assert resp.json()["total"] == 1

    def test_search_by_industry(self, client, db_session):
        _make_prospect(db_session, name="A", domain="a.com", industry="Semiconductor")
        _make_prospect(db_session, name="B", domain="b.com", industry="Automotive")
        resp = client.get("/api/prospects/suggested?search=semicon")
        assert resp.json()["total"] == 1

    def test_filter_region(self, client, db_session):
        _make_prospect(db_session, name="US Co", domain="us.com", region="US")
        _make_prospect(db_session, name="EU Co", domain="eu.com", region="EU")
        resp = client.get("/api/prospects/suggested?region=US")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "US Co"

    def test_filter_industry(self, client, db_session):
        _make_prospect(db_session, name="Elec", domain="elec.com", industry="Electronics")
        _make_prospect(db_session, name="Auto", domain="auto.com", industry="Automotive")
        resp = client.get("/api/prospects/suggested?industry=Electronics")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Elec"

    def test_filter_min_fit_score(self, client, db_session):
        _make_prospect(db_session, name="Low", domain="low.com", fit_score=30)
        _make_prospect(db_session, name="High", domain="high.com", fit_score=80)
        resp = client.get("/api/prospects/suggested?min_fit_score=50")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "High"

    def test_filter_readiness_call_now(self, client, db_session):
        _make_prospect(db_session, name="Hot", domain="hot.com", readiness_score=80)
        _make_prospect(db_session, name="Warm", domain="warm.com", readiness_score=50)
        _make_prospect(db_session, name="Cold", domain="cold.com", readiness_score=20)
        resp = client.get("/api/prospects/suggested?readiness_level=call_now")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Hot"

    def test_filter_readiness_nurture(self, client, db_session):
        _make_prospect(db_session, name="Hot", domain="hot.com", readiness_score=80)
        _make_prospect(db_session, name="Warm", domain="warm.com", readiness_score=50)
        _make_prospect(db_session, name="Cold", domain="cold.com", readiness_score=20)
        resp = client.get("/api/prospects/suggested?readiness_level=nurture")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Warm"

    def test_filter_readiness_monitor(self, client, db_session):
        _make_prospect(db_session, name="Hot", domain="hot.com", readiness_score=80)
        _make_prospect(db_session, name="Cold", domain="cold.com", readiness_score=20)
        resp = client.get("/api/prospects/suggested?readiness_level=monitor")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Cold"

    def test_sort_readiness_desc(self, client, db_session):
        _make_prospect(db_session, name="Low", domain="low.com", readiness_score=20)
        _make_prospect(db_session, name="High", domain="high.com", readiness_score=90)
        resp = client.get("/api/prospects/suggested?sort=readiness_desc")
        items = resp.json()["items"]
        assert items[0]["name"] == "High"
        assert items[1]["name"] == "Low"

    def test_sort_fit_desc(self, client, db_session):
        _make_prospect(db_session, name="Low Fit", domain="lf.com", fit_score=20)
        _make_prospect(db_session, name="High Fit", domain="hf.com", fit_score=90)
        resp = client.get("/api/prospects/suggested?sort=fit_desc")
        items = resp.json()["items"]
        assert items[0]["name"] == "High Fit"

    def test_sort_name_asc(self, client, db_session):
        _make_prospect(db_session, name="Zeta", domain="zeta.com")
        _make_prospect(db_session, name="Alpha", domain="alpha.com")
        resp = client.get("/api/prospects/suggested?sort=name_asc")
        items = resp.json()["items"]
        assert items[0]["name"] == "Alpha"
        assert items[1]["name"] == "Zeta"

    def test_sort_composite_desc(self, client, db_session):
        # composite = 0.6 * fit + 0.4 * readiness
        # a: 0.6*100 + 0.4*0 = 60
        # b: 0.6*0 + 0.4*100 = 40
        _make_prospect(db_session, name="A", domain="a.com", fit_score=100, readiness_score=0)
        _make_prospect(db_session, name="B", domain="b.com", fit_score=0, readiness_score=100)
        resp = client.get("/api/prospects/suggested?sort=composite_desc")
        items = resp.json()["items"]
        assert items[0]["name"] == "A"

    def test_pagination(self, client, db_session):
        for i in range(25):
            _make_prospect(db_session, name=f"Co-{i:03d}", domain=f"co{i}.com", readiness_score=100 - i)
        resp = client.get("/api/prospects/suggested?page=2&per_page=10")
        data = resp.json()
        assert data["page"] == 2
        assert data["per_page"] == 10
        assert len(data["items"]) == 10
        assert data["total"] == 25

    def test_per_page_clamped(self, client, db_session):
        _make_prospect(db_session, name="X", domain="x.com")
        resp = client.get("/api/prospects/suggested?per_page=999")
        data = resp.json()
        assert data["per_page"] == 100

    def test_card_serialization(self, client, db_session):
        _make_prospect(
            db_session,
            name="Full Card",
            domain="full.com",
            website="https://full.com",
            industry="Electronics",
            employee_count_range="51-200",
            revenue_range="$10M-$50M",
            hq_location="Austin, TX",
            region="US",
            fit_score=75,
            readiness_score=80,
            readiness_signals={
                "intent": {"strength": "strong"},
                "hiring": {"type": "engineering"},
            },
            contacts_preview=[
                {"name": "Jane Doe", "title": "VP Sales", "verified": True, "seniority": "decision_maker"},
                {"name": "Bob Smith", "title": "Engineer", "verified": False, "seniority": "executor"},
            ],
            similar_customers=[{"name": "Existing Co"}],
            ai_writeup="Strong fit due to industry alignment.",
            discovery_source="explorium",
            import_priority="priority",
        )
        resp = client.get("/api/prospects/suggested")
        item = resp.json()["items"][0]
        assert item["name"] == "Full Card"
        assert item["fit_score"] == 75
        assert item["readiness_score"] == 80
        assert item["readiness_tier"] == "call_now"
        assert len(item["signal_tags"]) == 2
        assert item["contacts_count"] == 2
        assert item["contacts_verified"] == 1
        assert item["contacts_decision_makers"] == 1
        assert len(item["contacts_preview"]) == 2
        assert item["similar_customers"] == [{"name": "Existing Co"}]
        assert item["ai_writeup"] == "Strong fit due to industry alignment."
        assert item["import_priority"] == "priority"

    def test_status_filter_claimed(self, client, db_session):
        _make_prospect(db_session, name="Sugg", domain="sugg.com", status="suggested")
        _make_prospect(db_session, name="Claim", domain="claim.com", status="claimed")
        resp = client.get("/api/prospects/suggested?status=claimed")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Claim"

    def test_search_strips_whitespace(self, client, db_session):
        _make_prospect(db_session, name="Target Co", domain="target.com")
        _make_prospect(db_session, name="Other", domain="other.com")
        resp = client.get("/api/prospects/suggested?search=%20Target%20")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Target Co"

    def test_combined_filters(self, client, db_session):
        _make_prospect(db_session, name="Match", domain="match.com", region="US", fit_score=80, readiness_score=75)
        _make_prospect(db_session, name="Wrong Region", domain="wr.com", region="EU", fit_score=80, readiness_score=75)
        _make_prospect(db_session, name="Low Fit", domain="lf.com", region="US", fit_score=30, readiness_score=75)
        resp = client.get("/api/prospects/suggested?region=US&min_fit_score=50&readiness_level=call_now")
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Match"


# ── suggested_stats ──────────────────────────────────────────────────────

class TestSuggestedStats:

    def test_empty_stats(self, client, db_session):
        resp = client.get("/api/prospects/suggested/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_available"] == 0
        assert data["call_now_count"] == 0
        assert data["nurture_count"] == 0
        assert data["high_fit_count"] == 0
        assert data["claimed_this_month"] == 0

    def test_stats_counts(self, client, db_session):
        _make_prospect(db_session, name="Hot", domain="hot.com", readiness_score=80, fit_score=90)
        _make_prospect(db_session, name="Warm", domain="warm.com", readiness_score=50, fit_score=30)
        _make_prospect(db_session, name="Cold", domain="cold.com", readiness_score=20, fit_score=20)
        resp = client.get("/api/prospects/suggested/stats")
        data = resp.json()
        assert data["total_available"] == 3
        assert data["call_now_count"] == 1
        assert data["nurture_count"] == 1
        assert data["high_fit_count"] == 1

    def test_claimed_this_month(self, client, db_session, test_user):
        p = _make_prospect(db_session, name="Claimed", domain="claimed.com", status="claimed")
        p.claimed_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.get("/api/prospects/suggested/stats")
        data = resp.json()
        assert data["claimed_this_month"] == 1

    def test_excludes_non_suggested_from_available(self, client, db_session):
        _make_prospect(db_session, name="Sugg", domain="sugg.com", status="suggested")
        _make_prospect(db_session, name="Claimed", domain="claimed.com", status="claimed")
        resp = client.get("/api/prospects/suggested/stats")
        data = resp.json()
        assert data["total_available"] == 1


# ── get_suggested_detail ─────────────────────────────────────────────────

class TestGetSuggestedDetail:

    def test_returns_full_detail(self, client, db_session):
        p = _make_prospect(
            db_session,
            name="Detail Co",
            domain="detail.com",
            fit_reasoning="Good ICP match",
            enrichment_data={"tech_stack": ["SAP"]},
            historical_context={"last_owner": "Mike"},
        )
        resp = client.get(f"/api/prospects/suggested/{p.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Detail Co"
        assert data["fit_reasoning"] == "Good ICP match"
        assert data["enrichment_data"]["tech_stack"] == ["SAP"]
        assert data["historical_context"]["last_owner"] == "Mike"

    def test_not_found(self, client, db_session):
        resp = client.get("/api/prospects/suggested/99999")
        assert resp.status_code == 404

    def test_includes_card_fields(self, client, db_session):
        p = _make_prospect(db_session, name="Card Fields", domain="cf.com", fit_score=65)
        resp = client.get(f"/api/prospects/suggested/{p.id}")
        data = resp.json()
        assert "readiness_tier" in data
        assert "signal_tags" in data
        assert "contacts_count" in data


# ── claim_suggested ──────────────────────────────────────────────────────

class TestClaimSuggested:

    def test_claim_new_discovery(self, client, db_session, test_user):
        """Claiming a prospect with no company_id creates a new Company."""
        p = _make_prospect(db_session, name="New Disc", domain="newdisc.com", website="https://newdisc.com", industry="Tech")
        resp = client.post(f"/api/prospects/suggested/{p.id}/claim")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "claimed"
        assert data["company_name"] == "New Disc"
        assert data["company_id"] is not None
        assert data["path"] == "new_company"
        assert data["enrichment_status"] == "pending"

        # Verify prospect updated
        db_session.refresh(p)
        assert p.status == "claimed"
        assert p.claimed_by == test_user.id
        assert p.claimed_at is not None
        assert p.company_id is not None

        # Verify Company created
        company = db_session.get(Company, p.company_id)
        assert company is not None
        assert company.name == "New Disc"
        assert company.account_owner_id == test_user.id
        assert company.source == "prospecting"

    def test_claim_sf_migrated(self, client, db_session, test_user):
        """Claiming a prospect with company_id updates the existing Company."""
        company = Company(
            name="SF Co",
            domain="sfco.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(company)
        db_session.flush()
        p = _make_prospect(db_session, name="SF Co", domain="sfco.com", company_id=company.id)
        resp = client.post(f"/api/prospects/suggested/{p.id}/claim")
        assert resp.status_code == 200
        assert resp.json()["path"] == "existing_company"

        db_session.refresh(company)
        assert company.account_owner_id == test_user.id

    def test_claim_already_claimed(self, client, db_session):
        p = _make_prospect(db_session, name="Already", domain="already.com", status="claimed")
        resp = client.post(f"/api/prospects/suggested/{p.id}/claim")
        assert resp.status_code == 409

    def test_claim_not_found(self, client, db_session):
        resp = client.post("/api/prospects/suggested/99999/claim")
        assert resp.status_code == 404

    def test_claim_creates_company_with_hq_city(self, client, db_session):
        """When hq_location has a comma, first part becomes hq_city."""
        p = _make_prospect(db_session, name="HQ Test", domain="hqtest.com", hq_location="Austin, TX")
        client.post(f"/api/prospects/suggested/{p.id}/claim")
        db_session.refresh(p)
        company = db_session.get(Company, p.company_id)
        assert company.hq_city == "Austin"
        assert company.hq_state == "TX"

    def test_claim_no_comma_in_hq(self, client, db_session):
        """When hq_location has no comma, hq_city is None."""
        p = _make_prospect(db_session, name="NoComma", domain="nocomma.com", hq_location="Austin")
        client.post(f"/api/prospects/suggested/{p.id}/claim")
        db_session.refresh(p)
        company = db_session.get(Company, p.company_id)
        assert company.hq_city is None

    def test_claim_domain_collision(self, client, db_session, test_user):
        """If Company with same domain exists, link to it instead of creating new."""
        existing = Company(
            name="Existing Corp",
            domain="collision.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing)
        db_session.commit()

        p = _make_prospect(db_session, name="New Corp", domain="collision.com")
        resp = client.post(f"/api/prospects/suggested/{p.id}/claim")
        assert resp.status_code == 200
        data = resp.json()
        assert data["path"] == "domain_collision"
        assert data["company_id"] == existing.id
        assert "warning" in data

        db_session.refresh(existing)
        assert existing.account_owner_id == test_user.id

    def test_claim_dismissed_prospect(self, client, db_session):
        """Cannot claim a dismissed prospect."""
        p = _make_prospect(db_session, name="Dismissed", domain="dismissed.com", status="dismissed")
        resp = client.post(f"/api/prospects/suggested/{p.id}/claim")
        assert resp.status_code == 409

    def test_claim_sets_enrichment_pending(self, client, db_session):
        """After claim, enrichment_data has claim_enrichment_status=pending."""
        p = _make_prospect(db_session, name="Enrich", domain="enrich.com")
        client.post(f"/api/prospects/suggested/{p.id}/claim")
        db_session.refresh(p)
        ed = p.enrichment_data or {}
        assert ed.get("claim_enrichment_status") == "pending"


# ── dismiss_suggested ────────────────────────────────────────────────────

class TestDismissSuggested:

    def test_dismiss_with_reason(self, client, db_session, test_user):
        p = _make_prospect(db_session, name="Dismiss Me", domain="dismiss.com")
        resp = client.post(
            f"/api/prospects/suggested/{p.id}/dismiss",
            json={"reason": "competitor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dismissed"
        assert data["company_name"] == "Dismiss Me"

        db_session.refresh(p)
        assert p.status == "dismissed"
        assert p.dismissed_by == test_user.id
        assert p.dismissed_at is not None
        assert p.dismiss_reason == "competitor"

    def test_dismiss_default_reason(self, client, db_session):
        p = _make_prospect(db_session, name="No Reason", domain="noreason.com")
        resp = client.post(
            f"/api/prospects/suggested/{p.id}/dismiss",
            json={},
        )
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.dismiss_reason == "other"

    def test_dismiss_not_suggested(self, client, db_session):
        p = _make_prospect(db_session, name="Claimed", domain="cl.com", status="claimed")
        resp = client.post(
            f"/api/prospects/suggested/{p.id}/dismiss",
            json={"reason": "not_relevant"},
        )
        assert resp.status_code == 409

    def test_dismiss_not_found(self, client, db_session):
        resp = client.post(
            "/api/prospects/suggested/99999/dismiss",
            json={"reason": "other"},
        )
        assert resp.status_code == 404


# ── Serialization edge cases ────────────────────────────────────────────

class TestSerializationEdgeCases:

    def test_null_scores_default_zero(self, client, db_session):
        _make_prospect(db_session, name="Null Scores", domain="null.com", fit_score=None, readiness_score=None)
        resp = client.get("/api/prospects/suggested")
        item = resp.json()["items"][0]
        assert item["fit_score"] == 0
        assert item["readiness_score"] == 0

    def test_readiness_tier_boundaries(self, client, db_session):
        """Test exact boundary values: 70 = call_now, 40 = nurture, 39 = monitor."""
        _make_prospect(db_session, name="Exact70", domain="e70.com", readiness_score=70)
        _make_prospect(db_session, name="Exact40", domain="e40.com", readiness_score=40)
        _make_prospect(db_session, name="Exact39", domain="e39.com", readiness_score=39)
        resp = client.get("/api/prospects/suggested?sort=readiness_desc")
        items = resp.json()["items"]
        tiers = {i["name"]: i["readiness_tier"] for i in items}
        assert tiers["Exact70"] == "call_now"
        assert tiers["Exact40"] == "nurture"
        assert tiers["Exact39"] == "monitor"

    def test_signal_tags_intent_moderate(self, client, db_session):
        _make_prospect(
            db_session, name="Intent", domain="intent.com",
            readiness_signals={"intent": {"strength": "moderate"}},
        )
        resp = client.get("/api/prospects/suggested")
        tags = resp.json()["items"][0]["signal_tags"]
        assert any(t["type"] == "intent" and "moderate" in t["label"] for t in tags)

    def test_signal_tags_weak_intent_excluded(self, client, db_session):
        _make_prospect(
            db_session, name="Weak", domain="weak.com",
            readiness_signals={"intent": {"strength": "weak"}},
        )
        resp = client.get("/api/prospects/suggested")
        tags = resp.json()["items"][0]["signal_tags"]
        assert not any(t["type"] == "intent" for t in tags)

    def test_signal_tags_events(self, client, db_session):
        _make_prospect(
            db_session, name="Events", domain="events.com",
            readiness_signals={"events": [{"type": "Acquisition"}, {"type": "Expansion"}]},
        )
        resp = client.get("/api/prospects/suggested")
        tags = resp.json()["items"][0]["signal_tags"]
        event_tag = next(t for t in tags if t["type"] == "event")
        assert "Acquisition" in event_tag["label"]

    def test_contacts_preview_limited_to_3(self, client, db_session):
        contacts = [{"name": f"C{i}", "verified": True, "seniority": "other"} for i in range(5)]
        _make_prospect(db_session, name="Many", domain="many.com", contacts_preview=contacts)
        resp = client.get("/api/prospects/suggested")
        item = resp.json()["items"][0]
        assert item["contacts_count"] == 5
        assert len(item["contacts_preview"]) == 3

    def test_similar_customers_limited_to_3(self, client, db_session):
        similar = [{"name": f"Sim{i}"} for i in range(5)]
        _make_prospect(db_session, name="Sim", domain="sim.com", similar_customers=similar)
        resp = client.get("/api/prospects/suggested")
        item = resp.json()["items"][0]
        assert len(item["similar_customers"]) == 3

    def test_empty_signals_no_tags(self, client, db_session):
        _make_prospect(db_session, name="Empty", domain="empty.com", readiness_signals={})
        resp = client.get("/api/prospects/suggested")
        item = resp.json()["items"][0]
        assert item["signal_tags"] == []

    def test_null_contacts_preview(self, client, db_session):
        _make_prospect(db_session, name="NullC", domain="nullc.com", contacts_preview=None)
        resp = client.get("/api/prospects/suggested")
        item = resp.json()["items"][0]
        assert item["contacts_count"] == 0
        assert item["contacts_preview"] == []


# ══════════════════════════════════════════════════════════════════════
# Phase 7 Tests — Claim Service, Contact Reveal, Enrichment, Briefing
# ══════════════════════════════════════════════════════════════════════


# ── claim_prospect service ───────────────────────────────────────────

class TestClaimProspectService:

    def test_claim_new_company_path(self, db_session, test_user):
        p = _make_prospect(db_session, name="Svc Test", domain="svctest.com", industry="Tech")
        result = claim_prospect(p.id, test_user.id, db_session)
        assert result["path"] == "new_company"
        assert result["status"] == "claimed"
        assert result["enrichment_status"] == "pending"
        assert result["company_id"] is not None

    def test_claim_existing_company_path(self, db_session, test_user):
        co = Company(name="Existing", domain="existing.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        p = _make_prospect(db_session, name="Existing", domain="existing.com", company_id=co.id)
        result = claim_prospect(p.id, test_user.id, db_session)
        assert result["path"] == "existing_company"

    def test_claim_domain_collision_path(self, db_session, test_user):
        co = Company(name="Old Corp", domain="dup.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()
        p = _make_prospect(db_session, name="New Corp", domain="dup.com")
        result = claim_prospect(p.id, test_user.id, db_session)
        assert result["path"] == "domain_collision"
        assert result["company_id"] == co.id
        assert "warning" in result

    def test_claim_already_claimed_raises(self, db_session, test_user):
        p = _make_prospect(db_session, name="Dup", domain="dup-claim.com", status="claimed")
        with pytest.raises(ValueError, match="Already claimed"):
            claim_prospect(p.id, test_user.id, db_session)

    def test_claim_dismissed_raises(self, db_session, test_user):
        p = _make_prospect(db_session, name="Dis", domain="dis.com", status="dismissed")
        with pytest.raises(ValueError, match="Cannot claim"):
            claim_prospect(p.id, test_user.id, db_session)

    def test_claim_not_found_raises(self, db_session, test_user):
        with pytest.raises(LookupError, match="Prospect not found"):
            claim_prospect(99999, test_user.id, db_session)

    def test_claim_user_not_found_raises(self, db_session):
        p = _make_prospect(db_session, name="NoUser", domain="nouser.com")
        with pytest.raises(LookupError, match="User not found"):
            claim_prospect(p.id, 99999, db_session)

    def test_claim_sets_hq_state(self, db_session, test_user):
        p = _make_prospect(db_session, name="State", domain="state.com", hq_location="Dallas, TX")
        claim_prospect(p.id, test_user.id, db_session)
        db_session.refresh(p)
        co = db_session.get(Company, p.company_id)
        assert co.hq_city == "Dallas"
        assert co.hq_state == "TX"

    def test_claim_sets_employee_size(self, db_session, test_user):
        p = _make_prospect(db_session, name="Sized", domain="sized.com", employee_count_range="51-200")
        claim_prospect(p.id, test_user.id, db_session)
        db_session.refresh(p)
        co = db_session.get(Company, p.company_id)
        assert co.employee_size == "51-200"

    def test_claim_no_domain_skips_collision_check(self, db_session, test_user):
        """If domain is empty, no collision check is done."""
        p = ProspectAccount(
            name="No Domain", domain="nodomain.com",
            discovery_source="manual", status="suggested",
        )
        db_session.add(p)
        db_session.commit()
        db_session.refresh(p)
        result = claim_prospect(p.id, test_user.id, db_session)
        assert result["path"] == "new_company"


# ── reveal_contacts service ──────────────────────────────────────────

class TestRevealContacts:

    def test_reveal_creates_site_and_contacts(self, db_session, test_user):
        co = Company(name="Rev Co", domain="rev.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        p = _make_prospect(
            db_session, name="Rev Co", domain="rev.com", company_id=co.id,
            enrichment_data={
                "contacts_full": [
                    {"name": "Jane VP", "title": "VP Procurement", "email": "jane@rev.com", "verified": True, "seniority": "decision_maker"},
                    {"name": "Bob Buyer", "title": "Buyer", "email": "bob@rev.com", "verified": True, "seniority": "executor"},
                ],
            },
        )
        created = reveal_contacts(p, db_session)
        assert len(created) == 2
        assert created[0]["email"] == "jane@rev.com"
        assert created[0]["seniority"] == "decision_maker"

        # Verify DB records
        site = db_session.query(CustomerSite).filter_by(company_id=co.id).first()
        assert site is not None
        assert "HQ" in site.site_name
        contacts = db_session.query(SiteContact).filter_by(customer_site_id=site.id).all()
        assert len(contacts) == 2
        assert contacts[0].is_primary is True
        assert contacts[1].is_primary is False

    def test_reveal_no_company_id(self, db_session):
        p = _make_prospect(db_session, name="No Co", domain="noco.com")
        result = reveal_contacts(p, db_session)
        assert result == []

    def test_reveal_no_contacts_full(self, db_session, test_user):
        co = Company(name="Empty Co", domain="empty.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        p = _make_prospect(db_session, name="Empty Co", domain="empty.com", company_id=co.id, enrichment_data={})
        result = reveal_contacts(p, db_session)
        assert result == []

    def test_reveal_deduplicates_emails(self, db_session, test_user):
        co = Company(name="Dedup Co", domain="dedup.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()

        # Pre-create a site with existing contact
        site = CustomerSite(company_id=co.id, site_name="Dedup Co - HQ", is_active=True)
        db_session.add(site)
        db_session.flush()
        sc = SiteContact(customer_site_id=site.id, full_name="Jane VP", email="jane@dedup.com")
        db_session.add(sc)
        db_session.commit()

        p = _make_prospect(
            db_session, name="Dedup Co", domain="dedup.com", company_id=co.id,
            enrichment_data={
                "contacts_full": [
                    {"name": "Jane VP", "email": "jane@dedup.com", "verified": True},
                    {"name": "New Person", "email": "new@dedup.com", "verified": True},
                ],
            },
        )
        created = reveal_contacts(p, db_session)
        assert len(created) == 1
        assert created[0]["email"] == "new@dedup.com"

    def test_reveal_uses_existing_site(self, db_session, test_user):
        co = Company(name="Site Co", domain="site.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="Existing Site", is_active=True)
        db_session.add(site)
        db_session.commit()

        p = _make_prospect(
            db_session, name="Site Co", domain="site.com", company_id=co.id,
            enrichment_data={
                "contacts_full": [{"name": "Test", "email": "test@site.com", "verified": True}],
            },
        )
        reveal_contacts(p, db_session)
        sites = db_session.query(CustomerSite).filter_by(company_id=co.id).all()
        assert len(sites) == 1  # Didn't create a duplicate

    def test_reveal_skips_empty_emails(self, db_session, test_user):
        co = Company(name="Skip Co", domain="skip.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        p = _make_prospect(
            db_session, name="Skip Co", domain="skip.com", company_id=co.id,
            enrichment_data={
                "contacts_full": [
                    {"name": "No Email", "email": "", "verified": False},
                    {"name": "Null Email", "email": None, "verified": False},
                    {"name": "Has Email", "email": "has@skip.com", "verified": True},
                ],
            },
        )
        created = reveal_contacts(p, db_session)
        assert len(created) == 1
        assert created[0]["email"] == "has@skip.com"


# ── enrichment_status endpoint ───────────────────────────────────────

class TestEnrichmentStatus:

    def test_status_none(self, client, db_session):
        p = _make_prospect(db_session, name="NoEnrich", domain="noenrich.com")
        resp = client.get(f"/api/prospects/suggested/{p.id}/enrichment")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "none"
        assert data["contacts_created"] == 0
        assert data["briefing_ready"] is False

    def test_status_pending(self, client, db_session, test_user):
        p = _make_prospect(
            db_session, name="Pending", domain="pending.com",
            enrichment_data={"claim_enrichment_status": "pending"},
        )
        resp = client.get(f"/api/prospects/suggested/{p.id}/enrichment")
        assert resp.json()["status"] == "pending"

    def test_status_complete(self, client, db_session):
        p = _make_prospect(
            db_session, name="Done", domain="done.com",
            enrichment_data={
                "claim_enrichment_status": "complete",
                "contacts_created_count": 3,
                "briefing": "Full briefing text here.",
            },
        )
        resp = client.get(f"/api/prospects/suggested/{p.id}/enrichment")
        data = resp.json()
        assert data["status"] == "complete"
        assert data["contacts_created"] == 3
        assert data["briefing_ready"] is True

    def test_status_failed(self, client, db_session):
        p = _make_prospect(
            db_session, name="Failed", domain="failed.com",
            enrichment_data={
                "claim_enrichment_status": "failed",
                "enrichment_error": "API timeout",
            },
        )
        resp = client.get(f"/api/prospects/suggested/{p.id}/enrichment")
        data = resp.json()
        assert data["status"] == "failed"
        assert data["error"] == "API timeout"

    def test_status_not_found(self, client, db_session):
        resp = client.get("/api/prospects/suggested/99999/enrichment")
        assert resp.status_code == 404


# ── check_enrichment_status service ──────────────────────────────────

class TestCheckEnrichmentStatusService:

    def test_not_found_raises(self, db_session):
        with pytest.raises(LookupError):
            check_enrichment_status(99999, db_session)

    def test_returns_correct_fields(self, db_session):
        p = _make_prospect(
            db_session, name="Check", domain="check.com",
            enrichment_data={
                "claim_enrichment_status": "enriching",
                "contacts_created_count": 2,
                "briefing": "Some text",
            },
        )
        result = check_enrichment_status(p.id, db_session)
        assert result["status"] == "enriching"
        assert result["contacts_created"] == 2
        assert result["briefing_ready"] is True
        assert result["error"] is None


# ── add_prospect endpoint ────────────────────────────────────────────

class TestAddProspect:

    def test_add_new_domain(self, client, db_session):
        resp = client.post("/api/prospects/add", json={"domain": "newcompany.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_new"] is True
        assert data["domain"] == "newcompany.com"
        assert data["status"] == "suggested"
        assert data["name"] == "Newcompany"

    def test_add_duplicate_domain(self, client, db_session):
        _make_prospect(db_session, name="Existing", domain="existingdomain.com")
        resp = client.post("/api/prospects/add", json={"domain": "existingdomain.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_new"] is False
        assert data["name"] == "Existing"

    def test_add_empty_domain(self, client, db_session):
        resp = client.post("/api/prospects/add", json={"domain": ""})
        assert resp.status_code == 400

    def test_add_no_domain_key(self, client, db_session):
        resp = client.post("/api/prospects/add", json={})
        assert resp.status_code == 400

    def test_add_normalizes_domain(self, client, db_session):
        resp = client.post("/api/prospects/add", json={"domain": "  MyCompany.COM  "})
        assert resp.status_code == 200
        assert resp.json()["domain"] == "mycompany.com"

    def test_add_stores_submitted_by(self, client, db_session, test_user):
        resp = client.post("/api/prospects/add", json={"domain": "submitted.com"})
        assert resp.status_code == 200
        p = db_session.query(ProspectAccount).filter_by(domain="submitted.com").first()
        ed = p.enrichment_data or {}
        assert ed.get("submitted_by") == test_user.id


# ── add_prospect_manually service ────────────────────────────────────

class TestAddProspectManuallyService:

    def test_creates_prospect(self, db_session, test_user):
        result = add_prospect_manually("brand-new.com", test_user.id, db_session)
        assert result["is_new"] is True
        assert result["domain"] == "brand-new.com"
        assert result["name"] == "Brand New"

    def test_deduplicates(self, db_session, test_user):
        _make_prospect(db_session, name="Dupe", domain="dupetest.com")
        result = add_prospect_manually("dupetest.com", test_user.id, db_session)
        assert result["is_new"] is False

    def test_empty_domain_raises(self, db_session, test_user):
        with pytest.raises(ValueError, match="Domain is required"):
            add_prospect_manually("", test_user.id, db_session)

    def test_whitespace_only_raises(self, db_session, test_user):
        with pytest.raises(ValueError, match="Domain is required"):
            add_prospect_manually("   ", test_user.id, db_session)


# ── list_batches endpoint ────────────────────────────────────────────

class TestListBatches:

    def test_empty_batches(self, client, db_session):
        resp = client.get("/api/prospects/batches")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_returns_batches(self, client, db_session):
        b = DiscoveryBatch(
            batch_id="batch-001",
            source="explorium",
            segment="Aerospace",
            regions=["US"],
            status="complete",
            prospects_found=10,
            prospects_new=8,
            credits_used=5,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(b)
        db_session.commit()

        resp = client.get("/api/prospects/batches")
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["batch_id"] == "batch-001"
        assert item["source"] == "explorium"
        assert item["prospects_found"] == 10

    def test_batches_pagination(self, client, db_session):
        for i in range(5):
            b = DiscoveryBatch(
                batch_id=f"batch-{i:03d}",
                source="explorium",
                status="complete",
                started_at=datetime.now(timezone.utc),
            )
            db_session.add(b)
        db_session.commit()

        resp = client.get("/api/prospects/batches?page=1&per_page=2")
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["page"] == 1


# ── template_briefing ────────────────────────────────────────────────

class TestTemplateBriefing:

    def test_basic_briefing(self, db_session):
        p = _make_prospect(
            db_session, name="Brief Co", domain="brief.com",
            industry="Aerospace", employee_count_range="201-500",
            hq_location="Phoenix, AZ", fit_score=80, readiness_score=65,
            ai_writeup="Good ICP match.",
        )
        signals = {"intent": {"strength": "strong"}, "hiring": {"type": "engineering"}}
        similar = [{"name": "Boeing"}, {"name": "Raytheon"}]
        result = _template_briefing(p, signals, similar)
        assert "Brief Co" in result
        assert "Aerospace" in result
        assert "strong" in result
        assert "engineering" in result
        assert "Boeing" in result

    def test_briefing_with_empty_signals(self, db_session):
        p = _make_prospect(db_session, name="Minimal", domain="minimal.com")
        result = _template_briefing(p, {}, [])
        assert "Minimal" in result
        assert "Not specified" in result

    def test_briefing_with_ai_writeup(self, db_session):
        p = _make_prospect(
            db_session, name="Writeup Co", domain="writeup.com",
            ai_writeup="This is a detailed analysis.",
        )
        result = _template_briefing(p, {}, [])
        assert "detailed analysis" in result


# ── generate_account_briefing (async, mocked AI) ────────────────────

class TestGenerateAccountBriefing:

    @pytest.mark.asyncio
    async def test_ai_briefing_success(self, db_session):
        from app.services.prospect_claim import generate_account_briefing

        p = _make_prospect(
            db_session, name="AI Co", domain="aico.com",
            industry="Electronics", fit_score=80, readiness_score=70,
        )
        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value="AI-generated briefing for AI Co.",
        ):
            result = await generate_account_briefing(p.id, db_session)
        assert result == "AI-generated briefing for AI Co."

    @pytest.mark.asyncio
    async def test_ai_briefing_fallback(self, db_session):
        from app.services.prospect_claim import generate_account_briefing

        p = _make_prospect(
            db_session, name="Fallback Co", domain="fallback.com",
            industry="Aerospace", fit_score=60, readiness_score=40,
        )
        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await generate_account_briefing(p.id, db_session)
        assert "Fallback Co" in result
        assert "Aerospace" in result

    @pytest.mark.asyncio
    async def test_ai_briefing_returns_none_for_missing(self, db_session):
        from app.services.prospect_claim import generate_account_briefing
        result = await generate_account_briefing(99999, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_ai_briefing_null_return_fallback(self, db_session):
        from app.services.prospect_claim import generate_account_briefing

        p = _make_prospect(
            db_session, name="Null AI", domain="nullai.com",
        )
        with patch(
            "app.utils.claude_client.claude_text",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await generate_account_briefing(p.id, db_session)
        assert "Null AI" in result


# ── trigger_deep_enrichment_bg (async background) ───────────────────

class TestTriggerDeepEnrichmentBg:

    @pytest.mark.asyncio
    async def test_enrichment_completes(self, db_session, test_user):
        from app.services.prospect_claim import trigger_deep_enrichment_bg

        co = Company(name="Deep Co", domain="deep.com", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        p = _make_prospect(
            db_session, name="Deep Co", domain="deep.com", company_id=co.id,
            enrichment_data={
                "claim_enrichment_status": "pending",
                "contacts_full": [
                    {"name": "Jane", "email": "jane@deep.com", "verified": True, "seniority": "decision_maker", "title": "VP"},
                ],
            },
        )
        pid = p.id

        with patch(
            "app.database.SessionLocal",
            return_value=db_session,
        ), patch.object(db_session, "close"):
            with patch(
                "app.utils.claude_client.claude_text",
                new_callable=AsyncMock,
                return_value="Great prospect briefing.",
            ):
                await trigger_deep_enrichment_bg(pid)

        db_session.refresh(p)
        ed = p.enrichment_data or {}
        assert ed["claim_enrichment_status"] == "complete"
        assert ed["contacts_created_count"] == 1
        assert ed["briefing"] == "Great prospect briefing."

    @pytest.mark.asyncio
    async def test_enrichment_handles_error(self, db_session, test_user):
        from app.services.prospect_claim import trigger_deep_enrichment_bg

        p = _make_prospect(
            db_session, name="Error Co", domain="error.com",
            enrichment_data={"claim_enrichment_status": "pending"},
        )
        pid = p.id

        with patch(
            "app.database.SessionLocal",
            return_value=db_session,
        ), patch.object(db_session, "close"):
            with patch(
                "app.services.prospect_claim.reveal_contacts",
                side_effect=Exception("DB crash"),
            ):
                await trigger_deep_enrichment_bg(pid)

        db_session.refresh(p)
        ed = p.enrichment_data or {}
        assert ed["claim_enrichment_status"] == "failed"
        assert "DB crash" in ed.get("enrichment_error", "")

    @pytest.mark.asyncio
    async def test_enrichment_not_found(self, db_session):
        from app.services.prospect_claim import trigger_deep_enrichment_bg

        with patch(
            "app.database.SessionLocal",
            return_value=db_session,
        ), patch.object(db_session, "close"):
            # Should not raise, just log
            await trigger_deep_enrichment_bg(99999)


# ── Additional router coverage (lines 76-77, 85, 210, 287-308, 334-335) ──


class TestListSuggestedFilters:
    """Cover employee_size and min_readiness_score filter branches."""

    def test_filter_employee_size(self, client, db_session):
        """employee_size filter uses ilike (line 76-77)."""
        p = _make_prospect(db_session, employee_count_range="100-500")
        resp = client.get("/api/prospects/suggested?employee_size=100")
        assert resp.status_code == 200

    def test_filter_min_readiness_score(self, client, db_session):
        """min_readiness_score filter (line 85)."""
        p = _make_prospect(db_session, readiness_score=80)
        resp = client.get("/api/prospects/suggested?min_readiness_score=70")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert all(i["readiness_score"] >= 70 for i in items)


class TestClaimSiteCap:
    """Cover site cap check (line 210)."""

    def test_claim_exceeds_site_cap(self, client, db_session, test_user, test_company):
        """User over site cap -> 409."""
        # Create SITE_CAP active sites owned by test_user
        for i in range(200):
            s = CustomerSite(
                company_id=test_company.id,
                site_name=f"Site{i}",
                owner_id=test_user.id,
                is_active=True,
            )
            db_session.add(s)
        db_session.flush()

        p = _make_prospect(db_session, status="suggested")
        db_session.commit()

        resp = client.post(f"/api/prospects/suggested/{p.id}/claim")
        assert resp.status_code == 409


class TestEnrichFreeEndpoint:
    """Cover /api/prospects/suggested/{id}/enrich-free (lines 287-308)."""

    def test_enrich_free_not_found(self, client):
        resp = client.post("/api/prospects/suggested/99999/enrich-free")
        assert resp.status_code == 404

    @patch("app.services.prospect_warm_intros.generate_one_liner", return_value="Great fit!")
    @patch("app.services.prospect_warm_intros.detect_warm_intros",
           return_value={"has_warm_intro": True, "shared_vendors": ["Arrow"]})
    @patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock,
           return_value={"sam_gov": True, "news_count": 3})
    def test_enrich_free_success(self, mock_enrich, mock_warm, mock_liner, client, db_session):
        p = _make_prospect(db_session, status="claimed")
        resp = client.post(f"/api/prospects/suggested/{p.id}/enrich-free")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sam_gov"] is True
        assert data["news_count"] == 3
        assert data["has_warm_intro"] is True
        assert data["one_liner"] == "Great fit!"


class TestAddProspectManualErrors:
    """Cover ValueError from add_prospect_manually (lines 334-335)."""

    @patch("app.routers.prospect_suggested.add_prospect_manually",
           side_effect=ValueError("Domain already tracked"))
    def test_add_prospect_duplicate(self, mock_fn, client):
        resp = client.post("/api/prospects/add", json={"domain": "dup.com"})
        assert resp.status_code == 400
