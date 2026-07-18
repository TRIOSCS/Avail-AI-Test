"""Route tests for the Approvals Workspace shell + the retired Buy Plans hub URLs.

The Approvals Workspace (Sales Orders / Buy Plans / Purchase Orders / Prepayments) is
the ONE approvals surface at /v2/approvals (routers/htmx/approvals_hub.py). The old
personal Buy Plans hub retired post-parity (spec §11.1;
docs/APPROVALS_PARITY_CHECKLIST.md): /v2/buy-plans, its detail deep links, and its
partial URLs all 308 onto the workspace.

Covers the workspace full-page shell + lazy partial, the tab threading (incl. legacy
3-tab keys), the retired-hub 308 contract, and the unknown-lens/tab 404 guards.

Depends on: app.routers.htmx.buy_plans (retired-hub redirects),
app.routers.htmx.approvals_hub, app.routers.htmx_views (full-page 308s), conftest
fixtures (client = buyer via require_user override; nonadmin_client = signed-session
buyer; manager_user; sales_user).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# ── Retired Buy Plans hub URLs 308 onto the workspace ───────────────────────


def test_buy_plans_full_page_308s_to_workspace(nonadmin_client: TestClient):
    """/v2/buy-plans retired — a 308 onto the workspace Buy Plans tab (spec §11.1)."""
    r = nonadmin_client.get("/v2/buy-plans", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/v2/approvals?tab=buy-plans"


def test_buy_plans_lens_url_308s_to_workspace(nonadmin_client: TestClient):
    """An old pushed ?lens= URL 308s too (the lens key has no workspace equivalent)."""
    r = nonadmin_client.get("/v2/buy-plans?lens=pipeline", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/v2/approvals?tab=buy-plans"


def test_buy_plan_detail_url_308s_to_workspace_tab(nonadmin_client: TestClient):
    """Detail deep links land on the workspace's Buy Plans tab CARRYING the plan id —
    ?select= drives the list's preselection (docs/APPROVALS_PARITY_CHECKLIST.md)."""
    r = nonadmin_client.get("/v2/buy-plans/999999", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/v2/approvals?tab=buy-plans&select=999999"


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


def test_approvals_page_threads_select(nonadmin_client: TestClient):
    """The redirected /v2/buy-plans/{id} URL (?tab=&select=) threads select into the
    partial URL on first full-page load; a non-numeric select is dropped (the shell
    route takes a typed int)."""
    r = nonadmin_client.get("/v2/approvals?tab=buy-plans&select=42")
    assert r.status_code == 200
    # Jinja autoescapes the & inside hx-get="{{ partial_url }}".
    assert "/v2/partials/approvals?tab=buy-plans&amp;select=42" in r.text
    bad = nonadmin_client.get("/v2/approvals?tab=buy-plans&select=abc")
    assert bad.status_code == 200
    assert 'hx-get="/v2/partials/approvals?tab=buy-plans"' in bad.text
    assert "select=abc" not in bad.text


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


# ── Retired hub lens partials 308 onto the workspace tab body ───────────────


def test_buy_plans_lens_partials_308_to_workspace_tab_body(nonadmin_client: TestClient):
    """Both retired lens bodies 308 onto the workspace Buy Plans tab body; scope threads
    through to seed the list's Mine/All toggle."""
    for lens in ("my-queue", "pipeline"):
        r = nonadmin_client.get(f"/v2/partials/buy-plans/{lens}", follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/v2/partials/approvals/buy-plans"
    scoped = nonadmin_client.get("/v2/partials/buy-plans/pipeline?scope=mine", follow_redirects=False)
    assert scoped.status_code == 308
    assert scoped.headers["location"] == "/v2/partials/approvals/buy-plans?scope=mine"


def test_buy_plans_pipeline_archive_308s_to_closed_list(nonadmin_client: TestClient):
    """The retired Done-archive pager 308s onto the workspace BP Closed list."""
    r = nonadmin_client.get("/v2/partials/buy-plans/pipeline-archive", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/v2/partials/approvals/buy-plans/list?show_closed=true"


def test_buy_plans_unknown_lens_404s(client: TestClient):
    """An unknown Buy Plans hub lens is a 404 (not a silent redirect)."""
    assert client.get("/v2/partials/buy-plans/bogus", follow_redirects=False).status_code == 404
