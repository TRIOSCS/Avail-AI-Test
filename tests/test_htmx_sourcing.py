"""
test_htmx_sourcing.py — Tests for Phase 3 Task 5: Sourcing results with source filters and confidence badges.
Verifies sourcing results partial, filter/sort, material card detail, sighting rows, and SSE stream endpoint.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/sourcing/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import LeadEvidence, LeadFeedbackEvent, MaterialCard, Requirement, Requisition, Sighting, SourcingLead


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/sourcing/{req_row_id}/results" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sourcing_data(db_session, test_user):
    """A requisition with a requirement and several sightings from different sources."""
    req = Requisition(
        name="Sourcing Test Req",
        customer_name="Test Corp",
        status="open",
        created_by=test_user.id,
        created_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    card = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        description="Adjustable Voltage Regulator",
        search_count=5,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        material_card_id=card.id,
        target_qty=1000,
        target_price=0.50,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    # Add sightings from different source types
    sightings_data = [
        ("brokerbin", "Arrow Electronics", 5000, 0.45, 0.85),
        ("nexar", "Digi-Key", 10000, 0.42, 0.90),
        ("vendor_affinity", "Preferred Vendor", None, None, 0.60),
        ("material_history", "Old Source", 200, 0.55, 0.30),
    ]
    for source_type, vendor, qty, price, confidence in sightings_data:
        db_session.add(Sighting(
            requirement_id=requirement.id,
            material_card_id=card.id,
            vendor_name=vendor,
            mpn_matched="LM317T",
            manufacturer="Texas Instruments",
            qty_available=qty,
            unit_price=price,
            source_type=source_type,
            confidence=confidence,
            score=confidence * 100,
            is_authorized=(source_type in ("nexar", "brokerbin")),
            created_at=datetime.now(timezone.utc),
        ))

    db_session.add(
        SourcingLead(
            lead_id="ld_htmx_test_001",
            requirement_id=requirement.id,
            requisition_id=req.id,
            part_number_requested="LM317T",
            part_number_matched="LM317T",
            match_type="exact",
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow electronics",
            primary_source_type="brokerbin",
            primary_source_name="Brokerbin",
            confidence_score=82.0,
            confidence_band="high",
            reason_summary="Strong stock signal from live source",
            vendor_safety_score=62.0,
            vendor_safety_band="medium_risk",
            vendor_safety_summary="Moderate caution: confirm business footprint and contact path.",
            buyer_status="new",
        )
    )

    db_session.commit()
    db_session.refresh(requirement)
    return {"req": req, "requirement": requirement, "card": card}


class TestSourcingResultsPartial:
    """Test GET /views/sourcing/{req_row_id}/results returns results HTML."""

    def test_sourcing_results_partial(self, htmx_client, sourcing_data):
        """Results endpoint returns HTML with sighting data."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results")
        assert resp.status_code == 200
        html = resp.text
        # Should contain result rows with vendor names
        assert "Arrow Electronics" in html
        assert "Digi-Key" in html
        # Should contain confidence values
        assert "90%" in html  # nexar confidence
        assert "85%" in html  # brokerbin confidence
        # Should contain source badges
        assert "source-badge" in html
        # Should include lead workflow/safety UI for rows with mapped leads
        assert "NEW" in html
        assert "MEDIUM RISK" in html
        assert "updateSourcingLeadStatus" in html
        # Should contain filter pills
        assert "filter-pill" in html
        assert "Live Stock" in html
        assert "Historical" in html

    def test_sourcing_results_404_for_missing(self, htmx_client):
        """Returns 404 for nonexistent requirement."""
        resp = htmx_client.get("/views/sourcing/99999/results")
        assert resp.status_code == 404

    def test_sourcing_results_empty(self, htmx_client, db_session, test_user):
        """Returns empty state when no sightings exist."""
        req = Requisition(
            name="Empty Req", status="open", created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id, primary_mpn="NOPART",
            target_qty=1, sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requirement)
        db_session.commit()

        resp = htmx_client.get(f"/views/sourcing/{requirement.id}/results")
        assert resp.status_code == 200
        assert "No sourcing results yet" in resp.text


