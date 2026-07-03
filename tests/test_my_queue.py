"""Tests for buyplan_hub.my_queue — the role-aware "what needs YOU now" builder.

Covers (one test per kind emitter + cross-cutting behaviour):
- each kind surfaces the right plan/line in the right state into a QueueRow;
- role/right/ownership gating (buyer vs approver vs PO-approver vs sales vs supervisor);
- risk-first, oldest-first ordering across mixed kinds;
- is_overdue split on the buyer-nudge SLA (cut_po vs cut_po_overdue);
- empty-user / empty-db baseline;
- prepay rows surface only for the routed prepay approver (engine-actionable);
- supervise_overview's contract is unchanged after the shared-helper extraction.

Reuses the _make_plan / _make_line builders from tests/test_buyplan_hub_supervise.py.

Depends on: app/services/buyplan_hub.my_queue,
            conftest fixtures (db_session, test_user, sales_user, manager_user,
            trader_user, test_quote, test_requisition, test_vendor_card).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalStepRule,
    ApprovalSubjectType,
    BuyPlanLineStatus,
    BuyPlanStatus,
)
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.auth import User
from app.models.buy_plan import VerificationGroupMember
from app.models.quality_plan import Prepayment
from app.services.buyplan_hub import QueueRow, my_queue
from tests.test_buyplan_hub_supervise import _make_line, _make_plan

# ── Local helpers ─────────────────────────────────────────────────────


def _grant(db: Session, user: User, **flags) -> User:
    """Set per-user approval-right columns and flush (admin-managed grants)."""
    for key, value in flags.items():
        setattr(user, key, value)
    db.flush()
    return user


def _add_ops(db: Session, user: User) -> User:
    """Make *user* an active ops verification-group member."""
    db.add(VerificationGroupMember(user_id=user.id, is_active=True))
    db.flush()
    return user


def _kinds(rows: list[QueueRow]) -> list[str]:
    return [r.kind for r in rows]


def _make_prepay_request(
    db: Session,
    *,
    recipient: User,
    buy_plan_id: int,
    amount: str = "1000.00",
    vendor_card_id: int | None = None,
) -> tuple[ApprovalRequest, Prepayment]:
    """Create a REQUESTED PREPAYMENT approval routed (PENDING) to *recipient*.

    Mirrors the engine's row shape so ``_actionable_request_ids`` returns it.
    """
    pp = Prepayment(
        buy_plan_id=buy_plan_id,
        vendor_card_id=vendor_card_id,
        total_incl_fees=Decimal(amount),
        currency="USD",
    )
    db.add(pp)
    db.flush()

    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        amount=Decimal(amount),
        currency="USD",
    )
    db.add(ar)
    db.flush()

    step = ApprovalStep(request_id=ar.id, seq=1, rule=ApprovalStepRule.ANY, status=ApprovalRecipientStatus.PENDING)
    db.add(step)
    db.flush()

    db.add(ApprovalStepRecipient(step_id=step.id, user_id=recipient.id, status=ApprovalRecipientStatus.PENDING))
    db.flush()
    return ar, pp


def _row(rows: list[QueueRow], kind: str) -> QueueRow:
    return next(r for r in rows if r.kind == kind)


# ── Baseline ──────────────────────────────────────────────────────────


def test_my_queue_empty(db_session, test_user):
    """A user with nothing to do on an empty DB gets an empty queue."""
    assert my_queue(db_session, test_user) == []


# ── halted (P1) ───────────────────────────────────────────────────────


def test_my_queue_halted_owner_sees_own(db_session, test_user, manager_user, test_quote, test_requisition):
    """A HALTED plan surfaces to its AM owner; a non-owner non-supervisor does not see
    it."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
    )

    rows = my_queue(db_session, test_user)
    assert "halted" in _kinds(rows)
    row = _row(rows, "halted")
    assert row.plan_id == plan.id
    assert row.priority == 1
    assert row.customer_name == "Acme Electronics"
    assert row.extra["owner_role"] == "AM"

    # A different buyer (not owner, not supervisor) sees nothing.
    other = User(email="other@trioscs.com", name="Other Buyer", role="buyer")
    db_session.add(other)
    db_session.flush()
    assert "halted" not in _kinds(my_queue(db_session, other))


