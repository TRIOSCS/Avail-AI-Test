"""test_offer_lead_time_structurizer.py — TDD tests for the offer lead-time structurizer
(survey idea #12, lead-time slice).

Offer.lead_time is free text ("2-3wk"), so offers can't be filtered by lead time.
This normalizes it into an integer lead_time_days:
  - apply_offer_lead_time(offer): deterministic normalize_lead_time at save
    (lead_time_days_ai stays False; NO AI on the save path);
  - backfill_offer_lead_times(db): a nightly sweep — deterministic for unset rows,
    then ONE Haiku call (hard-guarded to null, never a guess) for the free-text
    residue the deterministic parser can't resolve, marking those lead_time_days_ai
    True so the review-queue row shows the amber "AI — verify" chip.
    The AI value NEVER overrides a deterministic parse.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_offer_lead_time_structurizer.py -v)
Depends on: app.services.offer_lead_time, app.models.offers, conftest.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import OfferStatus
from app.models import Requirement, User
from app.models.offers import Offer
from app.models.sourcing import Requisition


def _offer(db: Session, user: User, lead_time: str | None, *, days=None, ai=False) -> Offer:
    req = Requisition(name="OLT", customer_name="Acme", status="open", created_by=user.id)
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317")
    db.add(rq)
    db.flush()
    o = Offer(
        requisition_id=req.id,
        requirement_id=rq.id,
        vendor_name="V",
        vendor_name_normalized="v",
        mpn="LM317",
        normalized_mpn="LM317",
        status=OfferStatus.ACTIVE.value,
        lead_time=lead_time,
        lead_time_days=days,
        lead_time_days_ai=ai,
    )
    db.add(o)
    db.commit()
    return o


# ── apply_offer_lead_time (save path, deterministic only) ──────────────────────


class TestApplyAtSave:
    def test_deterministic_parse_sets_days(self, db_session, test_user):
        from app.services.offer_lead_time import apply_offer_lead_time

        o = _offer(db_session, test_user, "4-6 weeks")
        apply_offer_lead_time(o)
        assert o.lead_time_days == 35  # midpoint of 4-6 weeks in days
        assert o.lead_time_days_ai is False

    def test_stock_is_zero_days(self, db_session, test_user):
        from app.services.offer_lead_time import apply_offer_lead_time

        o = _offer(db_session, test_user, "stock")
        apply_offer_lead_time(o)
        assert o.lead_time_days == 0
        assert o.lead_time_days_ai is False

    def test_unparseable_leaves_days_none_no_ai_on_save(self, db_session, test_user):
        from app.services.offer_lead_time import apply_offer_lead_time

        o = _offer(db_session, test_user, "ask vendor after CNY")
        apply_offer_lead_time(o)
        assert o.lead_time_days is None  # save path never calls AI
        assert o.lead_time_days_ai is False


# ── backfill_offer_lead_times (nightly) ────────────────────────────────────────


class TestBackfill:
    async def test_deterministic_backfill_fills_unset(self, db_session, test_user):
        from app.services.offer_lead_time import backfill_offer_lead_times

        _offer(db_session, test_user, "30 days")  # days unset
        with patch("app.services.offer_lead_time.claude_structured", new_callable=AsyncMock) as ai:
            n = await backfill_offer_lead_times(db_session, use_ai=True)
        assert n >= 1
        o = db_session.query(Offer).filter(Offer.lead_time == "30 days").one()
        assert o.lead_time_days == 30
        assert o.lead_time_days_ai is False
        ai.assert_not_called()  # deterministic resolved it — no AI spent

    async def test_ai_residue_backfill_sets_flag(self, db_session, test_user):
        from app.services.offer_lead_time import backfill_offer_lead_times

        _offer(db_session, test_user, "roughly a month and a half out")  # deterministic → None
        with patch(
            "app.services.offer_lead_time.claude_structured",
            new_callable=AsyncMock,
            return_value={"lead_time_days": 45},
        ):
            await backfill_offer_lead_times(db_session, use_ai=True)
        o = db_session.query(Offer).filter(Offer.lead_time.like("roughly%")).one()
        assert o.lead_time_days == 45
        assert o.lead_time_days_ai is True  # AI-sourced → chip

    async def test_ai_null_guard_leaves_unset(self, db_session, test_user):
        from app.services.offer_lead_time import backfill_offer_lead_times

        _offer(db_session, test_user, "call to discuss")
        with patch(
            "app.services.offer_lead_time.claude_structured",
            new_callable=AsyncMock,
            return_value={"lead_time_days": None},  # AI declines to guess
        ):
            await backfill_offer_lead_times(db_session, use_ai=True)
        o = db_session.query(Offer).filter(Offer.lead_time == "call to discuss").one()
        assert o.lead_time_days is None
        assert o.lead_time_days_ai is False

    async def test_ai_never_overrides_deterministic(self, db_session, test_user):
        from app.services.offer_lead_time import backfill_offer_lead_times

        # A row the deterministic parser CAN resolve must never reach the AI tier.
        _offer(db_session, test_user, "2 weeks")
        with patch("app.services.offer_lead_time.claude_structured", new_callable=AsyncMock) as ai:
            await backfill_offer_lead_times(db_session, use_ai=True)
        o = db_session.query(Offer).filter(Offer.lead_time == "2 weeks").one()
        assert o.lead_time_days == 14
        assert o.lead_time_days_ai is False
        ai.assert_not_called()

    async def test_use_ai_false_skips_residue(self, db_session, test_user):
        from app.services.offer_lead_time import backfill_offer_lead_times

        _offer(db_session, test_user, "sometime next quarter")
        with patch("app.services.offer_lead_time.claude_structured", new_callable=AsyncMock) as ai:
            await backfill_offer_lead_times(db_session, use_ai=False)
        ai.assert_not_called()
        o = db_session.query(Offer).filter(Offer.lead_time.like("sometime%")).one()
        assert o.lead_time_days is None


# ── Save wiring + display chip ─────────────────────────────────────────────────


def test_add_offer_normalizes_lead_time(client, db_session, test_user):
    req = Requisition(name="OLT-ROUTE", customer_name="Acme", status="open", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317")
    db_session.add(rq)
    db_session.commit()
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/add-offer",
        data={
            "vendor_name": "Acme Dist",
            "mpn": "LM317",
            "requirement_id": str(rq.id),
            "quantity": "100",
            "unit_price": "1.0",
            "lead_time": "3-4 weeks",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code in (200, 201)
    o = db_session.query(Offer).filter(Offer.lead_time == "3-4 weeks").first()
    assert o is not None
    assert o.lead_time_days == 24  # 3-4 weeks midpoint (3.5w = 24.5 → 24 days)


def test_review_queue_shows_days_and_ai_chip(client, db_session, test_user):
    o = _offer(db_session, test_user, "about 6 weeks", days=42, ai=True)
    o.status = OfferStatus.PENDING_REVIEW.value  # the review queue lists pending_review
    db_session.commit()
    resp = client.get("/v2/partials/offers/review-queue", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    body = resp.text
    assert "about 6 weeks" in body  # the free-text original still shows
    assert "42" in body  # the normalized days
    assert "verify" in body.lower()  # amber AI-verify chip (this value was AI-sourced)


def test_review_queue_no_chip_for_deterministic_value(client, db_session, test_user):
    o = _offer(db_session, test_user, "30 days", days=30, ai=False)
    o.status = OfferStatus.PENDING_REVIEW.value
    db_session.commit()
    resp = client.get("/v2/partials/offers/review-queue", headers={"HX-Request": "true"})
    body = resp.text
    assert "30 days" in body
    # a deterministically-normalized value carries no AI-verify chip
    assert "verify" not in body.lower()


# ── Adversarial-review fixes (3 confirmed findings) ────────────────────────────


class TestReviewFixes:
    async def test_backfill_marks_ai_attempted_and_second_run_skips(self, db_session, test_user):
        """A residue row the AI declines must be marked attempted so the nightly job
        never re-bills it — the unbounded re-spend fix."""
        from app.services.offer_lead_time import backfill_offer_lead_times

        _offer(db_session, test_user, "call to discuss")  # deterministic + AI both → None
        with patch(
            "app.services.offer_lead_time.claude_structured",
            new_callable=AsyncMock,
            return_value={"lead_time_days": None},
        ) as ai1:
            await backfill_offer_lead_times(db_session, use_ai=True)
        assert ai1.call_count == 1
        o = db_session.query(Offer).filter(Offer.lead_time == "call to discuss").one()
        assert o.lead_time_days is None  # still unknown (never fabricated)
        assert o.lead_time_ai_attempted_at is not None  # but marked tried

        # A SECOND nightly run must NOT re-send the already-tried row.
        with patch("app.services.offer_lead_time.claude_structured", new_callable=AsyncMock) as ai2:
            await backfill_offer_lead_times(db_session, use_ai=True)
        ai2.assert_not_called()

    async def test_backfill_marks_attempted_even_on_ai_success(self, db_session, test_user):
        from app.services.offer_lead_time import backfill_offer_lead_times

        _offer(db_session, test_user, "roughly six weeks out")
        with patch(
            "app.services.offer_lead_time.claude_structured",
            new_callable=AsyncMock,
            return_value={"lead_time_days": 42},
        ):
            await backfill_offer_lead_times(db_session, use_ai=True)
        o = db_session.query(Offer).filter(Offer.lead_time.like("roughly%")).one()
        assert o.lead_time_days == 42
        assert o.lead_time_ai_attempted_at is not None

    def test_edit_offer_renormalizes_lead_time_days(self, client, db_session, test_user):
        """Editing the free-text lead_time must re-derive lead_time_days (else a sort by
        'ships inside N days' returns stale wrong results)."""
        o = _offer(db_session, test_user, "4-6 weeks", days=35)
        resp = client.post(
            f"/v2/partials/requisitions/{o.requisition_id}/offers/{o.id}/edit",
            data={"lead_time": "stock", "vendor_name": "V", "mpn": "LM317", "quantity": "100"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code in (200, 201)
        db_session.refresh(o)
        assert o.lead_time == "stock"
        assert o.lead_time_days == 0  # re-normalized from the edited text, not the stale 35

    def test_ai_parsed_save_normalizes_lead_time_at_save(self, db_session, test_user):
        """The AI-parsed offer save path (the dominant real-world source) also
        normalizes lead_time at save, not only manual add_offer."""
        from types import SimpleNamespace

        from app.services.ai_offer_service import save_freeform_offers

        req = Requisition(name="OLT-FF", customer_name="Acme", status="open", created_by=test_user.id)
        db_session.add(req)
        db_session.flush()
        rq = Requirement(requisition_id=req.id, primary_mpn="LM317")
        db_session.add(rq)
        db_session.commit()
        parsed = [
            SimpleNamespace(
                mpn="LM317",
                vendor_name="Acme Dist",
                unit_price=1.0,
                qty_available=100,
                lead_time="3-4 weeks",
                date_code=None,
                condition="New",
                packaging=None,
                currency="USD",
                moq=None,
                notes=None,
                manufacturer=None,
            )
        ]
        save_freeform_offers(db_session, req.id, parsed, test_user.id)
        db_session.commit()
        o = db_session.query(Offer).filter(Offer.lead_time == "3-4 weeks").first()
        assert o is not None
        assert o.lead_time_days == 24  # normalized deterministically at save
