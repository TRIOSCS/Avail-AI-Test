"""Tests for unified score service — 4-category math, trader merge, ranking, blurb mock.

Validates cross-role normalization (Execution, Follow-Through, Closing, Depth),
weighted scoring (30/25/30/15), and AI blurb generation with mocked claude_structured.

Called by: pytest
Depends on: app/services/unified_score_service.py, app/models/unified_score.py
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.performance import AvailScoreSnapshot, MultiplierScoreSnapshot
from app.models.unified_score import UnifiedScoreSnapshot

# ── Category Math Tests ─────────────────────────────────────────────


class TestBuyerCategories:
    def test_perfect_scores(self):
        from app.services.unified_score_service import _buyer_categories

        snap = MagicMock()
        for attr in ("b1_score", "b3_score", "b4_score", "o1_score", "o2_score", "o3_score", "o4_score", "o5_score"):
            setattr(snap, attr, 10.0)

        cats = _buyer_categories(snap)
        assert len(cats) == 4
        assert "prospecting" not in cats
        assert cats["execution"] == 100.0  # (10+10+10)/30 * 100
        assert cats["followthrough"] == 100.0  # (10+10)/20 * 100
        assert cats["closing"] == 100.0  # (10+10)/20 * 100
        assert cats["depth"] == 100.0  # 10/10 * 100

    def test_zero_scores(self):
        from app.services.unified_score_service import _buyer_categories

        snap = MagicMock()
        for attr in ("b1_score", "b3_score", "b4_score", "o1_score", "o2_score", "o3_score", "o4_score", "o5_score"):
            setattr(snap, attr, 0)

        cats = _buyer_categories(snap)
        assert len(cats) == 4
        assert all(v == 0.0 for v in cats.values())

    def test_partial_scores(self):
        from app.services.unified_score_service import _buyer_categories

        snap = MagicMock()
        snap.b1_score = 5.0
        snap.b3_score = 6.0
        snap.b4_score = 7.0
        snap.o1_score = 3.0
        snap.o2_score = 9.0
        snap.o3_score = 2.0
        snap.o4_score = 8.0
        snap.o5_score = 6.0

        cats = _buyer_categories(snap)
        assert len(cats) == 4
        assert cats["execution"] == pytest.approx((5 + 7 + 3) / 30 * 100, abs=0.1)
        assert cats["followthrough"] == pytest.approx((6 + 9) / 20 * 100, abs=0.1)
        assert cats["closing"] == pytest.approx((2 + 8) / 20 * 100, abs=0.1)
        assert cats["depth"] == pytest.approx(6 / 10 * 100, abs=0.1)

    def test_none_scores_treated_as_zero(self):
        from app.services.unified_score_service import _buyer_categories

        snap = MagicMock()
        for attr in ("b1_score", "b3_score", "b4_score", "o1_score", "o2_score", "o3_score", "o4_score", "o5_score"):
            setattr(snap, attr, None)

        cats = _buyer_categories(snap)
        assert len(cats) == 4
        assert all(v == 0.0 for v in cats.values())


class TestSalesCategories:
    def test_perfect_scores(self):
        from app.services.unified_score_service import _sales_categories

        snap = MagicMock()
        for attr in ("b2_score", "b3_score", "b4_score", "o1_score", "o2_score", "o3_score", "o4_score", "o5_score"):
            setattr(snap, attr, 10.0)

        cats = _sales_categories(snap)
        assert len(cats) == 4
        assert "prospecting" not in cats
        assert cats["execution"] == 100.0  # (10+10+10)/30
        assert cats["followthrough"] == 100.0  # (10+10)/20
        assert cats["closing"] == 100.0  # (10+10)/20 — no O5 double-count
        assert cats["depth"] == 100.0  # 10/10


class TestTraderMerge:
    def test_averages_both_roles(self):
        from app.services.unified_score_service import _merge_trader_categories

        buyer = {"execution": 60, "followthrough": 40, "closing": 100, "depth": 20}
        sales = {"execution": 80, "followthrough": 60, "closing": 40, "depth": 80}

        merged = _merge_trader_categories(buyer, sales)
        assert len(merged) == 4
        assert merged["execution"] == 70.0
        assert merged["followthrough"] == 50.0
        assert merged["closing"] == 70.0
        assert merged["depth"] == 50.0

    def test_buyer_only(self):
        from app.services.unified_score_service import _merge_trader_categories

        buyer = {"execution": 60, "followthrough": 40, "closing": 100, "depth": 20}
        merged = _merge_trader_categories(buyer, None)
        assert merged == buyer

    def test_sales_only(self):
        from app.services.unified_score_service import _merge_trader_categories

        sales = {"execution": 80, "followthrough": 60, "closing": 40, "depth": 80}
        merged = _merge_trader_categories(None, sales)
        assert merged == sales

    def test_neither_role(self):
        from app.services.unified_score_service import _merge_trader_categories

        merged = _merge_trader_categories(None, None)
        assert len(merged) == 4
        assert all(v == 0.0 for v in merged.values())


class TestWeightedScore:
    def test_all_100(self):
        from app.services.unified_score_service import _weighted_score

        cats = {"execution": 100, "followthrough": 100, "closing": 100, "depth": 100}
        assert _weighted_score(cats) == pytest.approx(100.0)

    def test_all_zero(self):
        from app.services.unified_score_service import _weighted_score

        cats = {"execution": 0, "followthrough": 0, "closing": 0, "depth": 0}
        assert _weighted_score(cats) == 0.0

    def test_weighted_correctly(self):
        from app.services.unified_score_service import _weighted_score

        cats = {"execution": 80, "followthrough": 60, "closing": 70, "depth": 40}
        expected = 80 * 0.30 + 60 * 0.25 + 70 * 0.30 + 40 * 0.15
        assert _weighted_score(cats) == pytest.approx(expected)


# ── Compute + Ranking Tests ─────────────────────────────────────────


class TestComputeAllUnifiedScores:
    def test_computes_and_ranks(self, db_session, test_user, sales_user):
        from app.services.unified_score_service import compute_all_unified_scores

        month = date(2026, 2, 1)

        # Create AvailScoreSnapshots for both users
        buyer_snap = AvailScoreSnapshot(
            user_id=test_user.id,
            month=month,
            role_type="buyer",
            b1_score=8,
            b2_score=7,
            b3_score=6,
            b4_score=9,
            b5_score=5,
            o1_score=7,
            o2_score=8,
            o3_score=6,
            o4_score=9,
            o5_score=4,
            behavior_total=35,
            outcome_total=34,
            total_score=69,
        )
        sales_snap = AvailScoreSnapshot(
            user_id=sales_user.id,
            month=month,
            role_type="sales",
            b1_score=5,
            b2_score=6,
            b3_score=7,
            b4_score=4,
            b5_score=8,
            o1_score=9,
            o2_score=3,
            o3_score=7,
            o4_score=6,
            o5_score=5,
            behavior_total=30,
            outcome_total=30,
            total_score=60,
        )
        db_session.add_all([buyer_snap, sales_snap])
        db_session.commit()

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db_session, month)

        assert result["computed"] == 2
        assert result["saved"] == 2

        snaps = db_session.query(UnifiedScoreSnapshot).filter_by(month=month).order_by(UnifiedScoreSnapshot.rank).all()
        assert len(snaps) == 2
        assert snaps[0].rank == 1
        assert snaps[1].rank == 2
        assert snaps[0].unified_score > snaps[1].unified_score

    def test_trader_gets_averaged(self, db_session, trader_user):
        from app.services.unified_score_service import compute_all_unified_scores

        month = date(2026, 2, 1)

        buyer_snap = AvailScoreSnapshot(
            user_id=trader_user.id,
            month=month,
            role_type="buyer",
            b1_score=10,
            b2_score=10,
            b3_score=10,
            b4_score=10,
            b5_score=10,
            o1_score=10,
            o2_score=10,
            o3_score=10,
            o4_score=10,
            o5_score=10,
            behavior_total=50,
            outcome_total=50,
            total_score=100,
        )
        sales_snap = AvailScoreSnapshot(
            user_id=trader_user.id,
            month=month,
            role_type="sales",
            b1_score=0,
            b2_score=0,
            b3_score=0,
            b4_score=0,
            b5_score=0,
            o1_score=0,
            o2_score=0,
            o3_score=0,
            o4_score=0,
            o5_score=0,
            behavior_total=0,
            outcome_total=0,
            total_score=0,
        )
        db_session.add_all([buyer_snap, sales_snap])
        db_session.commit()

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db_session, month)

        snap = db_session.query(UnifiedScoreSnapshot).filter_by(user_id=trader_user.id, month=month).first()
        assert snap is not None
        assert snap.primary_role == "trader"
        # All buyer cats are 100%, all sales cats are 0%, so average = 50%
        assert snap.prospecting_pct == 0.0  # prospecting removed, always zeroed
        assert snap.execution_pct == pytest.approx(50.0)
        assert snap.unified_score == pytest.approx(50.0)

    def test_no_data_skips_user(self, db_session, test_user):
        from app.services.unified_score_service import compute_all_unified_scores

        month = date(2026, 2, 1)

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db_session, month)

        assert result["computed"] == 0
        assert result["saved"] == 0

    def test_upsert_updates_existing(self, db_session, test_user):
        from app.services.unified_score_service import compute_all_unified_scores

        month = date(2026, 2, 1)

        # Create initial snapshot
        existing = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=10,
            rank=1,
        )
        db_session.add(existing)
        db_session.commit()
        existing_id = existing.id

        # Create avail data
        snap = AvailScoreSnapshot(
            user_id=test_user.id,
            month=month,
            role_type="buyer",
            b1_score=10,
            b2_score=10,
            b3_score=10,
            b4_score=10,
            b5_score=10,
            o1_score=10,
            o2_score=10,
            o3_score=10,
            o4_score=10,
            o5_score=10,
            behavior_total=50,
            outcome_total=50,
            total_score=100,
        )
        db_session.add(snap)
        db_session.commit()

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db_session, month)

        updated = db_session.get(UnifiedScoreSnapshot, existing_id)
        assert updated.unified_score == pytest.approx(100.0)


# ── Blurb Generation Tests ──────────────────────────────────────────


class TestGenerateBlurb:
    @patch("app.utils.claude_client.claude_structured")
    def test_returns_strength_and_improvement(self, mock_claude):
        from app.services.unified_score_service import _generate_blurb

        mock_claude.return_value = {
            "strength": "You excel at execution.",
            "improvement": "Focus on closing more deals.",
        }

        cats = {"execution": 60, "followthrough": 40, "closing": 30, "depth": 50}
        result = _generate_blurb("Test User", "buyer", cats, 55.0, 2, 5)

        assert result is not None
        assert result["strength"] == "You excel at execution."
        assert result["improvement"] == "Focus on closing more deals."

    @patch("app.utils.claude_client.claude_structured")
    def test_returns_none_on_failure(self, mock_claude):
        from app.services.unified_score_service import _generate_blurb

        mock_claude.side_effect = Exception("API error")

        cats = {"execution": 50, "followthrough": 50, "closing": 50, "depth": 50}
        result = _generate_blurb("Test User", "buyer", cats, 50.0, 1, 1)
        assert result is None


# ── Leaderboard Query Tests ──────────────────────────────────────────


class TestGetUnifiedLeaderboard:
    def test_returns_ordered_entries(self, db_session, test_user, sales_user):
        from app.services.unified_score_service import get_unified_leaderboard

        month = date(2026, 2, 1)

        snap1 = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=85,
            rank=1,
            primary_role="buyer",
            prospecting_pct=90,
            execution_pct=80,
            followthrough_pct=70,
            closing_pct=90,
            depth_pct=60,
        )
        snap2 = UnifiedScoreSnapshot(
            user_id=sales_user.id,
            month=month,
            unified_score=65,
            rank=2,
            primary_role="sales",
            prospecting_pct=60,
            execution_pct=70,
            followthrough_pct=50,
            closing_pct=70,
            depth_pct=40,
        )
        db_session.add_all([snap1, snap2])
        db_session.commit()

        result = get_unified_leaderboard(db_session, month)
        assert result["month"] == "2026-02-01"
        assert len(result["entries"]) == 2
        assert result["entries"][0]["rank"] == 1
        assert result["entries"][0]["user_name"] == "Test Buyer"
        assert result["entries"][1]["rank"] == 2

    def test_empty_month(self, db_session):
        from app.services.unified_score_service import get_unified_leaderboard

        result = get_unified_leaderboard(db_session, date(2026, 1, 1))
        assert result["entries"] == []

    def test_includes_multiplier_breakdown(self, db_session, test_user):
        from app.services.unified_score_service import get_unified_leaderboard

        month = date(2026, 2, 1)

        snap = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=75,
            rank=1,
            primary_role="buyer",
            execution_pct=70,
            followthrough_pct=80,
            closing_pct=90,
            depth_pct=60,
        )
        # Add avail score data
        avail = AvailScoreSnapshot(
            user_id=test_user.id,
            month=month,
            role_type="buyer",
            b1_score=8,
            b2_score=7,
            b3_score=6,
            b4_score=9,
            b5_score=5,
            b1_label="Speed",
            b2_label="Multi",
            b3_label="Follow",
            b4_label="Hygiene",
            b5_label="Stock",
            o1_score=7,
            o2_score=8,
            o3_score=6,
            o4_score=9,
            o5_score=4,
            o1_label="Ratio",
            o2_label="OfferQuote",
            o3_label="WinRate",
            o4_label="BPComp",
            o5_label="Diversity",
            behavior_total=35,
            outcome_total=34,
            total_score=69,
        )
        # Add multiplier score data
        mult = MultiplierScoreSnapshot(
            user_id=test_user.id,
            month=month,
            role_type="buyer",
            offer_points=18,
            bonus_points=5,
            total_points=23,
            offers_base_count=10,
            offers_base_pts=2,
            offers_quoted_count=4,
            offers_quoted_pts=12,
            offers_bp_count=1,
            offers_bp_pts=5,
            offers_po_count=0,
            offers_po_pts=0,
            rfqs_sent_count=20,
            rfqs_sent_pts=5,
            stock_lists_count=0,
            stock_lists_pts=0,
            qualified=True,
            bonus_amount=500,
        )
        db_session.add_all([snap, avail, mult])
        db_session.commit()

        result = get_unified_leaderboard(db_session, month)
        entry = result["entries"][0]

        # Verify role-prefixed avail fields
        assert entry["buyer_b1_score"] == 8
        assert entry["buyer_b1_label"] == "Speed"
        assert entry["buyer_behavior_total"] == 35

        # Verify multiplier breakdown
        assert "buyer_breakdown" in entry
        bd = entry["buyer_breakdown"]
        assert bd["offers_base"] == 10
        assert bd["pts_base"] == 2
        assert bd["offers_quoted"] == 4
        assert bd["pts_quoted"] == 12
        assert bd["rfqs_sent"] == 20

        # Verify convenience top-level fields
        assert entry["total_points"] == 23
        assert entry["avail_score"] == 69  # behavior_total + outcome_total
        assert entry["avail_qualified"] is True


# ── Scoring Info Tests ───────────────────────────────────────────────


class TestGetScoringInfo:
    def test_returns_all_categories(self):
        from app.services.unified_score_service import get_scoring_info

        info = get_scoring_info()
        assert len(info["categories"]) == 4
        names = [c["name"] for c in info["categories"]]
        assert "Prospecting" not in names
        total_weight = sum(c["weight"] for c in info["categories"])
        assert total_weight == 100
        assert info["total_range"] == "0-100"

    def test_returns_avail_score_detail(self):
        from app.services.unified_score_service import get_scoring_info

        info = get_scoring_info()
        assert "avail_score" in info
        assert len(info["avail_score"]["buyer_behaviors"]) == 5
        assert len(info["avail_score"]["sales_outcomes"]) == 5

    def test_returns_multiplier_points_detail(self):
        from app.services.unified_score_service import get_scoring_info

        info = get_scoring_info()
        assert "multiplier_points" in info
        assert "buyer_tiers" in info["multiplier_points"]
        assert "sales_tiers" in info["multiplier_points"]

    def test_returns_bonus_tiers(self):
        from app.services.unified_score_service import get_scoring_info

        info = get_scoring_info()
        assert "bonus_tiers" in info
        assert "$500" in info["bonus_tiers"]["prizes"]


# ── Model Tests ──────────────────────────────────────────────────────


class TestUnifiedScoreSnapshotModel:
    def test_create_snapshot(self, db_session, test_user):
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=date(2026, 2, 1),
            prospecting_pct=75.5,
            execution_pct=60.0,
            followthrough_pct=80.0,
            closing_pct=55.0,
            depth_pct=40.0,
            unified_score=64.5,
            rank=1,
            primary_role="buyer",
        )
        db_session.add(snap)
        db_session.commit()
        db_session.refresh(snap)
        assert snap.id is not None
        assert snap.unified_score == 64.5

    def test_unique_constraint(self, db_session, test_user):
        month = date(2026, 2, 1)
        snap1 = UnifiedScoreSnapshot(user_id=test_user.id, month=month)
        db_session.add(snap1)
        db_session.commit()

        snap2 = UnifiedScoreSnapshot(user_id=test_user.id, month=month)
        db_session.add(snap2)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()


# ── Coverage Gap Tests ──────────────────────────────────────────────


class TestSafePctEdge:
    """Cover line 93: _safe_pct when max_raw <= 0."""

    def test_max_raw_zero_returns_zero(self):
        from app.services.unified_score_service import _safe_pct

        assert _safe_pct(10, 0) == 0.0

    def test_max_raw_negative_returns_zero(self):
        from app.services.unified_score_service import _safe_pct

        assert _safe_pct(5, -3) == 0.0


class TestMultiplierIndexPopulation:
    """Cover line 148: mult_idx population and multiplier_points in result."""

    def test_compute_with_multiplier_data(self, db_session, test_user):
        from app.services.unified_score_service import compute_all_unified_scores

        month = date(2026, 2, 1)

        # Create AvailScoreSnapshot
        avail = AvailScoreSnapshot(
            user_id=test_user.id,
            month=month,
            role_type="buyer",
            b1_score=8,
            b2_score=7,
            b3_score=6,
            b4_score=9,
            b5_score=5,
            o1_score=7,
            o2_score=8,
            o3_score=6,
            o4_score=9,
            o5_score=4,
            behavior_total=35,
            outcome_total=34,
            total_score=69,
        )
        # Create MultiplierScoreSnapshot
        mult = MultiplierScoreSnapshot(
            user_id=test_user.id,
            month=month,
            role_type="buyer",
            offer_points=18,
            bonus_points=5,
            total_points=23,
            offers_base_count=10,
            offers_base_pts=2,
            offers_quoted_count=4,
            offers_quoted_pts=12,
            offers_bp_count=1,
            offers_bp_pts=5,
            offers_po_count=0,
            offers_po_pts=0,
            rfqs_sent_count=20,
            rfqs_sent_pts=5,
            stock_lists_count=0,
            stock_lists_pts=0,
            qualified=True,
            bonus_amount=500,
        )
        db_session.add_all([avail, mult])
        db_session.commit()

        with patch("app.services.unified_score_service._refresh_blurbs"):
            result = compute_all_unified_scores(db_session, month)

        assert result["computed"] == 1
        assert result["saved"] == 1

        # Verify the UnifiedScoreSnapshot has multiplier_points_buyer populated
        snap = (
            db_session.query(UnifiedScoreSnapshot)
            .filter_by(
                user_id=test_user.id,
                month=month,
            )
            .first()
        )
        assert snap is not None
        assert snap.multiplier_points_buyer == 23


class TestRefreshBlurbs:
    """Cover lines 242-267: _refresh_blurbs iteration, fresh check, blurb save, exception."""

    def test_sets_blurb_fields(self, db_session, test_user):
        """Verify _refresh_blurbs calls _generate_blurb and sets snap fields."""
        from app.services.unified_score_service import _refresh_blurbs

        month = date(2026, 2, 1)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=75,
            rank=1,
            primary_role="buyer",
        )
        db_session.add(snap)
        db_session.commit()

        results = [
            {
                "user_id": test_user.id,
                "user_name": "Test Buyer",
                "primary_role": "buyer",
                "cats": {"execution": 70, "followthrough": 80, "closing": 90, "depth": 60},
                "score": 75.0,
                "rank": 1,
            }
        ]

        blurb_return = {"strength": "Great execution!", "improvement": "Work on depth."}
        with patch("app.services.unified_score_service._generate_blurb", return_value=blurb_return):
            _refresh_blurbs(db_session, month, results)

        db_session.refresh(snap)
        assert snap.ai_blurb_strength == "Great execution!"
        assert snap.ai_blurb_improvement == "Work on depth."
        assert snap.ai_blurb_generated_at is not None

    def test_stale_blurb_gets_refreshed(self, db_session, test_user):
        """Verify blurb is refreshed when blurb_generated_at is older than 2 hours."""
        from app.services.unified_score_service import _refresh_blurbs

        month = date(2026, 2, 1)
        # SQLite strips timezone info, so use naive UTC to match cutoff comparison
        stale_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=75,
            rank=1,
            primary_role="buyer",
            ai_blurb_strength="Old strength",
            ai_blurb_improvement="Old improvement",
            ai_blurb_generated_at=stale_time,
        )
        db_session.add(snap)
        db_session.commit()

        results = [
            {
                "user_id": test_user.id,
                "user_name": "Test Buyer",
                "primary_role": "buyer",
                "cats": {"execution": 70, "followthrough": 80, "closing": 90, "depth": 60},
                "score": 75.0,
                "rank": 1,
            }
        ]

        blurb_return = {"strength": "New strength!", "improvement": "New improvement!"}
        # Patch datetime.now inside the service so cutoff is also naive
        fake_now = datetime.now(timezone.utc).replace(tzinfo=None)
        with (
            patch("app.services.unified_score_service.datetime") as mock_dt,
            patch("app.services.unified_score_service._generate_blurb", return_value=blurb_return),
        ):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            _refresh_blurbs(db_session, month, results)

        db_session.refresh(snap)
        assert snap.ai_blurb_strength == "New strength!"
        assert snap.ai_blurb_improvement == "New improvement!"

    def test_fresh_blurb_not_refreshed(self, db_session, test_user):
        """Verify blurb is NOT refreshed when ai_blurb_generated_at < 2 hours old."""
        from app.services.unified_score_service import _refresh_blurbs

        month = date(2026, 2, 1)
        # SQLite strips timezone info, so use naive UTC to match cutoff comparison
        fresh_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=75,
            rank=1,
            primary_role="buyer",
            ai_blurb_strength="Keep this",
            ai_blurb_improvement="Keep this too",
            ai_blurb_generated_at=fresh_time,
        )
        db_session.add(snap)
        db_session.commit()

        results = [
            {
                "user_id": test_user.id,
                "user_name": "Test Buyer",
                "primary_role": "buyer",
                "cats": {"execution": 70, "followthrough": 80, "closing": 90, "depth": 60},
                "score": 75.0,
                "rank": 1,
            }
        ]

        # Patch datetime.now inside the service so cutoff is also naive
        fake_now = datetime.now(timezone.utc).replace(tzinfo=None)
        with (
            patch("app.services.unified_score_service.datetime") as mock_dt,
            patch("app.services.unified_score_service._generate_blurb") as mock_gen,
        ):
            mock_dt.now.return_value = fake_now
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            _refresh_blurbs(db_session, month, results)
            mock_gen.assert_not_called()

        db_session.refresh(snap)
        assert snap.ai_blurb_strength == "Keep this"
        assert snap.ai_blurb_improvement == "Keep this too"

    def test_generate_blurb_exception_caught(self, db_session, test_user):
        """Verify _refresh_blurbs catches exception from _generate_blurb, snap
        unchanged."""
        from app.services.unified_score_service import _refresh_blurbs

        month = date(2026, 2, 1)
        snap = UnifiedScoreSnapshot(
            user_id=test_user.id,
            month=month,
            unified_score=75,
            rank=1,
            primary_role="buyer",
        )
        db_session.add(snap)
        db_session.commit()

        results = [
            {
                "user_id": test_user.id,
                "user_name": "Test Buyer",
                "primary_role": "buyer",
                "cats": {"execution": 70, "followthrough": 80, "closing": 90, "depth": 60},
                "score": 75.0,
                "rank": 1,
            }
        ]

        with patch(
            "app.services.unified_score_service._generate_blurb",
            side_effect=RuntimeError("AI service down"),
        ):
            _refresh_blurbs(db_session, month, results)

        db_session.refresh(snap)
        assert snap.ai_blurb_strength is None
        assert snap.ai_blurb_improvement is None

    def test_snap_not_found_skips(self, db_session, test_user):
        """Verify _refresh_blurbs skips when UnifiedScoreSnapshot not found for user."""
        from app.services.unified_score_service import _refresh_blurbs

        month = date(2026, 2, 1)
        # No UnifiedScoreSnapshot created — result references non-existent snap
        results = [
            {
                "user_id": test_user.id,
                "user_name": "Test Buyer",
                "primary_role": "buyer",
                "cats": {"execution": 70, "followthrough": 80, "closing": 90, "depth": 60},
                "score": 75.0,
                "rank": 1,
            }
        ]

        with patch("app.services.unified_score_service._generate_blurb") as mock_gen:
            _refresh_blurbs(db_session, month, results)
            mock_gen.assert_not_called()


class TestGenerateBlurbEventLoop:
    """Cover lines 314-320: ThreadPoolExecutor path when event loop is running."""

    @patch("app.utils.claude_client.claude_structured")
    def test_running_event_loop_uses_thread_pool(self, mock_claude):
        from app.services.unified_score_service import _generate_blurb

        mock_claude.return_value = {
            "strength": "Thread pool strength.",
            "improvement": "Thread pool improvement.",
        }

        cats = {"execution": 60, "followthrough": 40, "closing": 30, "depth": 50}

        with (
            patch("asyncio.get_running_loop", return_value=MagicMock()),
            patch("concurrent.futures.ThreadPoolExecutor") as mock_executor_cls,
        ):
            # Set up the ThreadPoolExecutor context manager mock
            mock_pool = MagicMock()
            mock_executor_cls.return_value.__enter__ = MagicMock(return_value=mock_pool)
            mock_executor_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_future = MagicMock()
            mock_future.result.return_value = {
                "strength": "Thread pool strength.",
                "improvement": "Thread pool improvement.",
            }
            mock_pool.submit.return_value = mock_future

            result = _generate_blurb("Test User", "buyer", cats, 55.0, 2, 5)

        assert result is not None
        assert result["strength"] == "Thread pool strength."
        assert result["improvement"] == "Thread pool improvement."
        mock_pool.submit.assert_called_once()
        mock_future.result.assert_called_once_with(timeout=30)


class TestLeaderboardSalesBreakdown:
    """Cover lines 425-426: sales role MultiplierScoreSnapshot breakdown (else branch)."""

    def test_sales_breakdown_fields(self, db_session, sales_user):
        from app.services.unified_score_service import get_unified_leaderboard

        month = date(2026, 2, 1)

        snap = UnifiedScoreSnapshot(
            user_id=sales_user.id,
            month=month,
            unified_score=70,
            rank=1,
            primary_role="sales",
            execution_pct=70,
            followthrough_pct=60,
            closing_pct=80,
            depth_pct=50,
        )
        avail = AvailScoreSnapshot(
            user_id=sales_user.id,
            month=month,
            role_type="sales",
            b1_score=5,
            b2_score=6,
            b3_score=7,
            b4_score=4,
            b5_score=8,
            o1_score=9,
            o2_score=3,
            o3_score=7,
            o4_score=6,
            o5_score=5,
            behavior_total=30,
            outcome_total=30,
            total_score=60,
        )
        mult = MultiplierScoreSnapshot(
            user_id=sales_user.id,
            month=month,
            role_type="sales",
            offer_points=20,
            bonus_points=10,
            total_points=30,
            quotes_sent_count=15,
            quotes_sent_pts=30,
            quotes_won_count=5,
            quotes_won_pts=40,
            proactive_sent_count=8,
            proactive_sent_pts=8,
            proactive_converted_count=3,
            proactive_converted_pts=12,
            new_accounts_count=2,
            new_accounts_pts=6,
            qualified=True,
            bonus_amount=250,
        )
        db_session.add_all([snap, avail, mult])
        db_session.commit()

        result = get_unified_leaderboard(db_session, month)
        entry = result["entries"][0]

        # Verify sales breakdown (else branch at line 425)
        assert "sales_breakdown" in entry
        bd = entry["sales_breakdown"]
        assert bd["quotes_sent"] == 15
        assert bd["pts_quote_sent"] == 30
        assert bd["quotes_won"] == 5
        assert bd["pts_quote_won"] == 40
        assert bd["proactive_sent"] == 8
        assert bd["pts_proactive_sent"] == 8
        assert bd["proactive_converted"] == 3
        assert bd["pts_proactive_converted"] == 12
        assert bd["new_accounts"] == 2
        assert bd["pts_accounts"] == 6

        # Verify sales convenience fields
        assert entry["total_points"] == 30
        assert entry["avail_score"] == 60  # behavior_total + outcome_total
        assert entry["avail_qualified"] is True


class TestLeaderboardTraderConvenience:
    """Cover lines 444-446: trader total_points sums both roles, avail_score from snap."""

    def test_trader_sums_both_roles_points(self, db_session, trader_user):
        from app.services.unified_score_service import get_unified_leaderboard

        month = date(2026, 2, 1)

        snap = UnifiedScoreSnapshot(
            user_id=trader_user.id,
            month=month,
            unified_score=65,
            rank=1,
            primary_role="trader",
            execution_pct=60,
            followthrough_pct=50,
            closing_pct=70,
            depth_pct=40,
            avail_score_buyer=69,
            avail_score_sales=55,
            multiplier_points_buyer=23,
            multiplier_points_sales=30,
        )
        # Both buyer and sales multiplier data
        buyer_mult = MultiplierScoreSnapshot(
            user_id=trader_user.id,
            month=month,
            role_type="buyer",
            offer_points=18,
            bonus_points=5,
            total_points=23,
            offers_base_count=10,
            offers_base_pts=2,
            offers_quoted_count=4,
            offers_quoted_pts=12,
            offers_bp_count=1,
            offers_bp_pts=5,
            offers_po_count=0,
            offers_po_pts=0,
            rfqs_sent_count=20,
            rfqs_sent_pts=5,
            stock_lists_count=0,
            stock_lists_pts=0,
            qualified=True,
            bonus_amount=500,
        )
        sales_mult = MultiplierScoreSnapshot(
            user_id=trader_user.id,
            month=month,
            role_type="sales",
            offer_points=20,
            bonus_points=10,
            total_points=30,
            quotes_sent_count=15,
            quotes_sent_pts=30,
            quotes_won_count=5,
            quotes_won_pts=40,
            proactive_sent_count=8,
            proactive_sent_pts=8,
            proactive_converted_count=3,
            proactive_converted_pts=12,
            new_accounts_count=2,
            new_accounts_pts=6,
            qualified=False,
            bonus_amount=0,
        )
        db_session.add_all([snap, buyer_mult, sales_mult])
        db_session.commit()

        result = get_unified_leaderboard(db_session, month)
        entry = result["entries"][0]

        # Trader total_points sums both roles (line 445)
        assert entry["total_points"] == 23 + 30  # buyer + sales

        # Trader avail_score uses avail_score_buyer (line 446)
        assert entry["avail_score"] == 69

    def test_trader_avail_score_falls_back_to_sales(self, db_session, trader_user):
        """When avail_score_buyer is None, falls back to avail_score_sales."""
        from app.services.unified_score_service import get_unified_leaderboard

        month = date(2026, 2, 1)

        snap = UnifiedScoreSnapshot(
            user_id=trader_user.id,
            month=month,
            unified_score=55,
            rank=1,
            primary_role="trader",
            execution_pct=50,
            followthrough_pct=50,
            closing_pct=60,
            depth_pct=40,
            avail_score_buyer=None,
            avail_score_sales=42,
        )
        db_session.add(snap)
        db_session.commit()

        result = get_unified_leaderboard(db_session, month)
        entry = result["entries"][0]

        # Falls back to avail_score_sales
        assert entry["avail_score"] == 42
        # No multiplier data, so total_points = 0+0
        assert entry["total_points"] == 0
