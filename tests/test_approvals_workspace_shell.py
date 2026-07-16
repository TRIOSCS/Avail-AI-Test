"""test_approvals_workspace_shell.py — Phase 1 of the Approvals Workspace rebuild.

Covers the four-tab workspace shell at /v2/partials/approvals (specs/approvals-workspace.md):
  - the shell renders all four tab pills + the lazy #ws-body guard, for a manager AND a
    plain buyer;
  - legacy 3-tab ?tab= keys map to their workspace home (deep links keep working);
  - each workspace tab body renders (split view marker, search box, All/Mine toggle);
  - the legacy tab bodies still render at their old keys (the decide handlers'
    origin=approvals_hub branch depends on them until Phase 6 cutover);
  - "waiting on you" badge counts: an unlimited manager recipient matches the old hub's
    org-wide pending counts; a non-approver gets zero approval counts but their own
    worklist; decision rows sort oldest-first;
  - every SO#/PO# in a list renders as a copy chip.

Called by: pytest
Depends on: conftest (db_session, test_user, sales_user), app.routers.htmx.approvals_hub,
            app.services.approvals_workspace, app.models.*, app.constants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    UserRole,
)
from app.database import get_db
from app.dependencies import require_user
from app.models import Offer, Requirement, User
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.approvals.queue import pending_count_for_gate
from app.services.approvals_workspace import (
    WORKSPACE_TABS,
    plan_rows,
    resolve_workspace_tab,
    waiting_counts,
)

# ── Builders ─────────────────────────────────────────────────────────────


def _client_as(db: Session, user: User) -> TestClient:
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


def _teardown_overrides() -> None:
    from app.main import app

    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_user, None)


def _req_quote(db: Session, user: User) -> tuple[Requisition, Quote, Requirement]:
    req = Requisition(
        name=f"WS-{uuid.uuid4().hex[:6]}",
        customer_name="Foxconn CZ",
        created_by=user.id,
    )
    db.add(req)
    db.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="SN74LVC245")
    db.add(r)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        line_items=[],
        created_by_id=user.id,
    )
    db.add(quote)
    db.flush()
    return req, quote, r


def _plan(
    db: Session,
    user: User,
    *,
    status: str = BuyPlanStatus.PENDING.value,
    so_number: str | None = "8841",
    submitted_at: datetime | None = None,
) -> BuyPlan:
    req, quote, _r = _req_quote(db, user)
    plan = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=status,
        sales_order_number=so_number,
        submitted_by_id=user.id,
        submitted_at=submitted_at or datetime.now(UTC),
        total_cost=Decimal("1000.00"),
    )
    db.add(plan)
    db.flush()
    return plan


def _line(
    db: Session,
    plan: BuyPlan,
    *,
    status: str = BuyPlanLineStatus.PENDING_VERIFY.value,
    buyer: User | None = None,
    po_number: str | None = "20447",
) -> BuyPlanLine:
    req_row = db.query(Requirement).filter(Requirement.requisition_id == plan.requisition_id).first()
    offer = Offer(
        requirement_id=req_row.id if req_row else None,
        vendor_name="Win Source",
        vendor_name_normalized="win source",
        mpn="SN74LVC245",
        normalized_mpn="SN74LVC245",
        unit_price=1.15,
        status=OfferStatus.ACTIVE.value,
    )
    db.add(offer)
    db.flush()
    line = BuyPlanLine(
        buy_plan_id=plan.id,
        requirement_id=req_row.id if req_row else None,
        offer_id=offer.id,
        quantity=100,
        unit_cost=Decimal("1.15"),
        status=status,
        buyer_id=buyer.id if buyer else None,
        po_number=po_number,
        po_confirmed_at=datetime.now(UTC) if po_number else None,
    )
    db.add(line)
    db.flush()
    return line


def _open_request(db: Session, plan: BuyPlan, recipient: User, requested_by: User) -> ApprovalRequest:
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=plan.id,
        status=ApprovalRequestStatus.REQUESTED,
        amount=plan.total_cost,
        requested_by_id=requested_by.id,
        owner_id=requested_by.id,
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1)
    db.add(step)
    db.flush()
    db.add(
        ApprovalStepRecipient(
            step_id=step.id,
            user_id=recipient.id,
            status=ApprovalRecipientStatus.PENDING,
        )
    )
    db.flush()
    return ar


@pytest.fixture()
def manager(db_session: Session) -> User:
    u = User(
        email=f"mgr-{uuid.uuid4().hex[:6]}@trioscs.com",
        name="Aniket Manager",
        role=UserRole.MANAGER,
        is_active=True,
        can_approve_buy_plans=True,
        can_approve_purchase_orders=True,
        can_approve_prepayments=True,
    )
    db_session.add(u)
    db_session.commit()
    return u


# ── Shell ────────────────────────────────────────────────────────────────


def test_shell_renders_four_tabs_for_manager(db_session: Session, manager: User):
    client = _client_as(db_session, manager)
    try:
        resp = client.get("/v2/partials/approvals")
        assert resp.status_code == 200
        html = resp.text
        for label in ("Sales Orders", "Buy Plans", "Purchase Orders", "Prepayments"):
            assert label in html
        assert 'id="ws-body"' in html
        # The lazy body must carry the explicit hx-target guard.
        assert 'hx-target="#ws-body"' in html
    finally:
        _teardown_overrides()


def test_shell_renders_for_plain_buyer(db_session: Session, test_user: User):
    test_user.role = UserRole.BUYER
    db_session.commit()
    client = _client_as(db_session, test_user)
    try:
        resp = client.get("/v2/partials/approvals")
        assert resp.status_code == 200
        assert 'id="ws-body"' in resp.text
    finally:
        _teardown_overrides()


def test_legacy_tab_keys_map_to_workspace_home():
    assert resolve_workspace_tab("buy-plan") == "buy-plans"
    assert resolve_workspace_tab("po-approval") == "purchase-orders"
    assert resolve_workspace_tab("prepayment") == "prepayments"
    assert resolve_workspace_tab("") == "sales-orders"
    assert resolve_workspace_tab("nonsense") == "sales-orders"
    for tab in WORKSPACE_TABS:
        assert resolve_workspace_tab(tab) == tab


def test_shell_legacy_deep_link_lands_on_mapped_tab(db_session: Session, manager: User):
    client = _client_as(db_session, manager)
    try:
        resp = client.get("/v2/partials/approvals?tab=po-approval")
        assert resp.status_code == 200
        assert "/v2/partials/approvals/purchase-orders" in resp.text
    finally:
        _teardown_overrides()


# ── Tab bodies ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", list(WORKSPACE_TABS))
def test_workspace_tab_bodies_render(db_session: Session, manager: User, tab: str):
    plan = _plan(db_session, manager)
    _line(db_session, plan)
    _open_request(db_session, plan, manager, manager)
    db_session.commit()

    client = _client_as(db_session, manager)
    try:
        resp = client.get(f"/v2/partials/approvals/{tab}")
        assert resp.status_code == 200
        html = resp.text
        assert 'id="ws-pane"' in html  # split view present
        assert 'x-model="q"' in html  # search box present
        assert f"/v2/partials/approvals/{tab}?scope=mine" in html  # All/Mine toggle
    finally:
        _teardown_overrides()


@pytest.mark.parametrize("tab", ["buy-plan", "po-approval", "prepayment"])
def test_legacy_tab_bodies_still_render(db_session: Session, manager: User, tab: str):
    client = _client_as(db_session, manager)
    try:
        resp = client.get(f"/v2/partials/approvals/{tab}")
        assert resp.status_code == 200
        assert 'id="ws-pane"' not in resp.text  # old console body, not the workspace
    finally:
        _teardown_overrides()


def test_unknown_tab_404s(db_session: Session, manager: User):
    client = _client_as(db_session, manager)
    try:
        assert client.get("/v2/partials/approvals/not-a-tab").status_code == 404
    finally:
        _teardown_overrides()


def test_so_and_po_numbers_render_as_copy_chips(db_session: Session, manager: User):
    plan = _plan(db_session, manager, so_number="8841")
    _line(db_session, plan, po_number="20447")
    _open_request(db_session, plan, manager, manager)
    db_session.commit()

    client = _client_as(db_session, manager)
    try:
        so_html = client.get("/v2/partials/approvals/sales-orders").text
        assert 'data-value="8841"' in so_html
        po_html = client.get("/v2/partials/approvals/purchase-orders").text
        assert 'data-value="20447"' in po_html
    finally:
        _teardown_overrides()


# ── Badges + ordering ────────────────────────────────────────────────────


def test_manager_badges_match_org_wide_hub_counts(db_session: Session, manager: User, test_user: User):
    """An unlimited manager who is a recipient on every open request sees the same
    numbers the old hub's org-wide pills showed (the Phase 1 verify criterion)."""
    plan_a = _plan(db_session, test_user)
    plan_b = _plan(db_session, test_user)
    _open_request(db_session, plan_a, manager, test_user)
    _open_request(db_session, plan_b, manager, test_user)
    active = _plan(db_session, test_user, status=BuyPlanStatus.ACTIVE.value)
    _line(db_session, active, status=BuyPlanLineStatus.PENDING_VERIFY.value)
    db_session.commit()

    counts = waiting_counts(db_session, manager)
    assert counts["buy-plans"] == pending_count_for_gate(db_session, ApprovalGateType.BUY_PLAN) == 2
    assert counts["sales-orders"] == 2  # no drafts of their own
    assert counts["purchase-orders"] >= 1  # the pending_verify line is within their (unset) limit
    assert counts["prepayments"] == pending_count_for_gate(db_session, ApprovalGateType.PREPAYMENT)


