"""tests/test_enrichment_authz.py — Role-parity regression guard for enrichment
endpoints.

Two enrichment routes returned/mutated provider data (paid spend + contact PII)
behind only ``require_user`` while their sibling vendor-write route gates with
``require_buyer``:

  E1: enrich_vendor_card       (POST /api/enrich/vendor/{card_id})
  E2: get_suggested_contacts   (GET  /api/suggested-contacts)

These are role-parity gaps, NOT object-scoped IDOR — the data is freshly pulled
from external providers for an arbitrary card/domain, so there is no owned row to
scope with can_manage_account. The correct gate is require_buyer, mirroring
add_suggested_to_vendor. The "stranger" that proves the gap is therefore the
non-interactive AGENT service account (the only role require_user admits but
require_buyer rejects — see require_buyer's docstring), NOT a buyer-owning-nothing
(a plain buyer legitimately passes a role gate).

Each endpoint asserts:
  * AGENT service account  -> 403 (require_buyer rejects it before any provider spend)
  * a real buyer           -> 200 (passes the role gate; provider calls are mocked)

Called by: pytest
Depends on: conftest.py fixtures (db_session), app.routers.crm.enrichment, app.dependencies
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import UserRole
from app.models.auth import User
from app.models.vendors import VendorCard

# ── fixtures ─────────────────────────────────────────────────────────────────


def _client_for(db_session: Session, user: User) -> Iterator[TestClient]:
    """A TestClient authenticated as *user*.

    Overrides require_user to return *user*, and re-implements require_buyer's REAL role
    check against *user* (require_buyer calls require_user directly, not via DI, so it
    cannot be exercised through a plain require_user override — we must model the gate
    itself). This lets an agent-role user be rejected (403) while a buyer passes,
    exactly as the production dependency would behave.
    """
    from app.database import get_db
    from app.dependencies import has_buyer_role, require_buyer, require_user
    from app.main import app

    def _require_buyer_override() -> User:
        if not has_buyer_role(user):
            raise HTTPException(403, "Buyer role required for this action")
        return user

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_buyer] = _require_buyer_override

    with TestClient(app) as c:
        yield c

    for dep in [get_db, require_user, require_buyer]:
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def agent_user(db_session: Session) -> User:
    """The non-interactive AGENT service account — reaches require_user routes only."""
    u = User(
        email="agent_authz@example.com",
        name="Agent Authz",
        role=UserRole.AGENT,
        azure_id="agent-azure-authz",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def buyer_user(db_session: Session) -> User:
    """A plain buyer — the legitimately-allowed caller for buyer-tier actions."""
    u = User(
        email="buyer_authz@example.com",
        name="Buyer Authz",
        role=UserRole.BUYER,
        azure_id="buyer-azure-authz",
        created_at=datetime.now(UTC),
    )
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def agent_client(db_session: Session, agent_user: User) -> Iterator[TestClient]:
    yield from _client_for(db_session, agent_user)


@pytest.fixture()
def buyer_client(db_session: Session, buyer_user: User) -> Iterator[TestClient]:
    yield from _client_for(db_session, buyer_user)


@pytest.fixture()
def vendor_card(db_session: Session) -> VendorCard:
    """A vendor card with a domain set, so enrich_vendor_card proceeds past the 400."""
    card = VendorCard(
        normalized_name="authz-vendor-co",
        display_name="Authz Vendor Co",
        domain="vendor-authz.com",
        is_active=True,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


# ── E1: enrich_vendor_card ───────────────────────────────────────────────────


class TestEnrichVendorCardRoleParity:
    """enrich_vendor_card must gate on require_buyer (parity with
    add_suggested_to_vendor)."""

    def test_agent_gets_403(self, agent_client: TestClient, vendor_card: VendorCard):
        """The agent service account must be rejected before any provider spend."""
        resp = agent_client.post(f"/api/enrich/vendor/{vendor_card.id}")
        assert resp.status_code == 403

    def test_buyer_gets_200(self, buyer_client: TestClient, vendor_card: VendorCard):
        """A buyer passes the role gate (external providers mocked)."""
        with (
            patch("app.routers.crm.enrichment._require_enrichment_provider", return_value=None),
            patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value={}),
            patch("app.enrichment_service.apply_enrichment_to_vendor", return_value=[]),
        ):
            resp = buyer_client.post(f"/api/enrich/vendor/{vendor_card.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── E2: get_suggested_contacts ───────────────────────────────────────────────


class TestGetSuggestedContactsRoleParity:
    """get_suggested_contacts must gate on require_buyer — paid provider PII lookup."""

    def test_agent_gets_403(self, agent_client: TestClient):
        """The agent service account must not pull provider contact PII for any
        domain."""
        resp = agent_client.get("/api/suggested-contacts", params={"domain": "example.com"})
        assert resp.status_code == 403

    def test_buyer_gets_200(self, buyer_client: TestClient):
        """A buyer passes the role gate (provider lookup mocked)."""
        with (
            patch("app.routers.crm.enrichment._require_enrichment_provider", return_value=None),
            patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        ):
            resp = buyer_client.get("/api/suggested-contacts", params={"domain": "example.com"})
        assert resp.status_code == 200
        assert resp.json()["domain"] == "example.com"
