"""Tests for pipeline scoring & data quality fixes.

Covers:
  1. Team-leaderboard avail_rank recomputation (Bug TT-20260306-031)
  2. needs-attention scope=team support (Bug TT-20260306-040)
  3. Proactive scorecard outlier cap (Bug TT-20260306-036)
  4. Buyer-brief revenue cap (Bug TT-20260306-036)

Called by: pytest
Depends on: app/routers/dashboard/, app/services/proactive_service.py
"""

# Tests for _cap_outlier removed — capping is now done inline via SQL case()
# expressions in get_scorecard(). Behavior is covered by scorecard integration
# tests in test_proactive_service.py.
