"""Tests for the Reports page (Phase 5 / STREAM 1, Decision M) — access key parity, the
GET /v2/partials/reports route + templates, the relocated pipeline strip, nav
reachability.

Every test asserts on real output (rendered captions, pill URLs, map contents), never a bare
status/None check.

Called by: pytest
Depends on: app.routers.htmx.reports, app.services.gp_report_service, tests.conftest
            (_buyplan_req, _buyplan_plan, client, manager_client)
"""

import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.access_paths import module_key_for_path
from app.constants import MODULE_ACCESS_KEYS, ROLE_ACCESS_DEFAULTS, AccessKey, UserRole
from app.models.auth import User
from app.routers.admin.users import _ACCESS_KEY_LABELS, NAV_ID_TO_ACCESS
from tests.conftest import _buyplan_plan, _buyplan_req

_T = pathlib.Path("app/templates/htmx/partials")
PARTIAL = "/v2/partials/reports"
AMBER_UNPRICED = '<span class="text-amber-700"> · 1 unpriced</span>'


def _seed(db: Session, user: User, *, revenue="1000.00", cost="600.00", **overrides):
    """A booked plan submitted by *user* yesterday (always inside the default 6-month
    window)."""
    req = _buyplan_req(db, user)
    fields = dict(
        status="pending",
        submitted_by_id=user.id,
        submitted_at=datetime.now(UTC) - timedelta(days=1),
        total_revenue=Decimal(revenue) if revenue is not None else None,
        total_cost=Decimal(cost) if cost is not None else None,
    )
    fields.update(overrides)
    return _buyplan_plan(db, req, **fields)


# ── Task 2: access key parity ─────────────────────────────────────────────


def test_access_maps_cover_reports():
    assert AccessKey.REPORTS == "reports"
    assert AccessKey.REPORTS in MODULE_ACCESS_KEYS
    assert MODULE_ACCESS_KEYS[-1] == AccessKey.REPORTS
    assert NAV_ID_TO_ACCESS["reports"] == AccessKey.REPORTS
    assert _ACCESS_KEY_LABELS[AccessKey.REPORTS] == "Reports"
    # Owner question (c) default: on for every interactive role, admin always, agent never.
    for role in (UserRole.BUYER, UserRole.SALES, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN):
        assert AccessKey.REPORTS in ROLE_ACCESS_DEFAULTS[role], role
    assert AccessKey.REPORTS not in ROLE_ACCESS_DEFAULTS[UserRole.AGENT]
    # Middleware chokepoint: exact base or base + "/", never a bare prefix.
    assert module_key_for_path("/v2/partials/reports") == AccessKey.REPORTS
    assert module_key_for_path("/v2/partials/reports/x") == AccessKey.REPORTS
    assert module_key_for_path("/v2/partials/reportsx") is None


def test_full_page_gate_maps_reports():
    from app.routers.htmx_views import _MODULE_ENTRY_URLS, _VALID_NAV_KEYS, _VIEW_ACCESS

    assert _VIEW_ACCESS["reports"] == AccessKey.REPORTS
    # LAST: never a redirect target ahead of a real module.
    assert _MODULE_ENTRY_URLS[-1] == (AccessKey.REPORTS, "/v2/reports")
    assert "reports" in _VALID_NAV_KEYS


# ── Task 3: route + templates ─────────────────────────────────────────────


def test_full_page_lazy_loads_partial_and_threads_query(client: TestClient, test_user: User):
    # v2_page and v2_shell read get_user(request, db) directly → patch, not fixture
    # override (the tests/test_htmx_views.py:226-230 pattern).
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        resp = client.get("/v2/reports?basis=realized&group_by=rep")
    assert resp.status_code == 200
    # The shell's lazy hx-get is an attribute-value interpolation — Jinja autoescape
    # renders & as &amp; (precedent tests/test_approvals_hub_tabs.py:971-972).
    assert 'hx-get="/v2/partials/reports?basis=realized&amp;group_by=rep"' in resp.text


def test_partial_renders_caption_tiles_and_table(client: TestClient, db_session: Session, test_user: User):
    _seed(db_session, test_user)  # priced 1000/600
    _seed(db_session, test_user, revenue=None, cost=None)  # lite order type → unpriced
    resp = client.get(PARTIAL)
    assert resp.status_code == 200
    body = resp.text
    assert "Booked — by plan submit date" in body
    assert "UTC calendar months" in body
    assert "GP $" in body
    assert "1 of 2 plans" in body and "no priced header" in body
    assert "400.00" in body  # GP = 1000 − 600, from the one priced plan only


