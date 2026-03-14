"""Buy Plan Service Layer — re-export façade.

Split into domain modules:
  - buyplan_scoring: offer scoring, lead time parsing, buyer assignment, routing maps
  - buyplan_builder: plan building, AI summary, AI flags
  - buyplan_workflow: submit, approve, verify, complete, intelligence

All public names re-exported here for backward compatibility.
"""

# Re-export settings for test patching compatibility
from app.config import settings  # noqa: F401

# ── Plan Building & AI ──────────────────────────────────────────────
from app.services.buyplan_builder import (  # noqa: F401
    _build_lines_for_requirement,
    _check_better_offer,
    _check_geo_mismatch,
    _check_quantity_gaps,
    _create_line,
    build_buy_plan,
    generate_ai_flags,
    generate_ai_summary,
)

# ── Scoring & Routing ────────────────────────────────────────────────
from app.services.buyplan_scoring import (  # noqa: F401
    W_GEOGRAPHY,
    W_LEAD_TIME,
    W_PRICE,
    W_RELIABILITY,
    W_TERMS,
    _country_to_region,
    _get_routing_maps,
    _parse_lead_time_days,
    assign_buyer,
    score_offer,
)

# ── Notifications ──────────────────────────────────────────────────
from app.services.buyplan_notifications import (  # noqa: F401
    log_buyplan_activity,
    notify_stock_sale_approved,
)

# ── Workflow & Intelligence ─────────────────────────────────────────
from app.services.buyplan_workflow import (  # noqa: F401
    _apply_line_edits,
    _apply_line_overrides,
    _is_stock_sale,
    _recalculate_financials,
    approve_buy_plan,
    check_completion,
    confirm_po,
    detect_favoritism,
    flag_line_issue,
    generate_case_report,
    reset_buy_plan_to_draft,
    resubmit_buy_plan,
    submit_buy_plan,
    verify_po,
    verify_po_sent,
    verify_so,
)
