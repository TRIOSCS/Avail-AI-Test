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


# ── Additional coverage tests ─────────────────────────────────────────


import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User


@pytest.fixture()
def admin_perf_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth override for performance endpoints."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestVendorScorecardDetail:
    @patch("app.services.performance_service.get_vendor_scorecard_detail",
           return_value={"vendor_card_id": 1, "name": "Test", "composite_score": 85.0})
    def test_single_vendor_success(self, mock_fn, client, test_vendor_card):
        """Returns vendor scorecard detail."""
        resp = client.get(f"/api/performance/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "composite_score" in data

    @patch("app.services.performance_service.get_vendor_scorecard_detail", return_value=None)
    def test_single_vendor_returns_none_not_found(self, mock_fn, client):
        """Service returns None -> 404."""
        resp = client.get("/api/performance/vendors/1")
        assert resp.status_code == 404


class TestRefreshWithAdmin:
    @patch("app.services.performance_service.compute_all_vendor_scorecards",
           return_value={"computed": 10})
    def test_refresh_vendors_admin_success(self, mock_compute, admin_perf_client):
        """Admin can refresh vendor scorecards."""
        resp = admin_perf_client.post("/api/performance/vendors/refresh")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["computed"] == 10

    @patch("app.services.performance_service.compute_buyer_leaderboard",
           return_value={"computed": 5})
    def test_refresh_buyers_admin_success(self, mock_compute, admin_perf_client):
        """Admin can refresh buyer leaderboard."""
        resp = admin_perf_client.post("/api/performance/buyers/refresh")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSalespersonScorecard:
    def test_salesperson_scorecard_default_month(self, client):
        """GET /api/performance/salespeople with no month defaults to current."""
        with patch("app.services.performance_service.get_salesperson_scorecard",
                   return_value={"entries": []}):
            resp = client.get("/api/performance/salespeople")
        assert resp.status_code == 200

    def test_salesperson_scorecard_with_month(self, client):
        """GET /api/performance/salespeople with month param."""
        with patch("app.services.performance_service.get_salesperson_scorecard",
                   return_value={"entries": []}):
            resp = client.get("/api/performance/salespeople?month=2026-01")
        assert resp.status_code == 200

    def test_salesperson_scorecard_invalid_month(self, client):
        """GET /api/performance/salespeople with bad month -> 400."""
        resp = client.get("/api/performance/salespeople?month=invalid")
        assert resp.status_code == 400


class TestVendorScorecardSearch:
    def test_vendor_scorecards_with_search(self, client):
        """List vendor scorecards with search parameter."""
        resp = client.get("/api/performance/vendors?search=arrow")
        assert resp.status_code == 200
