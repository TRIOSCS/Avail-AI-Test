"""tests/test_nav_wayfinding.py — navigation & wayfinding mechanisms (nav audit
2026-08-20).

Covers the infrastructure that ends "I kept ending up on different pages with no way
back":
  1. ShellNegotiationMiddleware + /v2/shell — a browser TOP-LEVEL navigation
     (Sec-Fetch-Dest: document) to any /v2/partials/* URL is rewritten onto the shell
     route, which serves the full chrome lazy-loading that partial; htmx requests,
     tests, downloads, and logged-out users all get the ordinary behavior.
  2. Server-owned canonical history — the requisitions/prospecting list partials stamp
     HX-Replace-Url with the CANONICAL page URL (+query); replace, never push, so the
     address bar self-heals with zero history spam; templates no longer push partial
     URLs.
  3. Workspace state in the URL — string ?select= (plan-N / line-N / bare digits)
     resolves on all four tabs; the retired-detail redirect answers htmx callers with
     HX-Redirect (no doubled chrome).
  4. v2_page threads the full query so filtered canonical URLs reload filtered.

Shell tests use nonadmin_client (real signed session cookie) because the middleware
rewrite lands on v2_shell, whose get_user() reads the genuine session — dependency-
override-only clients carry no session and would render the login page.

Called by: pytest
Depends on: app.main (middleware), routers htmx_views / requisitions / prospecting /
    approvals_hub, conftest fixtures, tests.test_approvals_hub_tabs seed helpers (plain functions).
"""

import os

os.environ["TESTING"] = "1"

from fastapi.testclient import TestClient

from app.constants import BuyPlanLineStatus, BuyPlanStatus
from app.models import User
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote


def _grant_decide_rights(db, user: User) -> None:
    """Give test_user both decide toggles so workspace rows scope like a manager's.

    The section-3 tests only GET (require_user via the plain `client` fixture); the
    approver-gate dependency overrides that test_approvals_hub_tabs.hub_client adds
    matter only for decide POSTs, which this file never sends.
    """
    user.can_approve_buy_plans = True
    user.can_approve_purchase_orders = True
    db.commit()


_BROWSER_NAV = {"Sec-Fetch-Dest": "document"}
_HX = {"HX-Request": "true"}


# ── 1. Shell negotiation ──────────────────────────────────────────────────


def test_browser_navigation_to_partial_gets_full_shell(nonadmin_client: TestClient):
    r = nonadmin_client.get("/v2/partials/requisitions", headers=_BROWSER_NAV)
    assert r.status_code == 200
    assert "<html" in r.text  # the full app shell…
    assert 'id="main-content"' in r.text
    assert "/v2/partials/requisitions" in r.text  # …lazy-loading the SAME partial


def test_browser_navigation_preserves_query(nonadmin_client: TestClient):
    r = nonadmin_client.get("/v2/partials/requisitions?q=widget&status=open", headers=_BROWSER_NAV)
    assert "q=widget" in r.text and "status=open" in r.text


def test_htmx_request_still_gets_the_fragment(client: TestClient):
    r = client.get("/v2/partials/requisitions", headers=_HX)
    assert r.status_code == 200
    assert "<html" not in r.text  # fragment, not a shell


def test_plain_test_request_unaffected(client: TestClient):
    # No Sec-Fetch-Dest and no HX-Request (the whole existing test suite's shape).
    r = client.get("/v2/partials/requisitions")
    assert r.status_code == 200
    assert "<html" not in r.text


def test_exports_never_shell_wrapped(nonadmin_client: TestClient):
    # Browsers fetch downloads as documents too — the exclusion list keeps them raw.
    # The route itself answers (here: the buyer's 403 from the export access gate, as
    # JSON) — the point is the middleware never swallowed it into an HTML shell.
    r = nonadmin_client.get("/v2/partials/requisitions/export", headers=_BROWSER_NAV)
    assert "<html" not in r.text
    assert "text/html" not in r.headers.get("content-type", "")


def test_workspace_partial_survives_reload(nonadmin_client: TestClient):
    # The exact defect that started this: a naked 4KB fragment on reload.
    r = nonadmin_client.get("/v2/partials/approvals/sales-orders", headers=_BROWSER_NAV)
    assert "<html" in r.text
    assert 'id="main-content"' in r.text


