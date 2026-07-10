"""test_approvals_sp4_fall_down.py — Approvals SP-4: receiving-reject → re-source.

SP-4 is the fall-down path at receiving: the buyer can REJECT line(s) (wrong / defective /
short parts) after the goods arrive. A rejected line "falls down" into the SAME open
re-source pool the vendor-cancel Re-source uses (status RESOURCING) so it can be
backfilled from another vendor — it REUSES ``resource_line`` (and therefore the
POCancellation vendor-performance fact + the urgent buyer alert); it does NOT build a
parallel queue. Phase 3 retired the deal-level PO gate's INBOUND state, so a delivered
deal is a COMPLETED (auto-completed) plan — rejecting reopens it to ACTIVE (the kept
reopen-on-completed branch).

Covers:
  - rejecting a received line on a COMPLETED plan pools it (RESOURCING) and reopens the
    plan to ACTIVE so the line can be re-claimed/re-cut;
  - the rejection records a POCancellation with the receiving-specific reason;
  - the reject route fires one urgent backfill alert per pooled line;
  - the reject route is owner- + buyer-gated (non-owner sales → 404; owner-but-not-a-
    PO-cutter → 403).

Called by: pytest
Depends on: conftest fixtures, app.services.buyplan_workflow, app.routers.htmx.buy_plans.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import (
    RESOURCE_TO_UNAVAILABILITY_REASON,
    BuyPlanLineStatus,
    BuyPlanStatus,
    LineResourceReason,
    OfferStatus,
    SOVerificationStatus,
    UnavailabilityReason,
)
from app.models import Offer, POCancellation, User  # noqa: F401
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.sourcing import Requisition
from app.services.buyplan_workflow import resource_line

# ── Helpers (mirror tests/test_buyplan_resource.py) ──────────────────────


def _make_plan(db, quote, requisition, **overrides) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=requisition.id,
        status=BuyPlanStatus.COMPLETED.value,
        so_status=SOVerificationStatus.APPROVED.value,
        total_cost=10_000.0,
        total_revenue=20_000.0,
        total_margin_pct=50.0,
        ai_flags=[],
        created_at=datetime.now(UTC),
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


def _make_received_line(db, plan, requirement, offer, buyer, **overrides) -> BuyPlanLine:
    """A VERIFIED line on a COMPLETED plan — goods arrived (a live, received PO)."""
    defaults = dict(
        buy_plan_id=plan.id,
        requirement_id=requirement.id,
        offer_id=offer.id,
        quantity=100,
        unit_cost=1.0,
        unit_sell=2.0,
        buyer_id=buyer.id,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-7700",
        po_confirmed_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.flush()
    return line


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


# ── Service: receiving-reject pools the line via the EXISTING re-source ───


class TestReceivingRejectService:
    def test_reject_pools_line_and_reopens_completed_plan(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_received_line(db_session, plan, requirement, offer, test_user)

        payload = resource_line(plan.id, line.id, LineResourceReason.DEFECTIVE.value, "Bad caps", test_user, db_session)
        db_session.commit()
        db_session.refresh(line)
        db_session.refresh(plan)

        # Pooled into the SAME open re-source pool the vendor-cancel flow uses.
        assert line.status == BuyPlanLineStatus.RESOURCING.value
        assert line.buyer_id is None
        assert line.offer_id is None
        # The COMPLETED plan reopens to ACTIVE so the line can be re-claimed/re-cut.
        assert plan.status == BuyPlanStatus.ACTIVE.value
        assert len(payload["resourced_lines"]) == 1
        # Backorder flag: a COMPLETED plan was reopened → the fan-out must force the alert.
        assert payload["was_completed"] is True

    def test_active_plan_cancel_is_not_a_backorder(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        """A vendor-cancel on an ACTIVE (in-flight) plan does NOT set was_completed —
        the broadcast keeps normal preference gating (contrast with the completed-plan
        case)."""
        plan = _make_plan(db_session, test_quote, test_requisition, status=BuyPlanStatus.ACTIVE.value)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_received_line(db_session, plan, requirement, offer, test_user)

        payload = resource_line(plan.id, line.id, LineResourceReason.SOLD_ELSEWHERE.value, None, test_user, db_session)
        db_session.commit()
        db_session.refresh(plan)

        assert plan.status == BuyPlanStatus.ACTIVE.value
        assert payload["was_completed"] is False

    def test_reject_records_cancellation_fact(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_received_line(db_session, plan, requirement, offer, test_user)

        resource_line(plan.id, line.id, LineResourceReason.WRONG_PART.value, None, test_user, db_session)
        db_session.commit()

        cancel = db_session.query(POCancellation).filter_by(buy_plan_line_id=line.id).one()
        assert cancel.reason_code == LineResourceReason.WRONG_PART.value
        assert cancel.po_number == "PO-7700"

    @pytest.mark.parametrize(
        "reason",
        [
            LineResourceReason.DEFECTIVE.value,
            LineResourceReason.WRONG_PART.value,
            LineResourceReason.SHORT_SHIP.value,
        ],
    )
    def test_each_receiving_reason_is_valid_and_maps_to_unavailability(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card, reason
    ):
        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_received_line(db_session, plan, requirement, offer, test_user)

        resource_line(plan.id, line.id, reason, None, test_user, db_session)
        db_session.commit()
        db_session.refresh(line)

        assert line.status == BuyPlanLineStatus.RESOURCING.value
        # Every receiving reason maps to a real vendor-unavailability reason.
        mapped = RESOURCE_TO_UNAVAILABILITY_REASON[reason]
        assert mapped in {r.value for r in UnavailabilityReason}


# ── Route: /resource pools + alerts on a COMPLETED plan, owner/buyer gated ──
# Phase 3 retired the separate /reject-received route; a receiving-reject / late fall-down is
# now the same /resource route acting on a COMPLETED plan (it reopens to ACTIVE + re-sources).


class TestBackorderResourceRoute:
    @pytest.mark.asyncio
    async def test_route_pools_line_and_fires_alert(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.routers.htmx import buy_plans as htmx_buy_plans

        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_received_line(db_session, plan, requirement, offer, test_user)
        db_session.commit()

        mock_bg = AsyncMock()
        req = _FakeRequest({"reason_code": "defective", "scope": "line"})
        with (
            patch("app.services.buyplan_notifications.run_notify_bg", mock_bg),
            patch.object(htmx_buy_plans, "buy_plan_detail_partial", new_callable=AsyncMock, return_value="ok"),
        ):
            result = await htmx_buy_plans.buy_plan_resource_line_partial(
                req, plan.id, line.id, user=test_user, db=db_session
            )

        db_session.refresh(line)
        assert result == "ok"
        assert line.status == BuyPlanLineStatus.RESOURCING.value
        fired = [c.args[0].__name__ for c in mock_bg.await_args_list]
        assert "notify_resource_requested" in fired

    @pytest.mark.asyncio
    async def test_route_requires_reason(
        self, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
    ):
        from app.routers.htmx import buy_plans as htmx_buy_plans

        plan = _make_plan(db_session, test_quote, test_requisition)
        requirement = test_requisition.requirements[0]
        offer = _make_offer(db_session, requirement, test_vendor_card)
        line = _make_received_line(db_session, plan, requirement, offer, test_user)
        db_session.commit()

        req = _FakeRequest({"reason_code": "", "scope": "line"})
        with pytest.raises(HTTPException) as exc:
            await htmx_buy_plans.buy_plan_resource_line_partial(req, plan.id, line.id, user=test_user, db=db_session)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_route_buyer_gated_owner_but_not_po_cutter_403(
        self, db_session: Session, sales_user, test_quote, test_vendor_card
    ):
        """A sales owner (not a PO-cutter) is 403'd by the buyer gate."""
        from app.routers.htmx import buy_plans as htmx_buy_plans

        # Requisition OWNED by the sales user, so get_buyplan_for_user passes ownership and
        # the _require_po_cutter buyer gate is what fires.
        req_obj = Requisition(
            name="REQ-SALES-OWN",
            customer_name="SalesCo",
            status="open",
            created_by=sales_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req_obj)
        db_session.flush()
        plan = BuyPlan(
            requisition_id=req_obj.id,
            quote_id=test_quote.id,
            status=BuyPlanStatus.COMPLETED.value,
            so_status=SOVerificationStatus.APPROVED.value,
            total_cost=10_000.0,
        )
        db_session.add(plan)
        db_session.commit()

        req = _FakeRequest({"reason_code": "defective"})
        with pytest.raises(HTTPException) as exc:
            await htmx_buy_plans.buy_plan_resource_line_partial(req, plan.id, 1, user=sales_user, db=db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_route_non_owner_sales_404(
        self, db_session: Session, test_user, sales_user, test_quote, test_requisition, test_vendor_card
    ):
        """A sales user who does not own the plan is 404'd before any mutation."""
        from app.routers.htmx import buy_plans as htmx_buy_plans

        plan = _make_plan(db_session, test_quote, test_requisition)  # owned by test_user (buyer)
        db_session.commit()

        req = _FakeRequest({"reason_code": "defective"})
        with pytest.raises(HTTPException) as exc:
            await htmx_buy_plans.buy_plan_resource_line_partial(req, plan.id, 1, user=sales_user, db=db_session)
        assert exc.value.status_code == 404


# ── Render: the COMPLETED detail partial shows the per-line Re-source affordance ──


def test_completed_detail_renders_resource_affordance(
    client, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
):
    """The real detail template renders the fall-down affordance on a COMPLETED plan's
    cut line (catches Jinja errors + confirms the form posts to /resource — the late-
    fall-down / receiving-reject entry point now that INBOUND is retired)."""
    plan = _make_plan(db_session, test_quote, test_requisition)
    requirement = test_requisition.requirements[0]
    offer = _make_offer(db_session, requirement, test_vendor_card)
    _make_received_line(db_session, plan, requirement, offer, test_user)
    db_session.commit()

    resp = client.get(f"/v2/partials/buy-plans/{plan.id}")

    assert resp.status_code == 200
    assert f"/v2/partials/buy-plans/{plan.id}/lines/" in resp.text
    assert "/resource" in resp.text
    assert "Re-source" in resp.text