def test_my_queue_halted_supervisor_sees_all(db_session, test_user, manager_user, test_quote, test_requisition):
    """A supervisor (manager) sees every halted plan, even ones they don't own."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
    )
    rows = my_queue(db_session, manager_user)
    assert plan.id in [r.plan_id for r in rows if r.kind == "halted"]


# ── plan_draft (P9) / plan_returned (P2) ──────────────────────────────


def test_my_queue_plan_draft(db_session, test_user, test_quote, test_requisition):
    """A fresh DRAFT the user submitted surfaces as plan_draft (Submit)."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=test_user.id,
    )
    row = _row(my_queue(db_session, test_user), "plan_draft")
    assert row.plan_id == plan.id
    assert row.priority == 9
    assert row.action_label == "Submit"
    assert row.action_url == f"/v2/partials/buy-plans/{plan.id}/submit"


def test_my_queue_plan_returned(db_session, test_user, test_quote, test_requisition):
    """A DRAFT sent back by an approver (approved_at stamped) surfaces as
    plan_returned."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=test_user.id,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    rows = my_queue(db_session, test_user)
    kinds = _kinds(rows)
    assert "plan_returned" in kinds
    assert "plan_draft" not in kinds
    assert _row(rows, "plan_returned").priority == 2


# ── plan_approve (P3) ─────────────────────────────────────────────────


def test_my_queue_plan_approve_gated_by_right(db_session, test_user, manager_user, test_quote, test_requisition):
    """A PENDING plan surfaces to a buy-plan approver only; a plain buyer never sees
    it."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
        approved_by_id=None,
    )
    _grant(db_session, manager_user, can_approve_buy_plans=True)

    approver_rows = my_queue(db_session, manager_user)
    assert plan.id in [r.plan_id for r in approver_rows if r.kind == "plan_approve"]
    assert _row(approver_rows, "plan_approve").action_url == f"/v2/partials/buy-plans/{plan.id}/approve"

    # test_user is a buyer without the approval right → no plan_approve.
    assert "plan_approve" not in _kinds(my_queue(db_session, test_user))


# ── prepay_approve (P3) ───────────────────────────────────────────────


def test_my_queue_prepay_approve_for_recipient(db_session, test_user, manager_user, test_quote, test_requisition):
    """A routed prepay approver sees prepay_approve; a non-recipient does not."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_cost="5000.00",
    )
    _grant(db_session, manager_user, can_approve_prepayments=True)
    ar, _pp = _make_prepay_request(db_session, recipient=manager_user, buy_plan_id=plan.id, amount="2500.00")

    rows = my_queue(db_session, manager_user)
    assert "prepay_approve" in _kinds(rows)
    row = _row(rows, "prepay_approve")
    assert row.plan_id == plan.id
    assert row.priority == 3
    assert row.action_url == f"/v2/approvals/requests/{ar.id}/decision"
    assert row.customer_name == "Acme Electronics"
    assert row.extra["amount"] == Decimal("2500.00")

    # test_user is not a recipient → no prepay row.
    assert "prepay_approve" not in _kinds(my_queue(db_session, test_user))


# ── po_verify (P4) ────────────────────────────────────────────────────


def test_my_queue_po_verify_gated(db_session, test_user, manager_user, test_quote, test_requisition):
    """A PENDING_VERIFY line surfaces to a PO approver (or ops); a plain buyer does
    not."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.PENDING_VERIFY,
    )
    _grant(db_session, manager_user, can_approve_purchase_orders=True)

    rows = my_queue(db_session, manager_user)
    assert "po_verify" in _kinds(rows)
    row = _row(rows, "po_verify")
    assert row.line_id == line.id
    assert row.priority == 4
    assert row.action_url == f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/verify-po"

    # test_user (the line's buyer, no PO-approve right, not ops) does NOT verify it.
    assert "po_verify" not in _kinds(my_queue(db_session, test_user))


def test_my_queue_po_verify_excludes_ops_without_right(db_session, test_user, test_quote, test_requisition):
    """An ops member WITHOUT can_approve_purchase_orders does NOT get po_verify rows.

    Phase D moved the verify-PO gate onto the per-user right; My Queue must not surface
    a verify action the user would 403 on (ops membership alone no longer grants it).
    Existing ops verifiers keep the capability because migration 173 grandfathers them
    into the right.
    """
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.PENDING_VERIFY)
    _add_ops(db_session, test_user)  # ops member (halt authority), but no PO-approval right

    assert "po_verify" not in _kinds(my_queue(db_session, test_user))


# ── claim (P5) ────────────────────────────────────────────────────────


