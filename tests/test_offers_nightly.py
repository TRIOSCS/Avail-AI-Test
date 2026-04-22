"""test_offers_nightly.py — Nightly coverage tests for app/routers/crm/offers.py.

Targets lines 62, 282, 938-948, 976-990, 1004-1017 (review queue + promote/reject endpoints).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Offer, Requisition

# ── helpers ──────────────────────────────────────────────────────────


def _make_requisition(db: Session, user_id: int, status: str = "active") -> Requisition:
    req = Requisition(
        name="Test Req",
        status=status,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_offer(
    db: Session,
    req_id: int,
    user_id: int,
    status: str = "active",
    evidence_tier: str | None = None,
) -> Offer:
    offer = Offer(
        requisition_id=req_id,
        vendor_name="TestVendor",
        mpn="ABC123",
        qty_available=100,
        unit_price=1.00,
        entered_by_id=user_id,
        status=status,
        evidence_tier=evidence_tier,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()
    return offer


# ── list_offers — 404 path (line 62) ─────────────────────────────────


class TestListOffers:
    def test_list_offers_missing_req_returns_404(self, client):
        """GET /api/requisitions/{id}/offers with non-existent req → 404."""
        resp = client.get("/api/requisitions/999999/offers")
        assert resp.status_code == 404

    def test_list_offers_valid_req_returns_200(self, client, db_session, test_user):
        """GET /api/requisitions/{id}/offers with valid req → 200."""
        req = _make_requisition(db_session, test_user.id)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/offers")
        assert resp.status_code == 200


# ── create_offer — 404 path (line 282) ───────────────────────────────


class TestCreateOffer:
    def test_create_offer_missing_req_returns_404(self, client):
        """POST /api/requisitions/{id}/offers with non-existent req → 404."""
        resp = client.post(
            "/api/requisitions/999999/offers",
            json={
                "vendor_name": "Acme",
                "mpn": "LM317T",
                "qty_available": 100,
                "unit_price": 0.50,
            },
        )
        assert resp.status_code == 404


# ── review queue (lines 938-948) ─────────────────────────────────────


class TestReviewQueue:
    def test_list_review_queue_empty(self, client):
        """GET /api/offers/review-queue returns empty list when no T4 offers."""
        resp = client.get("/api/offers/review-queue")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_review_queue_with_t4_offer(self, client, db_session, test_user):
        """GET /api/offers/review-queue returns T4 pending_review offers."""
        req = _make_requisition(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review", evidence_tier="T4")
        db_session.commit()

        resp = client.get("/api/offers/review-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert any(o["id"] == offer.id for o in data)

    def test_list_review_queue_excludes_non_t4(self, client, db_session, test_user):
        """Review queue does not include active (non-T4) offers."""
        req = _make_requisition(db_session, test_user.id)
        _make_offer(db_session, req.id, test_user.id, status="active", evidence_tier="T5")
        db_session.commit()

        resp = client.get("/api/offers/review-queue")
        assert resp.status_code == 200
        # T5 active offer should NOT be in the list
        data = resp.json()
        assert all(o.get("evidence_tier") == "T4" for o in data)


# ── promote offer (lines 976-990) ────────────────────────────────────


class TestPromoteOffer:
    def test_promote_t4_offer_success(self, client, db_session, test_user):
        """POST /api/offers/{id}/promote promotes T4 pending_review → T5 active."""
        req = _make_requisition(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review", evidence_tier="T4")
        db_session.commit()

        resp = client.post(f"/api/offers/{offer.id}/promote")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "promoted"
        assert body["offer_id"] == offer.id

        db_session.refresh(offer)
        assert offer.evidence_tier == "T5"
        assert offer.status == "active"
        assert offer.promoted_by_id == test_user.id

    def test_promote_non_t4_returns_400(self, client, db_session, test_user):
        """POST /api/offers/{id}/promote on T5 offer returns 400."""
        req = _make_requisition(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="active", evidence_tier="T5")
        db_session.commit()

        resp = client.post(f"/api/offers/{offer.id}/promote")
        assert resp.status_code == 400

    def test_promote_missing_offer_returns_404(self, client):
        """POST /api/offers/99999/promote returns 404."""
        resp = client.post("/api/offers/99999/promote")
        assert resp.status_code == 404


# ── reject offer (lines 1004-1017) ───────────────────────────────────


class TestRejectOffer:
    def test_reject_pending_review_offer_success(self, client, db_session, test_user):
        """POST /api/offers/{id}/reject marks offer as rejected."""
        req = _make_requisition(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review", evidence_tier="T4")
        db_session.commit()

        resp = client.post(f"/api/offers/{offer.id}/reject")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"

        db_session.refresh(offer)
        assert offer.status == "rejected"

    def test_reject_active_offer_returns_400(self, client, db_session, test_user):
        """POST /api/offers/{id}/reject on active offer returns 400."""
        req = _make_requisition(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="active")
        db_session.commit()

        resp = client.post(f"/api/offers/{offer.id}/reject")
        assert resp.status_code == 400

    def test_reject_missing_offer_returns_404(self, client):
        """POST /api/offers/99999/reject returns 404."""
        resp = client.post("/api/offers/99999/reject")
        assert resp.status_code == 404
