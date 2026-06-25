"""Route tests for the Buy Plan Deal Hub shell + buyer Orders + sales Deals lenses.

Covers:
- /v2/partials/buy-plans renders the lens switcher + lazy body with the explicit
  hx-target="#bp-hub-body" (guards the cards-vanish landmine) and the orders default load.
- /v2/partials/buy-plans?lens=deals loads the board(scope=mine) by default.
- /v2/partials/buy-plans/orders shows a buyer's AWAITING_PO line, an origin=queue confirm
  form, and the rejection note on kicked-back rows.
- /v2/partials/buy-plans/board?scope=mine renders 4 columns and rings needs_my_action cards.
- /v2/partials/buy-plans/board?scope=all by a non-manager is forced to mine (no leak).
- confirm-po with origin=queue returns the queue partial; default origin returns detail.

Depends on: app/routers/htmx_views (hub routes), app/services/buyplan_hub,
            conftest fixtures (client, db_session, test_user, sales_user, manager_user,
            test_quote, test_requisition).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quotes import Quote


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


# ── Shell + lens routing ──────────────────────────────────────────────────


def test_hub_shell_buyer_defaults_to_orders(client: TestClient):
    """Buyer hub: lens switcher present, lazy body carries explicit target + orders URL."""
    resp = client.get("/v2/partials/buy-plans")
    assert resp.status_code == 200
    body = resp.text
    # Lens switcher — deals/orders for everyone; Supervise is gated (hidden for buyer)
    assert "My Deals" in body
    assert "My Orders" in body
    assert "?lens=supervise" not in body
    # Lazy body + the landmine guard: explicit hx-target on the load container
    assert 'id="bp-hub-body"' in body
    assert 'hx-target="#bp-hub-body"' in body
    # Buyer default loads the orders queue
    assert "/v2/partials/buy-plans/orders" in body


def test_hub_lens_highlight_is_alpine_reactive(client: TestClient):
    """The active-lens pill highlight is Alpine-reactive (:class on lens), so a lens
    click updates the indicator instantly instead of waiting for the server swap.

    The shell must carry the lens state in x-data and bind the active pill class to it,
    not bake the highlight into static Jinja (which goes stale on the @click).
    """
    resp = client.get("/v2/partials/buy-plans?lens=orders")
    assert resp.status_code == 200
    body = resp.text
    # Alpine holds the lens state, seeded from the server-resolved lens.
    assert "x-data=\"{ lens: 'orders' }\"" in body
    # Active-pill highlight is bound reactively to that lens var.
    assert ':class="lens ===' in body
    assert "bg-accent-600 text-white shadow-sm" in body


def test_hub_shell_lens_deals_loads_board_mine(client: TestClient):
    """Lens=deals loads the board scoped to mine by default."""
    resp = client.get("/v2/partials/buy-plans?lens=deals")
    assert resp.status_code == 200
    assert "/v2/partials/buy-plans/board?scope=mine" in resp.text
    assert 'hx-target="#bp-hub-body"' in resp.text


def test_hub_shell_sales_defaults_to_deals(client: TestClient, sales_user):
    """A sales user with no lens lands on the deals board (scope=mine)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        resp = client.get("/v2/partials/buy-plans")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "/v2/partials/buy-plans/board?scope=mine" in resp.text


def test_hub_shell_manager_defaults_to_supervise(client: TestClient, manager_user):
    """A manager with no lens lands on the supervise lens body."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.get("/v2/partials/buy-plans")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "/v2/partials/buy-plans/supervise" in resp.text


def test_hub_supervise_button_hidden_for_sales(client: TestClient, sales_user):
    """The Supervise switcher button is hidden for a non-supervisor (sales)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        resp = client.get("/v2/partials/buy-plans")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "?lens=supervise" not in resp.text


def test_hub_supervise_button_shown_for_manager(client: TestClient, manager_user):
    """The Supervise switcher button is present for a manager."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.get("/v2/partials/buy-plans")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "?lens=supervise" in resp.text


# ── Orders queue (buyer) ───────────────────────────────────────────────────


def test_orders_queue_shows_my_awaiting_line(client: TestClient, db_session: Session, test_user, test_quote):
    """A buyer's AWAITING_PO line on an ACTIVE approved plan appears with an
    origin=queue form."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
    line = _make_line(db_session, plan_id=plan.id, buyer_id=test_user.id)
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/orders")
    assert resp.status_code == 200
    body = resp.text
    assert f'id="bp-line-{line.id}"' in body
    # Confirm form posts to the existing confirm-po route with origin=queue hidden field
    assert f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po" in body
    assert 'name="origin"' in body
    assert 'value="queue"' in body


