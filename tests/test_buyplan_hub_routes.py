"""Route tests for the Approvals hub two-lens shell (My Queue + Pipeline).

Covers:
- /v2/partials/buy-plans renders the two-lens switcher (My Queue + Pipeline) + lazy body
  with the explicit hx-target="#bp-hub-body" (guards the cards-vanish landmine) and the
  role-default load.
- /v2/partials/buy-plans?lens=pipeline lazy-loads the Pipeline tab body.
- confirm-po / approve with the default origin return the detail partial.
- the role-scope predicate + resolver contract (_can_see_all_deals / _resolve_deal_scope).
- the persistent New Buy Plan origination button, the My Queue prepay-decide inline action,
  and the Pipeline's lazy Done (completed) archive paging.

Depends on: app/routers/htmx/buy_plans (hub routes), app/services/buyplan_hub,
            conftest fixtures (client, db_session, test_user, sales_user, manager_user,
            test_quote, test_requisition).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote
from app.routers.htmx.buy_plans import _can_see_all_deals, _resolve_deal_scope


@contextmanager
def _acting_as(user):
    """Override require_user for the duration of the block (drives role-scoped
    routes)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_user, None)


def _make_quote(db: Session, req_id: int) -> Quote:
    q = Quote(
        requisition_id=req_id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status="draft",
    )
    db.add(q)
    db.flush()
    return q


def _make_plan(db: Session, *, quote_id: int, req_id: int, **kw) -> BuyPlan:
    defaults = dict(
        quote_id=quote_id,
        requisition_id=req_id,
        status=BuyPlanStatus.ACTIVE,
        so_status=SOVerificationStatus.APPROVED,
    )
    defaults.update(kw)
    plan = BuyPlan(**defaults)
    db.add(plan)
    db.flush()
    return plan


def _make_line(db: Session, *, plan_id: int, **kw) -> BuyPlanLine:
    defaults = dict(
        buy_plan_id=plan_id,
        quantity=10,
        status=BuyPlanLineStatus.AWAITING_PO,
    )
    defaults.update(kw)
    line = BuyPlanLine(**defaults)
    db.add(line)
    db.flush()
    return line


def _add_ops(db: Session, user) -> None:
    """Register ``user`` as an active ops verification-group member."""
    from app.models.buy_plan import VerificationGroupMember

    db.add(VerificationGroupMember(user_id=user.id, is_active=True))
    db.flush()


# ── Shell + lens routing ──────────────────────────────────────────────────


def test_hub_shell_buyer_defaults_to_my_queue(client: TestClient):
    """Buyer hub: the two lens tabs (My Queue + Pipeline) render, the lazy body carries its
    explicit target, and the buyer's default landing is the My Queue body (Phase B)."""
    resp = client.get("/v2/partials/buy-plans")
    assert resp.status_code == 200
    body = resp.text
    # Two-lens switcher: My Queue + Pipeline. The retired stage lenses are gone.
    assert "My Queue" in body
    assert "Pipeline" in body
    assert "?lens=supervise" not in body
    # Lazy body + the landmine guard: explicit hx-target on the load container
    assert 'id="bp-hub-body"' in body
    assert 'hx-target="#bp-hub-body"' in body
    # Buyer default loads the My Queue tab body
    assert "/v2/partials/buy-plans/my-queue" in body


def test_hub_lens_highlight_is_alpine_reactive(client: TestClient):
    """The active-tab pill highlight is Alpine-reactive (:class on lens), so a tab click
    updates the indicator instantly instead of waiting for the server swap.

    The shell must carry the lens state in x-data and bind the active pill class to it,
    not bake the highlight into static Jinja (which goes stale on the @click).
    """
    resp = client.get("/v2/partials/buy-plans?lens=pipeline")
    assert resp.status_code == 200
    body = resp.text
    # Alpine holds the lens state, seeded from the server-resolved lens.
    assert "x-data=\"{ lens: 'pipeline' }\"" in body
    # Active-pill highlight is bound reactively to that lens var.
    assert ':class="lens ===' in body
    assert "bg-accent-600 text-white shadow-sm" in body


