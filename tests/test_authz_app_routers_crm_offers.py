"""Regression tests: requisition-ownership IDOR guards on app/routers/crm/offers.py.

A SALES (restricted) user who does NOT own the requisition behind an offer must be
blocked (404) from mutating/sending on that offer. Buyer/admin happy paths are
covered by the existing offer tests; here we assert the restricted-role lockout.
"""

import io

import pytest

from app.constants import UserRole
from app.models import Offer, OfferAttachment


@pytest.fixture()
def foreign_offer(db_session, test_requisition, test_user, admin_user):
    """An offer on a requisition owned by *someone else* (admin), so a SALES test_user
    is a non-owner.

    Offer.entered_by_id is also the admin so the owner_id fallback does not accidentally
    grant access.
    """
    test_requisition.created_by = admin_user.id
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=admin_user.id,
        status="pending_review",
        evidence_tier="T4",
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o


def _make_sales(test_user, db_session):
    test_user.role = UserRole.SALES
    db_session.commit()


def test_update_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.put(f"/api/offers/{foreign_offer.id}", json={"unit_price": 9.99})
    assert resp.status_code == 404


def test_delete_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.delete(f"/api/offers/{foreign_offer.id}")
    assert resp.status_code == 404


def test_reconfirm_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.put(f"/api/offers/{foreign_offer.id}/reconfirm")
    assert resp.status_code == 404


def test_approve_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.put(f"/api/offers/{foreign_offer.id}/approve")
    assert resp.status_code == 404


def test_reject_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.put(f"/api/offers/{foreign_offer.id}/reject")
    assert resp.status_code == 404


def test_promote_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/offers/{foreign_offer.id}/promote")
    assert resp.status_code == 404


def test_reject_t4_offer_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/offers/{foreign_offer.id}/reject")
    assert resp.status_code == 404


def test_upload_attachment_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.post(
        f"/api/offers/{foreign_offer.id}/attachments",
        files={"file": ("x.pdf", io.BytesIO(b"data"), "application/pdf")},
    )
    assert resp.status_code == 404


def test_attach_onedrive_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/offers/{foreign_offer.id}/attachments/onedrive", json={"item_id": "abc"})
    assert resp.status_code == 404


def test_delete_offer_attachment_blocks_non_owner_sales(client, db_session, test_user, foreign_offer):
    _make_sales(test_user, db_session)
    att = OfferAttachment(offer_id=foreign_offer.id, file_name="x.pdf")
    db_session.add(att)
    db_session.commit()
    db_session.refresh(att)
    resp = client.delete(f"/api/offer-attachments/{att.id}")
    assert resp.status_code == 404


# ── Happy-path sanity: an owning SALES user (created the requisition) is allowed. ──


def test_reconfirm_offer_allows_owning_sales(client, db_session, test_user, test_requisition):
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
    resp = client.put(f"/api/offers/{o.id}/reconfirm")
    assert resp.status_code == 200