def test_orders_queue_kicked_back_shows_note(client: TestClient, db_session: Session, test_user, test_quote):
    """A kicked-back line surfaces its po_rejection_note prominently."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
    _make_line(
        db_session,
        plan_id=plan.id,
        buyer_id=test_user.id,
        po_rejection_note="Wrong vendor — re-cut to Arrow",
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/orders")
    assert resp.status_code == 200
    assert "Wrong vendor — re-cut to Arrow" in resp.text


def test_orders_queue_empty_state(client: TestClient):
    """No actionable lines → friendly empty state."""
    resp = client.get("/v2/partials/buy-plans/orders")
    assert resp.status_code == 200
    assert "all caught up" in resp.text.lower()


def test_orders_queue_team_section_read_only(
    client: TestClient, db_session: Session, test_user, manager_user, test_quote
):
    """The Team Orders section shows another buyer's open line + name, read-only.

    The team row must carry NO action form (no confirm-po / issue endpoint), while the
    caller's own actionable row keeps its confirm form.
    """
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
    # My own actionable line (keeps its form).
    my_line = _make_line(db_session, plan_id=plan.id, buyer_id=test_user.id)
    # Another buyer's open line — surfaces in Team Orders, read-only.
    team_line = _make_line(
        db_session,
        plan_id=plan.id,
        buyer_id=manager_user.id,
        status=BuyPlanLineStatus.PENDING_VERIFY,
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/orders")
    assert resp.status_code == 200
    body = resp.text
    assert "Team Orders" in body
    assert manager_user.name in body
    # My own row still has its confirm-po form.
    assert f"/v2/partials/buy-plans/{plan.id}/lines/{my_line.id}/confirm-po" in body
    # The team line has NO action form (no confirm-po / issue endpoint for it).
    assert f"/lines/{team_line.id}/confirm-po" not in body
    assert f"/lines/{team_line.id}/issue" not in body


def test_orders_queue_no_team_section_when_alone(client: TestClient, db_session: Session, test_user, test_quote):
    """With no other-buyer open lines, the Team Orders section is omitted."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
    _make_line(db_session, plan_id=plan.id, buyer_id=test_user.id)
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/orders")
    assert resp.status_code == 200
    assert "Team Orders" not in resp.text


# ── Deals board (sales / manager) ──────────────────────────────────────────


def test_board_mine_rings_needs_my_action(client: TestClient, db_session: Session, test_user, test_requisition):
    """A DRAFT plan owned by me shows the needs_my_action ring class in its column."""
    q = _make_quote(db_session, test_requisition.id)
    _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        so_status=SOVerificationStatus.PENDING,
        submitted_by_id=test_user.id,
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/board?scope=mine")
    assert resp.status_code == 200
    body = resp.text
    # 3 active columns — "Done" is gone; completed work lives in the archive.
    for col in ("Draft", "Pending", "Active"):
        assert col in body
    assert ">Done<" not in body
    # Completed archive section is present below the board.
    assert "Completed" in body
    # needs_my_action ring
    assert "ring-2 ring-amber-400" in body


def test_board_scope_all_forced_to_mine_for_non_manager(
    client: TestClient, db_session: Session, test_user, manager_user, test_requisition
):
    """Scope=all requested by a plain buyer must NOT leak another user's plans."""
    q = _make_quote(db_session, test_requisition.id)
    # A plan owned by someone else — must be hidden when scope is forced to mine.
    other_plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=manager_user.id,
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/board?scope=all")
    assert resp.status_code == 200
    # The other user's plan id must not appear as an openable card
    assert f"/v2/partials/buy-plans/{other_plan.id}" not in resp.text


def test_board_scope_all_allowed_for_manager(
    client: TestClient, db_session: Session, manager_user, sales_user, test_requisition
):
    """A manager CAN see all plans with scope=all."""
    from app.dependencies import require_user
    from app.main import app

    q = _make_quote(db_session, test_requisition.id)
    sales_plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=sales_user.id,
    )
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.get("/v2/partials/buy-plans/board?scope=all")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert f"/v2/partials/buy-plans/{sales_plan.id}" in resp.text


# ── Completed archive ──────────────────────────────────────────────────────


def test_board_archive_shows_completed_count(client: TestClient, db_session: Session, test_user, test_requisition):
    """The board's archive section shows the completed count and a completed card."""
    from datetime import datetime, timezone

    q = _make_quote(db_session, test_requisition.id)
    done_plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/board?scope=mine")
    assert resp.status_code == 200
    body = resp.text
    # Count badge "(1)" rendered with the accent figure class.
    assert "Completed" in body
    assert "(1)" in body
    # Completed plan is an openable archive card, NOT in an active column.
    assert f"/v2/partials/buy-plans/{done_plan.id}" in body


def test_archive_partial_returns_rows(client: TestClient, db_session: Session, test_user, test_requisition):
    """The lazy archive route returns completed rows for the requested page."""
    from datetime import datetime, timedelta, timezone

    q = _make_quote(db_session, test_requisition.id)
    now = datetime.now(timezone.utc)
    plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=test_user.id,
        completed_at=now - timedelta(days=3),
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/archive?scope=mine&offset=0")
    assert resp.status_code == 200
    assert f"/v2/partials/buy-plans/{plan.id}" in resp.text


