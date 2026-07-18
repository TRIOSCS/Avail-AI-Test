"""test_approvals_hub_tabs.py — the Approvals Workspace 4-tab split-view console.

/v2/approvals is now one page, four tabs — Sales Orders · Buy Plans · Purchase Orders ·
Prepayments — served by routers/htmx/approvals_hub.py (specs/approvals-workspace.md).
Covers:
  - the 4-pill shell (per-viewer badges, lazy split-view body, legacy tab-key aliases);
  - each tab's split view (list URL + pane target + selection plumbing);
  - the left work lists: Needs-your-approval grouped first (oldest default-selected),
    search, Mine/All scope, the live/closed filter, order-type badges, copy chips;
  - the origin=approvals_hub decide re-render branches still return a workspace body;
  - the CSV export (legacy keys alias onto the new tabs).

Called by: pytest
Depends on: conftest (db_session, test_user), app.routers.htmx.approvals_hub,
            app.services.approvals, app.models.*, app.constants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    OfferStatus,
    SalesOrderType,
    SOVerificationStatus,
)
from app.database import get_db
from app.dependencies import (
    require_buyplan_approver,
    require_buyplan_po_approver,
    require_user,
)
from app.models import Offer, Requirement, User
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quality_plan import Prepayment
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard

# ── Fixtures / builders ──────────────────────────────────────────────────


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user, granted both decide rights, with the two plain-
    function approver gates overridden so the decide POSTs resolve to test_user."""
    from app.main import app

    test_user.can_approve_buy_plans = True
    test_user.can_approve_purchase_orders = True
    db_session.commit()

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


def _req_quote(db: Session, user: User) -> tuple[Requisition, Quote, Requirement]:
    req = Requisition(
        name=f"REQ-{uuid.uuid4().hex[:6]}",
        customer_name="AcmeCo",
        status="active",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn="LM317", created_at=datetime.now(UTC))
    db.add(rq)
    db.flush()
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(q)
    db.flush()
    return req, q, rq


def _plan(db: Session, req: Requisition, q: Quote, *, status: str, **overrides) -> BuyPlan:
    defaults = dict(
        requisition_id=req.id,
        quote_id=q.id,
        status=status,
        so_status=SOVerificationStatus.APPROVED.value,
        submitted_by_id=req.created_by,
        total_cost=1000.0,
        total_revenue=2000.0,
        total_margin_pct=50.0,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    bp = BuyPlan(**defaults)
    db.add(bp)
    db.flush()
    return bp


def _line(db: Session, bp: BuyPlan, rq: Requirement, user: User, *, status: str, **overrides) -> BuyPlanLine:
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="Acme Dist")
    db.add(vc)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name="Acme Dist",
        vendor_name_normalized="acme dist",
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=1.0,
        status=OfferStatus.ACTIVE.value,
    )
    db.add(off)
    db.flush()
    defaults = dict(
        buy_plan_id=bp.id,
        requirement_id=rq.id,
        offer_id=off.id,
        quantity=100,
        unit_cost=1.0,
        unit_sell=2.0,
        buyer_id=user.id,
        status=status,
    )
    defaults.update(overrides)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.flush()
    return line


def _pending_verify_line(db: Session, bp: BuyPlan, rq: Requirement, user: User) -> BuyPlanLine:
    return _line(
        db,
        bp,
        rq,
        user,
        status=BuyPlanLineStatus.PENDING_VERIFY.value,
        po_number="PO-9",
        po_confirmed_at=datetime.now(UTC),
    )


def _pending_buy_plan_request(db: Session, bp: BuyPlan, user: User) -> ApprovalRequest:
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=bp.id,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.flush()
    return ar


def _pending_prepay_request(db: Session, bp: BuyPlan, user: User) -> tuple[ApprovalRequest, Prepayment]:
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name="WireVendor")
    db.add(vc)
    db.flush()
    pp = Prepayment(
        buy_plan_id=bp.id, vendor_card_id=vc.id, total_incl_fees=2500, currency="USD", created_by_id=user.id
    )
    db.add(pp)
    db.flush()
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.PREPAYMENT,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.PREPAYMENT,
        subject_id=pp.id,
        requested_by_id=user.id,
        owner_id=user.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.commit()
    return ar, pp


