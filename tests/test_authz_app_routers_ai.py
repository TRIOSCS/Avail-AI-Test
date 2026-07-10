"""Authz regression tests for app/routers/ai.py requisition-ownership IDOR guards.

Restricted roles (SALES/TRADER) must not act on requisition-scoped resources they do not
own. Buyer/manager/admin remain unrestricted. We act as a restricted non-owner by
flipping the test user's role and reassigning ownership to another user, then assert the
mutating endpoints return 404.
"""

from datetime import UTC, datetime

import pytest

from app.config import settings
from app.constants import UserRole
from app.models import Requirement, VendorResponse


@pytest.fixture()
def ai_enabled(monkeypatch):
    """Force AI features on so ownership guards (which run after the AI gate) are
    reached."""
    monkeypatch.setattr(settings, "ai_features_enabled", "all")
    yield


def _other_requirement(db_session, admin_user):
    """A requirement under a requisition owned by someone other than test_user."""
    from app.models import Requisition

    req = Requisition(
        name="REQ-OTHER-OWNER",
        customer_name="Other Co",
        status="open",
        created_by=admin_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=10,
        created_at=datetime.now(UTC),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


# ── #48 POST /api/ai/generate-description/{requirement_id} ────────────────


def test_generate_description_blocks_non_owner_sales(client, db_session, test_user, admin_user):
    test_user.role = UserRole.SALES
    item = _other_requirement(db_session, admin_user)
    db_session.commit()

    resp = client.post(f"/api/ai/generate-description/{item.id}")
    assert resp.status_code == 404


def test_generate_description_blocks_non_owner_trader(client, db_session, test_user, admin_user):
    test_user.role = UserRole.TRADER
    item = _other_requirement(db_session, admin_user)
    db_session.commit()

    resp = client.post(f"/api/ai/generate-description/{item.id}")
    assert resp.status_code == 404


def test_generate_description_allows_buyer(client, db_session, test_user, test_requisition):
    # test_user is a buyer (unrestricted); test_requisition owned by test_user.
    item = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first()
    resp = client.post(f"/api/ai/generate-description/{item.id}")
    # Buyer passes the ownership guard; not a 404 from the guard.
    assert resp.status_code != 404


# ── #49 POST /api/ai/parse-response/{response_id} ─────────────────────────


def test_parse_response_blocks_non_owner_sales(client, db_session, test_user, admin_user, ai_enabled):
    test_user.role = UserRole.SALES
    item = _other_requirement(db_session, admin_user)
    vr = VendorResponse(
        requisition_id=item.requisition_id,
        vendor_name="Arrow",
        subject="re: rfq",
        body="price is $1",
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    resp = client.post(f"/api/ai/parse-response/{vr.id}")
    assert resp.status_code == 404


def test_parse_response_blocks_non_owner_trader(client, db_session, test_user, admin_user, ai_enabled):
    test_user.role = UserRole.TRADER
    item = _other_requirement(db_session, admin_user)
    vr = VendorResponse(
        requisition_id=item.requisition_id,
        vendor_name="Arrow",
        subject="re: rfq",
        body="price is $1",
    )
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)

    resp = client.post(f"/api/ai/parse-response/{vr.id}")
    assert resp.status_code == 404
