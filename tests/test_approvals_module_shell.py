"""Route tests for the Approvals module shell.

Covers the renamed module URL `/v2/approvals`, the 302 back-compat redirect from the
legacy `/v2/buy-plans` path (preserving the query string), the two-lens hub shell
(My Queue + Pipeline), the per-role default landing lens (`_default_lens`), and the
unknown-lens 404 guard.

Depends on: app.routers.htmx.buy_plans (buy_plans_legacy_redirect, _default_lens,
buy_plans_list_partial, approvals_tab_partial), conftest fixtures (client = buyer via
require_user override; nonadmin_client = signed-session buyer; manager_user; sales_user).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User

# ── Back-compat redirect + page threading ──────────────────────────────────


def test_buy_plans_redirects_to_approvals(nonadmin_client: TestClient):
    """The legacy /v2/buy-plans path 302s to the renamed /v2/approvals module."""
    r = nonadmin_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals"


def test_buy_plans_redirect_preserves_query(nonadmin_client: TestClient):
    """A pushed lens URL survives the redirect (no lost deep-link)."""
    r = nonadmin_client.get("/v2/buy-plans?lens=pipeline", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals?lens=pipeline"


def test_approvals_page_lazy_loads_hub_partial(nonadmin_client: TestClient):
    """/v2/approvals serves the full-page shell that lazy-loads the hub partial from its
    renamed /v2/partials/approvals URL (the body is fetched client-side by htmx)."""
    r = nonadmin_client.get("/v2/approvals")
    assert r.status_code == 200
    assert "/v2/partials/approvals" in r.text


def test_approvals_page_threads_lens(nonadmin_client: TestClient):
    """A pushed lens URL threads ?lens= into the lazy hub-partial URL on first load."""
    r = nonadmin_client.get("/v2/approvals?lens=pipeline")
    assert r.status_code == 200
    assert "/v2/partials/approvals?lens=pipeline" in r.text


def test_buy_plan_detail_url_not_redirected(nonadmin_client: TestClient):
    """Detail URLs stay /v2/buy-plans/{id} (deal-card push-urls unchanged) — they must
    render the shell, NOT 302 to the module list."""
    r = nonadmin_client.get("/v2/buy-plans/999999", follow_redirects=False)
    assert r.status_code == 200


# ── Two-lens hub shell ──────────────────────────────────────────────────────


def test_shell_renders_stage_tabs(nonadmin_client: TestClient):
    """The hub shell renders the two lens-tab URLs (My Queue + Pipeline) + the lazy-body
    landmine guard."""
    r = nonadmin_client.get("/v2/partials/approvals")
    assert r.status_code == 200
    for key in ("my_queue", "pipeline"):
        assert f"?lens={key}" in r.text
    assert 'hx-target="#bp-hub-body"' in r.text


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


# ── Unknown-lens guard ──────────────────────────────────────────────────────


def test_unknown_tab_404s(client: TestClient):
    """An unknown lens tab is a 404 (not a silent fallback)."""
    r = client.get("/v2/partials/approvals/bogus")
    assert r.status_code == 404
