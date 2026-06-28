"""test_sales_order_origination.py — Route tests for the New Sales Order origination UI.

Covers the two new approvals routes that wire the New-SO button → requisition picker →
quote-builder offer/sell form → DRAFT BuyPlan detail:
  - GET  /v2/partials/approvals/sales-orders/new      (requisition picker + builder form)
  - POST /v2/partials/approvals/sales-orders/create   (create DRAFT SO, render detail)

Also covers the duplicate-open-SO ValueError path (renders the existing SO's detail with
a toast, never a 500).

Called by: pytest
Depends on: conftest fixtures (nonadmin_client = buyer TestClient, db_session, test_user),
    app.routers.htmx_views (the two new routes), app.services.buyplan_builder.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Offer, Requirement, Requisition, User, VendorCard


@pytest.fixture
def so_setup(db_session: Session):
    """A requisition + one requirement with one scored ACTIVE offer (the SO origination
    path).

    Returns ``(requisition, requirement, offer)``. Mirrors the seed shape used by
    ``tests/test_buyplan_builder_so_origin.py::so_origin_fixture`` so the create route's
    form parsing is exercised against a realistic offer.
    """
    user = User(
        email="so-ui@trioscs.com",
        name="SO UI Buyer",
        role="buyer",
        azure_id="az-so-ui",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.flush()

    company = Company(name="SO UI Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        country="US",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="REQ-SO-UI",
        status="open",
        created_by=user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="SO-UI-MPN-1",
        target_qty=100,
        target_price=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    vendor = VendorCard(
        normalized_name="so ui vendor",
        display_name="SO UI Vendor",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(vendor)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor.id,
        vendor_name="SO UI Vendor",
        mpn="SO-UI-MPN-1",
        qty_available=100,
        unit_price=0.50,
        status="active",
        entered_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    return req, requirement, offer


def test_new_sales_order_picker_lists_open_requisitions(nonadmin_client: TestClient, so_setup):
    """The picker (no requisition_id) lists open requisitions that have a selectable
    offer."""
    req, _requirement, _offer = so_setup
    r = nonadmin_client.get("/v2/partials/approvals/sales-orders/new")
    assert r.status_code == 200
    assert str(req.id) in r.text


def test_new_sales_order_picker_renders_offer_form_for_requisition(nonadmin_client: TestClient, so_setup):
    """Selecting a requisition (requisition_id set) renders the per-requirement offer +
    sell-price form."""
    req, requirement, offer = so_setup
    r = nonadmin_client.get(f"/v2/partials/approvals/sales-orders/new?requisition_id={req.id}")
    assert r.status_code == 200
    # The form posts to the create route and carries this requirement's offer/sell fields.
    assert "/v2/partials/approvals/sales-orders/create" in r.text
    assert f"offer_{requirement.id}" in r.text
    assert f"sell_{requirement.id}" in r.text
    assert str(offer.id) in r.text


def test_create_sales_order_returns_draft_detail(nonadmin_client: TestClient, so_setup):
    """Posting the offer/sell selections creates a DRAFT buy plan and renders its detail
    (with the Submit form)."""
    req, requirement, offer = so_setup
    r = nonadmin_client.post(
        "/v2/partials/approvals/sales-orders/create",
        data={
            "requisition_id": req.id,
            f"offer_{requirement.id}": offer.id,
            f"sell_{requirement.id}": "1.25",
        },
    )
    assert r.status_code == 200
    assert "Submit" in r.text  # buy-plan detail submit form


def test_create_duplicate_open_so_returns_existing_with_toast(nonadmin_client: TestClient, so_setup):
    """A second create for the same requisition does NOT 500 — it renders the existing
    open SO's detail and fires a toast (HX-Trigger)."""
    req, requirement, offer = so_setup
    payload = {
        "requisition_id": req.id,
        f"offer_{requirement.id}": offer.id,
        f"sell_{requirement.id}": "1.25",
    }
    first = nonadmin_client.post("/v2/partials/approvals/sales-orders/create", data=payload)
    assert first.status_code == 200

    second = nonadmin_client.post("/v2/partials/approvals/sales-orders/create", data=payload)
    assert second.status_code == 200
    assert "Submit" in second.text  # the existing SO's detail
    trigger = second.headers.get("HX-Trigger", "")
    assert "showToast" in trigger
    assert "already" in json.loads(trigger)["showToast"]["message"].lower()