def test_hub_shell_lens_pipeline_loads_tab_body(client: TestClient):
    """Lens=pipeline lazy-loads the Pipeline tab body (the 4-stage deal board)."""
    resp = client.get("/v2/partials/buy-plans?lens=pipeline")
    assert resp.status_code == 200
    assert "/v2/partials/buy-plans/pipeline" in resp.text
    assert 'hx-target="#bp-hub-body"' in resp.text


def test_hub_shell_sales_defaults_to_my_queue(client: TestClient, sales_user):
    """A sales user with no lens lands on the My Queue surface (every non-supervisor
    does)."""
    with _acting_as(sales_user):
        resp = client.get("/v2/partials/buy-plans")
    assert resp.status_code == 200
    assert "/v2/partials/buy-plans/my-queue" in resp.text


def test_hub_shell_manager_defaults_to_pipeline(client: TestClient, manager_user):
    """A manager with no lens lands on the Pipeline tab body (Phase C default)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.get("/v2/partials/buy-plans")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "/v2/partials/buy-plans/pipeline" in resp.text


# ── confirm-po + approve origin behavior ───────────────────────────────────


def test_confirm_po_default_origin_returns_detail(client: TestClient, db_session: Session, test_user, test_quote):
    """Default origin (no value) preserves today's behavior: returns the detail
    partial."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
    line = _make_line(db_session, plan_id=plan.id, buyer_id=test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
        data={"po_number": "PO-67890"},
    )
    assert resp.status_code == 200
    # Detail partial has the "Line Items" section header.
    assert "Line Items" in resp.text


def test_approve_default_origin_returns_detail(
    client: TestClient, db_session: Session, manager_user, sales_user, test_requisition
):
    """Default origin (no value) preserves today's behavior: returns the detail
    partial."""
    from app.dependencies import require_user
    from app.main import app

    q = _make_quote(db_session, test_requisition.id)
    plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=sales_user.id,
    )
    manager_user.can_approve_buy_plans = True  # require_buyplan_approver gates the POST
    db_session.commit()

    from app.dependencies import require_buyplan_approver

    app.dependency_overrides[require_user] = lambda: manager_user
    app.dependency_overrides[require_buyplan_approver] = lambda: manager_user
    try:
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/approve",
            data={"action": "approve"},
        )
    finally:
        app.dependency_overrides.pop(require_user, None)
        app.dependency_overrides.pop(require_buyplan_approver, None)
    assert resp.status_code == 200
    assert "Line Items" in resp.text


# ── Role-scope predicate + resolver (lock the deal-scope contract directly) ──
#
# The Pipeline board route is exercised elsewhere. These unit-level tests pin the two
# helpers it leans on — `_can_see_all_deals` (who may see every owner's deals) and
# `_resolve_deal_scope` (how a requested scope is normalized) — so the role contract is
# locked independently of any template/route plumbing.


@pytest.mark.parametrize("fixture_name", ["test_user", "manager_user", "admin_user"])
def test_can_see_all_deals_true_for_po_cutters(request, db_session: Session, fixture_name):
    """PO-cutters (buyer/manager/admin) may view every owner's deals."""
    user = request.getfixturevalue(fixture_name)
    assert _can_see_all_deals(user, db_session) is True


@pytest.mark.parametrize("fixture_name", ["sales_user", "trader_user"])
def test_can_see_all_deals_false_for_restricted_roles(request, db_session: Session, fixture_name):
    """Sales/traders are scoped to their own deals — no cross-owner visibility."""
    user = request.getfixturevalue(fixture_name)
    assert _can_see_all_deals(user, db_session) is False


def test_can_see_all_deals_ops_membership_elevates_restricted_role(db_session: Session, sales_user):
    """The ops arm: a sales user gains all-deals visibility once in the ops group."""
    assert _can_see_all_deals(sales_user, db_session) is False
    _add_ops(db_session, sales_user)
    db_session.flush()
    assert _can_see_all_deals(sales_user, db_session) is True


@pytest.mark.parametrize(
    "scope,can_see_all,expected",
    [
        # can_see_all=True (PO-cutter / ops): default → all, explicit honored both ways.
        ("", True, "all"),  # role default
        ("garbage", True, "all"),  # unknown → role default
        ("all", True, "all"),
        ("mine", True, "mine"),  # narrow-to-mine via the toggle
        # can_see_all=False (sales/trader): default → mine, all coerced to mine (no leak).
        ("", False, "mine"),  # role default
        ("garbage", False, "mine"),  # unknown → role default
        ("all", False, "mine"),  # forced to mine — no cross-owner leak
        ("mine", False, "mine"),
    ],
)
def test_resolve_deal_scope_contract(scope, can_see_all, expected):
    """Normalize a requested scope against the viewer's visibility, per role."""
    assert _resolve_deal_scope(scope, can_see_all) == expected