def test_my_queue_claim_gated_to_po_cutters(db_session, test_user, sales_user, test_quote, test_requisition):
    """An unclaimed RESOURCING line surfaces to a PO-cutter (buyer); sales never
    claims."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=None, status=BuyPlanLineStatus.RESOURCING)

    rows = my_queue(db_session, test_user)
    assert "claim" in _kinds(rows)
    assert _row(rows, "claim").line_id == line.id
    assert _row(rows, "claim").priority == 5

    # sales is not a PO-cutter → no claim.
    assert "claim" not in _kinds(my_queue(db_session, sales_user))


# ── cut_po (P7) / cut_po_overdue (P6) ─────────────────────────────────


def test_my_queue_cut_po_not_overdue(db_session, test_user, test_quote, test_requisition):
    """A fresh AWAITING_PO line for the buyer is cut_po (not overdue)."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc),  # just approved → within SLA
    )
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)

    rows = my_queue(db_session, test_user)
    assert "cut_po" in _kinds(rows)
    assert "cut_po_overdue" not in _kinds(rows)
    row = _row(rows, "cut_po")
    assert row.line_id == line.id
    assert row.priority == 7
    assert row.is_overdue is False
    assert row.action_url == f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po"


def test_my_queue_cut_po_overdue(db_session, test_user, test_quote, test_requisition):
    """An AWAITING_PO line past the buyer-nudge SLA is cut_po_overdue
    (is_overdue=True)."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc) - timedelta(hours=48),
    )
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)

    rows = my_queue(db_session, test_user)
    assert "cut_po_overdue" in _kinds(rows)
    assert "cut_po" not in _kinds(rows)
    row = _row(rows, "cut_po_overdue")
    assert row.line_id == line.id
    assert row.priority == 6
    assert row.is_overdue is True


# ── Ordering: risk-first, oldest-first within a tier ──────────────────


def test_my_queue_priority_ordering(db_session, test_user, test_quote, test_requisition):
    """Across mixed kinds the queue sorts by priority (halted P1 before cut_po P7)."""
    halted = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
    )
    active = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc),
    )
    _make_line(db_session, buy_plan_id=active.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)

    rows = my_queue(db_session, test_user)
    assert rows[0].kind == "halted"
    assert rows[0].plan_id == halted.id
    # priorities are non-decreasing across the whole queue
    assert [r.priority for r in rows] == sorted(r.priority for r in rows)


def test_my_queue_oldest_first_within_tier(db_session, test_user, test_quote, test_requisition):
    """Within one tier the OLDER item (larger age_hours) leads."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    older = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.AWAITING_PO,
        created_at=datetime.now(timezone.utc) - timedelta(hours=10),
    )
    newer = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.AWAITING_PO,
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    cut_rows = [r for r in my_queue(db_session, test_user) if r.kind == "cut_po"]
    assert [r.line_id for r in cut_rows] == [older.id, newer.id]
    assert cut_rows[0].age_hours > cut_rows[1].age_hours


# ── Role gating summary (sales sees only owner-scoped work) ────────────


def test_my_queue_sales_sees_only_owned_drafts(db_session, sales_user, manager_user, test_quote, test_requisition):
    """A sales user is not a PO-cutter/approver: they only see their own
    draft/returned/halted.

    A PENDING plan they own routes to approvers, not to them; a RESOURCING pool line is
    invisible to non-cutters.
    """
    # A real buy-plan approver exists, so the owned PENDING plan is normally-pending (routed
    # to the manager) rather than config-stuck — it must NOT surface to sales as no_approver.
    _grant(db_session, manager_user, can_approve_buy_plans=True)
    own_draft = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=sales_user.id,
    )
    # Pending plan they own — should NOT appear (sales can't approve).
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=sales_user.id,
    )
    # A pooled line — PO-cutter only.
    pooled_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    _make_line(db_session, buy_plan_id=pooled_plan.id, buyer_id=None, status=BuyPlanLineStatus.RESOURCING)

    rows = my_queue(db_session, sales_user)
    assert _kinds(rows) == ["plan_draft"]
    assert rows[0].plan_id == own_draft.id


# ── flagged (P2) — supervisor-only triage (Phase F-1 gap-fill) ─────────


def test_my_queue_flagged_supervisor_only(db_session, test_user, manager_user, test_quote, test_requisition):
    """An ISSUE (buyer-flagged) line surfaces as a P2 flagged row to a supervisor only.

    The row carries the issue reason in extra, no inline action (action_url None), and
    links to the plan detail; a non-supervisor never sees the flagged kind.
    """
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.ISSUE,
        issue_type="sold_out",
    )
    line.issue_note = "Vendor sold the lot"
    db_session.flush()

    rows = my_queue(db_session, manager_user)
    assert "flagged" in _kinds(rows)
    row = _row(rows, "flagged")
    assert row.line_id == line.id
    assert row.priority == 2
    assert row.action_url is None
    assert row.detail_href == f"/v2/partials/buy-plans/{plan.id}"
    assert row.extra["issue_reason"] == "Vendor sold the lot"

    # A plain buyer (non-supervisor) never sees the flagged kind.
    assert "flagged" not in _kinds(my_queue(db_session, test_user))


