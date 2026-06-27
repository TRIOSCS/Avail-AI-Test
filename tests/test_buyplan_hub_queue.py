"""Tests for buyplan_hub.buyer_line_queue — the buyer's per-line PO queue.

Covers: my AWAITING_PO line on ACTIVE plan appears; DRAFT/PENDING plan does not;
another buyer's line does not; kicked-back line sorts first; dict carries expected fields.

Depends on: app/services/buyplan_hub.buyer_line_queue,
            conftest fixtures (db_session, test_user, manager_user, test_quote, test_requisition).
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
) -> BuyPlan:
    """Create + flush a minimal BuyPlan header."""
    plan = BuyPlan(
        quote_id=quote_id,
        requisition_id=requisition_id,
        status=status,
        so_status=SOVerificationStatus.APPROVED,
    )
    db.add(plan)
    db.flush()
    return plan


def _make_line(
    db: Session,
    *,
    buy_plan_id: int,
    buyer_id: int | None,
    status: str = BuyPlanLineStatus.AWAITING_PO,
    po_rejection_note: str | None = None,
    quantity: int = 10,
) -> BuyPlanLine:
    """Create + flush a minimal BuyPlanLine."""
    line = BuyPlanLine(
        buy_plan_id=buy_plan_id,
        buyer_id=buyer_id,
        quantity=quantity,
        status=status,
        po_rejection_note=po_rejection_note,
    )
    db.add(line)
    db.flush()
    return line


def test_buyer_queue_only_my_active_awaiting_lines(db_session, test_user, manager_user, test_quote, test_requisition):
    """Only AWAITING_PO lines on ACTIVE plans assigned to me appear."""
    from app.services.buyplan_hub import buyer_line_queue

    # (a) my AWAITING_PO line on an ACTIVE plan — should appear
    active_plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    my_active_line = _make_line(db_session, buy_plan_id=active_plan.id, buyer_id=test_user.id)

    # (b) my line on a DRAFT plan — should NOT appear
    draft_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
    )
    _make_line(db_session, buy_plan_id=draft_plan.id, buyer_id=test_user.id)

    # (c) another buyer's line on an ACTIVE plan — should NOT appear
    other_plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    _make_line(db_session, buy_plan_id=other_plan.id, buyer_id=manager_user.id)

    rows = buyer_line_queue(db_session, test_user)
    assert [r["line_id"] for r in rows] == [my_active_line.id]


def test_buyer_queue_pending_plan_excluded(db_session, test_user, test_quote, test_requisition):
    """A PENDING plan's line is not actionable — only ACTIVE plans qualify."""
    from app.services.buyplan_hub import buyer_line_queue

    pending_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
    )
    _make_line(db_session, buy_plan_id=pending_plan.id, buyer_id=test_user.id)

    rows = buyer_line_queue(db_session, test_user)
    assert rows == []


def test_buyer_queue_kicked_back_first(db_session, test_user, test_quote, test_requisition):
    """A line with po_rejection_note sorts before a normal line and kicked_back=True."""
    from app.services.buyplan_hub import buyer_line_queue

    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    # Normal line created first (so it has a lower id / earlier flush order)
    normal = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id)
    # Kicked-back line created second
    kicked = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=test_user.id,
        po_rejection_note="Wrong lead time",
    )

    rows = buyer_line_queue(db_session, test_user)
    assert rows[0]["kicked_back"] is True
    assert rows[0]["line_id"] == kicked.id
    assert rows[1]["kicked_back"] is False
    assert rows[1]["line_id"] == normal.id


def test_buyer_queue_dict_fields(db_session, test_user, test_quote, test_requisition):
    """Dict carries mpn, vendor_name, quantity, unit_cost, customer_name, and
    plan_id."""
    from app.services.buyplan_hub import buyer_line_queue

    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id, quantity=25)
    # Patch unit_cost onto line
    line.unit_cost = "12.5000"
    db_session.flush()

    rows = buyer_line_queue(db_session, test_user)
    assert len(rows) == 1
    r = rows[0]
    assert r["line_id"] == line.id
    assert r["plan_id"] == plan.id
    assert r["quantity"] == 25
    assert r["status"] == BuyPlanLineStatus.AWAITING_PO
    assert r["kicked_back"] is False
    assert r["po_rejection_note"] is None
    assert r["plan_created_at"] == plan.created_at
    # customer_name comes from plan.quote → customer_site → company
    assert r["customer_name"] is not None
    # mpn/description may be None (no requirement row), but keys exist
    assert "mpn" in r
    assert "description" in r
    assert "vendor_name" in r
    assert "vendor_contact_email" in r


