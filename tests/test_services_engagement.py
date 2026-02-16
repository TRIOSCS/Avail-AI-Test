"""
test_services_engagement.py — Tests for engagement_scorer service.

Tests the pure scoring function and threshold/boundary logic.
No DB mocking needed for compute_engagement_score (pure function).

Called by: pytest
Depends on: app/services/engagement_scorer.py
"""

from datetime import datetime, timedelta, timezone

from app.services.engagement_scorer import (
    compute_engagement_score,
    W_RESPONSE_RATE,
    W_GHOST_RATE,
    W_RECENCY,
    W_VELOCITY,
    W_WIN_RATE,
    MIN_OUTREACH_FOR_SCORE,
    VELOCITY_IDEAL_HOURS,
    VELOCITY_MAX_HOURS,
    RECENCY_IDEAL_DAYS,
    RECENCY_MAX_DAYS,
)


NOW = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)


# ── Cold start / minimum outreach ──────────────────────────────────


class TestColdStart:
    def test_zero_outreach_returns_none(self):
        result = compute_engagement_score(0, 0, 0, None, None, now=NOW)
        assert result["engagement_score"] is None
        assert result["ghost_rate"] == 0

    def test_one_outreach_below_threshold(self):
        result = compute_engagement_score(1, 0, 0, None, None, now=NOW)
        assert result["engagement_score"] is None
        assert result["ghost_rate"] == 1.0  # 1 outreach, 0 responses

    def test_one_outreach_with_response(self):
        result = compute_engagement_score(1, 1, 0, None, None, now=NOW)
        assert result["engagement_score"] is None  # still below MIN_OUTREACH

    def test_exactly_min_outreach(self):
        result = compute_engagement_score(2, 1, 0, 2.0, NOW - timedelta(days=1), now=NOW)
        assert result["engagement_score"] is not None


# ── Response rate ───────────────────────────────────────────────────


class TestResponseRate:
    def test_perfect_response_rate(self):
        result = compute_engagement_score(10, 10, 0, None, None, now=NOW)
        assert result["response_rate"] == 1.0

    def test_half_response_rate(self):
        result = compute_engagement_score(10, 5, 0, None, None, now=NOW)
        assert result["response_rate"] == 0.5

    def test_zero_response_rate(self):
        result = compute_engagement_score(10, 0, 0, None, None, now=NOW)
        assert result["response_rate"] == 0.0

    def test_response_capped_at_1(self):
        """More responses than outreach (edge case) capped at 1.0."""
        result = compute_engagement_score(5, 10, 0, None, None, now=NOW)
        assert result["response_rate"] == 1.0


# ── Ghost rate ──────────────────────────────────────────────────────


class TestGhostRate:
    def test_all_responded_no_ghosts(self):
        result = compute_engagement_score(10, 10, 0, None, None, now=NOW)
        assert result["ghost_rate"] == 0.0

    def test_all_ghosted(self):
        result = compute_engagement_score(10, 0, 0, None, None, now=NOW)
        assert result["ghost_rate"] == 1.0

    def test_partial_ghost(self):
        result = compute_engagement_score(10, 3, 0, None, None, now=NOW)
        assert result["ghost_rate"] == 0.7


# ── Recency ─────────────────────────────────────────────────────────


class TestRecency:
    def test_very_recent_contact(self):
        """Within ideal window → 100."""
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=1), now=NOW
        )
        assert result["recency_score"] == 100.0

    def test_exactly_ideal_boundary(self):
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=RECENCY_IDEAL_DAYS), now=NOW
        )
        assert result["recency_score"] == 100.0

    def test_very_old_contact(self):
        """Beyond max → 0."""
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=400), now=NOW
        )
        assert result["recency_score"] == 0.0

    def test_midway_decay(self):
        """Halfway between ideal and max → ~50."""
        mid_days = (RECENCY_IDEAL_DAYS + RECENCY_MAX_DAYS) / 2
        result = compute_engagement_score(
            5, 3, 0, None, NOW - timedelta(days=mid_days), now=NOW
        )
        assert 40 <= result["recency_score"] <= 60

    def test_no_contact_zero_recency(self):
        result = compute_engagement_score(5, 3, 0, None, None, now=NOW)
        assert result["recency_score"] == 0.0

    def test_naive_datetime_handled(self):
        """Naive datetime (no tzinfo) should still work."""
        naive_dt = datetime(2026, 2, 14, 12, 0, 0)  # no tzinfo
        result = compute_engagement_score(5, 3, 0, None, naive_dt, now=NOW)
        assert result["recency_score"] == 100.0


