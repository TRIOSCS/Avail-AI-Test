"""Tests for the prospect_suggested router — Suggested Accounts (Phase 6).

Covers: list_suggested, suggested_stats, get_suggested_detail,
        claim_suggested, dismiss_suggested.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.prospect_account import ProspectAccount


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

    def test_claim_no_comma_in_hq(self, client, db_session):
        """When hq_location has no comma, hq_city is None."""
        p = _make_prospect(db_session, name="NoComma", domain="nocomma.com", hq_location="Austin")
        client.post(f"/api/prospects/suggested/{p.id}/claim")
        db_session.refresh(p)
        company = db_session.get(Company, p.company_id)
        assert company.hq_city is None


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
