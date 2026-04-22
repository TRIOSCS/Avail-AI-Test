"""tests/test_htmx_views_nightly17.py — Coverage for vendor list, detail, and tabs.

Targets:
  - vendors_list_partial (GET /v2/partials/vendors)
  - find_by_part_partial (GET /v2/partials/vendors/find-by-part)
  - vendor_detail_partial (GET /v2/partials/vendors/{vendor_id})
  - vendor_tab (GET /v2/partials/vendors/{vendor_id}/tab/{tab})

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

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

    def test_list_show_blacklisted(self, client: TestClient):
        resp = client.get("/v2/partials/vendors?hide_blacklisted=false")
        assert resp.status_code == 200

    def test_list_sort_by_name(self, client: TestClient):
        resp = client.get("/v2/partials/vendors?sort=display_name&dir=asc")
        assert resp.status_code == 200

    def test_list_my_only(self, client: TestClient):
        resp = client.get("/v2/partials/vendors?my_only=true")
        assert resp.status_code == 200

    def test_list_pagination(self, client: TestClient):
        resp = client.get("/v2/partials/vendors?limit=10&offset=0")
        assert resp.status_code == 200


# ── Find By Part ──────────────────────────────────────────────────────────


class TestFindByPartPartial:
    def test_find_empty_mpn(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/find-by-part")
        assert resp.status_code == 200

    def test_find_with_mpn(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/find-by-part?mpn=NE555")
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
    def test_tab_overview(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/overview")
        assert resp.status_code == 200

    def test_tab_contacts(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/contacts")
        assert resp.status_code == 200

    def test_tab_find_contacts(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/find_contacts")
        assert resp.status_code == 200

    def test_tab_emails(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/emails")
        assert resp.status_code == 200

    def test_tab_analytics(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/analytics")
        assert resp.status_code == 200

    def test_tab_reviews(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/reviews")
        assert resp.status_code == 200

    def test_tab_offers(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/offers")
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