# ── _customer_name helper (shared derivation) ─────────────────────────────


def test_customer_name_returns_company_name(db_session, test_quote, test_requisition):
    """_customer_name derives the company name via quote → customer_site → company."""
    from app.services.buyplan_hub import _customer_name

    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    db_session.flush()
    # test_quote → test_customer_site → test_company("Acme Electronics")
    assert _customer_name(plan) == "Acme Electronics"


def test_customer_name_falls_back_to_requisition_when_quote_has_no_site(db_session, test_requisition, test_user):
    """A plan whose quote has a NULL customer_site (site deleted → SET NULL) falls back
    to the requisition's customer.

    SP-2 made the buy plan's customer label resilient for plans with no usable quote
    customer (originally for quote-less Sales Orders, but the same fallback applies
    whenever the quote yields no customer): ``_customer_name`` now returns the
    requisition's ``customer_name`` instead of ``None``.
    """
    from app.models.quotes import Quote
    from app.services.buyplan_hub import _customer_name

    siteless_quote = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=None,
        quote_number="Q-NO-SITE",
        status="sent",
        created_by_id=test_user.id,
    )
    db_session.add(siteless_quote)
    db_session.flush()
    plan = _make_plan(db_session, quote_id=siteless_quote.id, requisition_id=test_requisition.id)
    db_session.flush()
    # test_requisition fixture carries customer_name="Acme Electronics"
    assert _customer_name(plan) == "Acme Electronics"


# ── Team Orders (read-only awareness of OTHER buyers' open lines) ──────────


def test_team_queue_shows_other_buyers_not_mine(db_session, test_user, manager_user, test_quote, test_requisition):
    """team_line_queue returns OTHER buyers' open lines, never the caller's own."""
    from app.services.buyplan_hub import team_line_queue

    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    # My own AWAITING_PO line — must NOT appear (it's in buyer_line_queue).
    my_line = _make_line(db_session, buy_plan_id=plan.id, buyer_id=test_user.id)
    # Another buyer's PENDING_VERIFY line — must appear.
    other_line = _make_line(
        db_session,
        buy_plan_id=plan.id,
        buyer_id=manager_user.id,
        status=BuyPlanLineStatus.PENDING_VERIFY,
    )

    rows = team_line_queue(db_session, test_user)
    line_ids = [r["line_id"] for r in rows]
    assert other_line.id in line_ids
    assert my_line.id not in line_ids
    r = next(r for r in rows if r["line_id"] == other_line.id)
    assert r["buyer_name"] == manager_user.name
    assert r["status"] == BuyPlanLineStatus.PENDING_VERIFY


def test_team_queue_excludes_inactive_plans_and_null_buyer(
    db_session, test_user, manager_user, test_quote, test_requisition
):
    """Only AWAITING_PO/PENDING_VERIFY lines on ACTIVE plans with a buyer qualify."""
    from app.services.buyplan_hub import team_line_queue

    # COMPLETED plan with another buyer's line — excluded (not ACTIVE).
    done_plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
    )
    _make_line(db_session, buy_plan_id=done_plan.id, buyer_id=manager_user.id)

    # ACTIVE plan: a null-buyer line (excluded) + a VERIFIED line (excluded by status).
    active_plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    _make_line(db_session, buy_plan_id=active_plan.id, buyer_id=None)
    _make_line(
        db_session,
        buy_plan_id=active_plan.id,
        buyer_id=manager_user.id,
        status=BuyPlanLineStatus.VERIFIED,
    )

    assert team_line_queue(db_session, test_user) == []


def test_team_queue_ordered_by_buyer_name(
    db_session, test_user, manager_user, sales_user, test_quote, test_requisition
):
    """Rows are grouped/ordered by buyer_name ascending."""
    from app.services.buyplan_hub import team_line_queue

    plan = _make_plan(db_session, quote_id=test_quote.id, requisition_id=test_requisition.id)
    # manager_user.name="Test Manager", sales_user.name="Test Sales" → Manager sorts first.
    _make_line(db_session, buy_plan_id=plan.id, buyer_id=sales_user.id)
    _make_line(db_session, buy_plan_id=plan.id, buyer_id=manager_user.id)

    rows = team_line_queue(db_session, test_user)
    names = [r["buyer_name"] for r in rows]
    assert names == sorted(names)
    assert names[0] == manager_user.name