def test_archive_scope_all_forced_to_mine_for_non_manager(
    client: TestClient, db_session: Session, test_user, manager_user, test_requisition
):
    """Scope=all on the archive route must not leak another user's completed plans."""
    from datetime import datetime, timezone

    q = _make_quote(db_session, test_requisition.id)
    other = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.COMPLETED,
        submitted_by_id=manager_user.id,
        completed_at=datetime.now(timezone.utc),
    )
    db_session.commit()

    resp = client.get("/v2/partials/buy-plans/archive?scope=all")
    assert resp.status_code == 200
    assert f"/v2/partials/buy-plans/{other.id}" not in resp.text


# ── confirm-po origin behavior ─────────────────────────────────────────────


def test_confirm_po_origin_queue_returns_queue(client: TestClient, db_session: Session, test_user, test_quote):
    """Origin=queue → re-rendered orders queue (not the full detail)."""
    plan = _make_plan(db_session, quote_id=test_quote.id, req_id=test_quote.requisition_id)
    line = _make_line(db_session, plan_id=plan.id, buyer_id=test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/buy-plans/{plan.id}/lines/{line.id}/confirm-po",
        data={"po_number": "PO-12345", "origin": "queue"},
    )
    assert resp.status_code == 200
    body = resp.text
    # The queue partial carries the orders-specific empty/heading markers, not the detail title.
    assert "PO(s) to cut" in body or "all caught up" in body.lower()
    assert "Line Items" not in body  # detail.html section header must be absent


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


# ── Supervise lens ─────────────────────────────────────────────────────────


def _add_ops(db: Session, user) -> None:
    """Register ``user`` as an active ops verification-group member."""
    from app.models.buy_plan import VerificationGroupMember

    db.add(VerificationGroupMember(user_id=user.id, is_active=True))
    db.flush()


def test_supervise_manager_shows_strip_and_approvals(
    client: TestClient, db_session: Session, manager_user, sales_user, test_requisition
):
    """As a manager: strip + Approvals section with an Approve form posting origin=supervise."""
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
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.get("/v2/partials/buy-plans/supervise")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    body = resp.text
    assert "open value" in body  # metric strip
    assert "Approvals waiting" in body
    assert f"/v2/partials/buy-plans/{plan.id}/approve" in body
    assert 'name="origin"' in body
    assert 'value="supervise"' in body
    # The embedded board carries an explicit hx-target (no cards-vanish landmine)
    assert 'hx-target="#main-content"' in body


def test_supervise_ops_shows_verify_sections(
    client: TestClient, db_session: Session, test_user, manager_user, test_requisition
):
    """As an ops member: SO-verify and PO-verify sections render with their forms."""
    from app.dependencies import require_user
    from app.main import app

    _add_ops(db_session, manager_user)
    q = _make_quote(db_session, test_requisition.id)
    so_plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.ACTIVE,
        so_status=SOVerificationStatus.PENDING,
    )
    pv_plan = _make_plan(db_session, quote_id=q.id, req_id=test_requisition.id, status=BuyPlanStatus.ACTIVE)
    pv_line = _make_line(db_session, plan_id=pv_plan.id, buyer_id=test_user.id, status=BuyPlanLineStatus.PENDING_VERIFY)
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.get("/v2/partials/buy-plans/supervise")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    body = resp.text
    assert "Needs SO verification" in body
    assert f"/v2/partials/buy-plans/{so_plan.id}/verify-so" in body
    assert "POs awaiting verification" in body
    assert f"/v2/partials/buy-plans/{pv_plan.id}/lines/{pv_line.id}/verify-po" in body


def test_supervise_non_supervisor_no_leak(
    client: TestClient, db_session: Session, test_user, manager_user, test_requisition
):
    """A plain buyer hitting /supervise gets the mine-scope board, NOT other users'
    plans."""
    q = _make_quote(db_session, test_requisition.id)
    other_plan = _make_plan(
        db_session,
        quote_id=q.id,
        req_id=test_requisition.id,
        status=BuyPlanStatus.DRAFT,
        submitted_by_id=manager_user.id,
    )
    db_session.commit()

    # Default client user is a plain buyer (not ops, not manager).
    resp = client.get("/v2/partials/buy-plans/supervise")
    assert resp.status_code == 200
    # No triage panel and no leak of another user's plan.
    assert "Approvals waiting" not in resp.text
    assert f"/v2/partials/buy-plans/{other_plan.id}" not in resp.text


def test_approve_origin_supervise_returns_supervise_body(
    client: TestClient, db_session: Session, manager_user, sales_user, test_requisition
):
    """Approve with origin=supervise returns the supervise body (not the full
    detail)."""
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
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/approve",
            data={"action": "approve", "origin": "supervise"},
        )
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    body = resp.text
    # Supervise body carries the metric strip; the detail "Line Items" header is absent.
    assert "open value" in body
    assert "Line Items" not in body


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
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        resp = client.post(
            f"/v2/partials/buy-plans/{plan.id}/approve",
            data={"action": "approve"},
        )
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    assert "Line Items" in resp.text
