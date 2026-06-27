"""Route tests for the Approvals module shell (SP-1).

Covers the renamed module URL `/v2/approvals`, the 302 back-compat redirect from the
legacy `/v2/buy-plans` path (preserving the query string), and that the buy-plan detail
URL still serves directly (push-urls unchanged in SP-1). The stage-tab restructure is
covered incrementally by test_buyplan_hub_routes.py as it lands.

Depends on: app.routers.htmx_views (v2_page + buy_plans_legacy_redirect), conftest
fixtures (nonadmin_client = signed-session buyer).
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_buy_plans_redirects_to_approvals(nonadmin_client: TestClient):
    """The legacy /v2/buy-plans path 302s to the renamed /v2/approvals module."""
    r = nonadmin_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals"


def test_buy_plans_redirect_preserves_query(nonadmin_client: TestClient):
    """A pushed lens URL survives the redirect (no lost deep-link)."""
    r = nonadmin_client.get("/v2/buy-plans?lens=deals", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/v2/approvals?lens=deals"


def test_approvals_page_lazy_loads_hub_partial(nonadmin_client: TestClient):
    """/v2/approvals serves the full-page shell that lazy-loads the hub partial (the
    shell embeds the partial URL; the body is fetched client-side by htmx)."""
    r = nonadmin_client.get("/v2/approvals")
    assert r.status_code == 200
    assert "/v2/partials/buy-plans" in r.text


def test_buy_plan_detail_url_not_redirected(nonadmin_client: TestClient):
    """Detail URLs stay /v2/buy-plans/{id} in SP-1 (deal-card push-urls unchanged) —
    they must render the shell, NOT 302 to the module list."""
    r = nonadmin_client.get("/v2/buy-plans/999999", follow_redirects=False)
    assert r.status_code == 200
