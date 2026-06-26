"""Tests for the Re-source workflow: resource_line + claim_line + the open-claim queue.

resource_line moves a cut-PO line (vendor fell down) back into the open claim pool and
records the cancellation (via po_cancellation_service); claim_line is the first-to-claim
guarded assignment. The cancellation-metric/offer-sold/unavailability side effects are
owned + tested by po_cancellation_service; here we assert the workflow wiring + state
machine + escalation + claim race.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import (
    BuyPlanLineStatus,
    BuyPlanStatus,
    LineResourceReason,
    OfferStatus,
    SOVerificationStatus,
)
from app.models import Offer, POCancellation, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.services.buyplan_hub import resourcing_pool_queue
from app.services.buyplan_workflow import claim_line, resource_line


def _make_plan(db, quote, requisition, **overrides) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=requisition.id,
        status=BuyPlanStatus.ACTIVE.value,
        so_status=SOVerificationStatus.APPROVED.value,
        total_cost=100.0,
        total_revenue=200.0,
        total_margin_pct=50.0,
        ai_flags=[],
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.flush()
    return plan


def _make_offer(db, requirement, vendor_card, **overrides) -> Offer:
    defaults = dict(
        requirement_id=requirement.id,
        vendor_card_id=vendor_card.id,
        vendor_name=vendor_card.display_name or "Arrow Electronics",
        vendor_name_normalized=vendor_card.normalized_name,
        mpn=requirement.primary_mpn or "LM317T",
        normalized_mpn=requirement.primary_mpn or "LM317T",
        unit_price=1.0,
        status=OfferStatus.ACTIVE.value,
    )
    defaults.update(overrides)
    offer = Offer(**defaults)
    db.add(offer)
    db.flush()
    return offer


def _make_cut_line(db, plan, requirement, offer, buyer, **overrides) -> BuyPlanLine:
    """A line whose PO has been cut (pending_verify/verified)."""
    defaults = dict(
        buy_plan_id=plan.id,
        requirement_id=requirement.id,
        offer_id=offer.id,
        quantity=100,
        unit_cost=1.0,
        unit_sell=2.0,
        buyer_id=buyer.id,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-9001",
        po_confirmed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.flush()
    return line


def _second_buyer(db) -> User:
    u = User(email="buyer2@trioscs.com", name="Buyer Two", role="buyer", azure_id="az-buyer2")
    db.add(u)
    db.flush()
    return u


class TestResourceLine:
    def test_resets_line_into_open_pool(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)

        payload = resource_line(
            plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, "Vendor flaked", test_user, db_session
        )
        db_session.commit()
        db_session.refresh(line)

        assert line.status == BuyPlanLineStatus.RESOURCING.value
        assert line.buyer_id is None
        assert line.offer_id is None
        assert line.po_number is None
        assert line.po_confirmed_at is None
        assert payload["plan_id"] == plan.id
        assert len(payload["resourced_lines"]) == 1

    def test_records_cancellation_and_marks_offer_sold(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)

        resource_line(plan.id, line.id, LineResourceReason.CANNOT_DELIVER.value, None, test_user, db_session)
        db_session.commit()

        cancel = db_session.query(POCancellation).filter_by(buy_plan_line_id=line.id).one()
        assert cancel.vendor_card_id == test_vendor_card.id
        assert cancel.po_number == "PO-9001"
        assert cancel.days_to_cancel is not None and cancel.days_to_cancel > 0
        assert cancel.reason_code == LineResourceReason.CANNOT_DELIVER.value

        db_session.refresh(offer)
        assert offer.status == OfferStatus.SOLD.value

    def test_reopens_completed_plan(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition, status=BuyPlanStatus.COMPLETED.value)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)

        resource_line(plan.id, line.id, LineResourceReason.NO_STOCK.value, None, test_user, db_session)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.status == BuyPlanStatus.ACTIVE.value

    def test_rejects_line_without_live_po(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(
            db_session,
            plan,
            requirement,
            offer,
            test_user,
            status=BuyPlanLineStatus.AWAITING_PO.value,
            po_number=None,
            po_confirmed_at=None,
        )

        with pytest.raises(ValueError):
            resource_line(plan.id, line.id, LineResourceReason.OTHER.value, None, test_user, db_session)

    def test_escalation_resources_sibling_lines(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer_a = _make_offer(db_session, requirement, test_vendor_card)
        offer_b = _make_offer(db_session, requirement, test_vendor_card)
        line_a = _make_cut_line(db_session, plan, requirement, offer_a, test_user, po_number="PO-A")
        line_b = _make_cut_line(db_session, plan, requirement, offer_b, test_user, po_number="PO-B")

        payload = resource_line(
            plan.id,
            line_a.id,
            LineResourceReason.SOLD_ELSEWHERE.value,
            None,
            test_user,
            db_session,
            also_line_ids=[line_b.id],
        )
        db_session.commit()
        db_session.refresh(line_a)
        db_session.refresh(line_b)

        assert line_a.status == BuyPlanLineStatus.RESOURCING.value
        assert line_b.status == BuyPlanLineStatus.RESOURCING.value
        assert len(payload["resourced_lines"]) == 2
        assert db_session.query(POCancellation).count() == 2


class TestResourceLineEdgeCases:
    def test_rejects_cancelled_plan(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        # A VERIFIED line can survive on a CANCELLED plan (cancel only cascades open lines);
        # re-sourcing it would create a dead-end (claim → confirm_po needs an ACTIVE plan).
        plan = _make_plan(db_session, test_quote, test_requisition, status=BuyPlanStatus.CANCELLED.value)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)

        with pytest.raises(ValueError):
            resource_line(plan.id, line.id, LineResourceReason.OTHER.value, None, test_user, db_session)

    def test_offerless_line_pools_without_cancellation_row(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        # offer_id is SET NULL on offer delete; a live-PO line can lose its offer. Re-source
        # must still pool the line (no crash), just without a cancellation fact.
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)
        line.offer_id = None
        db_session.flush()

        resource_line(plan.id, line.id, LineResourceReason.OTHER.value, None, test_user, db_session)
        db_session.commit()
        db_session.refresh(line)

        assert line.status == BuyPlanLineStatus.RESOURCING.value
        assert db_session.query(POCancellation).filter_by(buy_plan_line_id=line.id).count() == 0

    def test_expired_offer_does_not_abort_resource(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        # An EXPIRED offer can't transition to SOLD; mark_offer_sold must be best-effort and
        # NOT abort the whole re-source.
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card, status=OfferStatus.EXPIRED.value)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)

        resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, None, test_user, db_session)
        db_session.commit()
        db_session.refresh(line)
        db_session.refresh(offer)

        assert line.status == BuyPlanLineStatus.RESOURCING.value
        assert offer.status == OfferStatus.EXPIRED.value  # left as-is (couldn't go SOLD)


class TestClaimLine:
    def _resourcing_line(self, db, test_user, quote, requisition, vendor_card):
        plan = _make_plan(db, quote, requisition)
        requirement = requisition.requirements[0]
        offer = _make_offer(db, requirement, vendor_card)
        line = _make_cut_line(db, plan, requirement, offer, test_user)
        resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, None, test_user, db)
        db.commit()
        return plan, line

    def test_claim_assigns_buyer_and_reopens_for_po(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan, line = self._resourcing_line(db_session, test_user, test_quote, test_requisition, test_vendor_card)
        claimer = _second_buyer(db_session)

        claim_line(plan.id, line.id, claimer, db_session)
        db_session.commit()
        db_session.refresh(line)

        assert line.buyer_id == claimer.id
        assert line.status == BuyPlanLineStatus.AWAITING_PO.value

    def test_second_claim_loses_the_race(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan, line = self._resourcing_line(db_session, test_user, test_quote, test_requisition, test_vendor_card)
        first = _second_buyer(db_session)
        second = User(email="buyer3@trioscs.com", name="Buyer Three", role="buyer", azure_id="az-buyer3")
        db_session.add(second)
        db_session.flush()

        claim_line(plan.id, line.id, first, db_session)
        db_session.commit()

        with pytest.raises(ValueError):
            claim_line(plan.id, line.id, second, db_session)


class TestResourcingPoolQueue:
    def test_lists_open_pool_line_with_canceled_vendor(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)
        resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, "gone", test_user, db_session)
        db_session.commit()

        rows = resourcing_pool_queue(db_session)
        assert len(rows) == 1
        row = rows[0]
        assert row["line_id"] == line.id
        assert row["plan_id"] == plan.id
        assert row["mpn"] == requirement.primary_mpn
        assert row["canceled_vendor"] == offer.vendor_name
        assert row["reason_code"] == LineResourceReason.SOLD_ELSEWHERE.value

    def test_claimed_line_leaves_the_pool(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)
        resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, None, test_user, db_session)
        db_session.commit()
        claimer = _second_buyer(db_session)
        claim_line(plan.id, line.id, claimer, db_session)
        db_session.commit()

        assert resourcing_pool_queue(db_session) == []


class TestResourcingAlertSource:
    def _open_pool_line(self, db, test_user, quote, requisition, vendor_card):
        plan = _make_plan(db, quote, requisition)
        requirement = requisition.requirements[0]
        offer = _make_offer(db, requirement, vendor_card)
        line = _make_cut_line(db, plan, requirement, offer, test_user)
        resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, None, test_user, db)
        db.commit()
        return plan, line

    def test_po_cutter_sees_open_pool_count(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.services.alerts.sources.resourcing import BuyplanResourcingSource

        plan, line = self._open_pool_line(db_session, test_user, test_quote, test_requisition, test_vendor_card)
        src = BuyplanResourcingSource()

        assert src.count_for_user(db_session, test_user) == 1
        items = src.new_items_for_user(db_session, test_user)
        assert len(items) == 1
        assert items[0].ref_id == line.id
        assert items[0].anchor == f"bp-{plan.id}"

    def test_non_po_cutter_sees_nothing(
        self, db_session: Session, test_user, sales_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.services.alerts.sources.resourcing import BuyplanResourcingSource

        self._open_pool_line(db_session, test_user, test_quote, test_requisition, test_vendor_card)
        src = BuyplanResourcingSource()

        assert src.count_for_user(db_session, sales_user) == 0


class _FakeForm(dict):
    """Minimal Starlette FormData stand-in: .get + .getlist."""

    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        return v if isinstance(v, list) else [v]


class _FakeRequest:
    def __init__(self, form_data):
        self._form = _FakeForm(form_data)

    async def form(self):
        return self._form


class TestResourceRoutes:
    def test_resource_lens_renders(self, client):
        # Empty pool → the calm empty state. Exercises real template rendering.
        resp = client.get("/v2/partials/buy-plans/resource")
        assert resp.status_code == 200
        assert "re-source" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_resource_route_pools_line_and_fires_alert(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.routers import htmx_views

        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)
        db_session.commit()

        mock_bg = AsyncMock()
        req = _FakeRequest({"reason_code": "sold_elsewhere", "scope": "line"})
        with (
            patch("app.services.buyplan_notifications.run_notify_bg", mock_bg),
            patch.object(htmx_views, "buy_plan_detail_partial", new_callable=AsyncMock, return_value="ok"),
        ):
            result = await htmx_views.buy_plan_resource_line_partial(
                req, plan.id, line.id, user=test_user, db=db_session
            )

        db_session.refresh(line)
        assert result == "ok"
        assert line.status == BuyPlanLineStatus.RESOURCING.value
        fired = [c.args[0].__name__ for c in mock_bg.await_args_list]
        assert "notify_resource_requested" in fired

    @pytest.mark.asyncio
    async def test_escalation_fires_one_alert_per_resourced_line(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.routers import htmx_views

        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer_a = _make_offer(db_session, requirement, test_vendor_card)
        offer_b = _make_offer(db_session, requirement, test_vendor_card)
        line_a = _make_cut_line(db_session, plan, requirement, offer_a, test_user, po_number="PO-A")
        line_b = _make_cut_line(db_session, plan, requirement, offer_b, test_user, po_number="PO-B")
        db_session.commit()

        mock_bg = AsyncMock()
        req = _FakeRequest({"reason_code": "sold_elsewhere", "scope": "plan", "also_line_ids": [str(line_b.id)]})
        with (
            patch("app.services.buyplan_notifications.run_notify_bg", mock_bg),
            patch.object(htmx_views, "buy_plan_detail_partial", new_callable=AsyncMock, return_value="ok"),
        ):
            await htmx_views.buy_plan_resource_line_partial(req, plan.id, line_a.id, user=test_user, db=db_session)

        alerted_line_ids = {c.kwargs["line_id"] for c in mock_bg.await_args_list}
        assert alerted_line_ids == {line_a.id, line_b.id}

    @pytest.mark.asyncio
    async def test_claim_forbidden_for_non_po_cutter(self, db_session: Session, sales_user):
        from app.routers import htmx_views

        req = _FakeRequest({})
        with pytest.raises(HTTPException) as exc:
            await htmx_views.buy_plan_claim_line_partial(req, 1, 1, user=sales_user, db=db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_losing_claim_returns_409(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.routers import htmx_views

        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_cut_line(db_session, plan, requirement, offer, test_user)
        resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, None, test_user, db_session)
        db_session.commit()

        winner = _second_buyer(db_session)
        req = _FakeRequest({})
        with patch.object(htmx_views, "buy_plan_detail_partial", new_callable=AsyncMock, return_value="ok"):
            await htmx_views.buy_plan_claim_line_partial(req, plan.id, line.id, user=winner, db=db_session)
            with pytest.raises(HTTPException) as exc:
                await htmx_views.buy_plan_claim_line_partial(req, plan.id, line.id, user=test_user, db=db_session)
        assert exc.value.status_code == 409
