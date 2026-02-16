"""
test_routers_performance.py — Tests for performance and proactive router endpoints.

Tests vendor scorecards, buyer leaderboard, and proactive matching endpoints.

Called by: pytest
Depends on: app/routers/performance.py, app/routers/proactive.py, conftest.py
"""


# ── Performance endpoints ───────────────────────────────────────────


class TestVendorScorecards:
    def test_list_vendor_scorecards(self, client):
        resp = client.get("/api/performance/vendors")
        assert resp.status_code == 200

    def test_list_with_params(self, client):
        resp = client.get("/api/performance/vendors?sort_by=composite_score&order=desc&limit=10&offset=0")
        assert resp.status_code == 200

    def test_single_vendor_not_found(self, client):
        resp = client.get("/api/performance/vendors/99999")
        assert resp.status_code == 404


class TestBuyerLeaderboard:
    def test_list_leaderboard(self, client):
        resp = client.get("/api/performance/buyers")
        assert resp.status_code == 200
        data = resp.json()
        assert "month" in data
        assert "entries" in data

    def test_list_with_month(self, client):
        resp = client.get("/api/performance/buyers?month=2026-01")
        assert resp.status_code == 200

    def test_invalid_month_format(self, client):
        resp = client.get("/api/performance/buyers?month=not-a-date")
        assert resp.status_code == 400

    def test_list_months(self, client):
        resp = client.get("/api/performance/buyers/months")
        assert resp.status_code == 200
        assert "months" in resp.json()


class TestRefreshEndpoints:
    """Refresh endpoints require admin — regular client (buyer) should get 403."""

    def test_refresh_vendors_requires_admin(self, client):
        resp = client.post("/api/performance/vendors/refresh")
        assert resp.status_code == 403

    def test_refresh_buyers_requires_admin(self, client):
        resp = client.post("/api/performance/buyers/refresh")
        assert resp.status_code == 403
