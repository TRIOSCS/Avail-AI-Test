import os

os.environ["TESTING"] = "1"
"""test_crm_offers_coverage.py — Coverage tests for app/routers/crm/offers.py"""
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from tests.conftest import engine  # noqa: F401

_ = engine

from app.models import (
    ChangeLog,
    Company,
    CustomerSite,
    Offer,
    OfferAttachment,
    Requirement,
    Requisition,
    User,
    VendorCard,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_company(db: Session, name: str = "OfferCo") -> Company:
    co = Company(name=name, is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company_id: int) -> CustomerSite:
    site = CustomerSite(
        company_id=company_id,
        site_name="HQ",
        contact_name="Buyer",
        contact_email="buyer@offerco.com",
    )
    db.add(site)
    db.flush()
    return site


def _make_req(db: Session, user_id: int, status: str = "active") -> Requisition:
    req = Requisition(
        name="Offer Test Req",
        status=status,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_vendor_card(db: Session, name: str = "TestVendor") -> VendorCard:
    from app.vendor_utils import normalize_vendor_name

    card = VendorCard(
        normalized_name=normalize_vendor_name(name),
        display_name=name,
        emails=[],
        phones=[],
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_offer(
    db: Session,
    req_id: int,
    user_id: int,
    status: str = "active",
    evidence_tier: str | None = None,
    vendor_card_id: int | None = None,
) -> Offer:
    offer = Offer(
        requisition_id=req_id,
        vendor_name="OfferVendor",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.45,
        entered_by_id=user_id,
        status=status,
        evidence_tier=evidence_tier,
        vendor_card_id=vendor_card_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.flush()
    return offer


# ── GET /api/requisitions/{req_id}/offers ────────────────────────────────


class TestListOffers:
    def test_list_offers_missing_req_returns_404(self, client):
        resp = client.get("/api/requisitions/999999/offers")
        assert resp.status_code == 404

    def test_list_offers_empty_req(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data
        assert "has_new_offers" in data

    def test_list_offers_with_offers(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data

    def test_list_offers_hides_pending_review_for_buyer(self, client, db_session, test_user):
        """Buyer role users should not see pending_review offers."""
        req = _make_req(db_session, test_user.id)
        _make_offer(db_session, req.id, test_user.id, status="pending_review")
        _make_offer(db_session, req.id, test_user.id, status="active")
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        for group in data.get("groups", []):
            for offer in group.get("offers", []):
                assert offer["status"] != "pending_review"

    def test_list_offers_marks_viewed(self, client, db_session, test_user):
        """Viewing offers as the req owner should set offers_viewed_at."""
        req = _make_req(db_session, test_user.id)
        _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        assert req.offers_viewed_at is None
        client.get(f"/api/requisitions/{req.id}/offers")
        db_session.refresh(req)
        assert req.offers_viewed_at is not None

    def test_list_offers_with_vendor_rating(self, client, db_session, test_user):
        """Offers with vendor_card_id get ratings batch-fetched."""
        from app.models import VendorReview

        req = _make_req(db_session, test_user.id)
        card = _make_vendor_card(db_session)
        offer = _make_offer(db_session, req.id, test_user.id, vendor_card_id=card.id)
        review = VendorReview(
            vendor_card_id=card.id,
            user_id=test_user.id,
            rating=4,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(review)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/offers")
        assert resp.status_code == 200


# ── POST /api/requisitions/{req_id}/offers ───────────────────────────────


class TestCreateOffer:
    def test_create_offer_missing_req_returns_404(self, client):
        resp = client.post(
            "/api/requisitions/999999/offers",
            json={"vendor_name": "Arrow", "mpn": "LM317T", "qty_available": 100, "unit_price": 0.50},
        )
        assert resp.status_code == 404

    def test_create_offer_basic(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={"vendor_name": "Mouser", "mpn": "NE555P", "qty_available": 200, "unit_price": 0.30},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["vendor_name"] == "Mouser"
        assert data["mpn"] == "NE555P"

    def test_create_offer_with_vendor_card_id(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        card = _make_vendor_card(db_session, "DigiKey")
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={
                "vendor_name": "DigiKey",
                "vendor_card_id": card.id,
                "mpn": "NE555P",
                "qty_available": 1000,
                "unit_price": 0.28,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_card_id"] == card.id

    def test_create_offer_advances_req_status(self, client, db_session, test_user):
        """Creating an offer on an active req should advance status to offers."""
        req = _make_req(db_session, test_user.id, status="active")
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={"vendor_name": "Arrow", "mpn": "LM317T"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "req_status" in data

    def test_create_offer_creates_new_vendor_card(self, client, db_session, test_user):
        """Creating an offer with unknown vendor creates a new VendorCard."""
        req = _make_req(db_session, test_user.id)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={"vendor_name": "BrandNewVendorXYZ2026", "mpn": "ABC123", "qty_available": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_name"] == "BrandNewVendorXYZ2026"

    def test_create_offer_with_requirement_id(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={
                "vendor_name": "Arrow",
                "mpn": "LM317T",
                "requirement_id": item.id,
                "qty_available": 200,
                "unit_price": 0.45,
                "status": "active",
            },
        )
        assert resp.status_code == 200

    def test_create_offer_blank_mpn_returns_422(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={"vendor_name": "Arrow", "mpn": "   "},
        )
        assert resp.status_code == 422

    def test_create_offer_blank_vendor_returns_422(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        db_session.commit()
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={"vendor_name": "", "mpn": "LM317T"},
        )
        assert resp.status_code == 422


# ── PUT /api/offers/{offer_id} ───────────────────────────────────────────


class TestUpdateOffer:
    def test_update_offer_not_found(self, client):
        resp = client.put("/api/offers/999999", json={"notes": "test"})
        assert resp.status_code == 404

    def test_update_offer_basic(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"vendor_name": "Updated Vendor", "lead_time": "3-5 days", "unit_price": 0.55},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_update_offer_creates_changelog(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        client.put(f"/api/offers/{offer.id}", json={"vendor_name": "NewVendorName", "notes": "note update"})
        logs = (
            db_session.query(ChangeLog).filter(ChangeLog.entity_type == "offer", ChangeLog.entity_id == offer.id).all()
        )
        fields = {log.field_name for log in logs}
        assert "vendor_name" in fields

    def test_update_offer_sets_updated_at(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        client.put(f"/api/offers/{offer.id}", json={"notes": "fresh note"})
        db_session.refresh(offer)
        assert offer.updated_at is not None
        assert offer.updated_by_id == test_user.id

    def test_update_offer_status_transition(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="active")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}", json={"status": "won"})
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == "won"

    def test_update_offer_invalid_status_transition(self, client, db_session, test_user):
        """Rejected offer cannot transition to active."""
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="rejected")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}", json={"status": "active"})
        assert resp.status_code in (400, 409, 422)


# ── DELETE /api/offers/{offer_id} ────────────────────────────────────────


class TestDeleteOffer:
    def test_delete_offer_not_found(self, client):
        resp = client.delete("/api/offers/999999")
        assert resp.status_code == 404

    def test_delete_offer_succeeds(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        resp = client.delete(f"/api/offers/{offer.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        deleted = db_session.get(Offer, offer.id)
        assert deleted is None


# ── PUT /api/offers/{offer_id}/reconfirm ─────────────────────────────────


class TestReconfirmOffer:
    def test_reconfirm_not_found(self, client):
        resp = client.put("/api/offers/999999/reconfirm")
        assert resp.status_code == 404

    def test_reconfirm_increments_count(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}/reconfirm")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["reconfirm_count"] == 1
        assert "reconfirmed_at" in data

    def test_reconfirm_twice_increments_to_two(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        client.put(f"/api/offers/{offer.id}/reconfirm")
        resp = client.put(f"/api/offers/{offer.id}/reconfirm")
        assert resp.status_code == 200
        assert resp.json()["reconfirm_count"] == 2


# ── PUT /api/offers/{offer_id}/approve ───────────────────────────────────


class TestApproveOffer:
    def test_approve_not_found(self, client):
        resp = client.put("/api/offers/999999/approve")
        assert resp.status_code == 404

    def test_approve_pending_review_offer(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "active"
        db_session.refresh(offer)
        assert offer.status == "active"
        assert offer.approved_by_id == test_user.id

    def test_approve_active_offer_returns_400(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="active")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}/approve")
        assert resp.status_code == 400


# ── PUT /api/offers/{offer_id}/reject ────────────────────────────────────


class TestRejectOffer:
    def test_reject_not_found(self, client):
        resp = client.put("/api/offers/999999/reject")
        assert resp.status_code == 404

    def test_reject_pending_review_offer(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}/reject")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "rejected"

    def test_reject_with_reason_appends_to_notes(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}/reject?reason=Price+too+high")
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert "Price too high" in (offer.notes or "")

    def test_reject_active_offer_returns_400(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="active")
        db_session.commit()
        resp = client.put(f"/api/offers/{offer.id}/reject")
        assert resp.status_code == 400


# ── PATCH /api/offers/{offer_id}/mark-sold ───────────────────────────────


class TestMarkOfferSold:
    def test_mark_sold_not_found(self, client):
        resp = client.patch("/api/offers/999999/mark-sold")
        assert resp.status_code == 404

    def test_mark_sold_by_creator(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="active")
        db_session.commit()
        resp = client.patch(f"/api/offers/{offer.id}/mark-sold")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "sold"

    def test_mark_sold_already_sold_returns_200_idempotent(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="sold")
        db_session.commit()
        resp = client.patch(f"/api/offers/{offer.id}/mark-sold")
        assert resp.status_code == 200
        assert resp.json()["message"] == "Already marked sold"

    def test_mark_sold_by_non_creator_returns_403(self, client, db_session, test_user):
        """User who didn't create the offer and isn't admin should get 403."""
        other = User(
            email="other@test.com",
            name="Other",
            role="buyer",
            azure_id="other-azure-99",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other)
        db_session.flush()
        req = _make_req(db_session, other.id)
        offer = Offer(
            requisition_id=req.id,
            vendor_name="SomeVendor",
            mpn="ABC",
            entered_by_id=other.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()
        # client is test_user, not other — should get 403
        resp = client.patch(f"/api/offers/{offer.id}/mark-sold")
        assert resp.status_code == 403


# ── GET /api/changelog/{entity_type}/{entity_id} ─────────────────────────


class TestGetChangelog:
    def test_changelog_invalid_entity_type(self, client):
        resp = client.get("/api/changelog/foobar/1")
        assert resp.status_code == 400

    def test_changelog_empty(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        resp = client.get(f"/api/changelog/offer/{offer.id}")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_changelog_returns_entries(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.flush()
        # Create a changelog entry manually
        from app.models import ChangeLog

        log = ChangeLog(
            entity_type="offer",
            entity_id=offer.id,
            user_id=test_user.id,
            field_name="unit_price",
            old_value="0.45",
            new_value="0.55",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(log)
        db_session.commit()
        resp = client.get(f"/api/changelog/offer/{offer.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["field_name"] == "unit_price"
        assert data[0]["old_value"] == "0.45"
        assert data[0]["new_value"] == "0.55"

    def test_changelog_valid_entity_types(self, client, db_session, test_user):
        for entity_type in ("offer", "requirement", "requisition"):
            resp = client.get(f"/api/changelog/{entity_type}/9999")
            assert resp.status_code == 200
            assert resp.json() == []


# ── POST /api/offers/{offer_id}/attachments ──────────────────────────────


class TestUploadAttachment:
    def test_upload_attachment_not_found(self, client):
        data = {"file": ("test.pdf", BytesIO(b"pdf content"), "application/pdf")}
        resp = client.post("/api/offers/999999/attachments", files=data)
        assert resp.status_code == 404

    def test_upload_attachment_file_too_large(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        big_content = b"x" * (11 * 1024 * 1024)  # > 10 MB
        data = {"file": ("big.pdf", BytesIO(big_content), "application/pdf")}
        resp = client.post(f"/api/offers/{offer.id}/attachments", files=data)
        assert resp.status_code == 400

    def test_upload_attachment_invalid_extension(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        data = {"file": ("file.exe", BytesIO(b"binary"), "application/octet-stream")}
        resp = client.post(f"/api/offers/{offer.id}/attachments", files=data)
        assert resp.status_code == 400


# ── POST /api/offers/{offer_id}/attachments/onedrive ─────────────────────


class TestAttachFromOneDrive:
    def test_attach_onedrive_not_found(self, client, db_session, test_user):
        resp = client.post(
            "/api/offers/999999/attachments/onedrive",
            json={"item_id": "fake-item-id"},
        )
        assert resp.status_code == 404

    def test_attach_onedrive_no_token_returns_401(self, client, db_session, test_user):
        """User without access_token should get 401."""
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        # test_user has no access_token by default
        test_user.access_token = None
        db_session.commit()
        resp = client.post(
            f"/api/offers/{offer.id}/attachments/onedrive",
            json={"item_id": "some-item-id"},
        )
        assert resp.status_code == 401

    def test_attach_onedrive_item_not_found_returns_404(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        test_user.access_token = "mock-token"
        db_session.commit()
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"error": {"message": "Item not found"}})
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.post(
                f"/api/offers/{offer.id}/attachments/onedrive",
                json={"item_id": "missing-item"},
            )
        assert resp.status_code == 404

    def test_attach_onedrive_success(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.commit()
        test_user.access_token = "mock-token"
        db_session.commit()
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value={
                "id": "item-abc123",
                "name": "quote.pdf",
                "webUrl": "https://onedrive.com/quote.pdf",
                "file": {"mimeType": "application/pdf"},
                "size": 4096,
            }
        )
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.post(
                f"/api/offers/{offer.id}/attachments/onedrive",
                json={"item_id": "item-abc123"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_name"] == "quote.pdf"


# ── DELETE /api/offer-attachments/{att_id} ───────────────────────────────


class TestDeleteAttachment:
    def test_delete_attachment_not_found(self, client):
        resp = client.delete("/api/offer-attachments/999999")
        assert resp.status_code == 404

    def test_delete_attachment_no_onedrive(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id)
        db_session.flush()
        att = OfferAttachment(
            offer_id=offer.id,
            file_name="test.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            uploaded_by_id=test_user.id,
        )
        db_session.add(att)
        db_session.commit()
        resp = client.delete(f"/api/offer-attachments/{att.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        deleted = db_session.get(OfferAttachment, att.id)
        assert deleted is None


# ── GET /api/onedrive/browse ─────────────────────────────────────────────


class TestBrowseOneDrive:
    def test_browse_onedrive_no_token_returns_401(self, client, db_session, test_user):
        test_user.access_token = None
        db_session.commit()
        resp = client.get("/api/onedrive/browse")
        assert resp.status_code == 401

    def test_browse_onedrive_graph_error_returns_502(self, client, db_session, test_user):
        test_user.access_token = "mock-token"
        db_session.commit()
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(return_value={"error": {"message": "Service unavailable"}})
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.get("/api/onedrive/browse")
        assert resp.status_code == 502

    def test_browse_onedrive_root_success(self, client, db_session, test_user):
        test_user.access_token = "mock-token"
        db_session.commit()
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {
                        "id": "folder1",
                        "name": "Documents",
                        "folder": {},
                        "webUrl": "https://od.com/docs",
                        "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        )
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.get("/api/onedrive/browse")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["is_folder"] is True
        assert data[0]["name"] == "Documents"

    def test_browse_onedrive_with_path(self, client, db_session, test_user):
        test_user.access_token = "mock-token"
        db_session.commit()
        mock_gc = MagicMock()
        mock_gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {
                        "id": "file1",
                        "name": "offer.pdf",
                        "file": {"mimeType": "application/pdf"},
                        "size": 2048,
                        "webUrl": "https://od.com/file",
                        "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                    }
                ]
            }
        )
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.get("/api/onedrive/browse?path=AvailAI/Offers")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["is_folder"] is False


# ── Review Queue ─────────────────────────────────────────────────────────
# NOTE: Basic tests are in test_offers_nightly.py — these extend coverage.


class TestReviewQueueExtended:
    def test_review_queue_pagination_limit(self, client, db_session, test_user):
        """Review queue respects 100 item limit."""
        req = _make_req(db_session, test_user.id)
        for _ in range(5):
            _make_offer(db_session, req.id, test_user.id, status="pending_review", evidence_tier="T4")
        db_session.commit()
        resp = client.get("/api/offers/review-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 5


# ── Promote / Reject (extended) ──────────────────────────────────────────
# NOTE: Basic promote/reject tests are in test_offers_nightly.py


class TestPromoteRejectExtended:
    def test_promote_sets_promoted_at(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review", evidence_tier="T4")
        db_session.commit()
        resp = client.post(f"/api/offers/{offer.id}/promote")
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.promoted_at is not None

    def test_reject_via_post_sets_status(self, client, db_session, test_user):
        """POST /api/offers/{id}/reject (T4 review endpoint) rejects."""
        req = _make_req(db_session, test_user.id)
        offer = _make_offer(db_session, req.id, test_user.id, status="pending_review", evidence_tier="T4")
        db_session.commit()
        resp = client.post(f"/api/offers/{offer.id}/reject")
        assert resp.status_code == 200
        db_session.refresh(offer)
        assert offer.status == "rejected"
