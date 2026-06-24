"""Regression test: requisition-ownership IDOR guard on mark_offer_sold.

PATCH /api/offers/{id}/mark-sold previously gated only on offer existence (bare
require_user). It now calls require_requisition_access exactly like its sibling offer
mutators (delete/reconfirm/approve/reject), so a SALES (restricted) non-owner is blocked
(404 — existence not leaked); the owning user passes.

Setup mirrors tests/test_authz_app_routers_crm_offers.py.
"""

import pytest

from app.constants import UserRole
from app.models import Offer


@pytest.fixture()
def foreign_offer(db_session, test_requisition, admin_user):
    """An offer on a requisition owned by admin (so SALES test_user is a non-owner).

    entered_by_id is admin too, so the owner_id fallback does not accidentally grant
    access.
    """
    test_requisition.created_by = admin_user.id
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=admin_user.id,
        status="active",
        evidence_tier="T4",
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def test_mark_sold_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.patch(f"/api/offers/{foreign_offer.id}/mark-sold")
    assert resp.status_code == 404
    db_session.refresh(foreign_offer)
    assert foreign_offer.status == "active"


def test_mark_sold_allows_owning_sales(client, db_session, test_user, test_requisition):
    """When the SALES user OWNS the requisition, the guard is a no-op."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow",
        mpn="LM317T",
        entered_by_id=test_user.id,
        status="active",
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    resp = client.patch(f"/api/offers/{o.id}/mark-sold")
    assert resp.status_code == 200
    assert resp.json()["status"] == "sold"


def test_mark_sold_missing_offer_404(client, db_session, test_user):
    resp = client.patch("/api/offers/999999/mark-sold")
    assert resp.status_code == 404
