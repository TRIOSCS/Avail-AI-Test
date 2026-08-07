"""Approvals-hub package — W4.8 split of the former 1,250-line
routers/htmx/approvals_hub.py (Approvals Workspace, 3-tab split-view console).

One page, three tabs — Deals · Purchase Orders · Prepayments — all views on the same
pipeline rooted at the sales order (specs/approvals-workspace.md; W4.3 merged the old
Sales Orders + Buy Plans tabs into one Deals tab — they were two names for the same
rows, the same pane, and the same single approval).
Every tab is a split view: LEFT the work list (search, Mine/All, live/closed filter,
age on every row, "Needs your approval" grouped first, oldest default-selected), RIGHT
the detail pane with the action at the bottom. The approvals ENGINE is untouched —
decisions post the existing buy_plans.py / prepayments routes.

Legacy tab keys (sales-orders / buy-plans / buy-plan / po-approval / prepayment) alias
onto the new tabs so old pushed URLs and the existing origin=approvals_hub decide
re-renders keep working.
``render_tab_body`` is shared: the tab GET route and the decide handlers both call it
so a one-click decision re-renders the refreshed tab in place.

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it in the original
registration order (shell, panes, notes, worklist — hence the isort splits). Every
former top-level name is re-exported here so existing imports
(`from app.routers.htmx.approvals_hub import ...`) keep working; test PATCH targets
for submodule-local lookups, however, must point at the defining submodule (patching
a package attribute cannot intercept a submodule-local lookup).

Called by: app/main.py (router mount); routers/htmx/buy_plans.py + routers/prepayments.py
    (decide handlers' re-render branches).
Depends on: app.dependencies, app.database, app.services.approvals.{queue,po_queue},
    app.services.prepayment_service (read helpers), .._shared (_base_ctx), app.template_env.
"""

from .common import (  # noqa: F401
    _TAB_LABELS,
    _TABS,
    DEFAULT_TAB,
    LEGACY_TAB_ALIASES,
    ORDER_TYPE_LABELS,
    PO_DECISION_LABELS,
    _notes_ctx,
    _resolve_tab,
    router,
)

# Submodule imports stay in the original route-registration order (isort: split
# markers keep ruff from re-sorting the blocks).
# isort: split
from .shell import (  # noqa: F401
    _decidable_gate_counts,
    _po_waiting_on_viewer,
    _viewer_badges,
    approvals_hub_shell,
    approvals_hub_tab,
    render_tab_body,
)

# isort: split
from .panes import (  # noqa: F401
    _viewer_can_decide_plan,
    approvals_plan_pane,
    approvals_plan_qp_sales,
    approvals_po_pane,
    approvals_po_sent_check,
    approvals_prepayment_method,
    approvals_prepayment_pane,
    render_plan_pane,
    render_po_pane,
    render_prepayment_pane,
)

# isort: split
from .notes import (  # noqa: F401
    _render_notes_thread,
    _resolve_note_subject,
    approvals_add_attachment,
    approvals_add_note,
    approvals_remove_attachment,
)

# isort: split
from .worklist import (  # noqa: F401
    _CLOSED_LINE_STATUSES,
    _CLOSED_PLAN_STATUSES,
    _LIVE_LINE_STATUSES,
    _LIVE_PLAN_STATUSES,
    WorkspaceRow,
    _matches,
    _plan_rows,
    _po_line_row,
    _po_rows,
    _prepayment_rows,
    _selected_plan_row,
    approvals_workspace_list,
)