def _other_user(db: Session) -> User:
    u = User(
        email=f"other-{uuid.uuid4().hex[:6]}@t.com",
        name="Other Owner",
        role="sales",
        azure_id=f"az-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


TABS = ("sales-orders", "buy-plans", "purchase-orders", "prepayments")


# ── Shell ────────────────────────────────────────────────────────────────


def test_shell_renders_four_tabs(hub_client: TestClient):
    r = hub_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    for key in TABS:
        assert f"?tab={key}" in r.text
    assert 'hx-target="#ap-hub-body"' in r.text


def test_shell_defaults_to_sales_orders(hub_client: TestClient):
    r = hub_client.get("/v2/partials/approvals")
    assert "/v2/partials/approvals/sales-orders" in r.text  # lazy body loads the default tab


@pytest.mark.parametrize(
    ("legacy", "mapped"),
    [("buy-plan", "buy-plans"), ("po-approval", "purchase-orders"), ("prepayment", "prepayments")],
)
def test_shell_legacy_tab_keys_alias(hub_client: TestClient, legacy: str, mapped: str):
    r = hub_client.get(f"/v2/partials/approvals?tab={legacy}")
    assert r.status_code == 200
    assert f"/v2/partials/approvals/{mapped}" in r.text  # lazy body loads the MAPPED tab


def test_shell_badges_show_waiting_on_viewer(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    # The decidable plan badges BOTH lenses (Sales Orders + Buy Plans pills).
    assert r.text.count("min-w-[18px]") >= 2


# ── Tab bodies (split view) ──────────────────────────────────────────────


@pytest.mark.parametrize("tab", TABS)
def test_tab_body_is_split_view(hub_client: TestClient, tab: str):
    r = hub_client.get(f"/v2/partials/approvals/{tab}")
    assert r.status_code == 200
    assert f"/v2/partials/approvals/{tab}/list" in r.text  # left list lazy URL
    assert 'id="aw-pane"' in r.text  # right pane target
    assert "aw-select" in r.text  # selection plumbing


@pytest.mark.parametrize(
    ("legacy", "mapped"),
    [("buy-plan", "buy-plans"), ("po-approval", "purchase-orders"), ("prepayment", "prepayments")],
)
def test_tab_body_legacy_keys_alias(hub_client: TestClient, legacy: str, mapped: str):
    r = hub_client.get(f"/v2/partials/approvals/{legacy}")
    assert r.status_code == 200
    assert f"/v2/partials/approvals/{mapped}/list" in r.text


def test_unknown_tab_404s(hub_client: TestClient):
    assert hub_client.get("/v2/partials/approvals/bogus").status_code == 404
    assert hub_client.get("/v2/partials/approvals/bogus/list").status_code == 404


# ── Sales Orders / Buy Plans lists ───────────────────────────────────────


def test_so_list_groups_needs_approval_first_and_default_selects(
    hub_client: TestClient, db_session: Session, test_user: User
):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/sales-orders/list")
    assert r.status_code == 200
    assert "Needs your approval" in r.text
    assert f"Plan #{bp.id}" in r.text
    # The oldest decidable row is the default selection, targeting the SO-lens pane.
    assert "aw-default" in r.text
    assert f"/v2/partials/approvals/plan/{bp.id}/pane?lens=sales-orders" in r.text


def test_buy_plans_list_is_a_lens_on_the_same_rows(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/buy-plans/list")
    assert r.status_code == 200
    assert f"Plan #{bp.id}" in r.text
    assert f"/v2/partials/approvals/plan/{bp.id}/pane?lens=buy-plans" in r.text


def test_so_list_oldest_decidable_first(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    older = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value, quote_id=None)
    req2, q2, _ = _req_quote(db_session, test_user)
    newer = _plan(db_session, req2, q2, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, older, test_user)
    _pending_buy_plan_request(db_session, newer, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/sales-orders/list")
    body = r.text
    # Decision queue is oldest-first: the older plan renders before the newer one AND
    # is the default selection.
    assert body.index(f"plan-{older.id}") < body.index(f"plan-{newer.id}")
    assert f"aw-default', {{key: 'plan-{older.id}'" in body or f"plan-{older.id}" in body.split("aw-default")[1]


def test_so_list_closed_filter(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    live = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    req2, q2, _ = _req_quote(db_session, test_user)
    done = _plan(db_session, req2, q2, status=BuyPlanStatus.COMPLETED.value)
    db_session.commit()

    live_txt = hub_client.get("/v2/partials/approvals/sales-orders/list").text
    assert f"Plan #{live.id}" in live_txt
    assert f"Plan #{done.id}" not in live_txt

    closed_txt = hub_client.get("/v2/partials/approvals/sales-orders/list?show_closed=true").text
    assert f"Plan #{done.id}" in closed_txt
    assert f"Plan #{live.id}" not in closed_txt


def test_so_list_search_filters(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    bp.sales_order_number = "SO-777"
    req2, q2, _ = _req_quote(db_session, test_user)
    other = _plan(db_session, req2, q2, status=BuyPlanStatus.ACTIVE.value)
    other.sales_order_number = "SO-888"
    db_session.commit()

    txt = hub_client.get("/v2/partials/approvals/sales-orders/list?q=SO-777").text
    assert f"plan-{bp.id}" in txt
    assert f"plan-{other.id}" not in txt


def test_so_list_scope_mine(hub_client: TestClient, db_session: Session, test_user: User):
    my_req, my_q, _ = _req_quote(db_session, test_user)
    mine = _plan(db_session, my_req, my_q, status=BuyPlanStatus.ACTIVE.value)
    other = _other_user(db_session)
    o_req, o_q, _ = _req_quote(db_session, other)
    theirs = _plan(db_session, o_req, o_q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()

    all_txt = hub_client.get("/v2/partials/approvals/sales-orders/list?scope=all").text
    assert f"plan-{mine.id}" in all_txt and f"plan-{theirs.id}" in all_txt

    mine_txt = hub_client.get("/v2/partials/approvals/sales-orders/list?scope=mine").text
    assert f"plan-{mine.id}" in mine_txt
    assert f"plan-{theirs.id}" not in mine_txt


def test_so_list_shows_order_type_badge_and_so_copy_chip(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value, order_type=SalesOrderType.STOCK_SALE.value)
    bp.sales_order_number = "SO-1234"
    db_session.commit()

    txt = hub_client.get("/v2/partials/approvals/sales-orders/list").text
    assert "Stock Sale" in txt  # order-type badge
    assert 'data-copy-value="SO-1234"' in txt  # one-tap Acctivate copy chip (spec §5)


def test_so_list_age_on_every_row(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    db_session.commit()
    txt = hub_client.get("/v2/partials/approvals/sales-orders/list").text
    assert "just now" in txt or "m ago" in txt or "h ago" in txt or "now" in txt.lower()


# ── Purchase Orders list ─────────────────────────────────────────────────


def test_po_list_pending_verify_needs_approval(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    bp.sales_order_number = "SO-4455"
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    r = hub_client.get("/v2/partials/approvals/purchase-orders/list")
    assert r.status_code == 200
    body = r.text
    assert "Needs your approval" in body
    assert f"/v2/partials/approvals/po/{line.id}/pane" in body
    assert "Pending approval" in body  # display vocabulary — never "pending_verify"
    assert "pending_verify" not in body
    assert 'data-copy-value="PO-9"' in body  # PO# copy chip
    assert "SO SO-4455" in body or "SO-4455" in body


def test_po_list_buyer_awaiting_po_lines_listed(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    body = hub_client.get("/v2/partials/approvals/purchase-orders/list").text
    assert f"line-{line.id}" in body
    assert "Awaiting PO" in body


def test_po_list_closed_shows_verified(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(
        db_session,
        bp,
        rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-DONE",
        po_confirmed_at=datetime.now(UTC),
    )
    db_session.commit()

    live_txt = hub_client.get("/v2/partials/approvals/purchase-orders/list").text
    assert f"line-{line.id}" not in live_txt

    closed_txt = hub_client.get("/v2/partials/approvals/purchase-orders/list?show_closed=true").text
    assert f"line-{line.id}" in closed_txt
    assert "Approved" in closed_txt  # display vocabulary for verified


def test_po_list_scope_mine(hub_client: TestClient, db_session: Session, test_user: User):
    my_req, my_q, my_rq = _req_quote(db_session, test_user)
    my_bp = _plan(db_session, my_req, my_q, status=BuyPlanStatus.ACTIVE.value)
    my_line = _pending_verify_line(db_session, my_bp, my_rq, test_user)
    other = _other_user(db_session)
    o_req, o_q, o_rq = _req_quote(db_session, other)
    o_bp = _plan(db_session, o_req, o_q, status=BuyPlanStatus.ACTIVE.value)
    o_line = _pending_verify_line(db_session, o_bp, o_rq, other)
    db_session.commit()

    all_txt = hub_client.get("/v2/partials/approvals/purchase-orders/list?scope=all").text
    assert f"line-{my_line.id}" in all_txt and f"line-{o_line.id}" in all_txt

    mine_txt = hub_client.get("/v2/partials/approvals/purchase-orders/list?scope=mine").text
    assert f"line-{my_line.id}" in mine_txt
    assert f"line-{o_line.id}" not in mine_txt


def test_po_list_closed_mine_filters_before_limit(hub_client: TestClient, db_session: Session, test_user: User):
    """Closed+Mine must filter in SQL BEFORE the 50-row limit: with 50 newer closed
    lines belonging to someone else, the viewer's older closed line must still show (a
    post-limit Python filter would drop it entirely)."""
    my_req, my_q, my_rq = _req_quote(db_session, test_user)
    my_bp = _plan(db_session, my_req, my_q, status=BuyPlanStatus.ACTIVE.value)
    my_line = _line(
        db_session,
        my_bp,
        my_rq,
        test_user,
        status=BuyPlanLineStatus.VERIFIED.value,
        po_number="PO-MINE",
        po_confirmed_at=datetime.now(UTC),
    )
    other = _other_user(db_session)
    o_req, o_q, o_rq = _req_quote(db_session, other)
    o_bp = _plan(db_session, o_req, o_q, status=BuyPlanStatus.ACTIVE.value)
    for i in range(50):  # 50 NEWER closed lines that are not the viewer's
        _line(
            db_session,
            o_bp,
            o_rq,
            other,
            status=BuyPlanLineStatus.VERIFIED.value,
            po_number=f"PO-O-{i}",
            po_confirmed_at=datetime.now(UTC),
        )
    db_session.commit()

    mine_txt = hub_client.get("/v2/partials/approvals/purchase-orders/list?show_closed=true&scope=mine").text
    assert f"line-{my_line.id}" in mine_txt  # survives despite 50 newer non-mine rows
    assert "PO-O-0" not in mine_txt


def test_list_filters_round_trip_show_closed(hub_client: TestClient, db_session: Session, test_user: User):
    """The #aw-filters form carries a hidden show_closed input, so a search (hx-
    include="#aw-filters") — and the split shell's awListRefresh refetch — stays inside
    the Closed view instead of snapping back to Live."""
    body = hub_client.get("/v2/partials/approvals/sales-orders/list?show_closed=true&q=acme").text
    assert 'name="show_closed" value="true"' in body
    assert 'name="scope" value="all"' in body

    live_body = hub_client.get("/v2/partials/approvals/sales-orders/list").text
    assert 'name="show_closed" value="false"' in live_body


def test_split_refetch_includes_filter_form(hub_client: TestClient, db_session: Session, test_user: User):
    """The split shell's #aw-list refetch must hx-include the rendered list's own filter
    form so awListRefresh preserves q/scope/show_closed."""
    body = hub_client.get("/v2/partials/approvals/purchase-orders").text
    assert 'hx-include="#aw-filters"' in body


# ── Prepayments list ─────────────────────────────────────────────────────


def test_prepayments_list_pending(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _ar, pp = _pending_prepay_request(db_session, bp, test_user)

    r = hub_client.get("/v2/partials/approvals/prepayments/list")
    assert r.status_code == 200
    assert "WireVendor" in r.text
    assert "Needs your approval" in r.text
    assert f"/v2/partials/approvals/prepayments/{pp.id}/pane" in r.text


def test_prepayments_list_closed_shows_resolved(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, pp = _pending_prepay_request(db_session, bp, test_user)
    ar.status = ApprovalRequestStatus.APPROVED.value
    ar.resolved_at = datetime.now(UTC)
    pp.status = "approved"
    db_session.commit()

    live_txt = hub_client.get("/v2/partials/approvals/prepayments/list").text
    assert f"prepay-{pp.id}" not in live_txt

    closed_txt = hub_client.get("/v2/partials/approvals/prepayments/list?show_closed=true").text
    assert f"prepay-{pp.id}" in closed_txt


def test_prepayments_list_amount_visible(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    _pending_prepay_request(db_session, bp, test_user)

    txt = hub_client.get("/v2/partials/approvals/prepayments/list").text
    assert "$2,500" in txt  # amount + payee always visible (spec §8)


# ── origin=approvals_hub decide re-renders (legacy branch → workspace body) ──


def test_verify_po_origin_approvals_hub_rerenders_workspace(
    hub_client: TestClient, db_session: Session, test_user: User
):
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _pending_verify_line(db_session, bp, rq, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/verify-po",
            data={"action": "approve", "origin": "approvals_hub"},
        )
    assert r.status_code == 200
    assert "/v2/partials/approvals/purchase-orders/list" in r.text  # the workspace PO tab body
    db_session.expire(line)
    assert line.status == BuyPlanLineStatus.VERIFIED.value


def test_approve_origin_approvals_hub_rerenders_workspace(hub_client: TestClient, db_session: Session, test_user: User):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
    _pending_buy_plan_request(db_session, bp, test_user)
    db_session.commit()

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/approve",
            data={"action": "approve", "origin": "approvals_hub"},
        )
    assert r.status_code == 200
    assert "/v2/partials/approvals/buy-plans/list" in r.text  # the workspace BP tab body
    db_session.expire(bp)
    assert bp.status == BuyPlanStatus.ACTIVE.value


def test_prepay_decide_origin_approvals_hub_rerenders_workspace(
    hub_client: TestClient, db_session: Session, test_user: User
):
    req, q, _ = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    ar, _pp = _pending_prepay_request(db_session, bp, test_user)

    with patch("app.services.buyplan_notifications.run_notify_bg", new_callable=AsyncMock):
        r = hub_client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve", "origin": "approvals_hub"},
        )
    assert r.status_code == 200
    assert "/v2/partials/approvals/prepayments/list" in r.text  # the workspace prepay tab body
    db_session.expire(ar)
    assert ar.status == ApprovalRequestStatus.APPROVED.value


# ── CSV export (legacy keys alias) ───────────────────────────────────────


@pytest.mark.parametrize("tab", ["sales-orders", "buy-plan", "purchase-orders", "prepayments"])
def test_export_streams_csv(hub_client: TestClient, tab: str):
    r = hub_client.get(f"/v2/partials/approvals/{tab}/export")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]


# ── Origination + hub home (unchanged homes) ─────────────────────────────


def test_sales_order_new_stays_off_approvals_prefix(hub_client: TestClient):
    assert hub_client.get("/v2/partials/buy-plans/sales-orders/new").status_code == 200
    assert hub_client.get("/v2/partials/approvals/sales-orders/new").status_code == 404


def test_buy_plans_hub_still_serves(hub_client: TestClient):
    # The old hub keeps its own home until post-parity retirement: the full page
    # must not have become a redirect into the workspace...
    r = hub_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 200
    # ...and the hub partial itself must still render the two-lens hub shell.
    partial = hub_client.get("/v2/partials/buy-plans")
    assert partial.status_code == 200
    assert "My Queue" in partial.text
    assert "Pipeline" in partial.text
