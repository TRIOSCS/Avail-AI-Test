"""Tests for BuyplanActionSource — the "buy-plan needs MY action" ACTION alert.

Covers the three-role union (buyer PO step / manager approval / ops verify), the
permission gating discovered in buyplan_workflow (approve = manager|admin role; ops
verify = active VerificationGroupMember), and the ACTION invariant that
``alert_seen`` rows never change the count.

Depends on: services/alerts/sources/buyplan.BuyplanActionSource,
            services/alerts/base.record_seen, conftest fixtures.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import (
    AlertKind,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.models.buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember
from app.services.alerts.base import record_seen
from app.services.alerts.sources.buyplan import BuyplanActionSource

SOURCE = BuyplanActionSource()


def _make_plan(
    db: Session,
    *,
    quote_id: int,
    requisition_id: int,
    status: str = BuyPlanStatus.ACTIVE.value,
    so_status: str = SOVerificationStatus.APPROVED.value,
    approved_by_id: int | None = None,
    so_verified_by_id: int | None = None,
) -> BuyPlan:
    """Create + flush a minimal BuyPlan header.

    Defaults are a 'no action needed' plan.
    """
    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=requisition_id,
        status=status,
        so_status=so_status,
        approved_by_id=approved_by_id,
        so_verified_by_id=so_verified_by_id,
    )
    db.add(plan)
    db.flush()
    return plan


def _make_line(
    db: Session,
    *,
    buy_plan_id: int,
    buyer_id: int | None,
    status: str = BuyPlanLineStatus.AWAITING_PO.value,
) -> BuyPlanLine:
    """Create + flush a minimal BuyPlanLine."""
    line = BuyPlanLine(buy_plan_id=buy_plan_id, buyer_id=buyer_id, quantity=10, status=status)
    db.add(line)
    db.flush()
    return line


# ── 1. Buyer PO step ──────────────────────────────────────────────────


def test_buyer_awaiting_po_line_counts(db_session, test_user, test_quote, test_requisition):
    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id)

    assert SOURCE.count_for_user(db_session, test_user) == 1
    items = SOURCE.new_items_for_user(db_session, test_user)
    assert [i.ref_id for i in items] == [line.id]
    # ref_id is the line (what we mark seen); anchor is the PLAN's row (list is per-plan).
    assert items[0].anchor == f"bp-{plan.id}"


# ── 2. Line assigned to a different user ────────────────────────────────


def test_line_for_other_user_not_counted(db_session, test_user, manager_user, test_quote, test_requisition):
    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    # manager_user used here only as a distinct (non-test_user) buyer id.
    _make_line(db_session, buy_plan_id=plan.id, buyer_id=manager_user.id)

    assert SOURCE.count_for_user(db_session, test_user) == 0
    assert SOURCE.new_items_for_user(db_session, test_user) == []


# ── 3. My line, but not awaiting PO ─────────────────────────────────────


def test_buyer_line_not_awaiting_po_not_counted(db_session, test_user, test_quote, test_requisition):
    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.VERIFIED.value,
    )

    assert SOURCE.count_for_user(db_session, test_user) == 0


# ── 4. Manager approval ─────────────────────────────────────────────────


def test_pending_plan_counts_for_approver_not_for_non_approver(
    db_session, test_user, manager_user, admin_user, test_quote, test_requisition
):
    # PENDING + no approver = needs manager approval. (BuyPlanStatus has no SUBMITTED;
    # PENDING is the awaiting-approval state — see source module docstring.)
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING.value,
        approved_by_id=None,
    )

    # manager + admin can approve (matches buyplan_workflow.approve_buy_plan's allowed set).
    assert SOURCE.count_for_user(db_session, manager_user) == 1
    assert SOURCE.count_for_user(db_session, admin_user) == 1
    # test_user is a plain buyer — cannot approve.
    assert SOURCE.count_for_user(db_session, test_user) == 0


def test_pending_plan_already_approved_not_counted(db_session, manager_user, admin_user, test_quote, test_requisition):
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING.value,
        approved_by_id=admin_user.id,
    )
    assert SOURCE.count_for_user(db_session, manager_user) == 0


# ── 5. Ops verify ───────────────────────────────────────────────────────


def test_so_pending_counts_for_ops_member_not_for_non_member(
    db_session, test_user, sales_user, test_quote, test_requisition
):
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        so_status=SOVerificationStatus.PENDING.value,
        so_verified_by_id=None,
    )
    # Make test_user an active ops-group member; sales_user is not.
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
    db_session.flush()

    items = SOURCE.new_items_for_user(db_session, test_user)
    assert SOURCE.count_for_user(db_session, test_user) == 1
    assert items[0].anchor.startswith("bp-")
    assert SOURCE.count_for_user(db_session, sales_user) == 0


def test_inactive_ops_member_not_counted(db_session, test_user, test_quote, test_requisition):
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        so_status=SOVerificationStatus.PENDING.value,
        so_verified_by_id=None,
    )
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=False))
    db_session.flush()

    assert SOURCE.count_for_user(db_session, test_user) == 0


def test_so_already_verified_not_counted(db_session, test_user, admin_user, test_quote, test_requisition):
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        so_status=SOVerificationStatus.PENDING.value,
        so_verified_by_id=admin_user.id,  # already grabbed by someone — first to act wins
    )
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
    db_session.flush()

    assert SOURCE.count_for_user(db_session, test_user) == 0


# ── 6. ACTION ignores seen ──────────────────────────────────────────────


def test_seen_does_not_change_count(db_session, test_user, test_quote, test_requisition):
    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id)
    db_session.commit()  # record_seen commits; keep the line durable across it

    assert SOURCE.count_for_user(db_session, test_user) == 1

    record_seen(db_session, test_user, AlertKind.BUYPLAN_ACTION, line.id)

    # ACTION temperament: seen only gates the cosmetic pulse, never the count.
    assert SOURCE.count_for_user(db_session, test_user) == 1
    assert [i.ref_id for i in SOURCE.new_items_for_user(db_session, test_user)] == [line.id]


# ── 7. Plan-status gates (only ACTIVE plans are actionable for PO + ops-verify) ──


def test_buyer_po_line_on_non_active_plan_not_counted(db_session, test_user, test_quote, test_requisition):
    """A DRAFT plan's AWAITING_PO line is not actionable — confirm_po requires
    ACTIVE."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT.value,
    )
    _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id)
    assert SOURCE.count_for_user(db_session, test_user) == 0


def test_ops_verify_draft_plan_not_counted(db_session, test_user, test_quote, test_requisition):
    """so_status DEFAULTS to 'pending' on a draft — must NOT count for ops until
    ACTIVE."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT.value,
        so_status=SOVerificationStatus.PENDING.value,
        so_verified_by_id=None,
    )
    db_session.add(VerificationGroupMember(user_id=test_user.id, is_active=True))
    db_session.flush()
    assert SOURCE.count_for_user(db_session, test_user) == 0


def test_pending_plan_not_double_counted_for_admin_ops_member(db_session, admin_user, test_quote, test_requisition):
    """A PENDING plan counts ONCE (approval) for an admin who is also an ops member —
    the ops-verify branch is gated to ACTIVE, so it no longer double-counts."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING.value,
        approved_by_id=None,
        so_status=SOVerificationStatus.PENDING.value,
        so_verified_by_id=None,
    )
    db_session.add(VerificationGroupMember(user_id=admin_user.id, is_active=True))
    db_session.flush()
    assert SOURCE.count_for_user(db_session, admin_user) == 1
