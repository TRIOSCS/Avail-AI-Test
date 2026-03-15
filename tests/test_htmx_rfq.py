"""Tests for HTMX RFQ compose & send UI (Phase 2B).

Covers RFQ compose form rendering, vendor selection, RFQ send, and results display.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Requirement, Requisition, Sighting, User, VendorCard
from app.models.offers import Contact as RfqContact
from app.models.vendors import VendorContact


@pytest.fixture()
def req_with_vendors(db_session: Session, test_user: User):
    """Create a requisition with parts, vendors, and sightings."""
    company = Company(name="RFQ Co", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="RFQ Test Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    part = Requirement(
        requisition_id=req.id,
        primary_mpn="TPS54331",
        target_qty=500,
        sourcing_status="sourcing",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(part)
    db_session.flush()

    vendor1 = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        domain="arrow.com",
        created_at=datetime.now(timezone.utc),
    )
    vendor2 = VendorCard(
        normalized_name="digikey",
        display_name="DigiKey",
        domain="digikey.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([vendor1, vendor2])
    db_session.flush()

    # Add contacts
    vc1 = VendorContact(
        vendor_card_id=vendor1.id,
        email="sales@arrow.com",
        full_name="Arrow Sales",
        source="manual",
    )
    vc2 = VendorContact(
        vendor_card_id=vendor2.id,
        email="quotes@digikey.com",
        full_name="DK Quotes",
        source="manual",
    )
    db_session.add_all([vc1, vc2])
    db_session.flush()

    # Add sightings linking vendors to the requirement via normalized name
    s1 = Sighting(
        requirement_id=part.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn_matched="TPS54331",
        source_type="brokerbin",
        created_at=datetime.now(timezone.utc),
    )
    s2 = Sighting(
        requirement_id=part.id,
        vendor_name="DigiKey",
        vendor_name_normalized="digikey",
        mpn_matched="TPS54331",
        source_type="digikey",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([s1, s2])
    db_session.commit()

    return {
        "req": req,
        "part": part,
        "vendors": [vendor1, vendor2],
        "contacts": [vc1, vc2],
    }


def test_rfq_compose_renders_vendors(client, req_with_vendors):
    """RFQ compose form shows vendors with sightings."""
    req = req_with_vendors["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
    assert resp.status_code == 200
    html = resp.text
    assert "Arrow Electronics" in html
    assert "DigiKey" in html
    assert "Send RFQ" in html


def test_rfq_compose_shows_parts(client, req_with_vendors):
    """RFQ compose form shows parts summary."""
    req = req_with_vendors["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
    html = resp.text
    assert "TPS54331" in html
    assert "Parts to Quote" in html


def test_rfq_compose_shows_vendor_emails(client, req_with_vendors):
    """RFQ compose form shows vendor contact emails."""
    req = req_with_vendors["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
    html = resp.text
    assert "sales@arrow.com" in html
    assert "quotes@digikey.com" in html


def test_rfq_compose_already_asked_warning(client, db_session, req_with_vendors):
    """Vendors already contacted show 'Already Asked' badge."""
    req = req_with_vendors["req"]

    # Create a previous RFQ contact for Arrow
    prev = RfqContact(
        requisition_id=req.id,
        user_id=1,
        contact_type="email",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        vendor_contact="sales@arrow.com",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(prev)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{req.id}/rfq-compose")
    html = resp.text
    assert "Already Asked" in html


def test_rfq_compose_404_for_missing_req(client):
    """RFQ compose returns 404 for non-existent requisition."""
    resp = client.get("/v2/partials/requisitions/99999/rfq-compose")
    assert resp.status_code == 404


def test_rfq_send_creates_contacts(client, db_session, req_with_vendors):
    """Sending RFQ creates Contact records."""
    req = req_with_vendors["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/rfq-send",
        data={
            "vendor_names": ["Arrow Electronics", "DigiKey"],
            "vendor_emails": ["sales@arrow.com", "quotes@digikey.com"],
            "subject": "RFQ - TPS54331",
            "parts_summary": "TPS54331",
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "vendor(s)" in html
    assert "Arrow Electronics" in html
    assert "DigiKey" in html

    # Verify Contact records
    contacts = db_session.query(RfqContact).filter(
        RfqContact.requisition_id == req.id,
    ).all()
    assert len(contacts) >= 2


def test_rfq_send_no_vendors_400(client, req_with_vendors):
    """Sending RFQ with no vendors returns 400."""
    req = req_with_vendors["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/rfq-send",
        data={"subject": "RFQ", "parts_summary": "TPS54331"},
    )
    assert resp.status_code == 400


def test_rfq_send_results_summary(client, req_with_vendors):
    """RFQ send results show per-vendor status table."""
    req = req_with_vendors["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/rfq-send",
        data={
            "vendor_names": ["Arrow Electronics"],
            "vendor_emails": ["sales@arrow.com"],
            "subject": "RFQ",
            "parts_summary": "TPS54331",
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "sales@arrow.com" in html
    assert "Sent" in html
    assert "View Activity" in html


def test_send_rfq_button_in_detail(client, db_session, test_user):
    """Requisition detail has 'Send RFQ' button."""
    company = Company(name="Detail Co", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="Button Test",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{req.id}")
    assert resp.status_code == 200
    html = resp.text
    assert "Send RFQ" in html
    assert f"/v2/partials/requisitions/{req.id}/rfq-compose" in html
