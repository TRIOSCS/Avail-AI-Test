"""Tests for Buy Plans primary-nav item (Tasks 4 + 5 of buy-plan-deal-hub).

Covers:
- GET /v2/buy-plans returns 200; GET /v2/reporting returns 404
- mobile_nav.html contains the 'buy-plans' nav item and NOT the 'reporting' item
- urlToNav maps /v2/buy-plans to 'buy-plans' (not 'reporting'); no /v2/reporting entry
- Badge elif contains 'buy-plans'
- _NAV_ID_ALIAS has no 'buy-plans' key; no alias resolves to 'reporting'
  (the quotes->reporting alias was removed in Task 11 when Reporting was retired)

Called by: pytest
Depends on: app.routers.htmx_views, app/templates/htmx/partials/shared/mobile_nav.html
"""

import pathlib

from fastapi.testclient import TestClient

from tests.conftest import engine  # noqa: F401

_TEMPLATE = pathlib.Path("app/templates/htmx/partials/shared/mobile_nav.html")


class TestMobileNavTemplate:
    """mobile_nav.html contains 'buy-plans' nav item and NOT 'reporting'."""

    def test_nav_items_has_buy_plans_entry(self):
        """nav_items tuple contains the 'buy-plans' entry."""
        src = _TEMPLATE.read_text()
        assert "('buy-plans'," in src, "'buy-plans' nav item missing from nav_items"
        assert "Buy Plans" in src, "Buy Plans label missing"

    def test_nav_items_has_no_reporting_entry(self):
        """nav_items tuple does NOT contain a 'reporting' entry."""
        src = _TEMPLATE.read_text()
        assert "('reporting'," not in src, "'reporting' nav item still present in nav_items"

    def test_nav_items_has_no_approvals_entry(self):
        """Approvals is folded into the Buy Plans hub lens — no standalone nav item."""
        src = _TEMPLATE.read_text()
        assert "('approvals'," not in src, "'approvals' nav item must be removed (folded into Buy Plans hub)"
        assert "'/v2/approvals/queue'" not in src, "the standalone Approvals nav link must be gone"
        # Deep links to /v2/approvals highlight the Buy Plans tab (it 302s to the hub lens).
        assert "'/v2/approvals':'buy-plans'" in src, "urlToNav must alias /v2/approvals to 'buy-plans'"

    def test_url_to_nav_maps_buy_plans_to_itself(self):
        """UrlToNav maps /v2/buy-plans to 'buy-plans'."""
        src = _TEMPLATE.read_text()
        assert "'/v2/buy-plans':'buy-plans'" in src

    def test_url_to_nav_has_no_reporting_entry(self):
        """UrlToNav does NOT map /v2/reporting to anything."""
        src = _TEMPLATE.read_text()
        assert "'/v2/reporting'" not in src

    def test_badge_elif_has_buy_plans(self):
        """Badge elif block wires 'buy-plans' into the cross-app alert-badge tuple.

        Anchored on the exact ``{% elif id in (...) %}`` membership tuple so this
        verifies the badge wiring, not an incidental 'buy-plans' string elsewhere.
        """
        src = _TEMPLATE.read_text()
        assert "{% elif id in ('requisitions', 'buy-plans', 'crm', 'my-day') %}" in src


class TestNavIdAlias:
    """_NAV_ID_ALIAS: 'buy-plans' key removed, 'quotes' key survives."""

    def test_buy_plans_not_in_alias(self):
        """'buy-plans' must NOT be in _NAV_ID_ALIAS (it is its own primary nav tab)."""
        from app.routers.htmx_views import _NAV_ID_ALIAS

        assert "buy-plans" not in _NAV_ID_ALIAS

    def test_no_alias_resolves_to_reporting(self):
        """Task 11 retired the Reporting surface — no nav alias may target 'reporting'.

        The quotes→reporting alias was removed (quote detail falls through to 'quotes',
        which highlights no nav item — correct, it has no parent tab).
        """
        from app.routers.htmx_views import _NAV_ID_ALIAS

        assert "reporting" not in _NAV_ID_ALIAS.values()
        assert "quotes" not in _NAV_ID_ALIAS

    def test_alias_is_dict(self):
        """_NAV_ID_ALIAS is a dict (shape check)."""
        from app.routers.htmx_views import _NAV_ID_ALIAS

        assert isinstance(_NAV_ID_ALIAS, dict)


class TestRoutes:
    """Route availability: /v2/buy-plans → 200; /v2/reporting → 404."""

    def test_buy_plans_route_returns_200(self, client: TestClient):
        """GET /v2/buy-plans returns 200."""
        resp = client.get("/v2/buy-plans")
        assert resp.status_code == 200

    def test_reporting_route_returns_404(self, client: TestClient):
        """GET /v2/reporting returns 404 (route removed from v2_page)."""
        resp = client.get("/v2/reporting")
        assert resp.status_code == 404
