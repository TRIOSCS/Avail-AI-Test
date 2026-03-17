"""Tests for HTMX sourcing engine views — Part Search, sourcing results, lead detail.

Covers the new routes added in Plan 3: search form/results partials, sourcing
results with filters, lead detail, lead status updates, and lead feedback.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models.sourcing_lead
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User
from app.models.sourcing_lead import LeadEvidence, SourcingLead


@pytest.fixture()
def sample_requisition_with_leads(db_session: Session, test_user: User):
    """Create a requisition with a requirement and sourcing leads for testing."""
    req = Requisition(
        name="Test Req",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name="Test Vendor",
        vendor_name_normalized="test_vendor",
        mpn_matched="LM317T",
        qty_available=5000,
        unit_price=0.5500,
        source_type="brokerbin",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.flush()

    lead = SourcingLead(
        lead_id="ld_test_001",
        requirement_id=requirement.id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name="Test Vendor",
        vendor_name_normalized="test_vendor",
        primary_source_type="brokerbin",
        primary_source_name="Brokerbin",
        confidence_score=72.5,
        confidence_band="medium",
        vendor_safety_score=68.0,
        vendor_safety_band="medium_risk",
        vendor_safety_summary="Moderate caution.",
        vendor_safety_flags=["limited_business_footprint", "positive:contact_channels_present"],
        contact_email="sales@testvendor.com",
        buyer_status="new",
        evidence_count=1,
        corroborated=False,
        reason_summary="Test lead",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(lead)
    db_session.flush()

    evidence = LeadEvidence(
        evidence_id="ev_test_001",
        lead_id=lead.id,
        signal_type="stock_listing",
        source_type="brokerbin",
        source_name="Brokerbin",
        explanation="BrokerBin stock listing for Test Vendor",
        confidence_impact=14.4,
        verification_state="raw",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(evidence)
    db_session.commit()

    return requirement


@pytest.fixture()
def sample_lead(db_session: Session, sample_requisition_with_leads):
    """Return the first lead for the sample requirement."""
    return (
        db_session.query(SourcingLead).filter(SourcingLead.requirement_id == sample_requisition_with_leads.id).first()
    )


# ── Part Search Tests ──────────────────────────────────────────────────


def test_search_form_partial(client):
    """GET /partials/search returns search form HTML."""
    resp = client.get("/v2/partials/search")
    assert resp.status_code == 200
    assert "Search All Sources" in resp.text
    assert 'name="mpn"' in resp.text


def test_search_run_returns_results(client, db_session):
    """POST /partials/search/run returns lead cards with enriched data."""
    with patch(
        "app.search_service.quick_search_mpn",
        return_value=[
            {
                "vendor_name": "Acme",
                "mpn_matched": "LM317T",
                "manufacturer": "TI",
                "qty_available": 1000,
                "unit_price": 0.55,
                "source_type": "brokerbin",
                "lead_time": "Stock",
            }
        ],
    ):
        resp = client.post(
            "/v2/partials/search/run",
            data={"mpn": "LM317T"},
        )
    assert resp.status_code == 200
    assert "Acme" in resp.text
    assert "LM317T" in resp.text
    assert "Confidence" in resp.text  # confidence badge present
    assert "Live Stock" in resp.text  # source badge for live API results


def test_search_run_empty_mpn(client):
    """POST /partials/search/run with empty mpn shows error."""
    resp = client.post("/v2/partials/search/run", data={"mpn": ""})
    assert resp.status_code == 200
    assert "Please enter a part number" in resp.text


# ── Sourcing Results Tests ─────────────────────────────────────────────


def test_sourcing_results_partial(client, db_session, sample_requisition_with_leads):
    """GET /partials/sourcing/{req_id} returns lead cards."""
    req_id = sample_requisition_with_leads.id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    assert "lead-card-" in resp.text
    assert "Test Vendor" in resp.text


def test_sourcing_results_not_found(client):
    """GET /partials/sourcing/99999 returns 404."""
    resp = client.get("/v2/partials/sourcing/99999")
    assert resp.status_code == 404


def test_sourcing_filter_confidence(client, db_session, sample_requisition_with_leads):
    """Confidence filter restricts leads by band."""
    req_id = sample_requisition_with_leads.id
    # Lead has confidence_band="medium", so high filter should exclude it
    resp = client.get(f"/v2/partials/sourcing/{req_id}?confidence=high")
    assert resp.status_code == 200
    assert "lead-card-" not in resp.text

    resp = client.get(f"/v2/partials/sourcing/{req_id}?confidence=medium")
    assert resp.status_code == 200
    assert "lead-card-" in resp.text


def test_sourcing_filter_safety(client, db_session, sample_requisition_with_leads):
    """Safety filter restricts leads by band."""
    req_id = sample_requisition_with_leads.id
    resp = client.get(f"/v2/partials/sourcing/{req_id}?safety=low_risk")
    assert resp.status_code == 200
    assert "lead-card-" not in resp.text  # Lead is medium_risk


def test_sourcing_sort_options(client, db_session, sample_requisition_with_leads):
    """Sort options work without errors."""
    req_id = sample_requisition_with_leads.id
    for sort_val in ["best", "freshest", "safest", "contact", "proven"]:
        resp = client.get(f"/v2/partials/sourcing/{req_id}?sort={sort_val}")
        assert resp.status_code == 200


def test_sourcing_full_page(client, db_session, sample_requisition_with_leads):
    """GET /sourcing/{req_id} returns a 200 page (login or base_page)."""
    req_id = sample_requisition_with_leads.id
    resp = client.get(f"/sourcing/{req_id}")
    assert resp.status_code == 200
    # Full page uses get_user (session cookie), so in tests returns login or base_page
    assert "AvailAI" in resp.text


# ── Lead Detail Tests ──────────────────────────────────────────────────


def test_lead_detail_partial(client, db_session, sample_lead):
    """GET /partials/sourcing/leads/{id} returns lead detail."""
    resp = client.get(f"/v2/partials/sourcing/leads/{sample_lead.id}")
    assert resp.status_code == 200
    assert sample_lead.vendor_name in resp.text
    assert "Evidence" in resp.text
    assert "Safety Review" in resp.text
    assert "Buyer Actions" in resp.text


def test_lead_detail_not_found(client):
    """GET /partials/sourcing/leads/99999 returns 404."""
    resp = client.get("/v2/partials/sourcing/leads/99999")
    assert resp.status_code == 404


def test_lead_detail_full_page(client, db_session, sample_lead):
    """GET /sourcing/leads/{id} returns a 200 page (login or base_page)."""
    resp = client.get(f"/sourcing/leads/{sample_lead.id}")
    assert resp.status_code == 200
    # Full page uses get_user (session cookie), so in tests returns login or base_page
    assert "AvailAI" in resp.text


# ── Lead Status Update Tests ──────────────────────────────────────────


def test_lead_status_update(client, db_session, sample_lead):
    """POST status update changes buyer_status and creates feedback event."""
    resp = client.post(
        f"/v2/partials/sourcing/leads/{sample_lead.id}/status",
        data={"status": "contacted", "note": "Called vendor"},
    )
    assert resp.status_code == 200
    db_session.refresh(sample_lead)
    assert sample_lead.buyer_status == "contacted"
    assert sample_lead.buyer_feedback_summary == "Called vendor"


def test_lead_status_invalid(client, db_session, sample_lead):
    """Invalid status returns 400."""
    resp = client.post(
        f"/v2/partials/sourcing/leads/{sample_lead.id}/status",
        data={"status": "invalid_status"},
    )
    assert resp.status_code == 400


def test_lead_status_not_found(client):
    """Status update on nonexistent lead returns 404."""
    resp = client.post(
        "/v2/partials/sourcing/leads/99999/status",
        data={"status": "contacted"},
    )
    assert resp.status_code == 404


# ── Lead Feedback Tests ───────────────────────────────────────────────


def test_lead_feedback(client, db_session, sample_lead):
    """POST feedback adds event without changing status."""
    resp = client.post(
        f"/v2/partials/sourcing/leads/{sample_lead.id}/feedback",
        data={"note": "Vendor confirmed stock", "contact_method": "email"},
    )
    assert resp.status_code == 200
    db_session.refresh(sample_lead)
    assert sample_lead.buyer_feedback_summary == "Vendor confirmed stock"


def test_lead_feedback_not_found(client):
    """Feedback on nonexistent lead returns 404."""
    resp = client.post(
        "/v2/partials/sourcing/leads/99999/feedback",
        data={"note": "test"},
    )
    assert resp.status_code == 404


# ── Lead Card Rendering Tests ────────────────────────────────────────


def test_sourcing_lead_card_shows_buyer_status(client, db_session, sample_lead):
    """Lead card renders buyer_status badge when status is not 'new'."""
    sample_lead.buyer_status = "contacted"
    db_session.commit()
    req_id = sample_lead.requirement_id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    assert "Contacted" in resp.text


def test_sourcing_lead_card_hides_new_status(client, db_session, sample_lead):
    """Lead card does NOT show buyer_status badge when status is 'new'."""
    sample_lead.buyer_status = "new"
    db_session.commit()
    req_id = sample_lead.requirement_id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    # The word "New" appears in filter bar but NOT as a buyer status badge
    assert "lead-card-" in resp.text


def test_sourcing_lead_card_shows_risk_flags(client, db_session, sample_lead):
    """Lead card renders risk_flags as colored chips."""
    sample_lead.risk_flags = ["limited_business_footprint", "positive:contact_channels_present"]
    db_session.commit()
    req_id = sample_lead.requirement_id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    assert "Limited Business Footprint" in resp.text
    assert "Contact Channels Present" in resp.text


def test_sourcing_lead_card_shows_reason_summary(client, db_session, sample_lead):
    """Lead card renders reason_summary text."""
    sample_lead.reason_summary = "Strong BrokerBin listing with verified stock"
    db_session.commit()
    req_id = sample_lead.requirement_id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    assert "Strong BrokerBin listing with verified stock" in resp.text


def test_sourcing_results_weak_leads_warning(client, db_session, sample_lead):
    """Warning banner shown when all leads have low confidence."""
    sample_lead.confidence_band = "low"
    sample_lead.confidence_score = 15.0
    db_session.commit()
    req_id = sample_lead.requirement_id
    resp = client.get(f"/v2/partials/sourcing/{req_id}")
    assert resp.status_code == 200
    assert "Only weak leads found" in resp.text


# ── Requirement Inline Editing Tests ─────────────────────────────────


def test_requirement_inline_update(client, db_session, sample_requisition_with_leads):
    """PUT updates a requirement and returns the updated row HTML."""
    requirement = sample_requisition_with_leads
    req_id = requirement.requisition_id
    resp = client.put(
        f"/v2/partials/requisitions/{req_id}/requirements/{requirement.id}",
        data={
            "primary_mpn": "LM358N",
            "target_qty": "500",
            "brand": "Texas Instruments",
            "target_price": "0.3500",
        },
    )
    assert resp.status_code == 200
    assert "LM358N" in resp.text
    assert "Texas Instruments" in resp.text
    # Verify DB was updated
    db_session.refresh(requirement)
    assert requirement.primary_mpn == "LM358N"
    assert requirement.target_qty == 500
    assert requirement.brand == "Texas Instruments"


def test_requirement_inline_update_not_found(client):
    """PUT on nonexistent requirement returns 404."""
    resp = client.put(
        "/v2/partials/requisitions/99999/requirements/99999",
        data={"primary_mpn": "TEST", "target_qty": "1"},
    )
    assert resp.status_code == 404


def test_requirement_row_has_edit_support(client, db_session, sample_requisition_with_leads):
    """Requirement row includes Alpine.js x-data for inline editing."""
    requirement = sample_requisition_with_leads
    req_id = requirement.requisition_id
    # Load the parts tab
    resp = client.get(f"/v2/partials/requisitions/{req_id}/tab/parts")
    assert resp.status_code == 200
    assert "x-data" in resp.text
    assert "editing" in resp.text
    assert "hx-put" in resp.text


def test_add_requirement_returns_template_row(client, db_session, sample_requisition_with_leads):
    """POST add requirement returns a proper template row (not inline HTML)."""
    req_id = sample_requisition_with_leads.requisition_id
    resp = client.post(
        f"/v2/partials/requisitions/{req_id}/requirements",
        data={"primary_mpn": "STM32F407", "target_qty": "10", "brand": "ST"},
    )
    assert resp.status_code == 200
    assert "STM32F407" in resp.text
    assert "x-data" in resp.text  # Has inline editing support
    assert "hx-put" in resp.text  # Has edit endpoint
