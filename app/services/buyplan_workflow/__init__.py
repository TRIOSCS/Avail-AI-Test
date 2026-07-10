"""Buy Plan — Workflow package: submit, approve, verify, complete, intelligence.

Phase 4: Approval + Execution — submit, approve, verify SO/PO, flag issues,
         auto-complete, favoritism detection, case reports.

P4.3 split of the former monolithic `buyplan_workflow.py` (1,855 lines) into a
package along its audited seams:
  - `buyplan_approval` — submit/approve/reject, halt/resume, reset/cancel/resubmit,
    and auto-completion (one module: every transition shares the same engine-
    request/prepayment teardown helpers).
  - `buyplan_po` — buyer PO confirmation + approver PO verification (sync scan).
  - `buyplan_lines` — claim/flag/resolve, re-source, and the add/edit/remove/SO#
    line-editing API.
  - `buyplan_reports` — favoritism detection + case-report generation.

This `__init__.py` re-exports every public name AND every internal name that
production code or tests reach via ``app.services.buyplan_workflow.<name>`` so no
caller needs to change its import path.

Called by: routers/htmx/buy_plans.py, routers/htmx_views.py, routers/prepayments.py,
    services/buyplan_service.py, services/buyplan_hub.py, services/approvals/service.py,
    jobs/inventory_jobs.py, startup.py
Depends on: buyplan_scoring, buyplan_builder, models, config
"""

from .buyplan_approval import (
    HALTABLE_STATUSES,
    RESUBMITTABLE_STATUSES,
    _apply_line_edits,
    _apply_line_overrides,
    _can_halt,
    _cancel_open_engine_requests_for_plan,
    _cancel_open_prepayment_requests_for_plan,
    _complete_plan,
    _generate_buyer_tasks,
    _has_open_po_gate,
    _is_stock_sale,
    _log_approval_activity,
    _open_engine_request_for_plan,
    _recalculate_financials,
    _run_approve_side_effects,
    _run_reject_side_effects,
    approve_buy_plan,
    cancel_buy_plan,
    check_completion,
    halt_plan,
    plan_needs_approver_reason,
    reset_buy_plan_to_draft,
    resubmit_buy_plan,
    resume_plan,
    submit_buy_plan,
)
from .buyplan_lines import (
    _LOCKED_EDIT_STATUSES,
    _MANAGER_ONLY_EDIT_STATUSES,
    RESOURCEABLE_LINE_STATUSES,
    _ensure_can_edit_lines,
    _has_cut_po,
    _line_margin_pct,
    _owns_plan,
    add_buy_plan_line,
    can_edit_buy_plan_lines,
    claim_line,
    edit_buy_plan_line,
    flag_line_issue,
    remove_buy_plan_line,
    resolve_line_issue,
    resource_line,
    set_sales_order_number,
)
from .buyplan_po import (
    _line_amount,
    _log_po_line_activity,
    confirm_po,
    verify_po,
    verify_po_sent,
)
from .buyplan_reports import detect_favoritism, generate_case_report

__all__ = [
    "HALTABLE_STATUSES",
    "RESOURCEABLE_LINE_STATUSES",
    "RESUBMITTABLE_STATUSES",
    "_LOCKED_EDIT_STATUSES",
    "_MANAGER_ONLY_EDIT_STATUSES",
    "_apply_line_edits",
    "_apply_line_overrides",
    "_can_halt",
    "_cancel_open_engine_requests_for_plan",
    "_cancel_open_prepayment_requests_for_plan",
    "_complete_plan",
    "_ensure_can_edit_lines",
    "_generate_buyer_tasks",
    "_has_cut_po",
    "_has_open_po_gate",
    "_is_stock_sale",
    "_line_amount",
    "_line_margin_pct",
    "_log_approval_activity",
    "_log_po_line_activity",
    "_open_engine_request_for_plan",
    "_owns_plan",
    "_recalculate_financials",
    "_run_approve_side_effects",
    "_run_reject_side_effects",
    "add_buy_plan_line",
    "approve_buy_plan",
    "can_edit_buy_plan_lines",
    "cancel_buy_plan",
    "check_completion",
    "claim_line",
    "confirm_po",
    "detect_favoritism",
    "edit_buy_plan_line",
    "flag_line_issue",
    "generate_case_report",
    "halt_plan",
    "plan_needs_approver_reason",
    "remove_buy_plan_line",
    "reset_buy_plan_to_draft",
    "resolve_line_issue",
    "resource_line",
    "resubmit_buy_plan",
    "resume_plan",
    "set_sales_order_number",
    "submit_buy_plan",
    "verify_po",
    "verify_po_sent",
]
