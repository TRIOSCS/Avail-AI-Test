"""Route tests for the Approvals module shell (SP-1).

Covers the renamed module URL `/v2/approvals`, the 302 back-compat redirect from the
legacy `/v2/buy-plans` path (preserving the query string), the stage-tab hub shell
(5 lifecycle tabs), the per-role default landing tab (`_default_lens`), each stage-tab
body's re-homed work surface, and the pinned per-gate "Pending approvals" section
(shown only to the matching approver).

Depends on: app.routers.htmx_views (v2_page, buy_plans_legacy_redirect, _default_lens,
buy_plans_list_partial, approvals_tab_partial), conftest fixtures (client = buyer via
require_user override; nonadmin_client = signed-session buyer; manager_user; sales_user).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ApprovalGateType,
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalSubjectType,
)
from app.models.approvals import ApprovalRequest, ApprovalStep, ApprovalStepRecipient
from app.models.auth import User


def _seed_pending_buy_plan_approval(db: Session, user: User) -> ApprovalRequest:
    """A REQUESTED buy_plan-gate ApprovalRequest routed to *user* (pending
    recipient)."""
    ar = ApprovalRequest(
        gate_type=ApprovalGateType.BUY_PLAN,
        status=ApprovalRequestStatus.REQUESTED,
        subject_type=ApprovalSubjectType.BUY_PLAN,
        subject_id=1,
        requested_by_id=user.id,
        owner_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(ar)
    db.flush()
    step = ApprovalStep(request_id=ar.id, seq=1, rule="any", status="pending")
    db.add(step)
    db.flush()
    db.add(ApprovalStepRecipient(step_id=step.id, user_id=user.id, status=ApprovalRecipientStatus.PENDING))
    db.flush()
    return ar


# ── Back-compat redirect + page threading ──────────────────────────────────


def test_buy_plans_redirects_to_approvals(nonadmin_client: TestClient):
    """The legacy /v2/buy-plans path 302s to the renamed /v2/approvals module."""
    r = nonadmin_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals"


def test_buy_plans_redirect_preserves_query(nonadmin_client: TestClient):
    """A pushed lens URL survives the redirect (no lost deep-link)."""
    r = nonadmin_client.get("/v2/buy-plans?lens=buy_plans", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals?lens=buy_plans"


def test_approvals_page_lazy_loads_hub_partial(nonadmin_client: TestClient):
    """/v2/approvals serves the full-page shell that lazy-loads the hub partial from its
    renamed /v2/partials/approvals URL (the body is fetched client-side by htmx)."""
    r = nonadmin_client.get("/v2/approvals")
    assert r.status_code == 200
    assert "/v2/partials/approvals" in r.text


def test_approvals_page_threads_lens(nonadmin_client: TestClient):
    """A pushed lens URL threads ?lens= into the lazy hub-partial URL on first load."""
    r = nonadmin_client.get("/v2/approvals?lens=purchase_orders")
    assert r.status_code == 200
    assert "/v2/partials/approvals?lens=purchase_orders" in r.text


def test_buy_plan_detail_url_not_redirected(nonadmin_client: TestClient):
    """Detail URLs stay /v2/buy-plans/{id} in SP-1 (deal-card push-urls unchanged) —
    they must render the shell, NOT 302 to the module list."""
    r = nonadmin_client.get("/v2/buy-plans/999999", follow_redirects=False)
    assert r.status_code == 200


# ── Stage-tab hub shell ─────────────────────────────────────────────────────


def test_shell_renders_stage_tabs(nonadmin_client: TestClient):
    """The hub shell renders the 4 always-visible stage-tab lens URLs + the lazy-body
    landmine guard.

    Supervise is gated (hidden for the buyer fixture).
    """
    r = nonadmin_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    for key in ("sales_orders", "buy_plans", "purchase_orders", "prepayments"):
        assert f"?lens={key}" in r.text
    assert 'hx-target="#bp-hub-body"' in r.text


# ── Per-role default landing tab (_default_lens) ────────────────────────────


def test_default_lens_buyer_is_my_queue(db_session: Session, test_user: User):
    """Buyers land on My Queue — their role-aware "what needs YOU now" surface (Phase
    B)."""
    from app.routers.htmx.buy_plans import _default_lens

    assert _default_lens(test_user, db_session) == "my_queue"


def test_default_lens_manager_is_pipeline(db_session: Session, manager_user: User):
    """Managers/ops land on Pipeline — the 4-stage deal board (Phase C)."""
    from app.routers.htmx.buy_plans import _default_lens

    assert _default_lens(manager_user, db_session) == "pipeline"


def test_default_lens_sales_is_my_queue(db_session: Session, sales_user: User):
    """Sales/trader land on My Queue too (every non-supervisor defaults there)."""
    from app.routers.htmx.buy_plans import _default_lens

    assert _default_lens(sales_user, db_session) == "my_queue"


# ── Stage-tab bodies (re-homed work surfaces) ───────────────────────────────


def test_buy_plans_tab_has_board(client: TestClient):
    """The Buy Plans tab body embeds the deal board (its metric strip)."""
    r = client.get("/v2/partials/approvals/buy-plans")
    assert r.status_code == 200
    assert "open deal" in r.text


def test_purchase_orders_tab_has_orders_and_resource(client: TestClient):
    """The Purchase Orders tab body embeds the buyer orders queue + re-sourcing pool."""
    r = client.get("/v2/partials/approvals/purchase-orders")
    assert r.status_code == 200
    assert "caught up" in r.text.lower()  # orders empty state
    assert "re-sourcing" in r.text  # resourcing pool metric strip


def test_sales_orders_tab_renders_board(client: TestClient):
    """The Sales Orders tab (SP-2) renders the DRAFT/PENDING deal board with column
    headers."""
    r = client.get("/v2/partials/approvals/sales-orders")
    assert r.status_code == 200
    # Board columns (Draft / Pending) must appear; the Active column is filtered out.
    assert "Draft" in r.text
    assert "Pending" in r.text


def test_prepayments_tab_empty_state(client: TestClient):
    """The Vendor Prepayments tab has no work surface in SP-1 → neutral empty state."""
    r = client.get("/v2/partials/approvals/prepayments")
    assert r.status_code == 200
    assert "No prepayment requests" in r.text


def test_supervise_tab_renders_for_manager(client: TestClient, manager_user: User):
    """The Supervise tab renders the unified action-queue body (calm header subline)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: manager_user
    try:
        r = client.get("/v2/partials/approvals/supervise")
    finally:
        app.dependency_overrides.pop(require_user, None)
    assert r.status_code == 200
    # Calm-header money subline always renders for a supervisor.
    assert "avg margin" in r.text


def test_unknown_tab_404s(client: TestClient):
    """An unknown stage tab is a 404 (not a silent fallback)."""
    r = client.get("/v2/partials/approvals/bogus")
    assert r.status_code == 404


# ── Pinned per-gate "Pending approvals" section ─────────────────────────────


def test_pending_section_absent_for_non_approver(client: TestClient, test_user: User, db_session: Session):
    """The default buyer lacks can_approve_buy_plans → no pinned section on Sales Orders
    (the BUY_PLAN gate tab after SP-2 repoint)."""
    _seed_pending_buy_plan_approval(db_session, test_user)
    db_session.commit()
    r = client.get("/v2/partials/approvals/sales-orders")
    assert r.status_code == 200
    assert "Pending approvals" not in r.text


def test_pending_section_present_for_approver(client: TestClient, test_user: User, db_session: Session):
    """Granting the gate's approve flag surfaces the pinned section with the pending row
    on the Sales Orders tab (BUY_PLAN gate after SP-2 repoint)."""
    test_user.can_approve_buy_plans = True
    _seed_pending_buy_plan_approval(db_session, test_user)
    db_session.commit()
    r = client.get("/v2/partials/approvals/sales-orders")
    assert r.status_code == 200
    assert "Pending approvals" in r.text
