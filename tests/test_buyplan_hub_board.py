"""Tests for buyplan_hub.deals_board — the stage-grouped deal board read model.

Covers:
- Column bucketing by status (DRAFT→draft, PENDING→pending, ACTIVE/HALTED→active,
  COMPLETED→done, CANCELLED omitted)
- scope=mine filters by submitted_by_id; scope=all returns everything
- po_progress counts (verified, total-non-cancelled)
- blocker text: ACTIVE + 2 AWAITING_PO lines → "2 POs to cut"
- blocker text: all lines VERIFIED + so_status APPROVED → "ready to fulfill"
- CANCELLED plans omitted from all columns

Depends on: app/services/buyplan_hub.deals_board,
            conftest fixtures (db_session, test_user, manager_user, test_quote,
            test_requisition).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models.buy_plan import BuyPlan, BuyPlanLine


def _make_plan(
    db: Session,
    *,
    quote_id: int,
    requisition_id: int,
    status: str = BuyPlanStatus.ACTIVE,
    so_status: str = SOVerificationStatus.APPROVED,
    submitted_by_id: int | None = None,
    total_cost=None,
    total_margin_pct=None,
    is_stock_sale: bool = False,
) -> BuyPlan:
    """Create + flush a minimal BuyPlan header."""
    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=requisition_id,
        status=status,
        so_status=so_status,
        submitted_by_id=submitted_by_id,
        total_cost=total_cost,
        total_margin_pct=total_margin_pct,
        is_stock_sale=is_stock_sale,
    )
    db.add(plan)
    db.flush()
    return plan


def _make_line(
    db: Session,
    *,
    buy_plan_id: int,
    status: str = BuyPlanLineStatus.AWAITING_PO,
    buyer_id: int | None = None,
    quantity: int = 10,
) -> BuyPlanLine:
    """Create + flush a minimal BuyPlanLine."""
    line = BuyPlanLine(
        buy_plan_id=buy_plan_id,
        buyer_id=buyer_id,
        quantity=quantity,
        status=status,
    )
    db.add(line)
    db.flush()
    return line


# ── 1. Column bucketing ───────────────────────────────────────────────


def test_deals_board_column_bucketing(db_session, test_user, test_quote, test_requisition):
    """Plans bucket into their correct columns; CANCELLED is omitted."""
    from app.services.buyplan_hub import deals_board

    kwargs = dict(quote_id=test_quote.id, requisition_id=test_requisition.id, db=db_session)

    draft_plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.DRAFT
    )
    pending_plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.PENDING
    )
    active_plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.ACTIVE
    )
    halted_plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.HALTED
    )
    completed_plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.COMPLETED
    )
    cancelled_plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.CANCELLED
    )

    board = deals_board(db_session, test_user, scope="all")

    # All four keys must always be present
    assert set(board.keys()) == {"draft", "pending", "active", "done"}

    draft_ids = [d["plan_id"] for d in board["draft"]]
    pending_ids = [d["plan_id"] for d in board["pending"]]
    active_ids = [d["plan_id"] for d in board["active"]]
    done_ids = [d["plan_id"] for d in board["done"]]

    assert draft_plan.id in draft_ids
    assert pending_plan.id in pending_ids
    assert active_plan.id in active_ids
    assert halted_plan.id in active_ids  # HALTED → active column
    assert completed_plan.id in done_ids
    assert cancelled_plan.id not in (draft_ids + pending_ids + active_ids + done_ids)


# ── 2. Scope mine vs all ──────────────────────────────────────────────


def test_deals_board_scope_mine(db_session, test_user, manager_user, test_quote, test_requisition):
    """Scope=mine only returns plans submitted_by_id == user.id."""
    from app.services.buyplan_hub import deals_board

    my_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )
    other_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=manager_user.id,
    )

    board = deals_board(db_session, test_user, scope="mine")
    all_ids = [d["plan_id"] for col in board.values() for d in col]

    assert my_plan.id in all_ids
    assert other_plan.id not in all_ids


def test_deals_board_scope_all(db_session, test_user, manager_user, test_quote, test_requisition):
    """Scope=all returns plans regardless of owner."""
    from app.services.buyplan_hub import deals_board

    my_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )
    other_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=manager_user.id,
    )

    board = deals_board(db_session, test_user, scope="all")
    all_ids = [d["plan_id"] for col in board.values() for d in col]

    assert my_plan.id in all_ids
    assert other_plan.id in all_ids


# ── 3. po_progress ────────────────────────────────────────────────────


def test_deals_board_po_progress(db_session, test_user, test_quote, test_requisition):
    """po_progress = (verified_count, total_non_cancelled_count)."""
    from app.services.buyplan_hub import deals_board

    plan = _make_plan(
        db_session, quote_id=test_quote.id, requisition_id=test_requisition.id, status=BuyPlanStatus.ACTIVE
    )
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.AWAITING_PO)
    # Cancelled lines excluded from total
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.CANCELLED)

    board = deals_board(db_session, test_user, scope="all")
    deals = board["active"]
    deal = next(d for d in deals if d["plan_id"] == plan.id)

    cut, total = deal["po_progress"]
    assert cut == 2  # two VERIFIED
    assert total == 3  # 3 non-cancelled (2 verified + 1 awaiting_po)


# ── 4. Blocker: 2 POs to cut ─────────────────────────────────────────


def test_deals_board_blocker_pos_to_cut(db_session, test_user, test_quote, test_requisition):
    """ACTIVE plan with 2 AWAITING_PO lines → blocker == '2 POs to cut'."""
    from app.services.buyplan_hub import deals_board

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        so_status=SOVerificationStatus.APPROVED,
    )
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.AWAITING_PO)
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.AWAITING_PO)

    board = deals_board(db_session, test_user, scope="all")
    deal = next(d for d in board["active"] if d["plan_id"] == plan.id)

    assert deal["blocker"] == "2 POs to cut"


# ── 5. Blocker: ready to fulfill ─────────────────────────────────────


def test_deals_board_blocker_ready_to_fulfill(db_session, test_user, test_quote, test_requisition):
    """ACTIVE + all lines VERIFIED + so_status APPROVED → 'ready to fulfill'."""
    from app.services.buyplan_hub import deals_board

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        so_status=SOVerificationStatus.APPROVED,
    )
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)
    _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)

    board = deals_board(db_session, test_user, scope="all")
    deal = next(d for d in board["active"] if d["plan_id"] == plan.id)

    assert deal["blocker"] == "ready to fulfill"


# ── 6. CANCELLED omitted ─────────────────────────────────────────────


def test_deals_board_cancelled_omitted(db_session, test_user, test_quote, test_requisition):
    """CANCELLED plans do not appear in any column."""
    from app.services.buyplan_hub import deals_board

    cancelled_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.CANCELLED,
    )

    board = deals_board(db_session, test_user, scope="all")
    all_ids = [d["plan_id"] for col in board.values() for d in col]

    assert cancelled_plan.id not in all_ids


# ── 7. Dict fields ───────────────────────────────────────────────────


def test_deals_board_dict_fields(db_session, test_user, test_quote, test_requisition):
    """Each deal dict carries all required keys."""
    from app.services.buyplan_hub import deals_board

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
        total_cost="5000.00",
        total_margin_pct="20.00",
        is_stock_sale=True,
    )

    board = deals_board(db_session, test_user, scope="all")
    deal = next(d for d in board["active"] if d["plan_id"] == plan.id)

    required_keys = {
        "plan_id",
        "customer_name",
        "value",
        "margin_pct",
        "stage_label",
        "blocker",
        "po_progress",
        "needs_my_action",
        "is_stock_sale",
    }
    assert required_keys.issubset(deal.keys())
    assert deal["plan_id"] == plan.id
    assert deal["is_stock_sale"] is True


# ── 8. needs_my_action for DRAFT ─────────────────────────────────────


def test_deals_board_needs_my_action_draft(db_session, test_user, manager_user, test_quote, test_requisition):
    """needs_my_action=True for plan owner when status=DRAFT."""
    from app.services.buyplan_hub import deals_board

    draft_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=test_user.id,
    )
    active_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )

    board = deals_board(db_session, test_user, scope="all")

    draft_deal = next(d for d in board["draft"] if d["plan_id"] == draft_plan.id)
    active_deal = next(d for d in board["active"] if d["plan_id"] == active_plan.id)

    assert draft_deal["needs_my_action"] is True
    assert active_deal["needs_my_action"] is False