def test_logged_out_browser_navigation_gets_login_page(unauthenticated_client: TestClient):
    r = unauthenticated_client.get("/v2/partials/requisitions", headers=_BROWSER_NAV)
    assert r.status_code == 200
    assert "login" in r.text.lower()


def test_shell_route_rejects_non_partial_urls(nonadmin_client: TestClient):
    assert nonadmin_client.get("/v2/shell?partial=/auth/logout").status_code == 404
    assert nonadmin_client.get("/v2/shell?partial=https://evil.example/x").status_code == 404


# ── 2. Server-owned canonical history ─────────────────────────────────────


def test_requisitions_list_stamps_canonical_replace(client: TestClient):
    r = client.get("/v2/partials/requisitions?q=abc&status=open", headers=_HX)
    assert "HX-Push-Url" not in r.headers  # push would trap Back on the shell's lazy load
    assert r.headers.get("HX-Replace-Url", "").startswith("/v2/requisitions?view=list")
    assert "q=abc" in r.headers["HX-Replace-Url"]


def test_prospecting_list_stamps_canonical_replace(client: TestClient):
    r = client.get("/v2/partials/prospecting?scope=all", headers=_HX)
    assert r.headers.get("HX-Replace-Url", "").startswith("/v2/prospecting")


def test_non_htmx_caller_gets_no_history_headers(client: TestClient):
    r = client.get("/v2/partials/requisitions")
    assert "HX-Replace-Url" not in r.headers
    assert "HX-Push-Url" not in r.headers


def test_templates_no_longer_push_partial_urls(client: TestClient):
    body = client.get("/v2/partials/requisitions", headers=_HX).text
    assert 'hx-push-url="true"' not in body
    body = client.get("/v2/partials/prospecting", headers=_HX).text
    assert 'hx-push-url="true"' not in body


# ── 3. Workspace select + retired-detail redirect ─────────────────────────


def test_po_tab_resolves_line_select(client: TestClient, db_session, test_user: User):
    _grant_decide_rights(db_session, test_user)
    req, q, rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
    line = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db_session.commit()

    r = client.get(f"/v2/partials/approvals/purchase-orders/list?select=line-{line.id}", headers=_HX)
    assert r.status_code == 200
    # The deep-linked line is the default selection (aw-default dispatch carries it).
    assert f"line-{line.id}" in r.text


def test_plan_select_accepts_prefixed_key(client: TestClient, db_session, test_user: User):
    _grant_decide_rights(db_session, test_user)
    req, q, _rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.DRAFT.value)
    db_session.commit()

    r = client.get(f"/v2/partials/approvals/sales-orders/list?select=plan-{bp.id}", headers=_HX)
    assert r.status_code == 200
    assert f"plan-{bp.id}" in r.text


def test_garbage_select_falls_back_silently(client: TestClient, db_session, test_user: User):
    _grant_decide_rights(db_session, test_user)
    req, q, _rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.DRAFT.value)
    db_session.commit()

    r = client.get("/v2/partials/approvals/sales-orders/list?select=%3Cscript%3EGARBAGE123", headers=_HX)
    assert r.status_code == 200
    assert f"plan-{bp.id}" in r.text  # the normal list rendered — garbage key ignored
    assert "GARBAGE123" not in r.text  # and the rejected key is never reflected


def test_retired_detail_htmx_gets_hx_redirect(client: TestClient, db_session, test_user: User):
    req, q, _rq = _req_quote(db_session, test_user)
    bp = _plan(db_session, req, q, status=BuyPlanStatus.DRAFT.value)
    db_session.commit()

    r = client.get(f"/v2/partials/buy-plans/{bp.id}", headers=_HX, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == f"/v2/approvals?tab=sales-orders&select={bp.id}"
    # A plain browser still gets the 308 (bookmarks keep working).
    r2 = client.get(f"/v2/partials/buy-plans/{bp.id}", follow_redirects=False)
    assert r2.status_code == 308


# ── 4. v2_page query threading ────────────────────────────────────────────


def test_canonical_list_url_reloads_filtered(nonadmin_client: TestClient):
    # nonadmin_client: v2_page reads the REAL session (get_user), not the Depends chain.
    r = nonadmin_client.get("/v2/requisitions?view=list&q=widget&status=open")
    assert r.status_code == 200
    # The shell's lazy partial URL carries the filters through.
    assert "q=widget" in r.text
    assert "status=open" in r.text
