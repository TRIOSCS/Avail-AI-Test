"""test_sales_order_origination.py — Route tests for the New Sales Order origination UI.

Covers the two new approvals routes that wire the New-SO button → requisition picker →
quote-builder offer/sell form → DRAFT BuyPlan detail:
  - GET  /v2/partials/approvals/sales-orders/new      (requisition picker + builder form)
  - POST /v2/partials/approvals/sales-orders/create   (create DRAFT SO, render detail)

Also covers the duplicate-open-SO path (renders the existing SO's detail with a toast,
never a 500) and the role-scoping property (a restricted SALES/TRADER role only sees /
can originate Sales Orders for requisitions it owns).

Called by: pytest
Depends on: conftest fixtures (nonadmin_client = buyer TestClient, db_session,
    sales_user), app.routers.htmx_views (the two new routes),
    app.services.buyplan_builder.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import OfferStatus
from app.models import Company, CustomerSite, Offer, Requirement, Requisition, User, VendorCard


def _seed_so_requisition(
    db: Session,
    owner: User,
    *,
    label: str,
    offer_status: str = OfferStatus.ACTIVE.value,
) -> tuple[Requisition, Requirement, Offer]:
    """Seed an open requisition (owned by ``owner``) + one requirement + one offer.

    ``label`` uniquifies the requisition name, MPN, company, and vendor so several seeds
    can coexist in one test without unique-constraint collisions. ``offer_status`` lets a
    caller seed a requisition whose only offer is NOT active (so it is excluded from the
    picker). Returns ``(requisition, requirement, offer)``.
    """
    company = Company(name=f"{label} Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(company)
    db.flush()

    site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        country="US",
        created_at=datetime.now(timezone.utc),
    )
    db.add(site)
    db.flush()

    req = Requisition(
        name=label,
        status="open",
        created_by=owner.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=f"{label}-MPN-1",
        target_qty=100,
        target_price=1.0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requirement)
    db.flush()

    vendor = VendorCard(
        normalized_name=f"{label.lower()} vendor",
        display_name=f"{label} Vendor",
        created_at=datetime.now(timezone.utc),
    )
    db.add(vendor)
    db.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_card_id=vendor.id,
        vendor_name=f"{label} Vendor",
        mpn=f"{label}-MPN-1",
        qty_available=100,
        unit_price=0.50,
        status=offer_status,
        entered_by_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    return req, requirement, offer


@pytest.fixture
def so_setup(db_session: Session):
    """A requisition + one requirement with one scored ACTIVE offer (the SO origination
    path).

    Returns ``(requisition, requirement, offer)``. Owned by a fresh buyer (NOT the
    restricted sales_user), so role-scoping tests can treat it as "not owned" by a
    restricted role. Mirrors the seed shape used by
    ``tests/test_buyplan_builder_so_origin.py::so_origin_fixture``.
    """
    owner = User(
        email="so-ui@trioscs.com",
        name="SO UI Buyer",
        role="buyer",
        azure_id="az-so-ui",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(owner)
    db_session.flush()
    return _seed_so_requisition(db_session, owner, label="REQ-SO-UI")


@pytest.fixture
def restricted_client(db_session: Session, sales_user: User) -> TestClient:
    """TestClient authed as a restricted SALES role (sees/originates only owned reqs).

    Replicates the ``nonadmin_client`` pattern (signed session cookie + a require_user
    override) but pointed at ``sales_user`` so the role-scoping branches in
    ``get_req_for_user`` / ``require_requisition_access`` are exercised against a
    non-owner.
    """
    from app.config import settings
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: sales_user
    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    session_cookie = signer.sign(base64.b64encode(json.dumps({"user_id": sales_user.id}).encode())).decode()
    try:
        with TestClient(app) as c:
            c.cookies.set("session", session_cookie)
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


def test_new_sales_order_picker_lists_open_requisitions(nonadmin_client: TestClient, so_setup):
    """The picker (no requisition_id) lists open requisitions that have a selectable
    offer."""
    req, _requirement, _offer = so_setup
    r = nonadmin_client.get("/v2/partials/approvals/sales-orders/new")
    assert r.status_code == 200
    # Distinctive tokens — the row's builder hx-get fragment AND the requisition name.
    # (A bare ``str(req.id)`` matches Tailwind/SVG digits and would pass on an empty list.)
    assert f"requisition_id={req.id}" in r.text
    assert "REQ-SO-UI" in r.text


def test_new_sales_order_picker_excludes_requisition_without_active_offers(
    nonadmin_client: TestClient, db_session: Session, so_setup
):
    """A requisition whose only offer is NOT active is excluded from the picker."""
    owner = User(
        email="so-ui-noof@trioscs.com",
        name="SO UI NoOffer Buyer",
        role="buyer",
        azure_id="az-so-ui-noof",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(owner)
    db_session.flush()
    no_offer_req, _req, _offer = _seed_so_requisition(
        db_session, owner, label="REQ-SO-NOOFFER", offer_status=OfferStatus.EXPIRED.value
    )
    r = nonadmin_client.get("/v2/partials/approvals/sales-orders/new")
    assert r.status_code == 200
    # The with-offers req still lists (selective filter, not an empty list)...
    assert "REQ-SO-UI" in r.text
    # ...but the no-active-offer req is excluded.
    assert "REQ-SO-NOOFFER" not in r.text
    assert f"requisition_id={no_offer_req.id}" not in r.text


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
    open SO's detail, fires a toast (HX-Trigger), AND pushes the existing plan's URL."""
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
    # The duplicate path also pushes the existing plan's canonical URL (parity with success).
    assert second.headers.get("HX-Push-Url", "").startswith("/v2/buy-plans/")