class TestSourcingFilter:
    """Test filter param narrows results."""

    def test_filter_live_only(self, htmx_client, sourcing_data):
        """Live filter shows only live stock sources."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=live")
        assert resp.status_code == 200
        html = resp.text
        assert "Arrow Electronics" in html
        assert "Digi-Key" in html
        # Historical and affinity should be filtered out
        assert "Old Source" not in html
        assert "Preferred Vendor" not in html

    def test_filter_affinity_only(self, htmx_client, sourcing_data):
        """Affinity filter shows only vendor affinity matches."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=affinity")
        assert resp.status_code == 200
        html = resp.text
        assert "Preferred Vendor" in html
        assert "Arrow Electronics" not in html

    def test_filter_historical_only(self, htmx_client, sourcing_data):
        """Historical filter shows only historical sightings."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=historical")
        assert resp.status_code == 200
        html = resp.text
        assert "Old Source" in html
        assert "Digi-Key" not in html

    def test_sort_price_asc(self, htmx_client, sourcing_data):
        """Price ascending sort puts cheapest first."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?sort_by=price_asc")
        assert resp.status_code == 200
        html = resp.text
        # Digi-Key at 0.42 should appear before Arrow at 0.45
        digi_pos = html.find("Digi-Key")
        arrow_pos = html.find("Arrow Electronics")
        assert digi_pos < arrow_pos


class TestMaterialCardPartial:
    """Test material card detail renders."""

    def test_material_card_partial(self, htmx_client, sourcing_data):
        """Material card endpoint returns card detail with sightings."""
        card_id = sourcing_data["card"].id
        resp = htmx_client.get(f"/views/materials/{card_id}")
        assert resp.status_code == 200
        html = resp.text
        assert "LM317T" in html
        assert "Texas Instruments" in html
        assert "Adjustable Voltage Regulator" in html
        # Should show sighting history
        assert "Sighting History" in html
        assert "Arrow Electronics" in html

    def test_material_card_404(self, htmx_client):
        """Returns 404 for nonexistent material card."""
        resp = htmx_client.get("/views/materials/99999")
        assert resp.status_code == 404


class TestSightingRowRender:
    """Test that sighting rows render correctly within the material card."""

    def test_sighting_row_in_card(self, htmx_client, sourcing_data):
        """Material card includes sighting rows with expected fields."""
        card_id = sourcing_data["card"].id
        resp = htmx_client.get(f"/views/materials/{card_id}")
        assert resp.status_code == 200
        html = resp.text
        # Check sighting row content
        assert "sighting-row" in html
        assert "source-badge" in html
        # Price should be formatted
        assert "$0.4500" in html or "$0.4200" in html


