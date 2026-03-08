"""Dashboard router package — split from monolithic dashboard.py.

Provides:
- /api/dashboard/needs-attention — stale accounts needing outreach
- /api/dashboard/attention-feed — unified prioritized attention list
- /api/dashboard/morning-brief — AI-generated daily summary
- /api/dashboard/hot-offers — recent vendor offers
- /api/dashboard/buyer-brief — buyer command center data
- /api/dashboard/team-leaderboard — combined scoring leaderboard
- /api/dashboard/reactivation-signals — dormant account signals
- /api/dashboard/unified-leaderboard — cross-role leaderboard
- /api/dashboard/scoring-info — scoring system explanation

Called by: app/static/app.js (loadDashboard)
Depends on: models/crm.py, models/intelligence.py, models/quotes.py
"""

from fastapi import APIRouter

# Re-export helpers and endpoint functions for backward compatibility
# (tests import these directly from app.routers.dashboard)
from ._shared import _age_label, _ensure_aware  # noqa: F401
from .briefs import router as briefs_router
from .leaderboard import router as leaderboard_router
from .overview import needs_attention  # noqa: F401
from .overview import router as overview_router

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
router.include_router(overview_router)
router.include_router(briefs_router)
router.include_router(leaderboard_router)
