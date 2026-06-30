"""Tests for buyplan_hub.supervise_overview — manager metric strip + unified queue.

Covers (each source query surfaces the right record into the flat ``queue`` list,
keyed by ``kind``, plus the unchanged strip aggregates):
- PENDING plan with no approver appears in approval_count + an ``approve`` queue row.
- HALTED plan appears in halted_count + a ``halted`` queue row.
- ISSUE line appears in flagged_count + a ``flagged`` queue row (with issue_reason).
- Old AWAITING_PO line on ACTIVE plan with old approved_at appears in overdue_po
  count + an ``overdue`` queue row (anchor is BuyPlan.approved_at, not line.created_at).
- Freshly-approved plan's AWAITING_PO line is NOT overdue (within SLA).
- Strip open_value sums non-terminal plan costs; COMPLETED/CANCELLED excluded.
- Strip avg_margin averages non-terminal plan margins; NULL rows skipped.

Depends on: app/services/buyplan_hub.supervise_overview,
            conftest fixtures (db_session, test_user, manager_user, test_quote,
            test_requisition).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, LineIssueType, SOVerificationStatus
from app.models.buy_plan import BuyPlan, BuyPlanLine


def _make_plan(
    db: Session,
    *,
    quote_id: int,
    requisition_id: int,
    status: str = BuyPlanStatus.ACTIVE,
    so_status: str = SOVerificationStatus.APPROVED,
    submitted_by_id: int | None = None,
    approved_by_id: int | None = None,
    approved_at: datetime | None = None,
    total_cost=None,
    total_margin_pct=None,
) -> BuyPlan:
    """Create + flush a minimal BuyPlan header.

    Pass ``approved_at`` to set the approval timestamp that drives the overdue
    clock (back-dated via UPDATE after flush so the ORM default is overwritten).
    """
    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=requisition_id,
        status=status,
        so_status=so_status,
        submitted_by_id=submitted_by_id,
        approved_by_id=approved_by_id,
        total_cost=total_cost,
        total_margin_pct=total_margin_pct,
    )
    db.add(plan)
    db.flush()
    if approved_at is not None:
        db.query(BuyPlan).filter(BuyPlan.id == plan.id).update(
            {"approved_at": approved_at}, synchronize_session="fetch"
        )
        db.flush()
        db.refresh(plan)
    return plan


def _make_line(
    db: Session,
    *,
    buy_plan_id: int,
    status: str = BuyPlanLineStatus.AWAITING_PO,
    buyer_id: int | None = None,
    issue_type: str | None = None,
    quantity: int = 10,
    last_nudge_at: datetime | None = None,
    created_at: datetime | None = None,
) -> BuyPlanLine:
    """Create + flush a minimal BuyPlanLine, optionally back-dating created_at."""
    line = BuyPlanLine(
        buy_plan_id=buy_plan_id,
        buyer_id=buyer_id,
        quantity=quantity,
        status=status,
        issue_type=issue_type,
        last_nudge_at=last_nudge_at,
    )
    db.add(line)
    db.flush()
    # Back-date created_at after flush so the ORM default is overwritten
    if created_at is not None:
        db.query(BuyPlanLine).filter(BuyPlanLine.id == line.id).update(
            {"created_at": created_at}, synchronize_session="fetch"
        )
        db.flush()
        db.refresh(line)
    return line


# ── 1. Approval count + triage ────────────────────────────────────────


def test_supervise_approvals(db_session, test_user, test_quote, test_requisition):
    """PENDING plan with no approver appears in approval_count and approvals triage."""
    from app.services.buyplan_hub import supervise_overview

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
        approved_by_id=None,  # no approver yet
    )

    result = supervise_overview(db_session)

    assert result["strip"]["approval_count"] >= 1
    approve_rows = [r for r in result["queue"] if r["kind"] == "approve"]
    assert plan.id in [r["plan_id"] for r in approve_rows]

    # The approve row is owned by the Account Manager (submitted_by).
    row = next(r for r in approve_rows if r["plan_id"] == plan.id)
    assert row["owner_role"] == "AM"
    assert row["label"] == "Approve"


def test_supervise_approved_plan_not_in_approvals(db_session, manager_user, test_quote, test_requisition):
    """PENDING plan that already has approved_by_id is NOT in the approval queue."""
    from app.services.buyplan_hub import supervise_overview

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        approved_by_id=manager_user.id,  # already approved
    )

    result = supervise_overview(db_session)
    approval_ids = [r["plan_id"] for r in result["queue"] if r["kind"] == "approve"]
    assert plan.id not in approval_ids


# ── 2. Halted count + triage ──────────────────────────────────────────


def test_supervise_halted(db_session, test_user, test_quote, test_requisition):
    """HALTED plan appears in halted_count and halted triage."""
    from app.services.buyplan_hub import supervise_overview

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
    )

    result = supervise_overview(db_session)

    assert result["strip"]["halted_count"] >= 1
    halted_rows = [r for r in result["queue"] if r["kind"] == "halted"]
    assert plan.id in [r["plan_id"] for r in halted_rows]

    row = next(r for r in halted_rows if r["plan_id"] == plan.id)
    assert row["owner_role"] == "AM"
    assert row["label"] == "Halted"


# ── 3. Flagged (ISSUE) lines ─────────────────────────────────────────


def test_supervise_flagged(db_session, test_user, test_quote, test_requisition):
    """ISSUE line appears in flagged_count and flagged triage with issue_type."""
    from app.services.buyplan_hub import supervise_overview

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
        issue_type=LineIssueType.LEAD_TIME_CHANGED,
    )

    result = supervise_overview(db_session)

    assert result["strip"]["flagged_count"] >= 1
    flagged_rows = [r for r in result["queue"] if r["kind"] == "flagged"]
    assert line.id in [r["line_id"] for r in flagged_rows]

    row = next(r for r in flagged_rows if r["line_id"] == line.id)
    assert row["owner_role"] == "Buyer"
    assert row["plan_id"] == plan.id
    # The flagged row states the ACTUAL reason (Part 4): humanised type code when no note.
    assert row["issue_reason"] == "Lead time changed"


def test_supervise_flagged_reason_prefers_note(db_session, test_user, test_quote, test_requisition):
    """When a buyer left a free-text issue_note, the flagged reason shows that note."""
    from app.services.buyplan_hub import supervise_overview

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
        issue_type=LineIssueType.OTHER,
    )
    line.issue_note = "Vendor MOQ doubled — needs reprice"
    db_session.flush()

    result = supervise_overview(db_session)
    row = next(r for r in result["queue"] if r["kind"] == "flagged" and r["line_id"] == line.id)
    # The specific note wins over the generic type label.
    assert row["issue_reason"] == "Vendor MOQ doubled — needs reprice"


# ── 4. Overdue AWAITING_PO lines ─────────────────────────────────────


def test_supervise_overdue_po(db_session, test_user, test_quote, test_requisition):
    """AWAITING_PO line on ACTIVE plan with old approved_at appears in overdue_po_count
    and overdue_pos triage.

    The anchor is BuyPlan.approved_at, not line.created_at.
    """
    from app.services.buyplan_hub import supervise_overview

    # Back-date approved_at well past the 4h SLA; last_nudge_at = None (never nudged)
    old_approved_at = datetime.now(timezone.utc) - timedelta(hours=24)
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=old_approved_at,
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.AWAITING_PO,
        # created_at left as default (now) — overdue is driven by approved_at, not created_at
    )

    result = supervise_overview(db_session)

    assert result["strip"]["overdue_po_count"] >= 1
    overdue_rows = [r for r in result["queue"] if r["kind"] == "overdue"]
    assert line.id in [r["line_id"] for r in overdue_rows]

    row = next(r for r in overdue_rows if r["line_id"] == line.id)
    assert row["owner_role"] == "Buyer"
    assert row["plan_id"] == plan.id
    # overdue waiting_since is anchored on the plan's approval clock, not line.created_at.
    assert row["waiting_since"] == plan.approved_at


def test_supervise_fresh_awaiting_po_not_overdue(db_session, test_user, test_quote, test_requisition):
    """A freshly-approved plan's AWAITING_PO line (approved_at = now) is NOT overdue."""
    from app.services.buyplan_hub import supervise_overview

    # approved_at set to just now — well within the SLA
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(timezone.utc),
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.AWAITING_PO,
    )

    result = supervise_overview(db_session)
    overdue_ids = [r["line_id"] for r in result["queue"] if r["kind"] == "overdue"]
    assert line.id not in overdue_ids


