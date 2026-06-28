"""Tests for buyplan_hub.deals_board + completed_archive — the deal hub read models.

Covers:
- Column bucketing by status (DRAFT→draft, PENDING→pending, ACTIVE/HALTED→active);
  COMPLETED moves to the archive, CANCELLED omitted entirely
- scope=mine filters by submitted_by_id; scope=all returns everything
- po_progress counts (verified, total-non-cancelled)
- blocker text: ACTIVE + 2 AWAITING_PO lines → "2 POs to cut"
- blocker text: all lines VERIFIED + so_status APPROVED → "ready to fulfill"
- CANCELLED plans omitted from all columns
- completed_archive: COMPLETED-only, completed_at-desc ordering, scope filtering,
  and limit/offset pagination (next_offset, total)

Depends on: app/services/buyplan_hub.deals_board + completed_archive,
            conftest fixtures (db_session, test_user, manager_user, test_quote,
            test_requisition).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    completed_at=None,
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
        completed_at=completed_at,
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

    # Active board holds in-progress work only — three columns, no "done".
    assert set(board.keys()) == {"draft", "pending", "active"}

    draft_ids = [d["plan_id"] for d in board["draft"]]
    pending_ids = [d["plan_id"] for d in board["pending"]]
    active_ids = [d["plan_id"] for d in board["active"]]
    all_active_ids = draft_ids + pending_ids + active_ids

    assert draft_plan.id in draft_ids
    assert pending_plan.id in pending_ids
    assert active_plan.id in active_ids
    assert halted_plan.id in active_ids  # HALTED → active column
    # COMPLETED moves to the archive — it must NOT clutter the active board.
    assert completed_plan.id not in all_active_ids
    # CANCELLED is omitted entirely.
    assert cancelled_plan.id not in all_active_ids


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
        "card_title",
        "customer_name",
        "owner_name",
        "tso",
        "po_numbers",
        "primary_mpn",
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


# ── 10. Card title (BP) + denser-tile deal facts ─────────────────────


def test_deals_board_card_title_is_buy_plan(db_session, test_user, test_quote, test_requisition):
    """Buy-Plan card title = '{SO#} - {Customer} - {Owner} - BP'; Owner = Account Manager."""
    from app.services.buyplan_hub import deals_board

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )
    plan.sales_order_number = "TSO-9001"
    db_session.flush()

    board = deals_board(db_session, test_user, scope="all")
    deal = next(d for d in board["active"] if d["plan_id"] == plan.id)

    # Owner on a BP card is the sales owner (submitted_by), NOT a buyer.
    owner = test_user.name or test_user.email
    assert deal["card_title"].endswith(" - BP")
    assert deal["card_title"].startswith("TSO-9001 - ")
    assert owner in deal["card_title"]
    assert deal["owner_name"] == owner
    assert deal["tso"] == "TSO-9001"


def test_deals_board_po_numbers_dedup_and_exclude_cancelled(db_session, test_user, test_quote, test_requisition):
    """Tile po_numbers = distinct line po_number values, cancelled lines excluded."""
    from app.services.buyplan_hub import deals_board

    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )
    l1 = _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)
    l2 = _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.VERIFIED)
    l3 = _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.PENDING_VERIFY)
    cancelled = _make_line(db_session, buy_plan_id=plan.id, status=BuyPlanLineStatus.CANCELLED)
    l1.po_number = "PO-100"
    l2.po_number = "PO-100"  # duplicate collapses
    l3.po_number = "PO-200"
    cancelled.po_number = "PO-DEAD"  # excluded (line cancelled)
    db_session.flush()

    board = deals_board(db_session, test_user, scope="all")
    deal = next(d for d in board["active"] if d["plan_id"] == plan.id)

    assert deal["po_numbers"] == ["PO-100", "PO-200"]


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


# ── 9. Completed archive: scope, contents, ordering ───────────────────


def _ts(days_ago: int) -> datetime:
    """A tz-aware UTC timestamp ``days_ago`` days in the past."""
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def test_completed_archive_only_completed_plans(db_session, test_user, test_quote, test_requisition):
    """Archive returns only COMPLETED plans — never active/cancelled ones."""
    from app.services.buyplan_hub import completed_archive

    completed = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=_ts(1),
    )
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.CANCELLED,
        submitted_by_id=test_user.id,
    )

    page = completed_archive(db_session, test_user, scope="all")

    assert page["total"] == 1
    assert [d["plan_id"] for d in page["deals"]] == [completed.id]
    # Card carries completed_at for the date-completed column.
    assert page["deals"][0]["completed_at"] is not None


def test_completed_archive_ordered_by_completed_at_desc(db_session, test_user, test_quote, test_requisition):
    """Most recently completed first; completed_at desc."""
    from app.services.buyplan_hub import completed_archive

    older = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=_ts(10),
    )
    newer = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=_ts(2),
    )

    page = completed_archive(db_session, test_user, scope="all")

    assert [d["plan_id"] for d in page["deals"]] == [newer.id, older.id]


def test_completed_archive_scope_mine_filters_owner(db_session, test_user, manager_user, test_quote, test_requisition):
    """Scope=mine returns only the caller's completed plans; scope=all returns both."""
    from app.services.buyplan_hub import completed_archive

    mine = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=_ts(1),
    )
    theirs = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=manager_user.id,
        completed_at=_ts(1),
    )

    mine_page = completed_archive(db_session, test_user, scope="mine")
    assert [d["plan_id"] for d in mine_page["deals"]] == [mine.id]
    assert mine_page["total"] == 1

    all_page = completed_archive(db_session, test_user, scope="all")
    assert {d["plan_id"] for d in all_page["deals"]} == {mine.id, theirs.id}
    assert all_page["total"] == 2