class TestSSEStreamEndpoint:
    """Test SSE stream endpoint returns progress events."""

    def test_sse_stream_returns_progress(self, htmx_client, sourcing_data):
        """SSE stream endpoint returns search progress HTML."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/stream")
        assert resp.status_code == 200
        html = resp.text
        assert "search-progress" in html
        # Should show source pills with counts
        assert "source-pill" in html
        assert "done" in html

    def test_sse_stream_404_for_missing(self, htmx_client):
        """SSE stream returns 404 for nonexistent requirement."""
        resp = htmx_client.get("/views/sourcing/99999/stream")
        assert resp.status_code == 404

    def test_sse_stream_empty_sources(self, htmx_client, db_session, test_user):
        """SSE stream with no sightings returns empty progress."""
        req = Requisition(
            name="No Sightings Req", status="open", created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id, primary_mpn="EMPTY123",
            target_qty=1, sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(requirement)
        db_session.commit()

        resp = htmx_client.get(f"/views/sourcing/{requirement.id}/stream")
        assert resp.status_code == 200
        assert "search-progress" in resp.text


class TestLeadDetailView:
    """Test GET /views/sourcing/leads/{lead_id} returns lead detail partial."""

    def test_lead_detail_renders_evidence_and_safety(self, htmx_client, sourcing_data, db_session):
        """Lead detail view returns HTML with evidence, safety, and contact sections."""
        lead = db_session.query(SourcingLead).filter(
            SourcingLead.lead_id == "ld_htmx_test_001"
        ).first()
        assert lead is not None

        # Add evidence and feedback for the lead
        ev = LeadEvidence(
            evidence_id="ev_htmx_test_001",
            lead_id=lead.id,
            signal_type="live_stock",
            source_type="brokerbin",
            source_name="Brokerbin",
            part_number_observed="LM317T",
            vendor_name_observed="Arrow Electronics",
            freshness_age_days=1.5,
            explanation="Live stock listing from Brokerbin",
            source_reliability_band="high",
        )
        fb = LeadFeedbackEvent(
            lead_id=lead.id,
            status="contacted",
            note="Called Arrow sales line",
            contact_method="phone",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([ev, fb])
        db_session.commit()

        resp = htmx_client.get(f"/views/sourcing/leads/{lead.id}")
        assert resp.status_code == 200
        html = resp.text

        # Lead header
        assert "Arrow Electronics" in html
        assert "HIGH" in html  # confidence band
        assert "LM317T" in html

        # Evidence tab
        assert "Brokerbin" in html
        assert "live_stock" in html

        # Safety tab
        assert "MEDIUM RISK" in html

        # Activity tab
        assert "Called Arrow sales line" in html
        assert "CONTACTED" in html

    def test_lead_detail_404(self, htmx_client):
        """Returns 404 for nonexistent lead."""
        resp = htmx_client.get("/views/sourcing/leads/99999")
        assert resp.status_code == 404


class TestFollowUpQueue:
    """Test GET /views/sourcing/follow-up-queue returns buyer queue."""

    def test_follow_up_queue_renders(self, htmx_client, sourcing_data):
        """Queue endpoint shows leads table with status tabs."""
        resp = htmx_client.get("/views/sourcing/follow-up-queue")
        assert resp.status_code == 200
        html = resp.text
        assert "Buyer Follow-Up Queue" in html
        assert "Arrow Electronics" in html
        assert "LM317T" in html
        assert "NEW" in html

    def test_follow_up_queue_filter_by_status(self, htmx_client, sourcing_data):
        """Queue filters by status parameter."""
        resp = htmx_client.get("/views/sourcing/follow-up-queue?status=contacted")
        assert resp.status_code == 200
        html = resp.text
        # No leads with contacted status in fixture
        assert "No leads found" in html

    def test_follow_up_queue_all_shows_leads(self, htmx_client, sourcing_data):
        """Queue with status=all shows all leads."""
        resp = htmx_client.get("/views/sourcing/follow-up-queue?status=all")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text


class TestFilterAndSort:
    """Test extended filter and sort options on sourcing results."""

    def test_filter_high_confidence(self, htmx_client, sourcing_data):
        """High confidence filter shows only high-confidence results with leads."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=has_lead")
        assert resp.status_code == 200
        html = resp.text
        # Only Arrow has a lead in fixture
        assert "Arrow Electronics" in html

    def test_sort_safest(self, htmx_client, sourcing_data):
        """Safest sort option returns 200."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?sort_by=safest")
        assert resp.status_code == 200

    def test_sort_freshest(self, htmx_client, sourcing_data):
        """Freshest sort option returns 200."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?sort_by=freshest")
        assert resp.status_code == 200

    def test_results_html_has_new_filter_pills(self, htmx_client, sourcing_data):
        """Results page includes the new filter pills."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results")
        assert resp.status_code == 200
        html = resp.text
        assert "High Confidence" in html
        assert "Safe Vendors" in html
        assert "Contactable" in html
        assert "Corroborated" in html
        assert "Best Overall" in html
        assert "Safest" in html
        assert "Easiest to Contact" in html
        assert "Most Proven" in html

    def test_sort_easiest_to_contact(self, htmx_client, sourcing_data):
        """Easiest to contact sort option returns 200."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?sort_by=easiest_to_contact")
        assert resp.status_code == 200

    def test_sort_most_proven(self, htmx_client, sourcing_data):
        """Most proven sort option returns 200."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?sort_by=most_proven")
        assert resp.status_code == 200

    def test_filter_contactable(self, htmx_client, sourcing_data):
        """Contactable filter returns 200."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=contactable")
        assert resp.status_code == 200

    def test_filter_corroborated(self, htmx_client, sourcing_data):
        """Corroborated filter returns 200."""
        req_row_id = sourcing_data["requirement"].id
        resp = htmx_client.get(f"/views/sourcing/{req_row_id}/results?filter=corroborated")
        assert resp.status_code == 200