# ── New Buy Plan origination button (hub shell, Phase F-1) ──────────────────


def test_hub_shell_has_new_buy_plan_button(client: TestClient):
    """The hub shell carries a persistent New Buy Plan origination button targeting the
    sales-order-new picker into the hub body (so it survives lens switches)."""
    resp = client.get("/v2/partials/buy-plans")
    assert resp.status_code == 200
    body = resp.text
    assert "New Buy Plan" in body
    assert 'hx-get="/v2/partials/buy-plans/sales-orders/new"' in body
    assert 'hx-target="#bp-hub-body"' in body


# ── Prepay decide (My Queue inline action, Phase F-1) ───────────────────────


def _prepay_setup(db_session, recipient, test_requisition, *, amount="1000.00"):
    """A committed ACTIVE plan + a REQUESTED prepayment routed to ``recipient``."""
    from tests.test_my_queue import _grant, _make_prepay_request

    q = _make_quote(db_session, test_requisition.id)
    plan = _make_plan(db_session, quote_id=q.id, req_id=test_requisition.id)
    _grant(db_session, recipient, can_approve_prepayments=True)
    ar, _pp = _make_prepay_request(db_session, recipient=recipient, buy_plan_id=plan.id, amount=amount)
    db_session.commit()
    return ar


def test_prepay_decide_approve_returns_my_queue_html(
    client: TestClient, db_session: Session, manager_user, test_requisition
):
    """Approve via the inline decide route → 200 + the re-rendered My Queue body (HTML,
    not JSON)."""
    ar = _prepay_setup(db_session, manager_user, test_requisition)
    with _acting_as(manager_user):
        resp = client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "approve"},
        )
    assert resp.status_code == 200
    body = resp.text
    # My Queue surface re-rendered (calm-header copy), never the JSON decision payload.
    assert "in play" in body or "caught up" in body.lower()
    assert "Line Items" not in body
    assert '"status"' not in body  # not the JSON decision response


def test_prepay_decide_reject_with_comment_ok(client: TestClient, db_session: Session, manager_user, test_requisition):
    """Reject with a comment → 200 + My Queue body."""
    ar = _prepay_setup(db_session, manager_user, test_requisition)
    with _acting_as(manager_user):
        resp = client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "reject", "comment": "Too expensive — renegotiate terms."},
        )
    assert resp.status_code == 200
    assert "Line Items" not in resp.text


def test_prepay_decide_reject_without_comment_400(
    client: TestClient, db_session: Session, manager_user, test_requisition
):
    """Reject without a comment is refused (400) — a prepayment reject needs a
    reason."""
    ar = _prepay_setup(db_session, manager_user, test_requisition)
    with _acting_as(manager_user):
        resp = client.post(
            f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
            data={"action": "reject"},
        )
    assert resp.status_code == 400


def test_prepay_decide_non_recipient_403(
    client: TestClient, db_session: Session, manager_user, test_user, test_requisition
):
    """A user with no PENDING recipient slot on the request gets 403 (engine
    PermissionError)."""
    ar = _prepay_setup(db_session, manager_user, test_requisition)
    # Default client acts as test_user (a buyer), who is NOT a recipient of the request.
    resp = client.post(
        f"/v2/partials/approvals/prepay-requests/{ar.id}/decide",
        data={"action": "approve"},
    )
    assert resp.status_code == 403


# ── Pipeline archive (lazy Done paging, Phase F-1) ──────────────────────────


def test_pipeline_archive_returns_rows(client: TestClient, db_session: Session, test_user, test_requisition):
    """The Pipeline archive route returns completed deal cards for the requested
    page."""
    from datetime import datetime, timezone

    q = _make_quote(db_session, test_requisition.id)
    plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/pipeline-archive?scope=mine&offset=0")
    assert resp.status_code == 200
    assert f"/v2/partials/buy-plans/{plan.id}" in resp.text


