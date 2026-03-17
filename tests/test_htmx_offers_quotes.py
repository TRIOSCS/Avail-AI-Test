"""Tests for HTMX offer actions, quote creation from offers, response review, and PDF buttons.

Covers Phase 2A (offer actions, create quote from offers, vendor response review)
and Phase 2D (PDF download buttons in templates).

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Quote, QuoteLine, Requirement, Requisition, User
from app.models.offers import VendorResponse


@pytest.fixture()
def sample_req_with_offers(db_session: Session, test_user: User):
    """Create a requisition with requirements and offers for testing."""
    company = Company(name="Test Company", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="Test Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        sourcing_status="offered",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    offer1 = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_name="Acme Electronics",
        mpn="LM317T",
        manufacturer="TI",
        qty_available=5000,
        unit_price=0.55,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    offer2 = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_name="Budget Parts",
        mpn="LM317T",
        manufacturer="TI",
        qty_available=1000,
        unit_price=0.75,
        status="pending_review",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([offer1, offer2])
    db_session.commit()
    db_session.refresh(req)

    return req


# ── Offers Tab Tests ──────────────────────────────────────────────────


def test_offers_tab_shows_action_buttons(client, db_session, sample_req_with_offers):
    """Offers tab renders checkboxes and action buttons."""
    req = sample_req_with_offers
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
    assert resp.status_code == 200
    assert "Acme Electronics" in resp.text
    assert "Budget Parts" in resp.text
    assert 'type="checkbox"' in resp.text
    assert "Create Quote from Selected" in resp.text


def test_offers_tab_shows_review_buttons(client, db_session, sample_req_with_offers):
    """Offers with pending_review status show approve/reject buttons."""
    req = sample_req_with_offers
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
    assert resp.status_code == 200
    assert "Approve" in resp.text
    assert "Reject" in resp.text


# ── Create Quote from Offers Tests ────────────────────────────────────


def test_create_quote_from_offers(client, db_session, sample_req_with_offers):
    """POST create-quote creates a draft quote from selected offers."""
    req = sample_req_with_offers
    offers = db_session.query(Offer).filter(Offer.requisition_id == req.id).all()
    offer_ids = [str(o.id) for o in offers]

    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/create-quote",
        data={"offer_ids": offer_ids},
    )
    assert resp.status_code == 200
    # Should return quote detail
    assert "DRAFT" in resp.text or "draft" in resp.text.lower()

    # Verify quote was created
    quote = db_session.query(Quote).filter(Quote.requisition_id == req.id).first()
    assert quote is not None
    assert quote.status == "draft"
    lines = db_session.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
    assert len(lines) == 2


def test_create_quote_no_offers_returns_400(client, db_session, sample_req_with_offers):
    """POST create-quote with no offer_ids returns 400."""
    req = sample_req_with_offers
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/create-quote",
        data={},
    )
    assert resp.status_code == 400


# ── Offer Review Tests ────────────────────────────────────────────────


def test_approve_offer(client, db_session, sample_req_with_offers):
    """POST approve changes offer status to approved."""
    req = sample_req_with_offers
    pending_offer = (
        db_session.query(Offer).filter(Offer.requisition_id == req.id, Offer.status == "pending_review").first()
    )

    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/offers/{pending_offer.id}/review",
        data={"action": "approve"},
    )
    assert resp.status_code == 200
    db_session.refresh(pending_offer)
    assert pending_offer.status == "approved"


def test_reject_offer(client, db_session, sample_req_with_offers):
    """POST reject changes offer status to rejected."""
    req = sample_req_with_offers
    pending_offer = (
        db_session.query(Offer).filter(Offer.requisition_id == req.id, Offer.status == "pending_review").first()
    )

    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/offers/{pending_offer.id}/review",
        data={"action": "reject"},
    )
    assert resp.status_code == 200
    db_session.refresh(pending_offer)
    assert pending_offer.status == "rejected"


# ── Responses Tab Tests ───────────────────────────────────────────────


def test_responses_tab_renders(client, db_session, sample_req_with_offers):
    """GET responses tab returns 200 even with no responses."""
    req = sample_req_with_offers
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/responses")
    assert resp.status_code == 200
    assert "No vendor responses yet" in resp.text


def test_responses_tab_shows_responses(client, db_session, sample_req_with_offers):
    """Responses tab shows vendor responses when present."""
    req = sample_req_with_offers
    vr = VendorResponse(
        requisition_id=req.id,
        vendor_name="Acme Electronics",
        vendor_email="sales@acme.com",
        subject="RE: RFQ LM317T",
        status="new",
        classification="quote",
        confidence=0.85,
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(vr)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/responses")
    assert resp.status_code == 200
    assert "Acme Electronics" in resp.text
    assert "Quote" in resp.text  # classification badge
    assert "85% conf" in resp.text  # confidence indicator
    assert "Mark Reviewed" in resp.text  # action button


# ── PDF Button Tests ──────────────────────────────────────────────────


def test_requisition_detail_has_pdf_button(client, db_session, sample_req_with_offers):
    """Requisition detail shows Download PDF button."""
    req = sample_req_with_offers
    resp = client.get(f"/v2/partials/requisitions/{req.id}")
    assert resp.status_code == 200
    assert "Download PDF" in resp.text
    assert f"/api/requisitions/{req.id}/pdf" in resp.text


def test_requisition_detail_has_responses_tab(client, db_session, sample_req_with_offers):
    """Requisition detail has a Responses tab in nav."""
    req = sample_req_with_offers
    resp = client.get(f"/v2/partials/requisitions/{req.id}")
    assert resp.status_code == 200
    assert "Responses" in resp.text