def test_completed_archive_pagination(db_session, test_user, test_quote, test_requisition):
    """Limit/offset page the archive; next_offset advances then exhausts."""
    from app.services.buyplan_hub import completed_archive

    # 5 completed plans, distinct completion dates so order is deterministic.
    plans = [
        _make_plan(
            db_session,
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=BuyPlanStatus.COMPLETED,
            submitted_by_id=test_user.id,
            completed_at=_ts(i + 1),
        )
        for i in range(5)
    ]
    newest_first = [p.id for p in plans]  # _ts(1) is newest → index 0 first

    page1 = completed_archive(db_session, test_user, scope="all", limit=2, offset=0)
    assert page1["total"] == 5
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert [d["plan_id"] for d in page1["deals"]] == newest_first[:2]
    assert page1["next_offset"] == 2

    page2 = completed_archive(db_session, test_user, scope="all", limit=2, offset=2)
    assert [d["plan_id"] for d in page2["deals"]] == newest_first[2:4]
    assert page2["next_offset"] == 4

    page3 = completed_archive(db_session, test_user, scope="all", limit=2, offset=4)
    assert [d["plan_id"] for d in page3["deals"]] == newest_first[4:]
    # Last page — no more to load.
    assert page3["next_offset"] is None


def test_completed_archive_empty(db_session, test_user, test_quote, test_requisition):
    """No completed plans → empty page, total 0, no next_offset."""
    from app.services.buyplan_hub import completed_archive

    _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        submitted_by_id=test_user.id,
    )

    page = completed_archive(db_session, test_user, scope="all")
    assert page["total"] == 0
    assert page["deals"] == []
    assert page["next_offset"] is None


def test_customer_name_falls_back_to_requisition_for_so_origin(db_session):
    """SO-origin plan (no quote) resolves customer from requisition.customer_name."""
    from app.models.sourcing import Requisition
    from app.services.buyplan_hub import _customer_name

    req = Requisition(name="SO-Test-Req", customer_name="Globex Corp")
    db_session.add(req)
    db_session.flush()
    plan = BuyPlan(quote_id=None, requisition_id=req.id)
    db_session.add(plan)
    db_session.flush()
    plan.requisition = req
    assert _customer_name(plan) == "Globex Corp"


