"""Tests for buyplan_hub.supervise_overview — the unified action *queue*.

The supervise lens reshapes its five source queries (approvals, halted, overdue POs,
PO-verify, flagged) into ONE flat, risk-first-ordered list of uniform row dicts
(``overview["queue"]``). This module covers that queue:

- Every row carries the identical key set (uniform shape).
- Risk-first tier order (halted → flagged → overdue → approve → verify_po)
  with oldest-first within each tier.
- Field population: value/margin from the parent plan, owner_role (AM vs Buyer),
  waiting_since per kind, issue_reason only on flagged, line-only fields only on
  line kinds.

Depends on: app/services/buyplan_hub.supervise_overview,
            conftest fixtures (db_session, test_user, test_quote, test_requisition,
            test_offer).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, LineIssueType, SOVerificationStatus
from app.models.buy_plan import BuyPlan, BuyPlanLine

#: The exact key set every queue row must carry, regardless of kind.
_ROW_KEYS = {
    "kind",
    "label",
    "priority",
    "plan_id",
    "line_id",
    "customer_name",
    "so_number",
    "mpn",
    "vendor_name",
    "owner_name",
    "owner_role",
    "value",
    "margin_pct",
    "waiting_since",
    "issue_reason",
}


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
    created_at: datetime | None = None,
    sales_order_number: str | None = None,
    total_cost=None,
    total_margin_pct=None,
) -> BuyPlan:
    """Create + flush a minimal BuyPlan, optionally back-dating
    approved_at/created_at."""
    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=requisition_id,
        status=status,
        so_status=so_status,
        submitted_by_id=submitted_by_id,
        approved_by_id=approved_by_id,
        sales_order_number=sales_order_number,
        total_cost=total_cost,
        total_margin_pct=total_margin_pct,
    )
    db.add(plan)
    db.flush()
    updates: dict = {}
    if approved_at is not None:
        updates["approved_at"] = approved_at
    if created_at is not None:
        updates["created_at"] = created_at
    if updates:
        db.query(BuyPlan).filter(BuyPlan.id == plan.id).update(updates, synchronize_session="fetch")
        db.flush()
        db.refresh(plan)
    return plan


def _make_line(
    db: Session,
    *,
    buy_plan_id: int,
    status: str = BuyPlanLineStatus.AWAITING_PO,
    buyer_id: int | None = None,
    offer_id: int | None = None,
    issue_type: str | None = None,
    quantity: int = 10,
    last_nudge_at: datetime | None = None,
    created_at: datetime | None = None,
) -> BuyPlanLine:
    """Create + flush a minimal BuyPlanLine, optionally back-dating created_at."""
    line = BuyPlanLine(
        buy_plan_id=buy_plan_id,
        buyer_id=buyer_id,
        offer_id=offer_id,
        quantity=quantity,
        status=status,
        issue_type=issue_type,
        last_nudge_at=last_nudge_at,
    )
    db.add(line)
    db.flush()
    if created_at is not None:
        db.query(BuyPlanLine).filter(BuyPlanLine.id == line.id).update(
            {"created_at": created_at}, synchronize_session="fetch"
        )
        db.flush()
        db.refresh(line)
    return line


# ── Uniform shape ─────────────────────────────────────────────────────


def test_queue_rows_have_uniform_shape(db_session, test_user, test_quote, test_requisition, test_offer):
    """Every queue row carries the identical key set, whatever its kind."""
    from app.services.buyplan_hub import supervise_overview

    # One plan kind (approve) + one line kind (flagged) is enough to prove uniformity.
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
    )
    active = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    _make_line(
        db_session,
        buy_plan_id=active.id,
        buyer_id=test_user.id,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.ISSUE,
        issue_type=LineIssueType.LEAD_TIME_CHANGED,
    )

    queue = supervise_overview(db_session)["queue"]
    assert len(queue) >= 2
    for row in queue:
        assert set(row.keys()) == _ROW_KEYS
        assert row["kind"] in {"halted", "flagged", "overdue", "approve", "verify_po"}


# ── Risk-first + oldest-first ordering ─────────────────────────────────


def test_queue_risk_first_ordering(db_session, test_user, test_quote, test_requisition, test_offer):
    """The queue is ordered halted → flagged → overdue → approve → verify_po.

    (Phase D folded SO verification into the single approval, so there is no verify_so
    kind.)
    """
    from app.services.buyplan_hub import supervise_overview

    # One plan/line per kind, in DELIBERATELY scrambled insertion order.
    approve_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
    )
    halted_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
    )
    overdue_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(UTC) - timedelta(hours=24),
    )
    overdue_line = _make_line(
        db_session,
        buy_plan_id=overdue_plan.id,
        buyer_id=test_user.id,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.AWAITING_PO,
    )
    pv_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    verify_po_line = _make_line(
        db_session,
        buy_plan_id=pv_plan.id,
        buyer_id=test_user.id,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.PENDING_VERIFY,
    )
    flagged_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    flagged_line = _make_line(
        db_session,
        buy_plan_id=flagged_plan.id,
        buyer_id=test_user.id,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.ISSUE,
        issue_type=LineIssueType.OTHER,
    )

    queue = supervise_overview(db_session)["queue"]
    kinds = [r["kind"] for r in queue]
    assert kinds == ["halted", "flagged", "overdue", "approve", "verify_po"]
    # priority values are strictly ascending across the queue.
    priorities = [r["priority"] for r in queue]
    assert priorities == sorted(priorities)
    assert priorities == [1, 2, 3, 4, 5]

    # Spot-check the row identities line up with their source records.
    by_kind = {r["kind"]: r for r in queue}
    assert by_kind["halted"]["plan_id"] == halted_plan.id
    assert by_kind["approve"]["plan_id"] == approve_plan.id
    assert by_kind["overdue"]["line_id"] == overdue_line.id
    assert by_kind["verify_po"]["line_id"] == verify_po_line.id
    assert by_kind["flagged"]["line_id"] == flagged_line.id


def test_queue_oldest_first_within_tier(db_session, test_user, test_quote, test_requisition):
    """Within one tier (halted), the older plan sorts before the newer one."""
    from app.services.buyplan_hub import supervise_overview

    now = datetime.now(UTC)
    newer = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
        created_at=now - timedelta(days=1),
    )
    older = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
        submitted_by_id=test_user.id,
        created_at=now - timedelta(days=10),
    )

    queue = supervise_overview(db_session)["queue"]
    halted = [r for r in queue if r["kind"] == "halted"]
    assert [r["plan_id"] for r in halted] == [older.id, newer.id]
    # waiting_since is ascending (oldest first).
    assert halted[0]["waiting_since"] <= halted[1]["waiting_since"]


# ── Field population ──────────────────────────────────────────────────


def test_queue_plan_kind_fields(db_session, test_user, test_quote, test_requisition):
    """A plan kind (approve) carries owner_role 'AM', plan value/margin, NULL line
    fields."""
    from app.services.buyplan_hub import supervise_overview

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
        sales_order_number="SO-9001",
        total_cost="4200.00",
        total_margin_pct="32.00",
    )

    row = next(r for r in supervise_overview(db_session)["queue"] if r["plan_id"] == plan.id)
    assert row["kind"] == "approve"
    assert row["label"] == "Approve"
    assert row["owner_role"] == "AM"
    assert row["owner_name"] == (test_user.name or test_user.email)
    assert row["so_number"] == "SO-9001"
    assert float(row["value"]) == 4200.00
    assert float(row["margin_pct"]) == 32.00
    assert row["waiting_since"] == plan.created_at
    # Line-only fields are NULL on plan kinds.
    assert row["line_id"] is None
    assert row["mpn"] is None
    assert row["vendor_name"] is None
    assert row["issue_reason"] is None


def test_queue_line_kind_uses_parent_plan_value_and_buyer_owner(
    db_session, test_user, test_quote, test_requisition, test_offer
):
    """A line kind (overdue) shows the PARENT plan's value/margin and owner_role
    'Buyer'."""
    from app.services.buyplan_hub import supervise_overview

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        approved_at=datetime.now(UTC) - timedelta(hours=24),
        sales_order_number="SO-9002",
        total_cost="7777.00",
        total_margin_pct="11.00",
    )
    line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.AWAITING_PO,
        last_nudge_at=None,
    )

    row = next(r for r in supervise_overview(db_session)["queue"] if r["line_id"] == line.id)
    assert row["kind"] == "overdue"
    assert row["label"] == "Overdue PO"
    assert row["owner_role"] == "Buyer"
    assert row["owner_name"] == (test_user.name or test_user.email)
    assert row["plan_id"] == plan.id
    # value/margin come from the parent plan, not the line.
    assert float(row["value"]) == 7777.00
    assert float(row["margin_pct"]) == 11.00
    # offer-sourced line fields populated.
    assert row["mpn"] == test_offer.mpn
    assert row["vendor_name"] == test_offer.vendor_name
    assert row["so_number"] == "SO-9002"
    # overdue waiting_since = coalesce(last_nudge_at, plan.approved_at) → approved_at here.
    assert row["waiting_since"] == plan.approved_at
    assert row["issue_reason"] is None


def test_queue_flagged_has_issue_reason_only(db_session, test_user, test_quote, test_requisition, test_offer):
    """issue_reason is populated on flagged rows and NULL everywhere else."""
    from app.services.buyplan_hub import supervise_overview

    active = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    flagged = _make_line(
        db_session,
        buy_plan_id=active.id,
        buyer_id=test_user.id,
        offer_id=test_offer.id,
        status=BuyPlanLineStatus.ISSUE,
        issue_type=LineIssueType.LEAD_TIME_CHANGED,
    )
    # A non-flagged (verify_po) line on a second plan, to prove issue_reason stays NULL.
    pv_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    _make_line(
        db_session,
        buy_plan_id=pv_plan.id,
        buyer_id=test_user.id,
        status=BuyPlanLineStatus.PENDING_VERIFY,
    )

    queue = supervise_overview(db_session)["queue"]
    flagged_row = next(r for r in queue if r["line_id"] == flagged.id)
    assert flagged_row["kind"] == "flagged"
    assert flagged_row["issue_reason"] == "Lead time changed"
    # Every other row has a NULL issue_reason.
    for row in queue:
        if row["kind"] != "flagged":
            assert row["issue_reason"] is None


def test_queue_flagged_reason_prefers_note(db_session, test_user, test_quote, test_requisition):
    """A buyer's free-text issue_note wins over the humanised type label."""
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

    row = next(r for r in supervise_overview(db_session)["queue"] if r["line_id"] == line.id)
    assert row["issue_reason"] == "Vendor MOQ doubled — needs reprice"


def test_queue_empty_on_clean_db(db_session):
    """An all-clean DB yields an empty queue (and the strip is still present)."""
    from app.services.buyplan_hub import supervise_overview

    result = supervise_overview(db_session)
    assert result["queue"] == []
    assert "strip" in result
