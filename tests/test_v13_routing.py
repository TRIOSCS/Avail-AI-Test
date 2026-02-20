"""
test_v13_routing.py — v1.3 Routing, Buyer Profiles & Misc Endpoint Tests

Tests routing assignments, claim flow, scoring preview, buyer profiles
(mocked — ARRAY columns incompatible with SQLite), offer reconfirm,
admin reload, and Graph webhook.

NOTE: Tests that hit routing_service directly fail in SQLite because
_assignment_to_dict subtracts tz-aware now() from tz-naive SQLite
timestamps.  We mock the service layer for those tests; the service
itself is tested separately with timezone-safe fixtures.

Covers: routing/*, buyer-profiles/*, offers/reconfirm,
        admin/reload-routing-maps, webhooks/graph
Called by: pytest
Depends on: conftest (client, test_user, test_requisition, test_vendor_card)
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, RoutingAssignment

# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def admin_client(db_session, admin_user):
    """TestClient authenticated as admin user."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_buyer] = lambda: admin_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def test_assignment(
    db_session: Session, test_user, test_requisition, test_vendor_card,
) -> RoutingAssignment:
    """A routing assignment with test_user as buyer_1."""
    req_item = test_requisition.requirements[0]
    now = datetime.now(timezone.utc)
    assignment = RoutingAssignment(
        requirement_id=req_item.id,
        vendor_card_id=test_vendor_card.id,
        buyer_1_id=test_user.id,
        buyer_1_score=85.0,
        assigned_at=now,
        expires_at=now + timedelta(hours=48),
        status="active",
    )
    db_session.add(assignment)
    db_session.commit()
    db_session.refresh(assignment)
    return assignment


