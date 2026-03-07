"""Buy Plan V1 Service Layer — re-export façade.

Split into domain modules:
  - buyplan_notifications: email/Teams/in-app notifications, audit trail, background runner
  - buyplan_po: PO email verification, auto-complete stock sales

All public names re-exported here for backward compatibility.
"""

# Re-export settings for test patching compatibility
from app.config import settings  # noqa: F401

# ── Notifications & Lifecycle ─────────────────────────────────────────
from app.services.buyplan_notifications import (  # noqa: F401
    _post_teams_card,
    _send_teams_dm,
    log_buyplan_activity,
    notify_buyplan_approved,
    notify_buyplan_cancelled,
    notify_buyplan_completed,
    notify_buyplan_rejected,
    notify_buyplan_submitted,
    notify_stock_sale_approved,
    run_buyplan_bg,
)

# ── PO Verification & Auto-Complete ──────────────────────────────────
from app.services.buyplan_po import (  # noqa: F401
    auto_complete_stock_sales,
    verify_po_sent,
)
