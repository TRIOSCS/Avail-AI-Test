"""
tests/test_htmx_proactive_strategic.py — Tests for Proactive + Strategic Vendor HTMX views.

Covers full page loads and partial rendering for the Proactive Part Match
and My Vendors (Strategic Vendors) pages.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, etc.)
"""

from fastapi.testclient import TestClient


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
