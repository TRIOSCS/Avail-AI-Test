"""
test_services_engagement.py — Tests for engagement_scorer service.

Tests the pure scoring function and threshold/boundary logic.
No DB mocking needed for compute_engagement_score (pure function).

Called by: pytest
Depends on: app/services/engagement_scorer.py
"""

from datetime import datetime, timedelta, timezone

from app.services.engagement_scorer import (
    RECENCY_IDEAL_DAYS,
    RECENCY_MAX_DAYS,
    VELOCITY_IDEAL_HOURS,
    VELOCITY_MAX_HOURS,
    W_GHOST_RATE,
    W_RECENCY,
    W_RESPONSE_RATE,
    W_VELOCITY,
    W_WIN_RATE,
    compute_engagement_score,
)

NOW = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)


# ── Cold start / minimum outreach ──────────────────────────────────


class TestColdStart:
    def test_zero_outreach_returns_cold_start(self):
        result = compute_engagement_score(0, 0, 0, None, None, now=NOW)
        assert result["engagement_score"] == 50  # COLD_START_SCORE
        assert result["ghost_rate"] == 0

    def test_one_outreach_below_threshold(self):
        result = compute_engagement_score(1, 0, 0, None, None, now=NOW)
        assert result["engagement_score"] == 50  # COLD_START_SCORE
        assert result["ghost_rate"] == 1.0  # 1 outreach, 0 responses

    def test_one_outreach_with_response(self):
        result = compute_engagement_score(1, 1, 0, None, None, now=NOW)
        assert result["engagement_score"] == 50  # still below MIN_OUTREACH

    def test_exactly_min_outreach(self):
        result = compute_engagement_score(2, 1, 0, 2.0, NOW - timedelta(days=1), now=NOW)
        assert result["engagement_score"] is not None


# ── Response rate ───────────────────────────────────────────────────


class TestResponseRate:
    def test_perfect_response_rate(self):
        result = compute_engagement_score(10, 10, 0, None, None, now=NOW)
        assert result["response_rate"] == 1.0

    def test_half_response_rate(self):
        result = compute_engagement_score(10, 5, 0, None, None, now=NOW)
        assert result["response_rate"] == 0.5

    def test_zero_response_rate(self):
        result = compute_engagement_score(10, 0, 0, None, None, now=NOW)
        assert result["response_rate"] == 0.0

    def test_response_capped_at_1(self):
        """More responses than outreach (edge case) capped at 1.0."""
        result = compute_engagement_score(5, 10, 0, None, None, now=NOW)
        assert result["response_rate"] == 1.0


# ── Ghost rate ──────────────────────────────────────────────────────


class TestGhostRate:
    def test_all_responded_no_ghosts(self):
        result = compute_engagement_score(10, 10, 0, None, None, now=NOW)
        assert result["ghost_rate"] == 0.0

    def test_all_ghosted(self):
        result = compute_engagement_score(10, 0, 0, None, None, now=NOW)
        assert result["ghost_rate"] == 1.0

    def test_partial_ghost(self):
        result = compute_engagement_score(10, 3, 0, None, None, now=NOW)
        assert result["ghost_rate"] == 0.7


# ── Recency ─────────────────────────────────────────────────────────


class TestRecency:
    def test_very_recent_contact(self):
        """Within ideal window → 100."""
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=1), now=NOW
        )
        assert result["recency_score"] == 100.0

    def test_exactly_ideal_boundary(self):
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=RECENCY_IDEAL_DAYS), now=NOW
        )
        assert result["recency_score"] == 100.0

    def test_very_old_contact(self):
        """Beyond max → 0."""
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=400), now=NOW
        )
        assert result["recency_score"] == 0.0

    def test_midway_decay(self):
        """Halfway between ideal and max → ~50."""
        mid_days = (RECENCY_IDEAL_DAYS + RECENCY_MAX_DAYS) / 2
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=mid_days), now=NOW
        )
        assert 40 <= result["recency_score"] <= 60

    def test_no_contact_zero_recency(self):
        result = compute_engagement_score(5, 3, 0, None, None, now=NOW)
        assert result["recency_score"] == 0.0

    def test_naive_datetime_handled(self):
        """Naive datetime (no tzinfo) should still work."""
        naive_dt = datetime(2026, 2, 14, 12, 0, 0)  # no tzinfo
        result = compute_engagement_score(5, 3, 0, None, naive_dt, now=NOW)
        assert result["recency_score"] == 100.0


# ── Velocity ────────────────────────────────────────────────────────


