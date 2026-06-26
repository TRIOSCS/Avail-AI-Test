"""tests/test_htmx_views_nightly17.py — Coverage for vendor list, detail, and tabs.

Targets:
  - vendors_list_partial (GET /v2/partials/vendors)
  - vendor_detail_partial (GET /v2/partials/vendors/{vendor_id})
  - vendor_tab (GET /v2/partials/vendors/{vendor_id}/tab/{tab})

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.models import VendorCard

# ── Vendors List ──────────────────────────────────────────────────────────


class TestVendorsListPartial:
    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200

    def test_list_with_vendor(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200

    def test_list_with_search(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors?q={test_vendor_card.display_name[:5]}")
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("hide_blacklisted=false", id="show_blacklisted"),
            pytest.param("sort=display_name&dir=asc", id="sort_by_name"),
            pytest.param("my_only=true", id="my_only"),
            pytest.param("limit=10&offset=0", id="pagination"),
        ],
    )
    def test_list_query_params(self, client: TestClient, query: str):
        resp = client.get(f"/v2/partials/vendors?{query}")
        assert resp.status_code == 200


# ── Vendor Detail ─────────────────────────────────────────────────────────


class TestVendorDetailPartial:
    def test_vendor_detail(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200

    def test_vendor_detail_with_mpn(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}?mpn=BC547")
        assert resp.status_code == 200

    def test_vendor_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999")
        assert resp.status_code == 404


# ── Vendor Tab ────────────────────────────────────────────────────────────


class TestVendorTab:
    @pytest.mark.parametrize(
        "tab",
        [
            "overview",
            "contacts",
            "find_contacts",
            "emails",
            "analytics",
            "reviews",
            "offers",
        ],
    )
    def test_tab_valid(self, client: TestClient, test_vendor_card: VendorCard, tab: str):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/{tab}")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/nonexistent")
        assert resp.status_code == 404

    def test_tab_vendor_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/tab/overview")
        assert resp.status_code == 404

    def test_tab_overview_with_mpn(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/overview?mpn=NE555")
        assert resp.status_code == 200