# ── cut_po kicked-back extra (Phase F-1 gap-fill) ──────────────────────


def test_my_queue_cut_po_kicked_back_extra(db_session, test_user, test_quote, test_requisition):
    """A kicked-back AWAITING_PO line carries kicked_back + po_rejection_note in
    extra."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc),
    )
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)
    line.po_rejection_note = "Wrong vendor — re-cut to Arrow"
    db_session.flush()

    row = _row(my_queue(db_session, test_user), "cut_po")
    assert row.extra["kicked_back"] is True
    assert row.extra["po_rejection_note"] == "Wrong vendor — re-cut to Arrow"


def test_my_queue_cut_po_not_kicked_back_extra(db_session, test_user, test_quote, test_requisition):
    """A normal AWAITING_PO line reports kicked_back=False and a None rejection note."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc),
    )
    _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.AWAITING_PO)

    row = _row(my_queue(db_session, test_user), "cut_po")
    assert row.extra["kicked_back"] is False
    assert row.extra["po_rejection_note"] is None


# ── prepay row carries request_id (Phase F-1 gap-fill) ─────────────────


def test_my_queue_prepay_row_carries_request_id(db_session, manager_user, test_quote, test_requisition):
    """A prepay_approve row exposes the originating ApprovalRequest id so the inline My
    Queue action can build the decide URL."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_cost="5000.00",
    )
    _grant(db_session, manager_user, can_approve_prepayments=True)
    ar, _pp = _make_prepay_request(db_session, recipient=manager_user, buy_plan_id=plan.id, amount="2500.00")

    row = _row(my_queue(db_session, manager_user), "prepay_approve")
    assert row.extra["request_id"] == ar.id


# ── open_avg_margin (My Queue + Pipeline metric parity, Phase F-1) ─────


def test_open_avg_margin_zero_when_no_open_plans(db_session):
    """No open plans → 0.0 (AVG of an empty set, coalesced)."""
    from app.services.buyplan_hub import open_avg_margin

    assert open_avg_margin(db_session) == 0.0


def test_open_avg_margin_averages_open_plans_only(db_session, test_quote, test_requisition):
    """Averages total_margin_pct across OPEN plans; terminal (COMPLETED) plans
    excluded."""
    from app.services.buyplan_hub import open_avg_margin

    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_margin_pct=20,
    )
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        total_margin_pct=40,
    )
    # COMPLETED is terminal → excluded from the open-book average.
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        total_margin_pct=100,
    )

    assert open_avg_margin(db_session) == 30.0


# ── supervise_overview contract unchanged (R4) ────────────────────────


def test_supervise_overview_contract_unchanged(db_session):
    """The shared-helper extraction left supervise_overview's strip/queue shape
    intact."""
    from app.services.buyplan_hub import supervise_overview

    result = supervise_overview(db_session)
    assert set(result.keys()) == {"strip", "queue"}
    assert set(result["strip"].keys()) == {
        "open_value",
        "avg_margin",
        "approval_count",
        "halted_count",
        "overdue_po_count",
        "po_pending_verify_count",
        "flagged_count",
    }
    assert result["queue"] == []


class TestNoApproverKind:
    """no_approver — a plan silently stalled because no approver is configured surfaces
    to its owner (who otherwise has no signal) and to admins (who can fix the
    config)."""

    def test_surfaces_stuck_pending_plan_to_owner(self, db_session, test_user, test_quote, test_requisition):
        _make_plan(
            db_session,
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.PENDING,
            submitted_by_id=test_user.id,
        )
        assert "no_approver" in _kinds(my_queue(db_session, test_user))

    def test_absent_when_buy_plan_approver_configured(
        self, db_session, test_user, manager_user, test_quote, test_requisition
    ):
        _grant(db_session, manager_user, can_approve_buy_plans=True)
        _make_plan(
            db_session,
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.PENDING,
            submitted_by_id=test_user.id,
        )
        assert "no_approver" not in _kinds(my_queue(db_session, test_user))

    def test_active_with_unverifiable_po_line_without_po_approver(
        self, db_session, test_user, test_quote, test_requisition
    ):
        """Phase 3: the PO stall is per PENDING_VERIFY line, not the plan total — a cut
        PO awaiting sign-off with no purchase-order approver configured surfaces."""
        plan = _make_plan(
            db_session,
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.ACTIVE,
            submitted_by_id=test_user.id,
            total_cost=Decimal("10000"),
        )
        _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.PENDING_VERIFY)
        assert "no_approver" in _kinds(my_queue(db_session, test_user))