class TestVelocity:
    def test_instant_reply_perfect_velocity(self):
        result = compute_engagement_score(5, 3, 0, 1.0, None, now=NOW)
        assert result["velocity_score"] == 100.0

    def test_exactly_ideal_boundary(self):
        result = compute_engagement_score(5, 3, 0, VELOCITY_IDEAL_HOURS, None, now=NOW)
        assert result["velocity_score"] == 100.0

    def test_very_slow_reply(self):
        result = compute_engagement_score(5, 3, 0, 200.0, None, now=NOW)
        assert result["velocity_score"] == 0.0

    def test_midway_velocity(self):
        mid = (VELOCITY_IDEAL_HOURS + VELOCITY_MAX_HOURS) / 2
        result = compute_engagement_score(5, 3, 0, mid, None, now=NOW)
        assert 40 <= result["velocity_score"] <= 60

    def test_no_velocity_data(self):
        result = compute_engagement_score(5, 3, 0, None, None, now=NOW)
        assert result["velocity_score"] == 0.0


# ── Win rate ────────────────────────────────────────────────────────


class TestWinRate:
    def test_all_wins(self):
        result = compute_engagement_score(5, 5, 5, None, None, now=NOW)
        assert result["win_rate"] == 1.0

    def test_no_wins(self):
        result = compute_engagement_score(5, 5, 0, None, None, now=NOW)
        assert result["win_rate"] == 0.0

    def test_no_responses_no_wins(self):
        result = compute_engagement_score(5, 0, 0, None, None, now=NOW)
        assert result["win_rate"] == 0.0


# ── Composite score ─────────────────────────────────────────────────


class TestCompositeScore:
    def test_perfect_score(self):
        """100% on every metric → 100."""
        result = compute_engagement_score(
            total_outreach=10,
            total_responses=10,
            total_wins=10,
            avg_velocity_hours=1.0,
            last_contact_at=NOW - timedelta(hours=1),
            now=NOW,
        )
        assert result["engagement_score"] == 100.0

    def test_worst_score(self):
        """0% on every metric → 0."""
        result = compute_engagement_score(
            total_outreach=10,
            total_responses=0,
            total_wins=0,
            avg_velocity_hours=None,
            last_contact_at=None,
            now=NOW,
        )
        assert result["engagement_score"] == 0.0

    def test_score_range(self):
        """Score always 0-100."""
        result = compute_engagement_score(
            total_outreach=5,
            total_responses=3,
            total_wins=1,
            avg_velocity_hours=48.0,
            last_contact_at=NOW - timedelta(days=30),
            now=NOW,
        )
        score = result["engagement_score"]
        assert 0 <= score <= 100

    def test_weights_sum_to_one(self):
        total = W_RESPONSE_RATE + W_GHOST_RATE + W_RECENCY + W_VELOCITY + W_WIN_RATE
        assert abs(total - 1.0) < 0.001

    def test_rounding(self):
        """All returned values should be rounded."""
        result = compute_engagement_score(
            total_outreach=3,
            total_responses=2,
            total_wins=1,
            avg_velocity_hours=50.5,
            last_contact_at=NOW - timedelta(days=100),
            now=NOW,
        )
        assert isinstance(result["engagement_score"], float)
        # Check decimal places
        score_str = str(result["engagement_score"])
        if "." in score_str:
            assert len(score_str.split(".")[1]) <= 1


# ── DB-dependent tests ─────────────────────────────────────────────
# These tests exercise the functions that query the database:
#   compute_all_engagement_scores, compute_single_vendor_score, apply_outbound_stats

import pytest
from sqlalchemy.orm import Session

from app.services.engagement_scorer import (
    apply_outbound_stats,
    compute_all_engagement_scores,
    compute_single_vendor_score,
)


# ── Helpers ────────────────────────────────────────────────────────


