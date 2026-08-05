"""tests/test_offers_idor.py — Cross-requisition IDOR regression guard for offers.

Covers the offer-scoped read endpoint in app/routers/crm/offers.py that previously
depended only on require_user and leaked requisition/offer-scoped data:

  GET /api/offers/{offer_id}/attachments — attachment listing must gate
      on require_requisition_access (parity with upload/delete peers).

require_requisition_access only restricts SALES/TRADER roles (buyers/managers/admins
are unrestricted), so the "stranger" is a SALES user who owns nothing and
the "owner" is the SALES user who created the requisition.

The former changelog (GET /api/changelog/...) and review-queue
(GET /api/offers/review-queue) probes were deleted with those orphan routes in
Wave 2; the owner-vs-stranger guarantee lives on in the attachments pair below.

Called by: pytest
Depends on: conftest.py fixtures (db_session), app.main.app, app.dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Requisition, User

# ── client helper ────────────────────────────────────────────────────────────


def _client_for(db_session: Session, user: User) -> TestClient:
    """Build a TestClient authenticated as *user* with the test db session."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    app.dependency_overrides[require_buyer] = lambda: user
    app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

    client = TestClient(app)
    client.__exit__wrapped_deps = [  # type: ignore[attr-defined]
        get_db,
        require_user,
        require_admin,
        require_buyer,
        require_fresh_token,
    ]
    return client


def _teardown_client(client: TestClient) -> None:
    from app.main import app

    for dep in getattr(client, "__exit__wrapped_deps", []):
        app.dependency_overrides.pop(dep, None)


# ── users ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def sales_owner(db_session: Session) -> User:
    """SALES user who will own the requisition/offer under test."""
    u = User(
        email="sales_owner_idor@example.com",
        name="Sales Owner",
        role="sales",
        azure_id="sales-owner-azure-idor",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def sales_stranger(db_session: Session) -> User:
    """SALES user who owns nothing (the attacker)."""
    u = User(
        email="sales_stranger_idor@example.com",
        name="Sales Stranger",
        role="sales",
        azure_id="sales-stranger-azure-idor",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


# ── owned requisition + offer ──────────────────────────────────────────────


@pytest.fixture()
def owned_requisition(db_session: Session, sales_owner: User) -> Requisition:
    req = Requisition(
        name="Owned Req IDOR",
        status="open",
        created_by=sales_owner.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def owned_offer(
    db_session: Session,
    owned_requisition: Requisition,
    sales_owner: User,
) -> Offer:
    offer = Offer(
        requisition_id=owned_requisition.id,
        vendor_name="Secret Vendor Inc",
        mpn="ABC-123",
        qty_available=100,
        unit_price=Decimal("1.2345"),
        entered_by_id=sales_owner.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


# ── list_offer_attachments ───────────────────────────────────────────────────


class TestOfferAttachmentsIDOR:
    """GET /api/offers/{id}/attachments must gate on require_requisition_access."""

    def test_unrelated_sales_gets_404(
        self,
        db_session: Session,
        sales_stranger: User,
        owned_offer: Offer,
    ):
        client = _client_for(db_session, sales_stranger)
        try:
            resp = client.get(f"/api/offers/{owned_offer.id}/attachments")
        finally:
            _teardown_client(client)
        assert resp.status_code == 404

    def test_owner_sales_gets_200(
        self,
        db_session: Session,
        sales_owner: User,
        owned_offer: Offer,
    ):
        client = _client_for(db_session, sales_owner)
        try:
            resp = client.get(f"/api/offers/{owned_offer.id}/attachments")
        finally:
            _teardown_client(client)
        assert resp.status_code == 200
        # The owner receives the (JSON) attachment list the stranger was denied.
        assert isinstance(resp.json(), list)