def test_supervise_no_approved_at_not_overdue(db_session, test_user, test_quote, test_requisition):
    """AWAITING_PO line on ACTIVE plan with approved_at=NULL is NOT overdue (the clock
    hasn't started — plan was never formally approved)."""
    from app.services.buyplan_hub import supervise_overview

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=None,  # no approved_at → excluded from overdue
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.AWAITING_PO,
    )

    result = supervise_overview(db_session)
    overdue_ids = [r["line_id"] for r in result["queue"] if r["kind"] == "overdue"]
    assert line.id not in overdue_ids


# ── 5. Open value + avg margin ────────────────────────────────────────


def test_supervise_open_value_and_avg_margin(db_session, test_quote, test_requisition):
    """open_value sums non-terminal plans; COMPLETED/CANCELLED excluded.

    avg_margin averages.
    """
    from app.services.buyplan_hub import supervise_overview

    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        total_cost="1000.00",
        total_margin_pct="20.00",
    )
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        total_cost="500.00",
        total_margin_pct="10.00",
    )
    # COMPLETED — must be excluded from open_value
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        total_cost="9999.00",
        total_margin_pct="50.00",
    )

    result = supervise_overview(db_session)
    strip = result["strip"]

    # open_value must include at least 1000 + 500 from the two open plans
    assert strip["open_value"] >= 1500.0
    # avg_margin must be between the two non-terminal margins (10–20)
    assert 10.0 <= strip["avg_margin"] <= 20.0


