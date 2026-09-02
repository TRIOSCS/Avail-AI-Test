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
