"""Tests for the /api/dashboard/team-leaderboard endpoint.

Verifies the combined Avail Score + Multiplier Points leaderboard returns
correctly merged, ranked, and role-filtered data.

Called by: pytest tests/test_team_leaderboard.py
Depends on: app/routers/dashboard.py, app/models/performance.py
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot


def _seed_avail(db, user_id, role, score=50, behavior=25, outcome=25, rank=1):
    """Insert an AvailScoreSnapshot for the current month."""
    month = date.today().replace(day=1)
    snap = AvailScoreSnapshot(
        user_id=user_id,
        month=month,
        role_type=role,
        behavior_total=behavior,
        outcome_total=outcome,
        total_score=score,
        rank=rank,
        qualified=score >= 50,
        bonus_amount=500 if rank == 1 and score >= 60 else 0,
        b1_score=5, b1_label="Response Time",
        b2_score=5, b2_label="RFQ Volume",
        b3_score=5, b3_label="Source Diversity",
        b4_score=5, b4_label="Data Quality",
        b5_score=5, b5_label="Follow-through",
        o1_score=5, o1_label="Win Rate",
        o2_score=5, o2_label="Quote Accuracy",
        o3_score=5, o3_label="Pipeline Growth",
        o4_score=5, o4_label="Revenue",
        o5_score=5, o5_label="Customer Sat",
    )
    db.add(snap)
    db.flush()
    return snap


def _seed_multiplier(db, user_id, role, total_pts=10, offer_pts=8, bonus_pts=2,
                     rank=1, qualified=True):
    """Insert a MultiplierScoreSnapshot for the current month."""
    month = date.today().replace(day=1)
    snap = MultiplierScoreSnapshot(
        user_id=user_id,
        month=month,
        role_type=role,
        offer_points=offer_pts,
        bonus_points=bonus_pts,
        total_points=total_pts,
        rank=rank,
        avail_score=50,
        qualified=qualified,
        bonus_amount=500 if rank == 1 else 0,
        offers_total=10,
        offers_base_count=5,
        offers_base_pts=5,
        offers_quoted_count=3,
        offers_quoted_pts=9,
        offers_bp_count=1,
        offers_bp_pts=5,
        offers_po_count=1,
        offers_po_pts=8,
        rfqs_sent_count=4,
        rfqs_sent_pts=1,
        stock_lists_count=1,
        stock_lists_pts=2,
    )
    db.add(snap)
    db.flush()
    return snap


class TestTeamLeaderboardEndpoint:
    def test_returns_200_with_empty_data(self, client, db_session, test_user):
        """Endpoint returns 200 with empty entries when no data exists."""
        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "buyer"
        assert data["entries"] == []
        assert "month" in data

    def test_buyer_leaderboard_merges_avail_and_multiplier(self, client, db_session, test_user):
        """Entries merge Avail Score + Multiplier data for the same user."""
        _seed_avail(db_session, test_user.id, "buyer", score=72, behavior=35, outcome=37)
        _seed_multiplier(db_session, test_user.id, "buyer", total_pts=25.5)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        assert resp.status_code == 200
        data = resp.json()
        entries = data["entries"]
        assert len(entries) == 1

        e = entries[0]
        assert e["user_id"] == test_user.id
        assert e["avail_score"] == 72
        assert e["behavior_total"] == 35
        assert e["outcome_total"] == 37
        assert e["total_points"] == 25.5
        assert e["rank"] == 1
        assert "breakdown" in e

    def test_ranking_by_total_points(self, client, db_session, test_user):
        """Users are ranked by total_points desc, with avail_score tiebreak."""
        user2 = User(name="Buyer Two", email="buyer2@test.com", role="buyer",
                     created_at=datetime.now(timezone.utc))
        db_session.add(user2)
        db_session.flush()

        # test_user: 10 pts, 60 avail
        _seed_avail(db_session, test_user.id, "buyer", score=60)
        _seed_multiplier(db_session, test_user.id, "buyer", total_pts=10, rank=2)
        # user2: 20 pts, 50 avail — should be rank 1
        _seed_avail(db_session, user2.id, "buyer", score=50, rank=2)
        _seed_multiplier(db_session, user2.id, "buyer", total_pts=20, rank=1)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        entries = resp.json()["entries"]
        assert len(entries) == 2
        assert entries[0]["user_id"] == user2.id
        assert entries[0]["rank"] == 1
        assert entries[1]["user_id"] == test_user.id
        assert entries[1]["rank"] == 2

    def test_sales_role_filter(self, client, db_session, test_user):
        """Requesting role=sales returns only sales data, not buyer data."""
        _seed_avail(db_session, test_user.id, "buyer", score=80)
        _seed_multiplier(db_session, test_user.id, "buyer", total_pts=30)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=sales")
        entries = resp.json()["entries"]
        assert len(entries) == 0

    def test_avail_only_user_still_appears(self, client, db_session, test_user):
        """User with Avail Score but no Multiplier data still shows up."""
        _seed_avail(db_session, test_user.id, "buyer", score=55)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        entries = resp.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["avail_score"] == 55
        assert entries[0]["total_points"] == 0

    def test_multiplier_only_user_still_appears(self, client, db_session, test_user):
        """User with Multiplier Score but no Avail data still shows up."""
        _seed_multiplier(db_session, test_user.id, "buyer", total_pts=15)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        entries = resp.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["total_points"] == 15
        assert entries[0]["avail_score"] == 0

    def test_buyer_breakdown_included(self, client, db_session, test_user):
        """Buyer breakdown includes offer tier counts and bonus categories."""
        _seed_multiplier(db_session, test_user.id, "buyer", total_pts=30)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        bd = resp.json()["entries"][0]["breakdown"]
        assert "offers_total" in bd
        assert "offers_base" in bd
        assert "offers_quoted" in bd
        assert "offers_bp" in bd
        assert "offers_po" in bd
        assert "rfqs_sent" in bd
        assert "stock_lists" in bd

    def test_avail_metric_breakdown_included(self, client, db_session, test_user):
        """Avail Score behavior/outcome metric labels are included."""
        _seed_avail(db_session, test_user.id, "buyer", score=70)
        db_session.commit()

        resp = client.get("/api/dashboard/team-leaderboard?role=buyer")
        e = resp.json()["entries"][0]
        assert e["b1_label"] == "Response Time"
        assert e["o1_label"] == "Win Rate"
        assert e["b1_score"] == 5

    def test_invalid_role_rejected(self, client, db_session, test_user):
        """Invalid role parameter returns 422."""
        resp = client.get("/api/dashboard/team-leaderboard?role=invalid")
        assert resp.status_code == 422