def _make_requisition(db: Session, user_id: int):
    """Create a minimal Requisition (needed as FK for Contact)."""
    from app.models import Requisition

    r = Requisition(
        name="REQ-ENG-TEST",
        customer_name="Test Customer",
        status="open",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_contact(db: Session, requisition_id: int, user_id: int, vendor_name: str, status="sent"):
    """Create a Contact record (outreach event)."""
    from app.models import Contact

    c = Contact(
        requisition_id=requisition_id,
        user_id=user_id,
        vendor_name=vendor_name,
        contact_type="email",
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    db.add(c)
    db.flush()
    return c


def _make_vendor_response(db: Session, vendor_name: str, vendor_email: str, contact_id=None, received_at=None):
    """Create a VendorResponse record."""
    from app.models import VendorResponse

    vr = VendorResponse(
        vendor_name=vendor_name,
        vendor_email=vendor_email,
        contact_id=contact_id,
        received_at=received_at or datetime.now(timezone.utc),
        status="new",
    )
    db.add(vr)
    db.flush()
    return vr


def _make_vendor_card(db: Session, normalized_name: str, display_name: str, domain=None, domain_aliases=None):
    """Create a VendorCard with engagement-relevant fields."""
    from app.models import VendorCard

    card = VendorCard(
        normalized_name=normalized_name,
        display_name=display_name,
        domain=domain,
        domain_aliases=domain_aliases or [],
        emails=[],
        phones=[],
        sighting_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


# ── compute_all_engagement_scores ──────────────────────────────────


class TestComputeAllEngagementScores:
    @pytest.mark.asyncio
    async def test_compute_all_empty_db(self, db_session):
        """No VendorCards in DB → returns {updated: 0, skipped: 0}."""
        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] == 0
        assert result["skipped"] == 0

    @pytest.mark.asyncio
    async def test_compute_all_updates_cards(self, db_session, test_user):
        """VendorCards with Contact/VendorResponse data get engagement_score updated."""
        # normalize_vendor_name("Acme Inc") → "acme", so card.normalized_name must match
        card = _make_vendor_card(db_session, "acme", "Acme Inc", domain="acme.com")
        req = _make_requisition(db_session, test_user.id)

        # Create 5 outreach contacts (vendor_name "Acme Inc" normalizes to "acme")
        for _ in range(5):
            _make_contact(db_session, req.id, test_user.id, "Acme Inc")
        # Create 3 responses from the acme.com domain
        for i in range(3):
            _make_vendor_response(db_session, "John Doe", f"john{i}@acme.com", received_at=datetime.now(timezone.utc))
        db_session.commit()

        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] == 1

        db_session.refresh(card)
        assert card.engagement_score is not None
        assert card.engagement_computed_at is not None
        # With 5 outreach (from Contact) and 3 responses (from VendorResponse),
        # the score should be above 0 (not cold start)
        assert card.total_outreach >= 5
        assert card.total_responses >= 3

    @pytest.mark.asyncio
    async def test_compute_all_domain_matching(self, db_session, test_user):
        """VendorResponse matched to VendorCard via email domain → correct response count."""
        card = _make_vendor_card(db_session, "globex", "Globex", domain="globex.com")
        req = _make_requisition(db_session, test_user.id)

        # Outreach via Contact.vendor_name = "Globex"
        for _ in range(3):
            _make_contact(db_session, req.id, test_user.id, "Globex")

        # Responses from globex.com domain (vendor_name is the person, not the company)
        _make_vendor_response(db_session, "Alice Smith", "alice@globex.com")
        _make_vendor_response(db_session, "Bob Jones", "bob@globex.com")
        db_session.commit()

        await compute_all_engagement_scores(db_session)
        db_session.refresh(card)

        # Outreach matched by normalized vendor_name "globex"
        assert card.total_outreach >= 3
        # Responses matched by email domain "globex.com"
        assert card.total_responses >= 2

    @pytest.mark.asyncio
    async def test_compute_all_velocity_calculation(self, db_session, test_user):
        """Contact + linked VendorResponse with timestamps → computes avg_velocity_hours."""
        card = _make_vendor_card(db_session, "speedy parts", "Speedy Parts", domain="speedyparts.com")
        req = _make_requisition(db_session, test_user.id)

        sent_time = datetime(2026, 2, 10, 10, 0, 0, tzinfo=timezone.utc)
        reply_time = datetime(2026, 2, 10, 14, 0, 0, tzinfo=timezone.utc)  # 4 hours later

        c = _make_contact(db_session, req.id, test_user.id, "Speedy Parts", status="sent")
        # Override created_at to a known time
        c.created_at = sent_time
        db_session.flush()

        _make_vendor_response(
            db_session,
            vendor_name="Rep at Speedy",
            vendor_email="rep@speedyparts.com",
            contact_id=c.id,
            received_at=reply_time,
        )
        db_session.commit()

        await compute_all_engagement_scores(db_session)
        db_session.refresh(card)

        # Velocity should be ~4.0 hours
        assert card.response_velocity_hours is not None
        assert 3.5 <= card.response_velocity_hours <= 4.5

    @pytest.mark.asyncio
    async def test_compute_all_batch_processing(self, db_session, test_user):
        """Multiple VendorCards (>1) all get processed in one call."""
        cards = []
        for i in range(6):
            c = _make_vendor_card(db_session, f"vendor{i}", f"Vendor {i}", domain=f"vendor{i}.com")
            cards.append(c)
        db_session.commit()

        result = await compute_all_engagement_scores(db_session)
        assert result["updated"] == 6

        for card in cards:
            db_session.refresh(card)
            assert card.engagement_score is not None
            assert card.engagement_computed_at is not None


# ── compute_single_vendor_score ────────────────────────────────────


class TestComputeSingleVendorScore:
    def test_single_vendor_no_data(self, db_session):
        """VendorCard with no contacts/responses → returns cold start score (50)."""
        card = _make_vendor_card(db_session, "lonely vendor", "Lonely Vendor", domain="lonely.com")
        db_session.commit()

        score = compute_single_vendor_score(card, db_session)
        assert score == 50  # COLD_START_SCORE (no outreach data)

    def test_single_vendor_with_data(self, db_session, test_user):
        """VendorCard with contacts + responses → returns computed score."""
        card = _make_vendor_card(db_session, "active vendor", "Active Vendor", domain="activevendor.com")
        req = _make_requisition(db_session, test_user.id)

        # Create enough outreach to pass MIN_OUTREACH_FOR_SCORE (2)
        for _ in range(5):
            _make_contact(db_session, req.id, test_user.id, "active vendor")

        # Create responses from the vendor's domain
        for i in range(3):
            _make_vendor_response(db_session, "Sales Rep", f"sales{i}@activevendor.com")
        db_session.commit()

        score = compute_single_vendor_score(card, db_session)
        assert score is not None
        assert 0 <= score <= 100
        # With real data, should differ from cold start
        assert score != 50

    def test_single_vendor_domain_aliases(self, db_session, test_user):
        """VendorCard with domain_aliases → counts responses matching any alias domain."""
        card = _make_vendor_card(
            db_session,
            "multi domain co",
            "Multi Domain Co",
            domain="multidomain.com",
            domain_aliases=["mdco.com", "multidomain.io"],
        )
        req = _make_requisition(db_session, test_user.id)

        # Outreach (>= 2 to avoid cold start)
        for _ in range(4):
            _make_contact(db_session, req.id, test_user.id, "multi domain co")

        # Responses spread across primary domain and aliases
        _make_vendor_response(db_session, "Alice", "alice@multidomain.com")
        _make_vendor_response(db_session, "Bob", "bob@mdco.com")
        _make_vendor_response(db_session, "Carol", "carol@multidomain.io")
        db_session.commit()

        score = compute_single_vendor_score(card, db_session)
        assert score is not None
        assert 0 <= score <= 100
        # 3 responses across 3 domains (1 primary + 2 aliases) from 4 outreach.
        # Without recency/velocity/wins, the weighted score is ~37.5:
        #   response_rate=0.75 → 22.5, ghost_score=75 → 15.0
        # If aliases weren't counted it would be ~12.5 (only 1 response).
        assert score > 25  # proves all 3 alias domains contributed responses


# ── apply_outbound_stats ───────────────────────────────────────────


class TestApplyOutboundStats:
    def test_apply_outbound_empty(self, db_session):
        """Empty vendors_contacted dict → returns 0 updated."""
        result = apply_outbound_stats(db_session, {})
        assert result == 0

    def test_apply_outbound_domain_match(self, db_session):
        """VendorCard matched by domain → total_outreach incremented."""
        card = _make_vendor_card(db_session, "acme", "Acme", domain="acme.com")
        card.total_outreach = 10
        db_session.commit()

        updated = apply_outbound_stats(db_session, {"acme.com": 5})
        assert updated == 1

        db_session.refresh(card)
        assert card.total_outreach == 15  # 10 + 5
        assert card.last_contact_at is not None

    def test_apply_outbound_name_fallback(self, db_session):
        """No domain match but normalized_name matches domain prefix → incremented."""
        # Card has no domain set, but normalized_name is "betacorp"
        card = _make_vendor_card(db_session, "betacorp", "BetaCorp", domain=None)
        card.total_outreach = 0
        db_session.commit()

        # "betacorp.com" → domain prefix "betacorp" matches normalized_name
        updated = apply_outbound_stats(db_session, {"betacorp.com": 3})
        assert updated == 1

        db_session.refresh(card)
        assert card.total_outreach == 3
        assert card.last_contact_at is not None

    def test_apply_outbound_no_match(self, db_session):
        """Domain/name doesn't match any card → skipped, returns 0."""
        _make_vendor_card(db_session, "acme", "Acme", domain="acme.com")
        db_session.commit()

        updated = apply_outbound_stats(db_session, {"unknownvendor.org": 10})
        assert updated == 0