# ── Role-scoping (restricted SALES/TRADER) ──────────────────────────────────────────


def test_picker_excludes_requisition_not_owned_by_restricted_role(restricted_client: TestClient, so_setup):
    """A restricted (SALES) user does NOT see a requisition owned by someone else."""
    req, _requirement, _offer = so_setup  # owned by a buyer, not sales_user
    r = restricted_client.get("/v2/partials/approvals/sales-orders/new")
    assert r.status_code == 200
    assert "REQ-SO-UI" not in r.text
    assert f"requisition_id={req.id}" not in r.text


def test_builder_get_404_for_restricted_non_owner(restricted_client: TestClient, so_setup):
    """Builder-mode GET for a not-owned requisition is a 404 for a restricted role."""
    req, _requirement, _offer = so_setup
    r = restricted_client.get(f"/v2/partials/approvals/sales-orders/new?requisition_id={req.id}")
    assert r.status_code == 404


def test_create_404_for_restricted_non_owner(restricted_client: TestClient, so_setup):
    """POST create for a not-owned requisition is a 404 for a restricted role."""
    req, requirement, offer = so_setup
    r = restricted_client.post(
        "/v2/partials/approvals/sales-orders/create",
        data={
            "requisition_id": req.id,
            f"offer_{requirement.id}": offer.id,
            f"sell_{requirement.id}": "1.25",
        },
    )
    assert r.status_code == 404


def test_restricted_role_can_originate_for_owned_requisition(
    restricted_client: TestClient, db_session: Session, sales_user: User
):
    """Sanity: a restricted role DOES see and can originate a SO for a req it owns."""
    owned_req, requirement, offer = _seed_so_requisition(db_session, sales_user, label="REQ-SO-OWNED")
    # Picker includes the owned req.
    picker = restricted_client.get("/v2/partials/approvals/sales-orders/new")
    assert picker.status_code == 200
    assert "REQ-SO-OWNED" in picker.text
    assert f"requisition_id={owned_req.id}" in picker.text

    # Builder GET is allowed (200, not 404).
    builder = restricted_client.get(f"/v2/partials/approvals/sales-orders/new?requisition_id={owned_req.id}")
    assert builder.status_code == 200
    assert f"offer_{requirement.id}" in builder.text

    # And create succeeds, rendering the DRAFT detail.
    created = restricted_client.post(
        "/v2/partials/approvals/sales-orders/create",
        data={
            "requisition_id": owned_req.id,
            f"offer_{requirement.id}": offer.id,
            f"sell_{requirement.id}": "1.25",
        },
    )
    assert created.status_code == 200
    assert "Submit" in created.text
