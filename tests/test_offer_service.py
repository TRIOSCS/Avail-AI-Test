"""test_offer_service.py — unit tests for the ONE offer lifecycle service (W3, spec §9).

Locks the canonical semantics named by the consolidation:
- create persists spq + currency (previously dropped by the retired JSON builder)
  and stamps qualification blobs with schema=1 + requests[] from every door;
- reconfirm is TTL-RESETTING (expires_at = now+14d, is_stale cleared, attribution
  reactivated) — the drift that left crm-path reconfirmed offers stuck stale;
- approve lands on the ONE status (ACTIVE) and only from pending_review.

Called by: pytest
Depends on: app.services.offer_service, app.schemas.crm, conftest fixtures
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import OfferStatus
from app.models import Offer
from app.schemas.crm import OfferCreate
from app.services import offer_service


def _pending_offer(db: Session, test_requisition, test_user) -> Offer:
    offer = Offer(
        requisition_id=test_requisition.id,
        requirement_id=test_requisition.requirements[0].id,
        vendor_name="PendVend",
        mpn="LM317T",
        status=OfferStatus.PENDING_REVIEW,
        entered_by_id=test_user.id,
    )
    db.add(offer)
    db.commit()
    return offer


class TestCreateOffer:
    async def test_persists_spq_currency_and_stamps_qualification(
        self, db_session: Session, test_requisition, test_user
    ):
        payload = OfferCreate(
            mpn="LM317T",
            vendor_name="Arrow Electronics",
            requirement_id=test_requisition.requirements[0].id,
            qty_available=500,
            unit_price=0.42,
            spq=25,
            currency="EUR",
            manufacturer="Texas Instruments",
            qualification={"usage": "new production line"},
        )
        offer = await offer_service.create_offer(db_session, test_requisition.id, payload, test_user)

        assert offer.spq == 25
        assert offer.currency == "EUR"
        assert offer.qualification["schema"] == 1
        assert offer.qualification["requests"] == []
        assert offer.qualification["usage"] == "new production line"
        # The canonical pipeline resolves/creates a vendor card for every door.
        assert offer.vendor_card_id is not None
        assert offer.normalized_mpn == "lm317t"

    async def test_no_currency_keeps_column_default(self, db_session: Session, test_requisition, test_user):
        payload = OfferCreate(mpn="LM317T", vendor_name="Arrow Electronics")
        offer = await offer_service.create_offer(db_session, test_requisition.id, payload, test_user)
        assert offer.currency == "USD"  # column default preserved when not supplied

    async def test_missing_requisition_404s(self, db_session: Session, test_user):
        payload = OfferCreate(mpn="LM317T", vendor_name="Arrow Electronics")
        with pytest.raises(HTTPException) as exc:
            await offer_service.create_offer(db_session, 999_999, payload, test_user)
        assert exc.value.status_code == 404


class TestReconfirmTTL:
    def test_reconfirm_resets_ttl_and_clears_stale(self, db_session: Session, test_requisition, test_user):
        offer = _pending_offer(db_session, test_requisition, test_user)
        offer.status = OfferStatus.ACTIVE
        offer.is_stale = True
        offer.attribution_status = "expired"
        offer.expires_at = None
        db_session.commit()

        before = datetime.now(UTC)
        offer_service.reconfirm_offer(db_session, offer, test_user)
        db_session.expire_all()

        refreshed = db_session.get(Offer, offer.id)
        assert refreshed.reconfirm_count == 1
        assert refreshed.reconfirmed_at is not None
        assert refreshed.expires_at is not None
        expires_at = refreshed.expires_at if refreshed.expires_at.tzinfo else refreshed.expires_at.replace(tzinfo=UTC)
        assert expires_at >= before + timedelta(days=13)
        assert expires_at <= datetime.now(UTC) + timedelta(days=15)
        assert refreshed.is_stale is False
        assert refreshed.attribution_status == "active"
        assert refreshed.updated_by_id == test_user.id


class TestApproveOneStatus:
    def test_approve_lands_on_active(self, db_session: Session, test_requisition, test_user):
        offer = _pending_offer(db_session, test_requisition, test_user)
        offer_service.approve_offer(db_session, offer, test_user)
        db_session.expire_all()
        refreshed = db_session.get(Offer, offer.id)
        assert refreshed.status == OfferStatus.ACTIVE  # the ONE approve landing status
        assert refreshed.approved_by_id == test_user.id
        assert refreshed.approved_at is not None

    def test_approve_non_pending_rejected_with_400(self, db_session: Session, test_requisition, test_user):
        offer = _pending_offer(db_session, test_requisition, test_user)
        offer.status = OfferStatus.ACTIVE
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            offer_service.approve_offer(db_session, offer, test_user)
        assert exc.value.status_code == 400

    def test_reject_non_pending_rejected_with_400(self, db_session: Session, test_requisition, test_user):
        offer = _pending_offer(db_session, test_requisition, test_user)
        offer.status = OfferStatus.ACTIVE
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            offer_service.reject_offer(db_session, offer, test_user)
        assert exc.value.status_code == 400
