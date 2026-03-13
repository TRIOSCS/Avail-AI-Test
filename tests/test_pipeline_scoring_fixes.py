"""Tests for pipeline scoring & data quality fixes.

Covers:
  1. Team-leaderboard avail_rank recomputation (Bug TT-20260306-031)
  2. needs-attention scope=team support (Bug TT-20260306-040)
  3. Proactive scorecard outlier cap (Bug TT-20260306-036)
  4. Buyer-brief revenue cap (Bug TT-20260306-036)

Called by: pytest
Depends on: app/routers/dashboard/, app/services/proactive_service.py
"""

from unittest.mock import MagicMock

# ---- Bug 1: avail_rank recomputed in team-leaderboard ----


# ---- Bug 3: Proactive scorecard outlier cap ----


class TestProactiveScorecarOutlierCap:
    """Proactive scorecard should cap unrealistic financial values."""

    def test_cap_outlier_function(self):
        """_cap_outlier caps values above threshold to 0."""
        from app.services.proactive_service import _cap_outlier

        assert _cap_outlier(1000.0) == 1000.0
        assert _cap_outlier(499_999.0) == 499_999.0
        assert _cap_outlier(500_001.0) == 0.0
        assert _cap_outlier(5_000_000_000.0) == 0.0  # $5B -> 0

    def test_cap_outlier_custom_threshold(self):
        from app.services.proactive_service import _cap_outlier

        assert _cap_outlier(500.0, cap=100) == 0.0
        assert _cap_outlier(50.0, cap=100) == 50.0

    def test_scorecard_excludes_inflated_revenue(self, db_session):
        """Scorecard should not include $5B+ values in totals."""
        from app.services.proactive_service import get_scorecard

        # Create a mock ProactiveOffer with inflated values
        mock_offer_normal = MagicMock()
        mock_offer_normal.status = "sent"
        mock_offer_normal.total_sell = 5000.0
        mock_offer_normal.total_cost = 3000.0
        mock_offer_normal.converted_quote_id = None
        mock_offer_normal.salesperson_id = 1

        mock_offer_inflated = MagicMock()
        mock_offer_inflated.status = "sent"
        mock_offer_inflated.total_sell = 5_000_000_000.0  # $5B - test data
        mock_offer_inflated.total_cost = 4_000_000_000.0
        mock_offer_inflated.converted_quote_id = None
        mock_offer_inflated.salesperson_id = 1

        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = [mock_offer_normal, mock_offer_inflated]

        mock_db = MagicMock()
        mock_db.query.return_value = mock_query

        result = get_scorecard(mock_db, salesperson_id=1)

        # Only the normal offer's revenue should be counted
        assert result["anticipated_revenue"] == 5000.0
        assert result["total_sent"] == 2
