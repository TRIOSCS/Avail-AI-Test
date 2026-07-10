"""test_vendor_score_cancel.py — Cancellation-dampener tests for vendor_score.py.

Proves: no cancels → dampener 1.0 (score unchanged); a slow cancel pulls the score down
more than a fast cancel; the dampener floor is respected; and the DB-backed inline path
(compute_single_vendor_score) and batch path (compute_all_vendor_scores) both read the
SAME po_cancellations table so they agree.

Does NOT edit tests/test_vendor_score.py.

Called by: pytest
Depends on: app/services/vendor_score.py, app/services/po_cancellation_service.py, conftest
"""

from datetime import UTC, datetime

import pytest

from app.constants import POCancellationReason
from app.models import Offer, Requisition, User, VendorCard
from app.models.po_cancellation import POCancellation
from app.services.vendor_score import (
    MIN_DAMPENER,
    _cancel_dampener,
    compute_all_vendor_scores,
    compute_single_vendor_score,
    compute_vendor_score,
)
from app.vendor_utils import normalize_vendor_name

# ── Pure dampener ────────────────────────────────────────────────────


class TestCancelDampener:
    def test_no_cancels_is_one(self):
        assert _cancel_dampener(0, 0, 10) == 1.0

    def test_no_total_pos_is_one(self):
        assert _cancel_dampener(3, 1, 0) == 1.0

    def test_slow_weighs_more_than_fast(self):
        fast = _cancel_dampener(1, 0, 10)  # 1 - 0.5*(1/10) = 0.95
        slow = _cancel_dampener(1, 1, 10)  # 1 - 0.5*(2/10) = 0.90
        assert fast == 0.95
        assert slow == 0.90
        assert slow < fast < 1.0

    def test_floor_respected(self):
        assert _cancel_dampener(10, 10, 10) == MIN_DAMPENER  # 1 - 0.5*2.0 = 0 → floored


# ── compute_vendor_score (pure) with cancellation kwargs ─────────────


class TestComputeVendorScoreDampened:
    def test_no_cancels_score_unchanged(self):
        base = compute_vendor_score(10, 80.0, None)
        damp = compute_vendor_score(10, 80.0, None, cancel_count=0, slow_cancel_count=0, total_pos=10)
        assert damp["vendor_score"] == base["vendor_score"]

    def test_slow_pulls_score_down_more_than_fast(self):
        base = compute_vendor_score(10, 80.0, None)["vendor_score"]  # 100.0
        fast = compute_vendor_score(10, 80.0, None, cancel_count=1, slow_cancel_count=0, total_pos=10)["vendor_score"]
        slow = compute_vendor_score(10, 80.0, None, cancel_count=1, slow_cancel_count=1, total_pos=10)["vendor_score"]
        assert base == 100.0
        assert fast == 95.0
        assert slow == 90.0
        assert slow < fast < base

    def test_floor_caps_the_penalty(self):
        score = compute_vendor_score(10, 80.0, None, cancel_count=10, slow_cancel_count=10, total_pos=10)
        assert score["vendor_score"] == round(100.0 * MIN_DAMPENER, 1)  # 40.0

    def test_cold_start_unaffected(self):
        score = compute_vendor_score(2, 2.0, None, cancel_count=5, slow_cancel_count=5, total_pos=10)
        assert score["vendor_score"] is None


# ── DB helpers ───────────────────────────────────────────────────────


def _make_card(db, name, *, total_pos=0):
    card = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        total_pos=total_pos,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


def _make_offers(db, card, name, count):
    user = User(email=f"u-{name}@t.com", name="U", role="buyer", azure_id=f"az-{name}", created_at=datetime.now(UTC))
    db.add(user)
    db.flush()
    req = Requisition(
        name=f"REQ-{name}", customer_name="C", status="open", created_by=user.id, created_at=datetime.now(UTC)
    )
    db.add(req)
    db.flush()
    for i in range(count):
        db.add(
            Offer(
                requisition_id=req.id,
                vendor_card_id=card.id,
                vendor_name=name,
                vendor_name_normalized=normalize_vendor_name(name),
                mpn=f"MPN-{name}-{i}",
                qty_available=100,
                unit_price=1.00,
                entered_by_id=user.id,
                status="active",
                created_at=datetime.now(UTC),
            )
        )
    db.flush()


def _add_cancel(db, card, days):
    db.add(
        POCancellation(
            vendor_card_id=card.id,
            vendor_name_normalized=card.normalized_name,
            normalized_mpn="LM317T",
            po_number="PO",
            cancelled_at=datetime.now(UTC),
            days_to_cancel=days,
            reason_code=POCancellationReason.OTHER.value,
        )
    )
    db.flush()


# ── compute_single_vendor_score reads po_cancellations ───────────────


class TestSingleScoreReadsCancellations:
    def test_slow_cancel_lowers_inline_score(self, db_session):
        card = _make_card(db_session, "single cancel vendor", total_pos=10)
        _make_offers(db_session, card, "single cancel vendor", 6)
        db_session.commit()
        baseline = compute_single_vendor_score(db_session, card.id)["vendor_score"]

        _add_cancel(db_session, card, days=20)  # slow
        db_session.commit()
        dampened = compute_single_vendor_score(db_session, card.id)["vendor_score"]

        assert baseline is not None
        assert dampened < baseline


# ── compute_all_vendor_scores reads the SAME table (inline == nightly) ─


class TestBatchScoreReadsCancellations:
    @pytest.mark.asyncio
    async def test_batch_matches_inline(self, db_session):
        card = _make_card(db_session, "batch cancel vendor", total_pos=10)
        _make_offers(db_session, card, "batch cancel vendor", 6)
        _add_cancel(db_session, card, days=20)  # slow
        db_session.commit()

        inline = compute_single_vendor_score(db_session, card.id)["vendor_score"]

        await compute_all_vendor_scores(db_session)
        db_session.refresh(card)
        # Inline (re-source) and nightly batch read the same po_cancellations rows.
        assert card.vendor_score == inline

    @pytest.mark.asyncio
    async def test_slow_cancel_scores_lower_than_fast_in_batch(self, db_session):
        slow_card = _make_card(db_session, "slow cancel vendor", total_pos=10)
        _make_offers(db_session, slow_card, "slow cancel vendor", 6)
        _add_cancel(db_session, slow_card, days=20)  # slow (> threshold)

        fast_card = _make_card(db_session, "fast cancel vendor", total_pos=10)
        _make_offers(db_session, fast_card, "fast cancel vendor", 6)
        _add_cancel(db_session, fast_card, days=1)  # fast
        db_session.commit()

        await compute_all_vendor_scores(db_session)
        db_session.refresh(slow_card)
        db_session.refresh(fast_card)
        assert slow_card.vendor_score < fast_card.vendor_score
