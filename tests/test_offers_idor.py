"""tests/test_offers_idor.py — Cross-requisition IDOR regression guard for offers.

Covers three read endpoints in app/routers/crm/offers.py that previously depended
only on require_user and leaked requisition/offer-scoped data:

  F1: GET /api/changelog/{entity_type}/{entity_id}  — change history (may include
      pricing, terms, PII) must gate on require_requisition_access.
  F2: GET /api/offers/{offer_id}/attachments        — attachment listing must gate
      on require_requisition_access (parity with upload/delete peers).
  F3: GET /api/offers/review-queue                  — cross-requisition aggregate of
      pending-review offers must be restricted to managers/admins.

require_requisition_access only restricts SALES/TRADER roles (buyers/managers/admins
are unrestricted), so the "stranger" for F1/F2 is a SALES user who owns nothing and
the "owner" is the SALES user who created the requisition. For F3 the aggregate is
manager/admin-only, so a SALES stranger is rejected and a MANAGER is allowed.

Called by: pytest
Depends on: conftest.py fixtures (db_session), app.main.app, app.dependencies
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import ChangeLog, Offer, Requisition, User

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


@pytest.fixture()
def manager(db_session: Session) -> User:
    """MANAGER user — allowed to see the review-queue aggregate."""
    u = User(
        email="manager_idor@example.com",
        name="Manager IDOR",
        role="manager",
        azure_id="manager-azure-idor",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


# ── owned requisition + offer + changelog ────────────────────────────────────


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


@pytest.fixture()
def owned_changelog(db_session: Session, owned_offer: Offer, sales_owner: User) -> ChangeLog:
    row = ChangeLog(
        entity_type="offer",
        entity_id=owned_offer.id,
        field_name="unit_price",
        old_value="2.0000",
        new_value="1.2345",
        user_id=sales_owner.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# ── F1: get_changelog ────────────────────────────────────────────────────────


class TestChangelogIDOR:
    """GET /api/changelog/offer/{id} must gate on require_requisition_access."""

    def test_unrelated_sales_gets_404(
        self,
        db_session: Session,
        sales_stranger: User,
        owned_offer: Offer,
        owned_changelog: ChangeLog,
    ):
        client = _client_for(db_session, sales_stranger)
        try:
            resp = client.get(f"/api/changelog/offer/{owned_offer.id}")
        finally:
            _teardown_client(client)
        assert resp.status_code == 404

    def test_owner_sales_gets_200(
        self,
        db_session: Session,
        sales_owner: User,
        owned_offer: Offer,
        owned_changelog: ChangeLog,
    ):
        client = _client_for(db_session, sales_owner)
        try:
            resp = client.get(f"/api/changelog/offer/{owned_offer.id}")
        finally:
            _teardown_client(client)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ── F2: list_offer_attachments ───────────────────────────────────────────────


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


# ── F3: list_review_queue ────────────────────────────────────────────────────


class TestReviewQueueIDOR:
    """GET /api/offers/review-queue must be restricted to managers/admins."""

    def test_non_manager_sales_gets_403(
        self,
        db_session: Session,
        sales_stranger: User,
    ):
        client = _client_for(db_session, sales_stranger)
        try:
            resp = client.get("/api/offers/review-queue")
        finally:
            _teardown_client(client)
        assert resp.status_code == 403

    def test_manager_gets_200(
        self,
        db_session: Session,
        manager: User,
    ):
        client = _client_for(db_session, manager)
        try:
            resp = client.get("/api/offers/review-queue")
        finally:
            _teardown_client(client)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