@pytest.fixture()
def test_offer(
    db_session: Session, test_user, test_requisition, test_vendor_card,
) -> Offer:
    """An active offer for reconfirm testing."""
    req_item = test_requisition.requirements[0]
    offer = Offer(
        requisition_id=test_requisition.id,
        requirement_id=req_item.id,
        vendor_card_id=test_vendor_card.id,
        vendor_name="Test Vendor",
        mpn="TEST-PART-001",
        unit_price=1.25,
        qty_available=500,
        lead_time="14 days",
        condition="New",
        status="active",
        entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


# ═══════════════════════════════════════════════════════════════════════
#  MY ASSIGNMENTS — mock service to avoid SQLite tz-naive bug
# ═══════════════════════════════════════════════════════════════════════

def test_my_assignments_empty(client):
    resp = client.get("/api/routing/my-assignments")
    assert resp.status_code == 200
    assert resp.json() == []


def test_my_assignments_shows_active(client, test_assignment):
    """Mock service because _assignment_to_dict has tz-naive bug in SQLite."""
    fake = [{"id": test_assignment.id, "status": "active", "score": 85}]
    with patch(
        "app.services.routing_service.get_active_assignments_for_buyer",
        return_value=fake,
    ):
        resp = client.get("/api/routing/my-assignments")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["id"] == test_assignment.id


# ═══════════════════════════════════════════════════════════════════════
#  ASSIGNMENT DETAIL
# ═══════════════════════════════════════════════════════════════════════

def test_assignment_detail_found(client, test_assignment):
    fake = {"id": test_assignment.id, "status": "active", "buyers": []}
    with patch(
        "app.services.routing_service.get_assignment_details",
        return_value=fake,
    ):
        resp = client.get(f"/api/routing/assignments/{test_assignment.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == test_assignment.id


def test_assignment_detail_not_found(client):
    resp = client.get("/api/routing/assignments/99999")
    assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
#  CLAIM ROUTING
# ═══════════════════════════════════════════════════════════════════════

def test_claim_routing_success(client, test_assignment):
    with patch(
        "app.services.routing_service.claim_routing",
        return_value={"success": True, "message": "Claimed"},
    ):
        resp = client.post(
            f"/api/routing/assignments/{test_assignment.id}/claim",
        )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_claim_routing_already_claimed(
    client, db_session, test_assignment, test_user,
):
    test_assignment.status = "claimed"
    test_assignment.claimed_by_id = test_user.id
    db_session.commit()
    resp = client.post(
        f"/api/routing/assignments/{test_assignment.id}/claim",
    )
    assert resp.status_code == 409


def test_claim_routing_expired(client, db_session, test_assignment):
    test_assignment.status = "expired"
    db_session.commit()
    resp = client.post(
        f"/api/routing/assignments/{test_assignment.id}/claim",
    )
    assert resp.status_code == 409


def test_claim_routing_not_found(client):
    resp = client.post("/api/routing/assignments/99999/claim")
    assert resp.status_code == 409


# ═══════════════════════════════════════════════════════════════════════
#  ROUTING SCORE PREVIEW
# ═══════════════════════════════════════════════════════════════════════

def test_score_routing_returns_list(client, test_requisition, test_vendor_card):
    req_item = test_requisition.requirements[0]
    fake = [{"buyer_id": 1, "total": 72}]
    with patch(
        "app.services.routing_service.rank_buyers_for_assignment",
        return_value=fake,
    ):
        resp = client.post("/api/routing/score", json={
            "requirement_id": req_item.id,
            "vendor_card_id": test_vendor_card.id,
        })
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_score_routing_invalid_ids(client):
    resp = client.post("/api/routing/score", json={
        "requirement_id": -1,
        "vendor_card_id": 1,
    })
    assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
#  CREATE ROUTING (admin only)
# ═══════════════════════════════════════════════════════════════════════

def test_create_routing_non_admin_forbidden(
    client, test_requisition, test_vendor_card,
):
    req_item = test_requisition.requirements[0]
    resp = client.post("/api/routing/create", json={
        "requirement_id": req_item.id,
        "vendor_card_id": test_vendor_card.id,
    })
    assert resp.status_code == 403


def test_create_routing_admin_success(
    admin_client, monkeypatch, test_requisition, test_vendor_card,
):
    from app import config as cfg
    monkeypatch.setattr(cfg.settings, "admin_emails", ["admin@trioscs.com"])

    req_item = test_requisition.requirements[0]
    fake_assignment = SimpleNamespace(id=99)
    fake_detail = {"id": 99, "status": "active"}
    with (
        patch(
            "app.services.routing_service.create_routing_assignment",
            return_value=fake_assignment,
        ),
        patch(
            "app.services.routing_service.notify_routing_assignment",
            return_value=None,
        ),
        patch(
            "app.services.routing_service.get_assignment_details",
            return_value=fake_detail,
        ),
    ):
        resp = admin_client.post("/api/routing/create", json={
            "requirement_id": req_item.id,
            "vendor_card_id": test_vendor_card.id,
        })
    assert resp.status_code == 200
    assert resp.json()["id"] == 99


# ═══════════════════════════════════════════════════════════════════════
#  OFFER RECONFIRM
# ═══════════════════════════════════════════════════════════════════════

def test_reconfirm_offer_success(client, test_offer):
    with patch(
        "app.services.routing_service.reconfirm_offer",
        return_value={"success": True, "new_expires_at": "2026-03-01"},
    ):
        resp = client.post(f"/api/offers/{test_offer.id}/reconfirm")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_reconfirm_offer_not_found(client):
    resp = client.post("/api/offers/99999/reconfirm")
    assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
#  BUYER PROFILES (mock buyer_service — ARRAY cols need PostgreSQL)
# ═══════════════════════════════════════════════════════════════════════

def test_list_buyer_profiles(client):
    with patch("app.services.buyer_service.list_profiles", return_value=[]):
        resp = client.get("/api/buyer-profiles")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_buyer_profile_found(client):
    fake = SimpleNamespace(
        user_id=1, primary_commodity="Semiconductors",
        secondary_commodity="Passives", primary_geography="US",
        brand_specialties=["TI", "Analog Devices"],
        brand_material_types=[], brand_usage_types=[],
        updated_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    with patch("app.services.buyer_service.get_profile", return_value=fake):
        resp = client.get("/api/buyer-profiles/1")
    assert resp.status_code == 200
    assert resp.json()["primary_commodity"] == "Semiconductors"
    assert "TI" in resp.json()["brand_specialties"]


def test_get_buyer_profile_not_found(client):
    with patch("app.services.buyer_service.get_profile", return_value=None):
        resp = client.get("/api/buyer-profiles/999")
    assert resp.status_code == 404


def test_upsert_buyer_profile_self(client, test_user, db_session):
    """Buyer can update their own profile."""
    test_user.role = "buyer"
    db_session.commit()
    fake = SimpleNamespace(
        user_id=test_user.id, primary_commodity="IC",
        secondary_commodity=None, primary_geography="Asia",
        brand_specialties=[], brand_material_types=[],
        brand_usage_types=[],
    )
    with patch("app.services.buyer_service.upsert_profile", return_value=fake):
        resp = client.put(
            f"/api/buyer-profiles/{test_user.id}",
            json={"primary_commodity": "IC", "primary_geography": "Asia"},
        )
    assert resp.status_code == 200
    assert resp.json()["primary_commodity"] == "IC"


def test_upsert_buyer_profile_other_forbidden(client, test_user):
    """Non-admin cannot edit another buyer's profile."""
    resp = client.put(
        f"/api/buyer-profiles/{test_user.id + 100}",
        json={"primary_commodity": "IC"},
    )
    assert resp.status_code == 403


# ═══════════════════════════════════════════════════════════════════════
#  ADMIN: RELOAD ROUTING MAPS
# ═══════════════════════════════════════════════════════════════════════

def test_reload_routing_maps_non_admin_forbidden(client):
    resp = client.post("/api/admin/reload-routing-maps")
    assert resp.status_code == 403


def test_reload_routing_maps_admin(admin_client, monkeypatch):
    from app import config as cfg
    monkeypatch.setattr(cfg.settings, "admin_emails", ["admin@trioscs.com"])

    with (
        patch("app.routing_maps.load_routing_maps"),
        patch(
            "app.routing_maps.get_brand_commodity_map",
            return_value={"TI": "Semi"},
        ),
        patch(
            "app.routing_maps.get_country_region_map",
            return_value={"US": "NA"},
        ),
    ):
        resp = admin_client.post("/api/admin/reload-routing-maps")
    assert resp.status_code == 200
    assert resp.json()["status"] == "reloaded"
    assert resp.json()["brands"] == 1


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH WEBHOOK
# ═══════════════════════════════════════════════════════════════════════

def test_graph_webhook_validation_token(client):
    """Graph webhook echoes validationToken for subscription setup."""
    resp = client.post(
        "/api/webhooks/graph?validationToken=abc123",
        content="",
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 200
