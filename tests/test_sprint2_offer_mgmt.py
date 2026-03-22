"""test_sprint2_offer_mgmt.py — Tests for Sprint 2 offer management completion.

Verifies: Edit offer, delete offer, mark sold, review queue, promote/reject,
and changelog view.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Requirement, Requisition, User

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_offer(db_session: Session, test_user: User):
    """A requisition with one active offer."""
    req = Requisition(
        name="Sprint2 Test Req",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    r = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=500)
    db_session.add(r)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        requirement_id=r.id,
        vendor_name="Arrow",
        mpn="LM317T",
        unit_price=0.45,
        qty_available=5000,
        lead_time="2 weeks",
        status="active",
        entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    db_session.refresh(req)
    return req, offer


@pytest.fixture()
def pending_review_offer(db_session: Session, test_user: User):
    """An offer in pending_review status for review queue tests."""
    req = Requisition(
        name="Review Queue Req",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    offer = Offer(
        requisition_id=req.id,
        vendor_name="Mouser",
        mpn="STM32F103",
        unit_price=3.50,
        qty_available=1000,
        status="pending_review",
        evidence_tier="T4",
        parse_confidence=0.65,
        entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()
    db_session.refresh(offer)
    return req, offer


# ── Edit Offer ────────────────────────────────────────────────────────


class TestEditOffer:
    def test_edit_form_renders(self, client: TestClient, req_with_offer):
        req, offer = req_with_offer
        resp = client.get(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Edit Offer" in resp.text
        assert offer.vendor_name in resp.text

    def test_edit_saves_changes(self, client: TestClient, req_with_offer, db_session: Session):
        req, offer = req_with_offer
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "Mouser", "unit_price": "0.55", "qty_available": "3000"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.vendor_name == "Mouser"
        assert float(offer.unit_price) == 0.55
        assert offer.qty_available == 3000

    def test_edit_creates_changelog(self, client: TestClient, req_with_offer, db_session: Session):
        from app.models.intelligence import ChangeLog

        req, offer = req_with_offer
        client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "Digi-Key", "unit_price": "0.60"},
            headers={"HX-Request": "true"},
        )
        logs = (
            db_session.query(ChangeLog).filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == offer.id).all()
        )
        assert len(logs) >= 1
        field_names = {entry.field_name for entry in logs}
        assert "vendor_name" in field_names

    def test_edit_nonexistent_offer_404(self, client: TestClient, req_with_offer):
        req, _ = req_with_offer
        resp = client.get(
            f"/v2/partials/requisitions/{req.id}/offers/99999/edit-form",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Delete Offer ──────────────────────────────────────────────────────


class TestDeleteOffer:
    def test_delete_removes_offer(self, client: TestClient, req_with_offer, db_session: Session):
        req, offer = req_with_offer
        offer_id = offer.id
        resp = client.delete(
            f"/v2/partials/requisitions/{req.id}/offers/{offer_id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert db_session.get(Offer, offer_id) is None

    def test_delete_nonexistent_404(self, client: TestClient, req_with_offer):
        req, _ = req_with_offer
        resp = client.delete(
            f"/v2/partials/requisitions/{req.id}/offers/99999",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── Mark Sold ─────────────────────────────────────────────────────────


class TestMarkSold:
    def test_mark_sold(self, client: TestClient, req_with_offer, db_session: Session):
        req, offer = req_with_offer
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/mark-sold",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == "sold"

    def test_mark_sold_idempotent(self, client: TestClient, req_with_offer, db_session: Session):
        req, offer = req_with_offer
        offer.status = "sold"
        db_session.commit()
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/mark-sold",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── Review Queue ──────────────────────────────────────────────────────


class TestReviewQueue:
    def test_queue_renders(self, client: TestClient, pending_review_offer):
        _, offer = pending_review_offer
        resp = client.get(
            "/v2/partials/offers/review-queue",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "pending review" in resp.text
        assert offer.vendor_name in resp.text

    def test_queue_empty_state(self, client: TestClient):
        resp = client.get(
            "/v2/partials/offers/review-queue",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No offers pending review" in resp.text

    def test_promote_offer(self, client: TestClient, pending_review_offer, db_session: Session):
        _, offer = pending_review_offer
        resp = client.post(
            f"/v2/partials/offers/{offer.id}/promote",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == "active"

    def test_reject_offer(self, client: TestClient, pending_review_offer, db_session: Session):
        _, offer = pending_review_offer
        resp = client.post(
            f"/v2/partials/offers/{offer.id}/reject",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == "rejected"

    def test_promote_non_pending_fails(self, client: TestClient, req_with_offer):
        _, offer = req_with_offer
        resp = client.post(
            f"/v2/partials/offers/{offer.id}/promote",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400


# ── Changelog ─────────────────────────────────────────────────────────


class TestChangelog:
    def test_changelog_renders_empty(self, client: TestClient, req_with_offer):
        _, offer = req_with_offer
        resp = client.get(
            f"/v2/partials/offers/{offer.id}/changelog",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Change History" in resp.text
        assert "No changes recorded" in resp.text

    def test_changelog_shows_edits(self, client: TestClient, req_with_offer, db_session: Session):
        req, offer = req_with_offer
        # Make an edit to generate changelog
        client.post(
            f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/edit",
            data={"vendor_name": "NewVendor"},
            headers={"HX-Request": "true"},
        )
        resp = client.get(
            f"/v2/partials/offers/{offer.id}/changelog",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "vendor_name" in resp.text

    def test_changelog_nonexistent_404(self, client: TestClient):
        resp = client.get(
            "/v2/partials/offers/99999/changelog",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404
