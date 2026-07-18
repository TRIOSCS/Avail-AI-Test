"""Route tests for the Buy Plans hub + Approvals hub shells (Phase 3 3-tab split).

Phase 3 split the single "Approvals" surface into two non-overlapping homes:
  - the personal Buy Plans hub (My Queue + Pipeline) reclaimed /v2/buy-plans as its real,
    non-redirected home (the legacy /v2/buy-plans→/v2/approvals 302 is retired);
  - /v2/approvals is now the org-wide 3-tab decide console (Buy Plan / PO Approval /
    Prepayment) served by routers/htmx/approvals_hub.py.

Covers both full-page shells + their lazy partials, the per-role default landing lens
(_default_lens), and the unknown-lens/tab 404 guards.

Depends on: app.routers.htmx.buy_plans (_default_lens, buy_plans_list_partial,
buy_plans_tab_partial), app.routers.htmx.approvals_hub, conftest fixtures (client = buyer
via require_user override; nonadmin_client = signed-session buyer; manager_user; sales_user).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User

# ── Buy Plans hub reclaims /v2/buy-plans (no redirect) ──────────────────────


def test_buy_plans_no_longer_redirects(nonadmin_client: TestClient):
    """/v2/buy-plans is the hub's real home now — 200, NOT a 302 to /v2/approvals."""
    r = nonadmin_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 200
    assert "/v2/partials/buy-plans" in r.text


def test_buy_plans_page_threads_lens(nonadmin_client: TestClient):
    """A pushed lens URL threads ?lens= into the lazy hub-partial URL on first load."""
    r = nonadmin_client.get("/v2/buy-plans?lens=pipeline")
    assert r.status_code == 200
    assert "/v2/partials/buy-plans?lens=pipeline" in r.text


def test_buy_plan_detail_url_not_a_lens(nonadmin_client: TestClient):
    """Detail URLs stay /v2/buy-plans/{id} and render the shell (int convertor, not a
    lens)."""
    r = nonadmin_client.get("/v2/buy-plans/999999", follow_redirects=False)
    assert r.status_code == 200


# ── Approvals Workspace (4-tab split-view console) ──────────────────────────


def test_approvals_page_lazy_loads_hub_partial(nonadmin_client: TestClient):
    """/v2/approvals serves the full-page shell that lazy-loads the workspace
    partial."""
    r = nonadmin_client.get("/v2/approvals")
    assert r.status_code == 200
    assert "/v2/partials/approvals" in r.text


def test_approvals_page_threads_tab(nonadmin_client: TestClient):
    """A pushed ?tab= URL (incl.

    a LEGACY 3-tab key) threads into the lazy partial URL on first load; the shell
    aliases legacy keys onto the workspace tabs.
    """
    r = nonadmin_client.get("/v2/approvals?tab=po-approval")
    assert r.status_code == 200
    assert "/v2/partials/approvals?tab=po-approval" in r.text


def test_approvals_page_threads_workspace_tab(nonadmin_client: TestClient):
    """A pushed workspace tab URL reloads straight into that tab's split view."""
    r = nonadmin_client.get("/v2/approvals?tab=purchase-orders")
    assert r.status_code == 200
    assert "/v2/partials/approvals?tab=purchase-orders" in r.text


def test_approvals_shell_renders_four_tabs(nonadmin_client: TestClient):
    """The Approvals Workspace shell renders all four tab URLs + the lazy-body guard."""
    r = nonadmin_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    for key in ("sales-orders", "buy-plans", "purchase-orders", "prepayments"):
        assert f"?tab={key}" in r.text
    assert 'hx-target="#ap-hub-body"' in r.text


def test_approvals_unknown_tab_404s(client: TestClient):
    """An unknown Approvals hub tab is a 404 (not a silent fallback)."""
    assert client.get("/v2/partials/approvals/bogus").status_code == 404


# ── Buy Plans hub shell (My Queue + Pipeline) ───────────────────────────────


def test_buy_plans_shell_renders_lens_tabs(nonadmin_client: TestClient):
    """The Buy Plans hub shell renders the two lens-tab URLs + the lazy-body guard."""
    r = nonadmin_client.get("/v2/partials/buy-plans")
    assert r.status_code == 200
    for key in ("my_queue", "pipeline"):
        assert f"?lens={key}" in r.text
    assert 'hx-target="#bp-hub-body"' in r.text


def test_buy_plans_unknown_lens_404s(client: TestClient):
    """An unknown Buy Plans hub lens is a 404 (not a silent fallback)."""
    assert client.get("/v2/partials/buy-plans/bogus").status_code == 404


# ── Per-role default landing lens (_default_lens) ───────────────────────────


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
