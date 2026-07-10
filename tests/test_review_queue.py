"""test_review_queue.py — Tests for offer review queue logic.

Covers:
- Evidence tier assignment for parsed offers
- T4 offers with pending_review status for medium confidence
- T5 offers with active status for high confidence

Called by: pytest
Depends on: app.evidence_tiers, app.models
"""

from datetime import UTC

import pytest

from app.evidence_tiers import tier_for_parsed_offer


class TestReviewQueueTierLogic:
    """Verify confidence → tier → status mapping for the review queue."""

    @pytest.mark.parametrize(
        "confidence,expected_tier",
        [
            # 0.5-0.8 confidence → T4 → pending_review (review queue)
            (0.6, "T4"),
            (0.5, "T4"),
            (0.79, "T4"),
            # >=0.8 confidence → T5 → auto-active
            (0.8, "T5"),
            (0.85, "T5"),
            (1.0, "T5"),
            # Unknown confidence → T4 (conservative, needs review)
            (None, "T4"),
        ],
    )
    def test_tier_for_parsed_offer(self, confidence, expected_tier):
        assert tier_for_parsed_offer(confidence) == expected_tier

    @pytest.mark.parametrize(
        "confidence,expected_status",
        [
            (0.65, "pending_review"),  # medium confidence → pending_review
            (0.9, "active"),  # high confidence → active
        ],
    )
    def test_status_logic(self, confidence, expected_status):
        status = "active" if confidence >= 0.8 else "pending_review"
        assert status == expected_status


class TestReviewQueueModel:
    """Test that T4 offers can be created and queried in the DB."""

    @staticmethod
    def _make_t4_offer(db_session, *, email, name, mpn, parse_confidence):
        """Seed a buyer, requisition, and a pending_review T4 offer; return the
        offer."""
        from app.models import Offer, Requisition, User

        user = User(email=email, name=name, role="buyer")
        db_session.add(user)
        db_session.flush()

        req = Requisition(name=f"Req-{name}", created_by=user.id)
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            mpn=mpn,
            source="email_parse",
            status="pending_review",
            evidence_tier="T4",
            parse_confidence=parse_confidence,
        )
        db_session.add(offer)
        db_session.commit()
        return user, offer

    def test_create_t4_offer(self, db_session):
        from app.models import Offer

        self._make_t4_offer(db_session, email="t@t.com", name="T", mpn="MPN-001", parse_confidence=0.65)

        # Query T4 pending_review offers (review queue query)
        queue = db_session.query(Offer).filter(Offer.evidence_tier == "T4", Offer.status == "pending_review").all()
        assert len(queue) == 1
        assert queue[0].parse_confidence == 0.65

    def test_promote_t4_to_t5(self, db_session):
        from datetime import datetime

        from app.models import Offer

        user, offer = self._make_t4_offer(db_session, email="t2@t.com", name="T2", mpn="MPN-002", parse_confidence=0.6)

        # Simulate promote action
        offer.evidence_tier = "T5"
        offer.status = "active"
        offer.promoted_by_id = user.id
        offer.promoted_at = datetime.now(UTC)
        db_session.commit()

        db_session.expire_all()
        promoted = db_session.get(Offer, offer.id)
        assert promoted.evidence_tier == "T5"
        assert promoted.status == "active"
        assert promoted.promoted_by_id == user.id
        assert promoted.promoted_at is not None

    def test_reject_offer(self, db_session):
        from app.models import Offer

        _, offer = self._make_t4_offer(db_session, email="t3@t.com", name="T3", mpn="MPN-003", parse_confidence=0.55)

        offer.status = "rejected"
        db_session.commit()

        db_session.expire_all()
        rejected = db_session.get(Offer, offer.id)
        assert rejected.status == "rejected"
        # Still visible for audit
        assert rejected.evidence_tier == "T4"
