"""buyplan_service.py — Buy Plan Service Layer re-export façade.

Split into domain modules:
  - buyplan_scoring: offer scoring, lead time parsing, buyer assignment, routing maps
  - buyplan_builder: plan building, AI summary, AI flags
  - buyplan_workflow: submit, approve, verify, complete, intelligence
  - buyplan_notifications: notification service for state transitions

All public names re-exported here for backward compatibility.

Called by: routers/htmx_views.py, tests
Depends on: buyplan_builder, buyplan_scoring, buyplan_workflow, buyplan_notifications
"""

# Re-export settings for test patching compatibility
from app.config import settings  # noqa: F401

# ── Plan Building & AI ──────────────────────────────────────────────
from app.services.buyplan_builder import (
    _build_lines_for_requirement,  # noqa: F401
    _check_better_offer,  # noqa: F401
    _check_geo_mismatch,  # noqa: F401
    _check_quantity_gaps,  # noqa: F401
    _create_line,  # noqa: F401
    build_buy_plan,  # noqa: F401
    generate_ai_flags,  # noqa: F401
    generate_ai_summary,  # noqa: F401
)

# ── Notifications ──────────────────────────────────────────────────
from app.services.buyplan_notifications import (
    log_buyplan_activity,  # noqa: F401
    notify_cancelled,  # noqa: F401
    notify_stock_sale_approved,  # noqa: F401
    run_v3_notify_bg,  # noqa: F401
)

# ── Scoring & Routing ────────────────────────────────────────────────
from app.services.buyplan_scoring import (
    W_GEOGRAPHY,  # noqa: F401
    W_LEAD_TIME,  # noqa: F401
    W_PRICE,  # noqa: F401
    W_RELIABILITY,  # noqa: F401
    W_TERMS,  # noqa: F401
    _country_to_region,  # noqa: F401
    _get_routing_maps,  # noqa: F401
    _parse_lead_time_days,  # noqa: F401
    assign_buyer,  # noqa: F401
    score_offer,  # noqa: F401
)

# ── Workflow & Intelligence ─────────────────────────────────────────
from app.services.buyplan_workflow import (
    _apply_line_edits,  # noqa: F401
    _apply_line_overrides,  # noqa: F401
    _is_stock_sale,  # noqa: F401
    _recalculate_financials,  # noqa: F401
    approve_buy_plan,  # noqa: F401
    cancel_buy_plan,  # noqa: F401
    check_completion,  # noqa: F401
    confirm_po,  # noqa: F401
    detect_favoritism,  # noqa: F401
    flag_line_issue,  # noqa: F401
    generate_case_report,  # noqa: F401
    halt_plan,  # noqa: F401
    reset_buy_plan_to_draft,  # noqa: F401
    resubmit_buy_plan,  # noqa: F401
    submit_buy_plan,  # noqa: F401
    verify_po,  # noqa: F401
    verify_po_sent,  # noqa: F401
)