# ── 11. statuses filter ───────────────────────────────────────────────


def test_deals_board_statuses_filter_active_only(db_session, test_user, test_quote, test_requisition):
    """Statuses=[ACTIVE] returns only ACTIVE plans; DRAFT plan is excluded."""
    from app.constants import BuyPlanStatus
    from app.services.buyplan_hub import deals_board

    draft_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
    )
    active_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )

    filtered = deals_board(db_session, test_user, scope="all", statuses=[BuyPlanStatus.ACTIVE.value])
    filtered_ids = {d["plan_id"] for col in filtered.values() for d in col}

    assert active_plan.id in filtered_ids
    assert draft_plan.id not in filtered_ids


def test_deals_board_statuses_none_is_unchanged(db_session, test_user, test_quote, test_requisition):
    """Statuses=None (default) keeps the original CANCELLED/COMPLETED-exclusion
    behaviour."""
    from app.constants import BuyPlanStatus
    from app.services.buyplan_hub import deals_board

    draft_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
    )
    active_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )

    # No statuses arg → default behaviour: both DRAFT and ACTIVE appear; CANCELLED/COMPLETED do not.
    full = deals_board(db_session, test_user, scope="all")
    full_ids = {d["plan_id"] for col in full.values() for d in col}

    assert draft_plan.id in full_ids
    assert active_plan.id in full_ids


def test_deals_board_statuses_filtered_subset_of_default(db_session, test_user, test_quote, test_requisition):
    """Filtered board is always a subset of the default (unfiltered) board."""
    from app.constants import BuyPlanStatus
    from app.services.buyplan_hub import deals_board

    for status in (BuyPlanStatus.DRAFT, BuyPlanStatus.PENDING, BuyPlanStatus.ACTIVE, BuyPlanStatus.HALTED):
        _make_plan(
            db_session,
            quote_id=test_quote.id,
            requisition_id=test_requisition.id,
            status=status,
        )

    active_only = deals_board(db_session, test_user, scope="all", statuses=[BuyPlanStatus.ACTIVE.value])
    active_ids = {d["plan_id"] for col in active_only.values() for d in col}

    full = deals_board(db_session, test_user, scope="all")
    full_ids = {d["plan_id"] for col in full.values() for d in col}

    assert active_ids.issubset(full_ids)


def test_buy_plans_tab_statuses_excludes_draft(db_session, test_user, test_quote, test_requisition):
    """Buy Plans tab filter (ACTIVE+HALTED) excludes DRAFT plans."""
    from app.constants import BuyPlanStatus
    from app.services.buyplan_hub import deals_board

    draft_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
    )
    active_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )
    halted_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.HALTED,
    )

    board = deals_board(
        db_session,
        test_user,
        scope="all",
        statuses=[BuyPlanStatus.ACTIVE.value, BuyPlanStatus.HALTED.value],
    )
    ids = {d["plan_id"] for col in board.values() for d in col}

    assert active_plan.id in ids
    assert halted_plan.id in ids
    assert draft_plan.id not in ids


def test_sales_orders_tab_statuses_includes_draft_and_pending(db_session, test_user, test_quote, test_requisition):
    """Sales Orders tab filter (DRAFT+PENDING) includes DRAFT/PENDING; excludes
    ACTIVE."""
    from app.constants import BuyPlanStatus
    from app.services.buyplan_hub import deals_board

    draft_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
    )
    pending_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
    )
    active_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
    )

    board = deals_board(
        db_session,
        test_user,
        scope="all",
        statuses=[BuyPlanStatus.DRAFT.value, BuyPlanStatus.PENDING.value],
    )
    ids = {d["plan_id"] for col in board.values() for d in col}

    assert draft_plan.id in ids
    assert pending_plan.id in ids
    assert active_plan.id not in ids
