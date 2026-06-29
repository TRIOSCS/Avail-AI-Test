"""test_approvals_sp4_fall_down.py — Approvals SP-4: receiving-reject → re-source.

The SP-3 receiving step lets a buyer mark an INBOUND plan received (→ COMPLETED). SP-4
adds the fall-down path: at receiving the buyer can REJECT line(s) (wrong / defective /
short parts) instead. A rejected line "falls down" into the SAME open re-source pool the
vendor-cancel Re-source uses (status RESOURCING) so it can be backfilled from another
vendor — it REUSES ``resource_line`` (and therefore the POCancellation vendor-performance
fact + the urgent buyer alert); it does NOT build a parallel queue.

Covers:
  - rejecting a received line on an INBOUND plan pools it (RESOURCING) and reopens the
    plan to ACTIVE so the line can be re-claimed/re-cut;
  - the rejection records a POCancellation with the receiving-specific reason;
  - a fully-received PO still completes (the happy path is untouched);
  - the reject route fires one urgent backfill alert per pooled line;
  - the reject route is owner- + buyer-gated (non-owner sales → 404; owner-but-not-a-
    PO-cutter → 403).

Called by: pytest
Depends on: conftest fixtures, app.services.buyplan_workflow, app.routers.htmx.buy_plans.
"""

from datetime import datetime, timezone
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
from app.models import Offer, POCancellation, User  # noqa: F401 (User kept for parity/typing)
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.sourcing import Requisition
from app.services.buyplan_workflow import receive_buy_plan, resource_line

# ── Helpers (mirror tests/test_buyplan_resource.py) ──────────────────────


def _make_plan(db, quote, requisition, **overrides) -> BuyPlan:
    defaults = dict(
        quote_id=quote.id,
        requisition_id=requisition.id,
        status=BuyPlanStatus.INBOUND.value,
        so_status=SOVerificationStatus.APPROVED.value,
        total_cost=10_000.0,
        total_revenue=20_000.0,
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


def _make_received_line(db, plan, requirement, offer, buyer, **overrides) -> BuyPlanLine:
    """A VERIFIED line on an INBOUND plan — goods arrived (a live, received PO)."""
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
        po_confirmed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
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
    def test_reject_pools_line_and_reopens_inbound_plan(
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
        # The INBOUND plan reopens to ACTIVE so the line can be re-claimed/re-cut.
        assert plan.status == BuyPlanStatus.ACTIVE.value
        assert len(payload["resourced_lines"]) == 1

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


# ── Service: the happy path (fully received → COMPLETED) is untouched ─────


def test_fully_received_plan_still_completes(
    db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
):
    """With no rejected lines, mark-received still drives an INBOUND plan to
    COMPLETED."""
    plan = _make_plan(db_session, test_quote, test_requisition)
    requirement = test_requisition.requirements[0]
    offer = _make_offer(db_session, requirement, test_vendor_card)
    _make_received_line(db_session, plan, requirement, offer, test_user)

    received = receive_buy_plan(plan.id, test_user, db_session)

    assert received.status == BuyPlanStatus.COMPLETED.value
    assert received.completed_at is not None
    assert received.case_report


# ── Route: reject-received pools + alerts, owner/buyer gated ──────────────


class TestRejectReceivedRoute:
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
            result = await htmx_buy_plans.buy_plan_reject_received_line_partial(
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
            await htmx_buy_plans.buy_plan_reject_received_line_partial(
                req, plan.id, line.id, user=test_user, db=db_session
            )
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
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req_obj)
        db_session.flush()
        plan = BuyPlan(
            requisition_id=req_obj.id,
            quote_id=test_quote.id,
            status=BuyPlanStatus.INBOUND.value,
            so_status=SOVerificationStatus.APPROVED.value,
            total_cost=10_000.0,
        )
        db_session.add(plan)
        db_session.commit()

        req = _FakeRequest({"reason_code": "defective"})
        with pytest.raises(HTTPException) as exc:
            await htmx_buy_plans.buy_plan_reject_received_line_partial(req, plan.id, 1, user=sales_user, db=db_session)
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
            await htmx_buy_plans.buy_plan_reject_received_line_partial(req, plan.id, 1, user=sales_user, db=db_session)
        assert exc.value.status_code == 404


# ── Render: the INBOUND detail partial shows the per-line Reject affordance ──


def test_inbound_detail_renders_reject_affordance(
    client, db_session: Session, test_user, test_quote, test_requisition, test_vendor_card
):
    """The real detail template renders the SP-4 Reject affordance on an INBOUND plan's
    cut line (catches Jinja errors + confirms the button posts to /reject-received)."""
    plan = _make_plan(db_session, test_quote, test_requisition)
    requirement = test_requisition.requirements[0]
    offer = _make_offer(db_session, requirement, test_vendor_card)
    _make_received_line(db_session, plan, requirement, offer, test_user)
    db_session.commit()

    resp = client.get(f"/v2/partials/buy-plans/{plan.id}")

    assert resp.status_code == 200
    assert "reject-received" in resp.text
    assert "Reject &amp; re-source" in resp.text
