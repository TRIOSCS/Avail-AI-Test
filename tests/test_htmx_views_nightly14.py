"""tests/test_htmx_views_nightly14.py — Coverage for offer management, bulk actions, inline-edit.

Targets:
  - review_offer (approve/reject)
  - add_offer_form / add_offer
  - reconfirm_offer
  - edit_offer_form / edit_offer
  - delete_offer_htmx
  - mark_offer_sold_htmx
  - save_parsed_offers
  - requisitions_bulk_action
  - create_quote_from_offers
  - requisition_inline_edit_cell
  - requisition_inline_save
  - requisition_row_action

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import OfferStatus, RequisitionStatus
from app.models import (
    Offer,
    Requirement,
    Requisition,
    User,
)
from app.models.quotes import Quote, QuoteLine


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_offer(db: Session, req: Requisition, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        vendor_name="TestVendor",
        mpn="BC547",
        status=OfferStatus.ACTIVE,
        source="manual",
        entered_by_id=user.id,
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_requirement(db: Session, req: Requisition, mpn: str = "BC547", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=10,
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── Review Offer ──────────────────────────────────────────────────────────


class TestReviewOffer:
    def test_approve_offer(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/review",
            data={"action": "approve"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.APPROVED

    def test_reject_offer(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/review",
            data={"action": "reject"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.REJECTED

    def test_invalid_action(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/review",
            data={"action": "frobnicate"},
        )
        assert resp.status_code == 400

    def test_offer_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/99999/review",
            data={"action": "approve"},
        )
        assert resp.status_code == 404


# ── Add Offer Form ────────────────────────────────────────────────────────


class TestAddOfferForm:
    def test_get_form(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/add-offer-form")
        assert resp.status_code == 200

    def test_req_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/add-offer-form")
        assert resp.status_code == 404


# ── Add Offer ─────────────────────────────────────────────────────────────


class TestAddOffer:
    def test_add_offer_success(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
            data={"vendor_name": "NewVendor", "mpn": "LM317T", "qty_available": "100", "unit_price": "1.25"},
        )
        assert resp.status_code == 200

    def test_missing_vendor_name(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
            data={"vendor_name": "", "mpn": "LM317T"},
        )
        assert resp.status_code == 400

    def test_missing_mpn(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
            data={"vendor_name": "Vendor", "mpn": ""},
        )
        assert resp.status_code == 400

    def test_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/add-offer",
            data={"vendor_name": "V", "mpn": "M"},
        )
        assert resp.status_code == 404


# ── Reconfirm Offer ───────────────────────────────────────────────────────


class TestReconfirmOffer:
    def test_reconfirm_success(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/reconfirm",
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert (offer.reconfirm_count or 0) >= 1

    def test_reconfirm_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/99999/reconfirm",
        )
        assert resp.status_code == 404


# ── Edit Offer Form ───────────────────────────────────────────────────────


class TestEditOfferForm:
    def test_get_edit_form(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user)
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/edit-form",
        )
        assert resp.status_code == 200

    def test_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/99999/edit-form",
        )
        assert resp.status_code == 404


# ── Edit Offer ────────────────────────────────────────────────────────────


class TestEditOffer:
    def test_edit_offer_success(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, vendor_name="OldVendor")
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/edit",
            data={"vendor_name": "UpdatedVendor", "qty_available": "200", "unit_price": "2.50"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.vendor_name == "UpdatedVendor"

    def test_edit_offer_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/99999/edit",
            data={"vendor_name": "X"},
        )
        assert resp.status_code == 404

    def test_edit_offer_invalid_int(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        """Non-numeric int field is silently skipped."""
        offer = _make_offer(db_session, test_requisition, test_user)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/edit",
            data={"qty_available": "notanumber"},
        )
        assert resp.status_code == 200


# ── Delete Offer ──────────────────────────────────────────────────────────


class TestDeleteOffer:
    def test_delete_success(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user)
        oid = offer.id
        resp = client.delete(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{oid}",
        )
        assert resp.status_code == 200
        assert db_session.get(Offer, oid) is None

    def test_delete_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.delete(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/99999",
        )
        assert resp.status_code == 404


# ── Mark Offer Sold ───────────────────────────────────────────────────────


class TestMarkOfferSold:
    def test_mark_sold_success(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.PENDING_REVIEW)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/mark-sold",
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == OfferStatus.SOLD

    def test_already_sold_returns_tab(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, status=OfferStatus.SOLD)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/{offer.id}/mark-sold",
        )
        assert resp.status_code == 200

    def test_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/offers/99999/mark-sold",
        )
        assert resp.status_code == 404


# ── Save Parsed Offers ────────────────────────────────────────────────────


class TestSaveParsedOffers:
    def test_no_offers_returns_message(self, client: TestClient, test_requisition: Requisition):
        """Submitting form with no offer rows returns the 'No offers to save' message."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/save-parsed-offers",
            data={"vendor_name": "AcmeCorp"},
        )
        assert resp.status_code == 200
        assert b"No offers to save" in resp.content

    def test_saves_offers_from_form(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Submitting offers[0].mpn saves the offer."""
        _make_requirement(db_session, test_requisition, mpn="BC547")
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/save-parsed-offers",
            data={
                "vendor_name": "AcmeCorp",
                "offers[0].mpn": "BC547",
                "offers[0].qty_available": "500",
                "offers[0].unit_price": "0.25",
                "offers[0].condition": "new",
            },
        )
        assert resp.status_code == 200

    def test_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/save-parsed-offers",
            data={"vendor_name": "V"},
        )
        assert resp.status_code == 404


# ── Requisitions Bulk Action ──────────────────────────────────────────────


class TestRequisitionsBulkAction:
    def test_bulk_archive(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.status == RequisitionStatus.ARCHIVED

    def test_bulk_activate(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/activate",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.status == RequisitionStatus.ACTIVE

    def test_bulk_assign(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(test_requisition.id), "owner_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.created_by == test_user.id

    def test_bulk_assign_invalid_owner_id(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/assign",
            data={"ids": str(test_requisition.id), "owner_id": "notanint"},
        )
        assert resp.status_code == 400

    def test_bulk_invalid_action(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/destroy",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 400

    def test_bulk_no_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={},
        )
        assert resp.status_code == 400

    def test_bulk_invalid_id_format(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": "abc,def"},
        )
        assert resp.status_code == 400


# ── Create Quote From Offers ──────────────────────────────────────────────


class TestCreateQuoteFromOffers:
    def test_create_quote_success(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        offer = _make_offer(db_session, test_requisition, test_user, qty_available=10, unit_price=1.5)
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": str(offer.id)},
        )
        assert resp.status_code == 200

    def test_no_offers_selected(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={},
        )
        assert resp.status_code == 400

    def test_no_matching_offers(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": "99999"},
        )
        assert resp.status_code == 404

    def test_invalid_offer_id_format(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
            data={"offer_ids": "abc"},
        )
        assert resp.status_code == 400


# ── Requisition Inline Edit Cell ─────────────────────────────────────────


class TestRequisitionInlineEditCell:
    def test_get_name_field(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/edit/name")
        assert resp.status_code == 200

    def test_get_owner_field(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/edit/owner")
        assert resp.status_code == 200

    def test_get_urgency_field(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/edit/urgency")
        assert resp.status_code == 200

    def test_invalid_field(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/edit/secret_field")
        assert resp.status_code == 400

    def test_req_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/edit/name")
        assert resp.status_code == 404


# ── Requisition Inline Save ───────────────────────────────────────────────


class TestRequisitionInlineSave:
    def test_save_name(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "name", "value": "New Req Name", "context": "row"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.name == "New Req Name"

    def test_save_urgency(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "urgency", "value": "hot", "context": "row"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.urgency == "hot"

    def test_save_deadline(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "deadline", "value": "2026-12-31", "context": "row"},
        )
        assert resp.status_code == 200

    def test_save_owner(self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "owner", "value": str(test_user.id), "context": "row"},
        )
        assert resp.status_code == 200

    def test_save_tab_context(self, client: TestClient, test_requisition: Requisition):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "name", "value": "Tab Context Name", "context": "tab"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger")

    def test_req_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/requisitions/99999/inline",
            data={"field": "name", "value": "X", "context": "row"},
        )
        assert resp.status_code == 404


# ── Requisition Row Action ────────────────────────────────────────────────


class TestRequisitionRowAction:
    def test_invalid_action(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/destroy",
            data={},
        )
        assert resp.status_code == 400

    def test_req_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/action/archive",
            data={},
        )
        assert resp.status_code == 404

    def test_archive_action(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/archive",
            data={},
        )
        assert resp.status_code == 200

    def test_activate_action(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/activate",
            data={},
        )
        assert resp.status_code == 200

    def test_claim_action(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/claim",
            data={},
        )
        assert resp.status_code == 200

    def test_unclaim_action(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/unclaim",
            data={},
        )
        assert resp.status_code == 200

    def test_clone_action(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/clone",
            data={},
        )
        assert resp.status_code == 200

    def test_return_detail_format(self, client: TestClient, test_requisition: Requisition):
        """return=detail format returns empty 200 response."""
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/activate",
            data={"return": "detail"},
        )
        assert resp.status_code == 200