def test_supervise_null_cost_treated_as_zero(db_session, test_quote, test_requisition):
    """Plans with NULL total_cost contribute 0 to open_value (no crash, no None)."""
    from app.services.buyplan_hub import supervise_overview

    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        total_cost=None,
        total_margin_pct=None,
    )

    result = supervise_overview(db_session)
    # Must return a numeric, not None
    assert isinstance(result["strip"]["open_value"], float)
    assert isinstance(result["strip"]["avg_margin"], float)


# ── 6. SO needs verification (ops) ────────────────────────────────────


# Phase D folded SO verification into the single approval — supervise_overview no longer
# emits a verify_so kind or a so_pending_count, so the old SO-pending triage tests were
# removed (the approval path is covered by test_supervise_approvals / the workflow suite).


# ── 7. POs awaiting verification (ops) ────────────────────────────────


def test_supervise_po_pending_verify(db_session, test_user, test_quote, test_requisition):
    """PENDING_VERIFY line appears in po_pending_verify_count + triage."""
    from app.services.buyplan_hub import supervise_overview

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

    result = supervise_overview(db_session)

    assert result["strip"]["po_pending_verify_count"] >= 1
    pv_rows = [r for r in result["queue"] if r["kind"] == "verify_po"]
    assert line.id in [r["line_id"] for r in pv_rows]

    row = next(r for r in pv_rows if r["line_id"] == line.id)
    assert row["label"] == "Verify PO"
    assert row["plan_id"] == plan.id
    # PO-verify row is owned by the Buyer (per-line procurement owner).
    assert row["owner_role"] == "Buyer"
    assert row["owner_name"] == (test_user.name or test_user.email)


def test_supervise_awaiting_po_not_pending_verify(db_session, test_user, test_quote, test_requisition):
    """An AWAITING_PO line is NOT in the po_pending_verify bucket."""
    from app.services.buyplan_hub import supervise_overview

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
        status=BuyPlanLineStatus.AWAITING_PO,
    )

    result = supervise_overview(db_session)
    pv_ids = [r["line_id"] for r in result["queue"] if r["kind"] == "verify_po"]
    assert line.id not in pv_ids


# ── 8. All-clean baseline ─────────────────────────────────────────────


def test_supervise_empty_db(db_session):
    """supervise_overview returns zero strip counts on an empty DB."""
    from app.services.buyplan_hub import supervise_overview

    result = supervise_overview(db_session)
    strip = result["strip"]

    assert strip["open_value"] == 0.0
    assert strip["avg_margin"] == 0.0
    assert strip["approval_count"] == 0
    assert strip["halted_count"] == 0
    assert strip["overdue_po_count"] == 0
    assert strip["po_pending_verify_count"] == 0
    assert strip["flagged_count"] == 0

    # The unified action queue is empty on a clean DB.
    assert result["queue"] == []
