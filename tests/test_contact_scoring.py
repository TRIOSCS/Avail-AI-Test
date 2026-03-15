"""test_contact_scoring.py — Tests for contact relationship scoring algorithm.

Tests the pure scoring functions and trend computation.
No DB needed for compute_contact_relationship_score (pure function).

Called by: pytest
Depends on: app/services/contact_intelligence.py
"""

from datetime import datetime, timedelta, timezone

from app.services.contact_intelligence import (
    W_CHANNEL_DIVERSITY,
    W_FREQUENCY,
    W_RECENCY,
    W_RESPONSIVENESS,
    W_WIN_RATE,
    _compute_trend,
    compute_contact_relationship_score,
    split_name,
)

NOW = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)


# ── split_name ─────────────────────────────────────────────────────


class TestSplitName:
    def test_simple_name(self):
        assert split_name("John Doe") == ("John", "Doe")

    def test_single_name(self):
        assert split_name("Madonna") == ("Madonna", None)

    def test_prefix_name(self):
        assert split_name("John van der Berg") == ("John", "van der Berg")

    def test_prefix_de(self):
        assert split_name("Maria de la Cruz") == ("Maria", "de la Cruz")

    def test_three_part_name(self):
        assert split_name("John Michael Smith") == ("John", "Michael Smith")

    def test_none(self):
        assert split_name(None) == (None, None)

    def test_empty(self):
        assert split_name("") == (None, None)

    def test_whitespace(self):
        assert split_name("   ") == (None, None)

    def test_leading_trailing_spaces(self):
        assert split_name("  John Doe  ") == ("John", "Doe")


# ── Recency scoring ────────────────────────────────────────────────


class TestRecencyScore:
    def test_recent_interaction(self):
        """Within 7 days = 100."""
        result = compute_contact_relationship_score(
            last_interaction_at=NOW - timedelta(days=1),
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["recency_score"] == 100.0

    def test_exactly_7_days(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW - timedelta(days=7),
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["recency_score"] == 100.0

    def test_old_interaction(self):
        """365+ days = 0."""
        result = compute_contact_relationship_score(
            last_interaction_at=NOW - timedelta(days=400),
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=10,
            distinct_channels=1,
            now=NOW,
        )
        assert result["recency_score"] == 0.0

    def test_no_interaction(self):
        result = compute_contact_relationship_score(
            last_interaction_at=None,
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=0,
            distinct_channels=0,
            now=NOW,
        )
        assert result["recency_score"] == 0.0

    def test_mid_recency(self):
        """~186 days should be roughly 50%."""
        result = compute_contact_relationship_score(
            last_interaction_at=NOW - timedelta(days=186),
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=10,
            distinct_channels=1,
            now=NOW,
        )
        assert 40 <= result["recency_score"] <= 60


# ── Frequency scoring ──────────────────────────────────────────────


class TestFrequencyScore:
    def test_high_frequency(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=15,
            interactions_60d=20,
            interactions_90d=25,
            avg_response_hours=None,
            wins=0,
            total_interactions=25,
            distinct_channels=1,
            now=NOW,
        )
        assert result["frequency_score"] == 100.0  # capped at 100

    def test_zero_frequency(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=0,
            distinct_channels=1,
            now=NOW,
        )
        assert result["frequency_score"] == 0.0

    def test_half_frequency(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["frequency_score"] == 50.0


# ── Responsiveness scoring ─────────────────────────────────────────


class TestResponsivenessScore:
    def test_fast_response(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=2.0,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["responsiveness_score"] == 100.0

    def test_slow_response(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=200.0,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["responsiveness_score"] == 0.0

    def test_unknown_response(self):
        """None defaults to neutral 50."""
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["responsiveness_score"] == 50.0


# ── Win rate scoring ───────────────────────────────────────────────


class TestWinRateScore:
    def test_good_win_rate(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=8,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert 50 <= result["win_rate_score"] <= 60

    def test_zero_wins(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert result["win_rate_score"] == 0.0

    def test_zero_interactions(self):
        result = compute_contact_relationship_score(
            last_interaction_at=None,
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=0,
            distinct_channels=0,
            now=NOW,
        )
        assert result["win_rate_score"] == 0.0


# ── Channel diversity scoring ──────────────────────────────────────


class TestChannelScore:
    def test_three_channels(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=3,
            now=NOW,
        )
        assert result["channel_score"] == 100.0

    def test_one_channel(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,
            wins=0,
            total_interactions=15,
            distinct_channels=1,
            now=NOW,
        )
        assert 30 <= result["channel_score"] <= 40

    def test_zero_channels(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=0,
            distinct_channels=0,
            now=NOW,
        )
        assert result["channel_score"] == 0.0


# ── Trend computation ──────────────────────────────────────────────


class TestComputeTrend:
    def test_all_zero_dormant(self):
        assert _compute_trend(0, 0, 0) == "dormant"

    def test_warming_from_zero(self):
        assert _compute_trend(5, 0, 0) == "warming"

    def test_warming_high_30d(self):
        """30d rate (5) > 1.5 * older rate ((10-5)/2 = 2.5) → warming."""
        assert _compute_trend(5, 7, 10) == "warming"

    def test_cooling(self):
        """30d rate (1) < 0.5 * older rate ((10-1)/2 = 4.5) → cooling."""
        assert _compute_trend(1, 5, 10) == "cooling"

    def test_stable(self):
        """30d rate (3) is between bounds of older rate ((9-3)/2 = 3) → stable."""
        assert _compute_trend(3, 6, 9) == "stable"

    def test_only_30d_activity(self):
        assert _compute_trend(3, 0, 0) == "warming"

    def test_equal_across_windows(self):
        assert _compute_trend(3, 3, 3) == "warming"


# ── Overall score integration ──────────────────────────────────────


class TestOverallScore:
    def test_perfect_score(self):
        """All metrics maxed out → high score."""
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=15,
            interactions_60d=30,
            interactions_90d=45,
            avg_response_hours=2.0,
            wins=10,
            total_interactions=20,
            distinct_channels=4,
            now=NOW,
        )
        assert result["relationship_score"] >= 80

    def test_worst_score(self):
        """All metrics zeroed → zero score."""
        result = compute_contact_relationship_score(
            last_interaction_at=None,
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=None,
            wins=0,
            total_interactions=0,
            distinct_channels=0,
            now=NOW,
        )
        # responsiveness defaults to 50, so score won't be exactly 0
        assert result["relationship_score"] <= 15

    def test_weights_sum_to_1(self):
        total = W_RECENCY + W_FREQUENCY + W_RESPONSIVENESS + W_WIN_RATE + W_CHANNEL_DIVERSITY
        assert abs(total - 1.0) < 0.001

    def test_returns_all_fields(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW,
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=4.0,
            wins=2,
            total_interactions=15,
            distinct_channels=2,
            now=NOW,
        )
        assert "relationship_score" in result
        assert "recency_score" in result
        assert "frequency_score" in result
        assert "responsiveness_score" in result
        assert "win_rate_score" in result
        assert "channel_score" in result
        assert "activity_trend" in result

    def test_score_bounded_0_100(self):
        result = compute_contact_relationship_score(
            last_interaction_at=NOW - timedelta(days=500),
            interactions_30d=0,
            interactions_60d=0,
            interactions_90d=0,
            avg_response_hours=500.0,
            wins=0,
            total_interactions=0,
            distinct_channels=0,
            now=NOW,
        )
        assert 0 <= result["relationship_score"] <= 100
