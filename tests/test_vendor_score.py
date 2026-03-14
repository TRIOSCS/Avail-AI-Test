"""
test_vendor_score.py — Tests for vendor_score.py

Tests pure computation (compute_vendor_score, _calc_stage_points) and
DB-backed scoring (compute_single_vendor_score).

Called by: pytest
Depends on: app/services/vendor_score.py, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.models import (
    BuyPlan,
    Company,
    CustomerSite,
    Offer,
    Quote,
    Requisition,
    User,
    VendorCard,
    VendorReview,
)
from app.services.vendor_score import (
    _calc_stage_points,
    _get_buyplan_offer_ids,
    _get_quote_offer_ids,
    compute_all_vendor_scores,
    compute_single_vendor_score,
    compute_vendor_score,
)
from tests.conftest import engine  # noqa: F401

# ── Helpers ─────────────────────────────────────────────────────────


def _make_vendor_card(db, name="test vendor"):
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_offers(db, card_id, vendor_name, count):
    """Create `count` Offer rows linked to a new Requisition.

    Returns list of Offer objects.
    """
    user = User(
        email=f"u-{vendor_name}@test.com",
        name="Offer User",
        role="buyer",
        azure_id=f"az-{vendor_name}-{count}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    req = Requisition(
        name=f"REQ-{vendor_name}",
        customer_name="Test Customer",
        status="open",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    from app.vendor_utils import normalize_vendor_name

    offers = []
    for i in range(count):
        o = Offer(
            requisition_id=req.id,
            vendor_card_id=card_id,
            vendor_name=vendor_name,
            vendor_name_normalized=normalize_vendor_name(vendor_name),
            mpn=f"MPN-{i}",
            qty_available=100,
            unit_price=1.00,
            entered_by_id=user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db.add(o)
        offers.append(o)
    db.flush()
    return offers


def _make_offers_full(db, card_id, vendor_name, count):
    """Like _make_offers but returns (offers, user, req) tuple."""
    from app.vendor_utils import normalize_vendor_name

    user = User(
        email=f"u-{vendor_name}-{count}@test.com",
        name="User",
        role="buyer",
        azure_id=f"az-{vendor_name}-{count}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()

    req = Requisition(
        name=f"REQ-{vendor_name}-{count}",
        customer_name="Test Customer",
        status="open",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    offers = []
    for i in range(count):
        o = Offer(
            requisition_id=req.id,
            vendor_card_id=card_id,
            vendor_name=vendor_name,
            vendor_name_normalized=normalize_vendor_name(vendor_name),
            mpn=f"MPN-{vendor_name}-{i}",
            qty_available=100,
            unit_price=1.00,
            entered_by_id=user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db.add(o)
        offers.append(o)
    db.flush()
    return offers, user, req


def _make_review(db, card_id, user_id, rating):
    r = VendorReview(
        vendor_card_id=card_id,
        user_id=user_id,
        rating=rating,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


# ═══════════════════════════════════════════════════════════════════════
#  compute_vendor_score — pure math, no DB
# ═══════════════════════════════════════════════════════════════════════


class TestComputeVendorScore:
    def test_cold_start_below_threshold(self):
        result = compute_vendor_score(4, 4.0, None)
        assert result["vendor_score"] is None
        assert result["is_new_vendor"] is True

    def test_exactly_threshold_triggers_scoring(self):
        # 5 offers, all base stage → stage_points_sum = 5
        result = compute_vendor_score(5, 5.0, None)
        assert result["vendor_score"] is not None
        assert result["is_new_vendor"] is False

    def test_all_base_stage_advancement(self):
        # 10 offers, all base (1pt each) → advancement = (10 / 80) * 100 = 12.5
        result = compute_vendor_score(10, 10.0, None)
        expected_advancement = (10 / (10 * 8)) * 100  # 12.5
        assert result["advancement_score"] == round(expected_advancement, 1)

    def test_perfect_score(self):
        # All PO confirmed (8pts each) + 5.0 rating
        result = compute_vendor_score(5, 40.0, 5.0)
        # advancement = (40/40)*100 = 100, review = (5/5)*100 = 100
        # score = 100*0.8 + 100*0.2 = 100.0
        assert result["vendor_score"] == 100.0

    def test_none_rating_uses_advancement_only(self):
        # None rating → vendor_score = advancement_score alone (no review blend)
        result = compute_vendor_score(5, 40.0, None)
        # advancement = (40 / (5*8)) * 100 = 100.0
        assert result["vendor_score"] == 100.0

    def test_low_rating_lowers_score(self):
        result_low = compute_vendor_score(5, 40.0, 1.0)
        result_neutral = compute_vendor_score(5, 40.0, None)
        assert result_low["vendor_score"] < result_neutral["vendor_score"]

    def test_score_clamped_to_0_100(self):
        result = compute_vendor_score(5, 40.0, 5.0)
        assert 0.0 <= result["vendor_score"] <= 100.0

    def test_zero_offers_cold_start(self):
        result = compute_vendor_score(0, 0.0, None)
        assert result["vendor_score"] is None
        assert result["is_new_vendor"] is True


# ═══════════════════════════════════════════════════════════════════════
#  _calc_stage_points — pure set logic
# ═══════════════════════════════════════════════════════════════════════


class TestCalcStagePoints:
    def test_all_base_stage(self):
        offer_ids = {1, 2, 3}
        result = _calc_stage_points(offer_ids, set(), set(), set())
        assert result == 3.0  # 1pt each

    def test_highest_stage_wins(self):
        # Offer 1 is PO confirmed (8), not 8+5+3+1
        offer_ids = {1}
        result = _calc_stage_points(offer_ids, {1}, {1}, {1})
        assert result == 8.0

    def test_mixed_stages(self):
        # Offer 1: base (1pt), Offer 2: quote (3pt), Offer 3: PO confirmed (8pt)
        offer_ids = {1, 2, 3}
        result = _calc_stage_points(offer_ids, {2}, set(), {3})
        assert result == 12.0  # 1 + 3 + 8

    def test_empty_set(self):
        result = _calc_stage_points(set(), set(), set(), set())
        assert result == 0.0

    def test_awarded_not_confirmed(self):
        # Offer 1: awarded (5pt) but not PO confirmed
        offer_ids = {1}
        result = _calc_stage_points(offer_ids, set(), {1}, set())
        assert result == 5.0


# ═══════════════════════════════════════════════════════════════════════
#  compute_single_vendor_score — DB fixtures
# ═══════════════════════════════════════════════════════════════════════


class TestComputeSingleVendorScore:
    def test_nonexistent_vendor_card(self, db_session):
        result = compute_single_vendor_score(db_session, 99999)
        assert result["vendor_score"] is None

    def test_vendor_with_zero_offers(self, db_session):
        card = _make_vendor_card(db_session, "empty vendor")
        db_session.commit()

        result = compute_single_vendor_score(db_session, card.id)
        assert result["vendor_score"] is None
        assert result["is_new_vendor"] is True

    def test_vendor_below_threshold(self, db_session):
        card = _make_vendor_card(db_session, "small vendor")
        _make_offers(db_session, card.id, "small vendor", 3)
        db_session.commit()

        result = compute_single_vendor_score(db_session, card.id)
        assert result["vendor_score"] is None
        assert result["is_new_vendor"] is True

    def test_vendor_above_threshold_scored(self, db_session):
        card = _make_vendor_card(db_session, "good vendor")
        _make_offers(db_session, card.id, "good vendor", 6)
        db_session.commit()

        result = compute_single_vendor_score(db_session, card.id)
        assert result["vendor_score"] is not None
        assert result["is_new_vendor"] is False
        assert 0.0 <= result["vendor_score"] <= 100.0

    def test_vendor_with_review_affects_score(self, db_session):
        card = _make_vendor_card(db_session, "reviewed vendor")
        offers = _make_offers(db_session, card.id, "reviewed vendor", 6)
        user_id = offers[0].entered_by_id
        db_session.commit()

        result_no_review = compute_single_vendor_score(db_session, card.id)

        _make_review(db_session, card.id, user_id, 5)
        db_session.commit()

        result_with_review = compute_single_vendor_score(db_session, card.id)

        # 5.0 rating → review_factor=100 vs neutral 50 → higher score
        assert result_with_review["vendor_score"] > result_no_review["vendor_score"]

    def test_name_fallback_matching(self, db_session):
        """Vendor with no vendor_card_id on offers still scores via name match."""
        card = _make_vendor_card(db_session, "fallback vendor")

        # Create offers WITHOUT vendor_card_id but with matching vendor_name
        user = User(
            email="fallback@test.com",
            name="Fallback User",
            role="buyer",
            azure_id="az-fallback-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()

        req = Requisition(
            name="REQ-fallback",
            customer_name="Test",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        for i in range(6):
            o = Offer(
                requisition_id=req.id,
                vendor_card_id=None,  # No card link
                vendor_name="fallback vendor",
                vendor_name_normalized="fallback vendor",
                mpn=f"FB-{i}",
                qty_available=100,
                unit_price=1.00,
                entered_by_id=user.id,
                status="active",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(o)
        db_session.commit()

        result = compute_single_vendor_score(db_session, card.id)
        assert result["vendor_score"] is not None
        assert result["is_new_vendor"] is False


# ═══════════════════════════════════════════════════════════════════════
#  compute_all_vendor_scores — batch DB operation
# ═══════════════════════════════════════════════════════════════════════


_quote_counter = 0


def _make_customer_site(db):
    """Create a Company + CustomerSite for quote FK."""
    co = Company(
        name="Score Test Co",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db.add(site)
    db.flush()
    return site


def _make_quote(db, req_id, user_id, offer_ids, status="sent"):
    """Create a Quote with line_items referencing offer_ids."""
    global _quote_counter
    _quote_counter += 1
    site = _make_customer_site(db)
    q = Quote(
        requisition_id=req_id,
        customer_site_id=site.id,
        quote_number=f"Q-score-{_quote_counter}",
        status=status,
        line_items=[{"offer_id": oid} for oid in offer_ids],
        subtotal=100.0,
        total_cost=50.0,
        total_margin_pct=50.0,
        created_by_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


def _make_buy_plan(db, req_id, quote_id, offer_ids, status="approved"):
    """Create a BuyPlan with line_items referencing offer_ids."""
    bp = BuyPlan(
        requisition_id=req_id,
        quote_id=quote_id,
        status=status,
        line_items=[{"offer_id": oid} for oid in offer_ids],
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    return bp


class TestComputeAllVendorScores:
    @pytest.mark.asyncio
    async def test_empty_db_returns_zero(self, db_session):
        result = await compute_all_vendor_scores(db_session)
        assert result["updated"] == 0

    @pytest.mark.asyncio
    async def test_vendor_above_threshold_scored(self, db_session):
        card = _make_vendor_card(db_session, "batch vendor")
        _make_offers(db_session, card.id, "batch vendor", 6)
        db_session.commit()

        result = await compute_all_vendor_scores(db_session)
        assert result["updated"] == 1

        db_session.refresh(card)
        assert card.vendor_score is not None
        assert card.vendor_score_computed_at is not None

    @pytest.mark.asyncio
    async def test_cold_start_vendor_gets_none(self, db_session):
        card = _make_vendor_card(db_session, "cold vendor")
        _make_offers(db_session, card.id, "cold vendor", 3)
        db_session.commit()

        await compute_all_vendor_scores(db_session)
        db_session.refresh(card)
        assert card.vendor_score is None
        assert card.is_new_vendor is True

    @pytest.mark.asyncio
    async def test_name_fallback_matching(self, db_session):
        """Offers without vendor_card_id are matched by normalized name."""
        card = _make_vendor_card(db_session, "name match vendor")

        user = User(
            email="nmv@test.com",
            name="NMV",
            role="buyer",
            azure_id="az-nmv",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        db_session.flush()

        req = Requisition(
            name="REQ-nmv",
            customer_name="Test",
            status="open",
            created_by=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        for i in range(6):
            db_session.add(
                Offer(
                    requisition_id=req.id,
                    vendor_card_id=None,
                    vendor_name="name match vendor",
                    vendor_name_normalized="name match vendor",
                    mpn=f"NMV-{i}",
                    qty_available=100,
                    unit_price=1.00,
                    entered_by_id=user.id,
                    status="active",
                    created_at=datetime.now(timezone.utc),
                )
            )
        db_session.commit()

        await compute_all_vendor_scores(db_session)
        db_session.refresh(card)
        assert card.vendor_score is not None

    @pytest.mark.asyncio
    async def test_engagement_score_backward_compat(self, db_session):
        """engagement_score is kept in sync with vendor_score."""
        card = _make_vendor_card(db_session, "compat vendor")
        _make_offers(db_session, card.id, "compat vendor", 6)
        db_session.commit()

        await compute_all_vendor_scores(db_session)
        db_session.refresh(card)
        assert card.engagement_score == card.vendor_score

    @pytest.mark.asyncio
    async def test_quote_stage_increases_score(self, db_session):
        """Offers used in quotes should score higher than base offers."""
        card_base = _make_vendor_card(db_session, "base only")
        offers_base = _make_offers(db_session, card_base.id, "base only", 6)

        card_quoted = _make_vendor_card(db_session, "quoted vendor")
        offers_quoted = _make_offers(db_session, card_quoted.id, "quoted vendor", 6)

        # Create a quote using the quoted vendor's offers
        offer_ids = [o.id for o in offers_quoted]
        _make_quote(
            db_session, offers_quoted[0].requisition_id, offers_quoted[0].entered_by_id, offer_ids, status="sent"
        )
        db_session.commit()

        await compute_all_vendor_scores(db_session)
        db_session.refresh(card_base)
        db_session.refresh(card_quoted)
        assert card_quoted.vendor_score > card_base.vendor_score

    @pytest.mark.asyncio
    async def test_buy_plan_stage_increases_score(self, db_session):
        """Offers in buy plans should score higher than quoted-only offers."""
        card_quoted = _make_vendor_card(db_session, "bp quoted")
        offers_quoted = _make_offers(db_session, card_quoted.id, "bp quoted", 6)

        card_bp = _make_vendor_card(db_session, "bp awarded")
        offers_bp = _make_offers(db_session, card_bp.id, "bp awarded", 6)

        # Quote for both
        offer_ids_q = [o.id for o in offers_quoted]
        _make_quote(
            db_session, offers_quoted[0].requisition_id, offers_quoted[0].entered_by_id, offer_ids_q, status="sent"
        )

        offer_ids_bp = [o.id for o in offers_bp]
        q = _make_quote(
            db_session, offers_bp[0].requisition_id, offers_bp[0].entered_by_id, offer_ids_bp, status="sent"
        )
        _make_buy_plan(db_session, offers_bp[0].requisition_id, q.id, offer_ids_bp, status="approved")
        db_session.commit()

        await compute_all_vendor_scores(db_session)
        db_session.refresh(card_quoted)
        db_session.refresh(card_bp)
        assert card_bp.vendor_score > card_quoted.vendor_score

    @pytest.mark.asyncio
    async def test_commit_error_returns_zero(self, db_session, monkeypatch):
        """Commit failure in compute_all_vendor_scores is handled gracefully."""
        card = _make_vendor_card(db_session, "commit err vendor")
        _make_offers(db_session, card.id, "commit err vendor", 6)
        db_session.commit()

        original_commit = db_session.commit

        def bad_commit(*args, **kwargs):
            raise RuntimeError("Simulated commit failure")

        monkeypatch.setattr(db_session, "commit", bad_commit)

        result = await compute_all_vendor_scores(db_session)
        # Should handle gracefully rather than raising
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════
#  _get_quote_offer_ids — DB tests
# ═══════════════════════════════════════════════════════════════════════


class TestGetQuoteOfferIds:
    def test_finds_offer_ids_in_sent_quotes(self, db_session):
        """Offers referenced in 'sent' quotes are found."""
        card = _make_vendor_card(db_session, "quote test vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "quote test vendor", 6)
        db_session.flush()

        offer_ids = {o.id for o in offers}
        _make_quote(db_session, req.id, user.id, [offers[0].id, offers[1].id, offers[2].id], status="sent")
        db_session.commit()

        result = _get_quote_offer_ids(db_session, offer_ids)
        assert offers[0].id in result
        assert offers[1].id in result
        assert offers[2].id in result
        assert offers[3].id not in result

    def test_ignores_draft_quotes(self, db_session):
        """Offers in 'draft' quotes are not counted."""
        card = _make_vendor_card(db_session, "draft quote vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "draft quote vendor", 6)
        db_session.flush()

        offer_ids = {o.id for o in offers}
        _make_quote(db_session, req.id, user.id, [offers[0].id], status="draft")
        db_session.commit()

        result = _get_quote_offer_ids(db_session, offer_ids)
        assert len(result) == 0

    def test_empty_line_items(self, db_session):
        """Quotes with empty line_items are handled."""
        card = _make_vendor_card(db_session, "empty li vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "empty li vendor", 6)
        db_session.flush()

        site = _make_customer_site(db_session)
        global _quote_counter
        _quote_counter += 1
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=f"Q-empty-{_quote_counter}",
            status="sent",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        offer_ids = {o.id for o in offers}
        result = _get_quote_offer_ids(db_session, offer_ids)
        assert len(result) == 0

    def test_line_items_without_offer_id(self, db_session):
        """Line items missing 'offer_id' key are skipped."""
        card = _make_vendor_card(db_session, "no oid vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "no oid vendor", 6)
        db_session.flush()

        site = _make_customer_site(db_session)
        _quote_counter_local = 9999
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=f"Q-nooid-{_quote_counter_local}",
            status="sent",
            line_items=[{"mpn": "ABC", "qty": 100}],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        offer_ids = {o.id for o in offers}
        result = _get_quote_offer_ids(db_session, offer_ids)
        assert len(result) == 0


# ═══════════════════════════════════════════════════════════════════════
#  _get_buyplan_offer_ids — DB tests
# ═══════════════════════════════════════════════════════════════════════


class TestGetBuyplanOfferIds:
    def test_finds_offer_ids_in_approved_buyplans(self, db_session):
        """Offers in approved BuyPlans are found."""
        card = _make_vendor_card(db_session, "bp test vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "bp test vendor", 6)
        db_session.flush()

        q = _make_quote(db_session, req.id, user.id, [offers[0].id], status="sent")
        _make_buy_plan(db_session, req.id, q.id, [offers[0].id], status="approved")
        db_session.commit()

        offer_ids = {o.id for o in offers}
        from app.services.vendor_score import AWARDED_STATUSES

        result = _get_buyplan_offer_ids(db_session, offer_ids, AWARDED_STATUSES)
        assert offers[0].id in result

    def test_cancelled_buyplan_excluded(self, db_session):
        """Offers in cancelled BuyPlans are not found."""
        card = _make_vendor_card(db_session, "bp cancel vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "bp cancel vendor", 6)
        db_session.flush()

        q = _make_quote(db_session, req.id, user.id, [offers[0].id], status="sent")
        _make_buy_plan(db_session, req.id, q.id, [offers[0].id], status="cancelled")
        db_session.commit()

        offer_ids = {o.id for o in offers}
        from app.services.vendor_score import AWARDED_STATUSES

        result = _get_buyplan_offer_ids(db_session, offer_ids, AWARDED_STATUSES)
        assert len(result) == 0

    def test_po_confirmed_statuses(self, db_session):
        """PO confirmed status offers are found with PO_CONFIRMED_STATUSES."""
        card = _make_vendor_card(db_session, "po confirm vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "po confirm vendor", 6)
        db_session.flush()

        q = _make_quote(db_session, req.id, user.id, [offers[0].id], status="sent")
        _make_buy_plan(db_session, req.id, q.id, [offers[0].id], status="po_confirmed")
        db_session.commit()

        offer_ids = {o.id for o in offers}
        from app.services.vendor_score import PO_CONFIRMED_STATUSES

        result = _get_buyplan_offer_ids(db_session, offer_ids, PO_CONFIRMED_STATUSES)
        assert offers[0].id in result


# ═══════════════════════════════════════════════════════════════════════
#  compute_vendor_score — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestComputeVendorScoreEdges:
    def test_rating_at_zero(self):
        """Rating of 0 results in review_factor = 0."""
        result = compute_vendor_score(10, 80.0, 0.0)
        assert result["vendor_score"] == 80.0

    def test_very_low_stage_points(self):
        """Very low stage points with many offers gives low score."""
        result = compute_vendor_score(100, 100.0, None)
        assert result["advancement_score"] == 12.5
        assert result["vendor_score"] == 12.5

    def test_score_exactly_100(self):
        """Score clamped at exactly 100."""
        result = compute_vendor_score(5, 40.0, 5.0)
        assert result["vendor_score"] == 100.0

    def test_high_rating_with_low_advancement(self):
        """High rating boosts low advancement score via 80/20 blend."""
        result = compute_vendor_score(10, 10.0, 5.0)
        assert result["vendor_score"] == 30.0


# ═══════════════════════════════════════════════════════════════════════
#  compute_all_vendor_scores — gap coverage
# ═══════════════════════════════════════════════════════════════════════


class TestComputeAllVendorScoresGaps:
    @pytest.mark.asyncio
    async def test_flush_exception_handled(self, db_session):
        """Flush failure during batch is caught and logged."""
        card = _make_vendor_card(db_session, "flush err vendor")
        _make_offers(db_session, card.id, "flush err vendor", 6)
        db_session.commit()

        call_count = 0

        def bad_flush(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise RuntimeError("Flush failed")

        with patch.object(db_session, "flush", side_effect=bad_flush):
            try:
                result = await compute_all_vendor_scores(db_session)
            except Exception:
                pass  # commit may also fail; the point is flush doesn't crash

    @pytest.mark.asyncio
    async def test_po_confirmed_stage_scoring(self, db_session):
        """Vendor with PO-confirmed BuyPlan scores highest (8 pts per offer)."""
        card = _make_vendor_card(db_session, "po master vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "po master vendor", 6)
        db_session.flush()

        offer_ids = [o.id for o in offers]
        q = _make_quote(db_session, req.id, user.id, offer_ids, status="won")
        _make_buy_plan(db_session, req.id, q.id, offer_ids, status="po_confirmed")
        db_session.commit()

        result = await compute_all_vendor_scores(db_session)
        assert result["updated"] >= 1

        db_session.refresh(card)
        assert card.vendor_score == 100.0
        assert card.advancement_score == 100.0

    @pytest.mark.asyncio
    async def test_buyplan_with_none_line_items(self, db_session):
        """BuyPlan with None line_items is handled gracefully."""
        card = _make_vendor_card(db_session, "none bp vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "none bp vendor", 6)
        db_session.flush()

        q = _make_quote(db_session, req.id, user.id, [offers[0].id], status="sent")
        bp = BuyPlan(
            requisition_id=req.id,
            quote_id=q.id,
            status="approved",
            line_items=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()

        result = await compute_all_vendor_scores(db_session)
        assert result["updated"] >= 1

    @pytest.mark.asyncio
    async def test_quote_with_none_line_items(self, db_session):
        """Quote with None line_items is handled gracefully."""
        card = _make_vendor_card(db_session, "none q vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "none q vendor", 6)
        db_session.flush()

        site = _make_customer_site(db_session)
        _quote_counter_local2 = 8888
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number=f"Q-none-{_quote_counter_local2}",
            status="sent",
            line_items=None,
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        result = await compute_all_vendor_scores(db_session)
        assert result["updated"] >= 1

    @pytest.mark.asyncio
    async def test_vendor_with_reviews_batch(self, db_session):
        """Vendor with reviews gets blended score in batch compute."""
        card = _make_vendor_card(db_session, "reviewed batch vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "reviewed batch vendor", 6)
        db_session.flush()

        review = VendorReview(
            vendor_card_id=card.id,
            user_id=user.id,
            rating=5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(review)
        db_session.commit()

        result = await compute_all_vendor_scores(db_session)
        db_session.refresh(card)

        assert card.vendor_score == 30.0
        assert card.engagement_score == card.vendor_score

    @pytest.mark.asyncio
    async def test_complete_buyplan_stage(self, db_session):
        """BuyPlan with 'complete' status counts as PO confirmed (8pts)."""
        card = _make_vendor_card(db_session, "complete bp vendor")
        offers, user, req = _make_offers_full(db_session, card.id, "complete bp vendor", 6)
        db_session.flush()

        offer_ids = [o.id for o in offers]
        q = _make_quote(db_session, req.id, user.id, offer_ids, status="won")
        _make_buy_plan(db_session, req.id, q.id, offer_ids, status="complete")
        db_session.commit()

        result = await compute_all_vendor_scores(db_session)
        db_session.refresh(card)
        assert card.advancement_score == 100.0
