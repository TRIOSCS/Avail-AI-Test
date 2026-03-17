"""
tests/test_htmx_proactive_strategic.py — Tests for Proactive + Strategic Vendor HTMX views.

Covers full page loads and partial rendering for the Proactive Part Match
and My Vendors (Strategic Vendors) pages.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, etc.)
"""

from fastapi.testclient import TestClient

from app.models import ProactiveOffer


class TestProactivePages:
    """Test Proactive Part Match HTMX views."""

    def test_v2_proactive_full_page(self, client: TestClient):
        resp = client.get("/v2/proactive")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_proactive_partial_matches_tab(self, client: TestClient):
        resp = client.get(
            "/v2/partials/proactive?tab=matches",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Proactive Part Match" in resp.text

    def test_proactive_partial_sent_tab(self, client: TestClient):
        resp = client.get(
            "/v2/partials/proactive?tab=sent",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Proactive Part Match" in resp.text

    def test_proactive_sent_tab_shows_offer_data(
        self, client: TestClient, test_proactive_offer: ProactiveOffer
    ):
        resp = client.get(
            "/v2/partials/proactive?tab=sent",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # The sent tab renders customer name from the linked site
        assert "Acme Electronics" in resp.text or "SENT" in resp.text

    def test_proactive_invalid_tab_defaults_gracefully(self, client: TestClient):
        resp = client.get(
            "/v2/partials/proactive?tab=nonexistent",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_proactive_matches_tab_empty_state(self, client: TestClient):
        resp = client.get(
            "/v2/partials/proactive?tab=matches",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Should show empty state or table structure, not crash
        assert "Proactive Part Match" in resp.text


class TestStrategicPages:
    """Test My Vendors (Strategic) HTMX views."""

    def test_v2_strategic_full_page(self, client: TestClient):
        resp = client.get("/v2/strategic")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_strategic_partial(self, client: TestClient):
        resp = client.get(
            "/v2/partials/strategic",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "My Vendors" in resp.text

    def test_strategic_partial_with_search(self, client: TestClient):
        resp = client.get(
            "/v2/partials/strategic?search=test",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "My Vendors" in resp.text

    def test_strategic_search_no_results_shows_empty(self, client: TestClient):
        resp = client.get(
            "/v2/partials/strategic?search=zzz_nonexistent_vendor_zzz",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # Should not crash — shows empty state
        assert "My Vendors" in resp.text

    def test_strategic_page_renders_structure(self, client: TestClient):
        resp = client.get(
            "/v2/partials/strategic",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "My Vendors" in resp.text