# ── Velocity ────────────────────────────────────────────────────────


class TestVelocity:
    def test_instant_reply_perfect_velocity(self):
        result = compute_engagement_score(5, 3, 0, 1.0, None, now=NOW)
        assert result["velocity_score"] == 100.0

    def test_exactly_ideal_boundary(self):
        result = compute_engagement_score(5, 3, 0, VELOCITY_IDEAL_HOURS, None, now=NOW)
        assert result["velocity_score"] == 100.0

    def test_very_slow_reply(self):
        result = compute_engagement_score(5, 3, 0, 200.0, None, now=NOW)
        assert result["velocity_score"] == 0.0

    def test_midway_velocity(self):
        mid = (VELOCITY_IDEAL_HOURS + VELOCITY_MAX_HOURS) / 2
        result = compute_engagement_score(5, 3, 0, mid, None, now=NOW)
        assert 40 <= result["velocity_score"] <= 60

    def test_no_velocity_data(self):
        result = compute_engagement_score(5, 3, 0, None, None, now=NOW)
        assert result["velocity_score"] == 0.0


# ── Win rate ────────────────────────────────────────────────────────


class TestWinRate:
    def test_all_wins(self):
        result = compute_engagement_score(5, 5, 5, None, None, now=NOW)
        assert result["win_rate"] == 1.0

    def test_no_wins(self):
        result = compute_engagement_score(5, 5, 0, None, None, now=NOW)
        assert result["win_rate"] == 0.0

    def test_no_responses_no_wins(self):
        result = compute_engagement_score(5, 0, 0, None, None, now=NOW)
        assert result["win_rate"] == 0.0


# ── Composite score ─────────────────────────────────────────────────


class TestCompositeScore:
    def test_perfect_score(self):
        """100% on every metric → 100."""
        result = compute_engagement_score(
            total_outreach=10,
            total_responses=10,
            total_wins=10,
            avg_velocity_hours=1.0,
            last_contact_at=NOW - timedelta(hours=1),
            now=NOW,
        )
        assert result["engagement_score"] == 100.0

    def test_worst_score(self):
        """0% on every metric → 0."""
        result = compute_engagement_score(
            total_outreach=10,
            total_responses=0,
            total_wins=0,
            avg_velocity_hours=None,
            last_contact_at=None,
            now=NOW,
        )
        assert result["engagement_score"] == 0.0

    def test_score_range(self):
        """Score always 0-100."""
        result = compute_engagement_score(
            total_outreach=5,
            total_responses=3,
            total_wins=1,
            avg_velocity_hours=48.0,
            last_contact_at=NOW - timedelta(days=30),
            now=NOW,
        )
        score = result["engagement_score"]
        assert 0 <= score <= 100

    def test_weights_sum_to_one(self):
        total = W_RESPONSE_RATE + W_GHOST_RATE + W_RECENCY + W_VELOCITY + W_WIN_RATE
        assert abs(total - 1.0) < 0.001

    def test_rounding(self):
        """All returned values should be rounded."""
        result = compute_engagement_score(
            total_outreach=3,
            total_responses=2,
            total_wins=1,
            avg_velocity_hours=50.5,
            last_contact_at=NOW - timedelta(days=100),
            now=NOW,
        )
        assert isinstance(result["engagement_score"], float)
        # Check decimal places
        score_str = str(result["engagement_score"])
        if "." in score_str:
            assert len(score_str.split(".")[1]) <= 1
