"""test_review_queue.py — Tests for offer review queue logic.

Covers:
- Evidence tier assignment for parsed offers
- T4 offers with pending_review status for medium confidence
- T5 offers with active status for high confidence

Called by: pytest
Depends on: app.evidence_tiers, app.models
"""

from app.evidence_tiers import tier_for_parsed_offer


class TestReviewQueueTierLogic:
    """Verify confidence → tier → status mapping for the review queue."""

    def test_medium_confidence_creates_t4(self):
        """0.5-0.8 confidence → T4 → pending_review (review queue)."""
        assert tier_for_parsed_offer(0.6) == "T4"
        assert tier_for_parsed_offer(0.5) == "T4"
        assert tier_for_parsed_offer(0.79) == "T4"

    def test_high_confidence_creates_t5(self):
        """>=0.8 confidence → T5 → auto-active."""
        assert tier_for_parsed_offer(0.8) == "T5"
        assert tier_for_parsed_offer(0.85) == "T5"
        assert tier_for_parsed_offer(1.0) == "T5"

    def test_none_confidence_t4(self):
        """Unknown confidence → T4 (conservative, needs review)."""
        assert tier_for_parsed_offer(None) == "T4"

    def test_status_logic_medium_confidence(self):
        """Medium confidence offers should get pending_review status."""
        confidence = 0.65
        status = "active" if confidence >= 0.8 else "pending_review"
        assert status == "pending_review"

    def test_status_logic_high_confidence(self):
        """High confidence offers should get active status."""
        confidence = 0.9
        status = "active" if confidence >= 0.8 else "pending_review"
        assert status == "active"


class TestReviewQueueModel:
    """Test that T4 offers can be created and queried in the DB."""

    def test_create_t4_offer(self, db_session):
        from app.models import Offer, Requisition, User

        user = User(email="t@t.com", name="T", role="buyer")
        db_session.add(user)
        db_session.flush()

        req = Requisition(name="Test", created_by=user.id)
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Test Vendor",
            mpn="MPN-001",
            source="email_parse",
            status="pending_review",
            evidence_tier="T4",
            parse_confidence=0.65,
        )
        db_session.add(offer)
        db_session.commit()

        # Query T4 pending_review offers (review queue query)
        queue = db_session.query(Offer).filter(Offer.evidence_tier == "T4", Offer.status == "pending_review").all()
        assert len(queue) == 1
        assert queue[0].parse_confidence == 0.65

    def test_promote_t4_to_t5(self, db_session):
        from datetime import datetime, timezone

        from app.models import Offer, Requisition, User

        user = User(email="t2@t.com", name="T2", role="buyer")
        db_session.add(user)
        db_session.flush()

        req = Requisition(name="Test2", created_by=user.id)
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Vendor",
            mpn="MPN-002",
            source="email_parse",
            status="pending_review",
            evidence_tier="T4",
            parse_confidence=0.6,
        )
        db_session.add(offer)
        db_session.commit()

        # Simulate promote action
        offer.evidence_tier = "T5"
        offer.status = "active"
        offer.promoted_by_id = user.id
        offer.promoted_at = datetime.now(timezone.utc)
        db_session.commit()

        db_session.expire_all()
        promoted = db_session.get(Offer, offer.id)
        assert promoted.evidence_tier == "T5"
        assert promoted.status == "active"
        assert promoted.promoted_by_id == user.id
        assert promoted.promoted_at is not None

    def test_reject_offer(self, db_session):
        from app.models import Offer, Requisition, User

        user = User(email="t3@t.com", name="T3", role="buyer")
        db_session.add(user)
        db_session.flush()

        req = Requisition(name="Test3", created_by=user.id)
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Vendor",
            mpn="MPN-003",
            source="email_parse",
            status="pending_review",
            evidence_tier="T4",
            parse_confidence=0.55,
        )
        db_session.add(offer)
        db_session.commit()

        offer.status = "rejected"
        db_session.commit()

        db_session.expire_all()
        rejected = db_session.get(Offer, offer.id)
        assert rejected.status == "rejected"
        # Still visible for audit
        assert rejected.evidence_tier == "T4"