def test_non_approver_gets_no_approval_badges(db_session: Session, test_user: User, manager: User):
    test_user.role = UserRole.SALES
    db_session.commit()
    plan = _plan(db_session, test_user)
    _open_request(db_session, plan, manager, test_user)
    db_session.commit()

    counts = waiting_counts(db_session, test_user)
    assert counts["buy-plans"] == 0
    assert counts["prepayments"] == 0
    assert counts["purchase-orders"] == 0  # sales is not a PO cutter


def test_sales_draft_counts_toward_sales_orders_badge(db_session: Session, test_user: User):
    _plan(db_session, test_user, status=BuyPlanStatus.DRAFT.value, so_number=None)
    db_session.commit()
    counts = waiting_counts(db_session, test_user)
    assert counts["sales-orders"] == 1
    assert counts["buy-plans"] == 0


def test_decision_rows_sort_oldest_first(db_session: Session, manager: User, test_user: User):
    old = _plan(db_session, test_user, submitted_at=datetime.now(UTC) - timedelta(days=5))
    new = _plan(db_session, test_user, submitted_at=datetime.now(UTC) - timedelta(hours=2))
    _open_request(db_session, old, manager, test_user)
    _open_request(db_session, new, manager, test_user)
    db_session.commit()

    rows = plan_rows(db_session, manager, scope="all")
    decidable = [r for r in rows if r.can_decide]
    assert [r.plan_id for r in decidable][:2] == [old.id, new.id]
    assert decidable[0].age_hours > decidable[1].age_hours
