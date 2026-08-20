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


def test_filter_control_stamps_canonical_replace(client: TestClient):
    # A NAMED control (HX-Trigger-Name) = state change within the page → replace with
    # the canonical page URL + live query. Never a push (that would stack entries per
    # keystroke/filter tap).
    r = client.get("/v2/partials/requisitions?q=abc&status=open", headers={**_HX, "HX-Trigger-Name": "q"})
    assert "HX-Push-Url" not in r.headers
    assert r.headers.get("HX-Replace-Url", "").startswith("/v2/requisitions?view=list")
    assert "q=abc" in r.headers["HX-Replace-Url"]


def test_prospecting_filter_stamps_canonical_replace(client: TestClient):
    r = client.get("/v2/partials/prospecting?scope=all", headers={**_HX, "HX-Trigger-Name": "scope"})
    assert r.headers.get("HX-Replace-Url", "").startswith("/v2/prospecting")


def test_navigation_request_gets_no_replace_stamp(client: TestClient):
    # An UN-named trigger (nav <a>, view toggle, "view all" link) must get NO history
    # header: HX-Replace-Url in a response OVERRIDES the element's own hx-push-url in
    # htmx, so stamping a navigation would erase the page the user came from instead
    # of pushing. Same for pollers (reqListRefresh) — they must never touch history.
    r = client.get("/v2/partials/requisitions?q=abc", headers=_HX)
    assert "HX-Replace-Url" not in r.headers
    assert "HX-Push-Url" not in r.headers


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


# ── 5. Link & history hygiene sweeps (PR-B, nav audit #5-#9, #11-#12) ─────

from pathlib import Path

_T = Path("app/templates/htmx/partials")


def test_no_raw_pushstate_in_templates_or_js():
    # Raw history.pushState entries can never be restored by htmx (nav audit #7) —
    # imperative navigations must use htmx.ajax's push option instead.
    offenders = []
    for f in list(Path("app/templates").rglob("*.html")) + [Path("app/static/htmx_app.js")]:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "history.pushState" in line:
                offenders.append(f"{f}:{i}")
    assert not offenders, "raw history.pushState (htmx can't restore these): " + ", ".join(offenders)


def test_row_containers_disinherit_push_url():
    # A row's hx-push-url is INHERITED by descendant actions (dblclick edits, Claim)
    # unless disinherited — the leak that made the URL lie (nav audit #6).
    for rel in (
        "requisitions/req_row.html",
        "tickets/_row.html",
        "vendors/list.html",
        "vendors/vendor_card.html",
        "materials/list.html",
    ):
        assert 'hx-disinherit="hx-push-url"' in (_T / rel).read_text(), rel


def test_lateral_pages_have_identity_and_way_back():
    fu = (_T / "follow_ups/list.html").read_text()
    rq = (_T / "offers/review_queue.html").read_text()
    qp = (_T / "qp/detail.html").read_text()
    for src_ in (fu, rq):
        assert "title hx-swap-oob" in src_
        assert 'href="/v2/sightings"' in src_  # the way back
    assert 'href="/v2/approvals?tab=sales-orders&select=' in qp  # back to the deal


def test_mobile_nav_capped_at_five_slots():
    # 10 slots at 62px demanded 682px on a 390px phone (nav audit #9); primary bar
    # is 4 items + More, the rest live in the More sheet with badges intact.
    src_ = (_T / "shared/mobile_nav.html").read_text()
    primary = src_.split("more_nav_items")[0]
    assert primary.count("('") <= 5  # 4 item tuples + the set-open paren noise guard
    assert "more_nav_items" in src_
    assert 'id="crm-nav-badge"' in src_  # badge poller survived the move
    assert 'id="proactive-nav-badge"' in src_


def test_best_match_cards_never_emit_dead_links():
    for rel in ("shared/search_results.html", "search/full_results.html"):
        src_ = (_T / rel).read_text()
        assert "bm_dead" in src_, rel  # the guard exists
        assert "hx-push-url=\"{{ bm_url_map.get(bm.type, '#') }}\"" not in src_, rel


# ── 6. Security & regression fixes (adversarial review 2026-08-20) ────────


def test_shell_rejects_xss_segment_no_reflection(nonadmin_client: TestClient):
    # The ?partial= segment lands in base.html's Alpine x-data (HTML-decoded before JS
    # eval), so a raw segment would be reflected XSS. It must be validated against the
    # nav-key allowlist, never echoed. Route 200s (valid /v2/partials/ prefix) but the
    # payload never appears in current_view.
    payload = "/v2/partials/x',init(){alert(1)},x:'"
    r = nonadmin_client.get("/v2/shell", params={"partial": payload})
    assert r.status_code == 200
    # current_view seeds EMPTY for an unknown segment — the raw payload never reaches
    # Alpine's x-data JS-eval context. (It appears once, Jinja-escaped and inert, in the
    # hx-get URL echo; the breakout signature — an UNescaped quote+comma — must not.)
    assert "currentView: ''" in r.text
    assert "',init(){" not in r.text


def test_shell_rejects_path_traversal(nonadmin_client: TestClient):
    # /v2/partials/../../auth/logout passes the prefix check but the browser would
    # normalize the embedded hx-get out of /v2/partials/ — reject traversal outright.
    r = nonadmin_client.get("/v2/shell", params={"partial": "/v2/partials/../../auth/logout"})
    assert r.status_code == 404


def test_shell_valid_segment_still_highlights(nonadmin_client: TestClient):
    # A legitimate segment maps to its nav key (the allowlist must not over-reject).
    r = nonadmin_client.get("/v2/shell", params={"partial": "/v2/partials/approvals/sales-orders"})
    assert r.status_code == 200
    assert "buy-plans" in r.text  # approvals → buy-plans nav highlight


def test_bid_sheet_in_shell_exclusions():
    # The resell bid-sheet streams a CSV whose URL contains no ".csv"/"/export"/
    # "/download" substring, so it must be named explicitly in the middleware exclusion
    # denylist — otherwise a browser download gets wrapped in the app shell (the CSV is
    # swapped into #main-content). The exclusion MECHANISM is exercised end-to-end by
    # test_exports_never_shell_wrapped; this pins the bid-sheet path is covered.
    from app.main import ShellNegotiationMiddleware

    assert "/bid-sheet" in ShellNegotiationMiddleware._EXCLUDE_SUBSTRINGS


def test_best_match_null_id_uses_boolean_default():
    # Jinja default("") does NOT substitute None (only undefined), so a null
    # requisition_id stringified to a live "/v2/requisitions/None" card. The , true
    # boolean-default flag catches None → "" → the bm_dead guard's trailing-"/" check.
    for rel in ("shared/search_results.html", "search/full_results.html"):
        src_ = (_T / rel).read_text()
        assert 'bm.requisition_id|default("")' not in src_, rel  # the unsafe form is gone
        assert 'bm.requisition_id|default("", true)' in src_, rel
