"""Performance Tracking — re-export façade.

Split into domain modules:
  - vendor_scorecard: 6 metrics over 90-day rolling window
  - buyer_leaderboard: multiplier scoring with grace period + stock list dedup
  - salesperson_scorecard: 12 activity metrics (monthly + YTD)

All public names re-exported here for backward compatibility.
"""

# ── Vendor Scorecard ──────────────────────────────────────────────────
# ── Buyer Leaderboard ────────────────────────────────────────────────
from app.services.buyer_leaderboard import (  # noqa: F401
    GRACE_DAYS,
    PTS_BUYPLAN,
    PTS_LOGGED,
    PTS_PO_CONFIRMED,
    PTS_QUOTED,
    PTS_STOCK_LIST,
    check_and_record_stock_list,
    compute_buyer_leaderboard,
    compute_stock_list_hash,
    get_buyer_leaderboard,
    get_buyer_leaderboard_months,
)

# ── Salesperson Scorecard ────────────────────────────────────────────
from app.services.salesperson_scorecard import (  # noqa: F401
    _salesperson_metrics,
    _salesperson_metrics_batch,
    get_salesperson_scorecard,
)
from app.services.vendor_scorecard import (  # noqa: F401
    COLD_START_THRESHOLD,
    VENDOR_WINDOW_DAYS,
    W_PO_CONVERSION,
    W_QUOTE_CONVERSION,
    W_RESPONSE_RATE,
    W_REVIEW_RATING,
    _compute_composite,
    compute_all_vendor_scorecards,
    compute_vendor_scorecard,
    get_vendor_scorecard_detail,
    get_vendor_scorecard_list,
)
