"""Tests for unified leaderboard and scoring-info API endpoints.

Validates response shapes, caching, auth, and admin refresh endpoint.

Called by: pytest
Depends on: app/routers/dashboard.py, app/routers/performance.py
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.unified_score import UnifiedScoreSnapshot


class TestUnifiedLeaderboardEndpoint:
    def test_returns_entries(self, client, db_session, test_user):
        month = date.today().replace(day=1)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id, month=month, unified_score=72, rank=1,
            primary_role="buyer", prospecting_pct=80, execution_pct=70,
            followthrough_pct=60, closing_pct=80, depth_pct=50,
        )
        db_session.add(snap)
        db_session.commit()

        resp = client.get("/api/dashboard/unified-leaderboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "month" in data
        assert len(data["entries"]) == 1
        entry = data["entries"][0]
        assert entry["user_id"] == test_user.id
        assert entry["unified_score"] == 72
        assert entry["rank"] == 1
        assert entry["primary_role"] == "buyer"

    def test_accepts_month_param(self, client, db_session, test_user):
        month = date(2026, 1, 1)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id, month=month, unified_score=55, rank=1,
            primary_role="buyer",
        )
        db_session.add(snap)
        db_session.commit()

        resp = client.get("/api/dashboard/unified-leaderboard?month=2026-01")
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-01-01"
        assert len(data["entries"]) == 1

    def test_invalid_month_returns_400(self, client):
        resp = client.get("/api/dashboard/unified-leaderboard?month=bad")
        assert resp.status_code == 400

    def test_empty_month_returns_empty(self, client):
        resp = client.get("/api/dashboard/unified-leaderboard?month=2020-01")
        assert resp.status_code == 200
        assert resp.json()["entries"] == []

    def test_entries_have_required_fields(self, client, db_session, test_user):
        month = date.today().replace(day=1)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id, month=month, unified_score=60, rank=1,
            primary_role="buyer", prospecting_pct=50, execution_pct=60,
            followthrough_pct=70, closing_pct=80, depth_pct=30,
            ai_blurb_strength="Great work!",
            ai_blurb_improvement="Try harder on depth.",
        )
        db_session.add(snap)
        db_session.commit()

        resp = client.get("/api/dashboard/unified-leaderboard")
        entry = resp.json()["entries"][0]
        required_fields = [
            "user_id", "user_name", "primary_role", "unified_score", "rank",
            "prospecting_pct", "execution_pct", "followthrough_pct",
            "closing_pct", "depth_pct", "ai_blurb_strength", "ai_blurb_improvement",
        ]
        for field in required_fields:
            assert field in entry, f"Missing field: {field}"


class TestScoringInfoEndpoint:
    def test_returns_categories(self, client):
        resp = client.get("/api/dashboard/scoring-info")
        assert resp.status_code == 200
        data = resp.json()
        assert "categories" in data
        assert len(data["categories"]) == 4
        assert data["total_range"] == "0-100"

    def test_weights_sum_to_100(self, client):
        resp = client.get("/api/dashboard/scoring-info")
        cats = resp.json()["categories"]
        total = sum(c["weight"] for c in cats)
        assert total == 100

    def test_each_category_has_fields(self, client):
        resp = client.get("/api/dashboard/scoring-info")
        for cat in resp.json()["categories"]:
            assert "name" in cat
            assert "weight" in cat
            assert "description" in cat


class TestUnifiedScoresRefreshEndpoint:
    def test_non_admin_gets_403(self, client):
        resp = client.post("/api/performance/unified-scores/refresh")
        assert resp.status_code == 403

    def test_admin_can_refresh(self, db_session, admin_user):
        from app.main import app
        from app.database import get_db
        from app.dependencies import require_user
        from fastapi.testclient import TestClient

        def _override_db():
            yield db_session

        def _override_user():
            return admin_user

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_user
        try:
            with TestClient(app) as c:
                with patch("app.services.unified_score_service.compute_all_unified_scores",
                           return_value={"computed": 3, "saved": 3}):
                    resp = c.post("/api/performance/unified-scores/refresh")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["status"] == "ok"
                    assert data["computed"] == 3
        finally:
            app.dependency_overrides.clear()