def test_pipeline_archive_next_page_button(client: TestClient, db_session: Session, test_user, test_requisition):
    """More than one page of completed deals → a Load older button pointing at the next
    offset on the pipeline-archive route (not the legacy board archive)."""
    from datetime import datetime, timedelta, timezone

    from app.services.buyplan_hub import ARCHIVE_PAGE_SIZE

    q = _make_quote(db_session, test_requisition.id)
    now = datetime.now(timezone.utc)
    for i in range(ARCHIVE_PAGE_SIZE + 1):
        _make_plan(
            db_session,
            quote_id=q.id,
            req_id=test_requisition.id,
            status=BuyPlanStatus.COMPLETED,
            submitted_by_id=test_user.id,
            completed_at=now - timedelta(hours=i),
        )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/pipeline-archive?scope=mine&offset=0")
    assert resp.status_code == 200
    body = resp.text
    assert "Load older" in body
    assert f"/v2/partials/buy-plans/pipeline-archive?scope=mine&offset={ARCHIVE_PAGE_SIZE}" in body


def test_my_queue_renders_no_approver_row(client: TestClient, db_session, test_user, test_quote, test_requisition):
    """A stuck PENDING plan (no approver configured) renders as a No-approver row for
    its owner — verifies the new kind's display maps resolve (no Jinja error)."""
    _make_plan(
        db_session,
        quote_id=test_quote.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
    )
    resp = client.get("/v2/partials/buy-plans/my-queue")
    assert resp.status_code == 200
    assert "No approver" in resp.text


def test_detail_renders_no_approver_banner(client: TestClient, db_session, test_user, test_quote, test_requisition):
    """The plan detail surfaces the no-approver banner when the plan is stalled without
    an approver — the owner otherwise had no signal."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.PENDING,
        submitted_by_id=test_user.id,
    )
    resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
    assert resp.status_code == 200
    assert "no buy-plan approver" in resp.text


def test_resolve_issue_route_supervisor_ok(
    client: TestClient, db_session: Session, manager_user, test_quote, test_requisition
):
    """A supervisor POSTs resolve-issue → 200 and the line returns to awaiting_po."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_requisition.id, status=BuyPlanStatus.ACTIVE)
    line = _make_line(db_session, plan_id=plan.id, status=BuyPlanLineStatus.ISSUE, issue_type="price_changed")
    with _acting_as(manager_user):
        resp = client.post(f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/resolve-issue")
    assert resp.status_code == 200
    db_session.refresh(line)
    assert line.status == BuyPlanLineStatus.AWAITING_PO.value


def test_resolve_issue_route_non_supervisor_403(
    client: TestClient, db_session: Session, test_user, test_quote, test_requisition
):
    """A plain buyer (the client default) can't resolve a flagged issue → 403; the line
    is left untouched."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_requisition.id, status=BuyPlanStatus.ACTIVE)
    line = _make_line(db_session, plan_id=plan.id, status=BuyPlanLineStatus.ISSUE, issue_type="sold_out")
    resp = client.post(f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/resolve-issue")
    assert resp.status_code == 403
    db_session.refresh(line)
    assert line.status == BuyPlanLineStatus.ISSUE.value


def test_detail_shows_so_signed_chip(
    client: TestClient, db_session: Session, manager_user, test_quote, test_requisition
):
    """An approved (active) plan surfaces a 'SO signed · <approver>' chip so the Phase-D
    fold (one approval also signs the sales order) is no longer invisible."""
    plan = _make_plan(
        db_session,
        quote_id=test_quote.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        so_status=SOVerificationStatus.APPROVED,
        approved_by_id=manager_user.id,
    )
    resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
    assert resp.status_code == 200
    assert "SO signed" in resp.text


def test_approval_banner_notes_so_fold(
    client: TestClient, db_session: Session, manager_user, test_quote, test_requisition
):
    """The approval banner tells the approver that approving also signs off the sales
    order."""
    manager_user.can_approve_buy_plans = True
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_requisition.id, status=BuyPlanStatus.PENDING)
    db_session.commit()
    with _acting_as(manager_user):
        resp = client.get(f"/v2/partials/buy-plans/{plan.id}")
    assert resp.status_code == 200
    assert "signs off the sales order" in resp.text
