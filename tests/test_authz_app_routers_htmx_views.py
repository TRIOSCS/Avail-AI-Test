"""Authz regression tests for requisition-ownership IDOR guards in htmx_views.py.

A SALES/TRADER user may only act on requisitions they created. These tests flip the
test user's role to SALES and re-own the requisition to someone else (admin_user),
then assert each mutating/sending endpoint returns 404 (existence not leaked).

The `client` fixture overrides `require_user` to return the *same* `test_user`
object, so mutating `test_user.role` is observed by the endpoint at request time.
"""

from datetime import datetime, timezone

import pytest

from app.constants import OfferStatus, UserRole
from app.models import Offer, Requirement, Requisition
from app.models.offers import VendorResponse


@pytest.fixture()
def foreign_req(db_session, test_requisition, admin_user):
    """test_requisition re-owned by admin_user (so test_user-as-SALES is a non-
    owner)."""
    test_requisition.created_by = admin_user.id
    db_session.commit()
    return test_requisition


def _make_sales(db_session, test_user):
    test_user.role = UserRole.SALES
    db_session.commit()


def _requirement_id(db_session, req: Requisition) -> int:
    return db_session.query(Requirement).filter_by(requisition_id=req.id).first().id


def _make_offer(db_session, req: Requisition, *, status="active") -> Offer:
    rid = _requirement_id(db_session, req)
    offer = Offer(
        requisition_id=req.id,
        requirement_id=rid,
        vendor_name="Foreign Vendor",
        mpn="LM317T",
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return offer


# ── HIGH severity ──────────────────────────────────────────────────────


def test_rfq_send_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/rfq-send",
        data={"vendor_names": "Acme", "vendor_emails": "a@b.com", "subject": "x", "body": "y"},
    )
    assert resp.status_code == 404


def test_create_quote_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/create-quote",
        data={"offer_ids": str(offer.id)},
    )
    assert resp.status_code == 404


def test_mark_sold_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req, status=OfferStatus.APPROVED)
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/offers/{offer.id}/mark-sold",
    )
    assert resp.status_code == 404


def test_review_offer_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req, status=OfferStatus.PENDING_REVIEW)
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/offers/{offer.id}/review",
        data={"action": "approve"},
    )
    assert resp.status_code == 404


def test_promote_offer_queue_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req, status="pending_review")
    _make_sales(db_session, test_user)
    resp = client.post(f"/v2/partials/offers/{offer.id}/promote")
    assert resp.status_code == 404


def test_reject_offer_queue_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req, status="pending_review")
    _make_sales(db_session, test_user)
    resp = client.post(f"/v2/partials/offers/{offer.id}/reject")
    assert resp.status_code == 404


# ── MED severity ───────────────────────────────────────────────────────


def test_add_offer_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/add-offer",
        data={"vendor_name": "V", "mpn": "LM317T"},
    )
    assert resp.status_code == 404


def test_edit_offer_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/offers/{offer.id}/edit",
        data={"vendor_name": "Changed"},
    )
    assert resp.status_code == 404


def test_delete_offer_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.delete(f"/v2/partials/requisitions/{foreign_req.id}/offers/{offer.id}")
    assert resp.status_code == 404


def test_reconfirm_offer_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    offer = _make_offer(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.post(f"/v2/partials/requisitions/{foreign_req.id}/offers/{offer.id}/reconfirm")
    assert resp.status_code == 404


def test_add_requirement_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/requirements",
        data={"primary_mpn": "ABC123", "manufacturer": "TI"},
    )
    assert resp.status_code == 404


def test_update_requirement_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.put(
        f"/v2/partials/requisitions/{foreign_req.id}/requirements/{rid}",
        data={"primary_mpn": "LM317T", "manufacturer": "TI"},
    )
    assert resp.status_code == 404


def test_delete_requirement_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.delete(f"/v2/partials/requisitions/{foreign_req.id}/requirements/{rid}")
    assert resp.status_code == 404


def test_save_parsed_offers_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/save-parsed-offers",
        data={"vendor_name": "V"},
    )
    assert resp.status_code == 404


def test_log_phone_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/log-phone",
        data={"vendor_name": "V", "vendor_phone": "555"},
    )
    assert resp.status_code == 404


def test_response_status_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    vr = VendorResponse(requisition_id=foreign_req.id, vendor_name="V", status="new")
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)
    _make_sales(db_session, test_user)
    resp = client.patch(
        f"/v2/partials/requisitions/{foreign_req.id}/responses/{vr.id}/status",
        data={"status": "reviewed"},
    )
    assert resp.status_code == 404


def test_review_response_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    # review_response_htmx loads app.models.offers.VendorResponse by (id, requisition_id).
    vr = VendorResponse(requisition_id=foreign_req.id, vendor_name="V", status="new")
    db_session.add(vr)
    db_session.commit()
    db_session.refresh(vr)
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/responses/{vr.id}/review",
        data={"status": "reviewed"},
    )
    assert resp.status_code == 404


def test_archive_requisition_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.patch(f"/v2/partials/requisitions/{foreign_req.id}/archive")
    assert resp.status_code == 404


def test_unarchive_requisition_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.patch(f"/v2/partials/requisitions/{foreign_req.id}/unarchive")
    assert resp.status_code == 404


def test_part_header_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.patch(
        f"/v2/partials/parts/{rid}/header",
        data={"field": "target_qty", "value": "5"},
    )
    assert resp.status_code == 404


def test_part_cell_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.patch(
        f"/v2/partials/parts/{rid}/cell",
        data={"field": "target_qty", "value": "5"},
    )
    assert resp.status_code == 404


def test_part_save_spec_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.patch(
        f"/v2/partials/parts/{rid}/save-spec",
        data={"field": "condition", "value": "New"},
    )
    assert resp.status_code == 404


def test_part_notes_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.patch(f"/v2/partials/parts/{rid}/notes", data={"sale_notes": "hi"})
    assert resp.status_code == 404


def test_part_tasks_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.post(f"/v2/partials/parts/{rid}/tasks", data={"title": "T"})
    assert resp.status_code == 404


def test_part_archive_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    rid = _requirement_id(db_session, foreign_req)
    _make_sales(db_session, test_user)
    resp = client.patch(f"/v2/partials/parts/{rid}/archive")
    assert resp.status_code == 404


def test_log_activity_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(
        f"/v2/partials/requisitions/{foreign_req.id}/log-activity",
        data={"activity_type": "note", "notes": "x"},
    )
    assert resp.status_code == 404


def test_search_all_blocks_non_owner_sales(client, db_session, foreign_req, test_user):
    _make_sales(db_session, test_user)
    resp = client.post(f"/v2/partials/requisitions/{foreign_req.id}/search-all")
    assert resp.status_code == 404


# ── Happy path: owner (buyer) still allowed ────────────────────────────


def test_owner_buyer_can_log_activity(client, db_session, test_requisition, test_user):
    # test_user is buyer and owns test_requisition by default — must NOT 404.
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/log-activity",
        data={"activity_type": "note", "notes": "owner note"},
    )
    assert resp.status_code == 200


def test_sales_owner_can_add_offer(client, db_session, test_requisition, test_user):
    # SALES user who OWNS the requisition is allowed through the guard.
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
        data={"vendor_name": "V", "mpn": "LM317T"},
    )
    assert resp.status_code == 200