def test_partial_hx_target_gp_panel_returns_fragment_only(client: TestClient, db_session: Session, test_user: User):
    _seed(db_session, test_user)
    resp = client.get(PARTIAL, headers={"HX-Target": "gp-panel"})
    assert resp.status_code == 200
    body = resp.text
    assert "<table" in body
    assert "<h1" not in body
    assert "Requisition pipeline" not in body
    assert 'id="gp-panel"' not in body  # the fragment swaps INTO #gp-panel, never nests it


def test_pills_carry_replace_url_and_never_push(client: TestClient, db_session: Session, test_user: User):
    _seed(db_session, test_user)
    body = client.get(PARTIAL).text
    # Pill URLs are literal template text — the & separators stay raw & (the
    # approvals_hub.html:39 precedent; contrast the autoescaped shell hx-get above).
    assert 'hx-replace-url="/v2/reports?basis=realized&' in body
    assert 'hx-target="#gp-panel"' in body
    assert "hx-push-url" not in body  # inherited attr — banned on this page (Global Constraints)


def test_group_by_and_basis_pills_switch_headers(client: TestClient, db_session: Session, test_user: User):
    _seed(db_session, test_user)
    assert "Rep (submitted by)" in client.get(PARTIAL + "?group_by=rep").text
    cust = client.get(PARTIAL + "?group_by=customer").text
    assert "<th" in cust and "Customer" in cust
    assert "Realized — by plan completion date" in client.get(PARTIAL + "?basis=realized").text


def test_non_manager_scope_all_falls_back_to_mine(
    client: TestClient, db_session: Session, test_user: User, manager_user: User
):
    _seed(db_session, test_user)
    _seed(db_session, manager_user, revenue="7777.00", cost="1.00")
    body = client.get(PARTIAL + "?scope=all").text
    assert "Mine (plans you submitted)" in body  # silent fallback — no 403, no leak
    assert "scope=all" not in body  # a non-manager gets no All pill and no all-URLs
    assert "7,777.00" not in body  # the other user's revenue never renders


def test_manager_defaults_to_all_and_sees_all_pill(
    manager_client: TestClient, db_session: Session, test_user: User, manager_user: User
):
    _seed(db_session, test_user, revenue="7777.00", cost="1.00")
    body = manager_client.get(PARTIAL).text  # no scope param at all
    assert "All (plans from every user)" in body  # MANAGER_DEFAULT_SCOPE — owner question (f)
    assert "scope=all" in body and "scope=mine" in body  # both pills present
    assert "7,777.00" in body  # another user's plan visible on the All lens


def test_manager_scope_mine_honoured(
    manager_client: TestClient, db_session: Session, test_user: User, manager_user: User
):
    _seed(db_session, test_user, revenue="7777.00", cost="1.00")
    _seed(db_session, manager_user)
    body = manager_client.get(PARTIAL + "?scope=mine").text
    assert "Mine (plans you submitted)" in body
    assert "7,777.00" not in body


def test_unpriced_count_rendered_amber(client: TestClient, db_session: Session, test_user: User):
    _seed(db_session, test_user)
    _seed(db_session, test_user, revenue=None, cost=None)
    assert AMBER_UNPRICED in client.get(PARTIAL).text


def test_empty_state_message(client: TestClient, test_user: User):
    body = client.get(PARTIAL).text
    assert "No booked plans" in body and "Try 12 months" in body
    assert "<table" not in body


def test_pipeline_strip_renders_all_fields_labeled(client: TestClient, test_user: User):
    body = client.get(PARTIAL).text
    assert "Requisition pipeline · all users · all-time" in body
    for label in ("open deal", "open value", "weighted forecast", "won (", "lost", "win rate (all-time)"):
        assert label in body, label
    assert "not plan revenue" in body  # deal value ≠ plan revenue — honesty caption


def test_reports_revoked_blocks_partial_and_redirects_page(client: TestClient, db_session: Session, test_user: User):
    # The tests/test_access_control.py:592-605 revoked-redirect pattern.
    test_user.access_overrides = {"reports": False}
    db_session.commit()
    assert client.get(PARTIAL).status_code == 403
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        resp = client.get("/v2/reports", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/v2/requisitions"  # first allowed _MODULE_ENTRY_URLS row


def test_v2_shell_heals_reports_partial_with_nav_key(client: TestClient, test_user: User):
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        resp = client.get("/v2/shell?partial=/v2/partials/reports")
    assert resp.status_code == 200
    assert "activeNav: 'reports'" in resp.text  # segment → nav key via _SHELL_NAV_KEYS .get fallback
    assert 'hx-get="/v2/partials/reports"' in resp.text
