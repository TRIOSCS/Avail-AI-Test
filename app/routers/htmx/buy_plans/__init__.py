"""Buy Plans package — W4.8 split of the former 1,543-line routers/htmx/buy_plans.py.

Server-rendered HTML partials for the buy-plan workflow the Approvals Workspace
drives: sales-order new/create (origination), buy-plan detail, editable lines
(add/edit/remove — role×status gated), the SO-number field, and per-plan lifecycle
actions (submit, approve, halt, resume, confirm-po, resource, claim, verify-po,
issue, cancel, reset). The retired /v2/buy-plans hub's partial URLs 308 onto their
workspace equivalents (spec §11.1; docs/APPROVALS_PARITY_CHECKLIST.md).

Pure structural move (spec §5.1/§10): URLs and behavior identical. common.py owns
the single APIRouter; importing the submodules below attaches their routes to it.
The order-sensitive GET triple ({plan_id:int} → pipeline-archive → {tab}) is
co-located in common.py in its original relative order, so registration is
independent of the import order here. Every former top-level name is re-exported
so existing imports (`from app.routers.htmx.buy_plans import ...`) keep working;
test PATCH targets for ``buy_plan_detail_partial``, however, must point at the
CALLING submodule (each action module binds it via ``from .common import ...``,
so patching a package attribute cannot intercept a submodule-local lookup).

Called by: app/main.py (router mount); routers/prepayments.py + routers/htmx/
    approvals_hub/panes.py (lazy back-imports via these re-exports).
Depends on: app.models, app.dependencies, app.database, app.services.approvals,
    .._shared (imports _is_ops_member shared with a staying quotes route).
"""

from .common import (  # noqa: F401
    _PO_CUTTER_ROLES,
    _can_resource,
    _can_supervise,
    _notify_if_completed,
    _require_po_cutter,
    buy_plan_detail_partial,
    buy_plans_tab_partial,
    pipeline_archive_partial,
    router,
)
from .lifecycle import (  # noqa: F401
    SEND_BACK_DEFAULT_NOTE,
    _workspace_pane_response,
    buy_plan_approve_partial,
    buy_plan_cancel_partial,
    buy_plan_halt_partial,
    buy_plan_reset_partial,
    buy_plan_resume_partial,
    buy_plan_set_so_partial,
    buy_plan_submit_partial,
    prepay_request_decide,
)
from .line_edits import (  # noqa: F401
    _parse_optional_float,
    _parse_optional_int,
    buy_plan_add_line_partial,
    buy_plan_bulk_lines_partial,
    buy_plan_edit_line_partial,
    buy_plan_remove_line_partial,
)
from .po_lines import (  # noqa: F401
    _resource_lines_and_alert,
    buy_plan_claim_line_partial,
    buy_plan_confirm_po_partial,
    buy_plan_flag_issue_partial,
    buy_plan_receive_line_partial,
    buy_plan_resolve_issue_partial,
    buy_plan_resource_line_partial,
    buy_plan_verify_po_partial,
)
from .sales_orders import (  # noqa: F401
    _normalize_order_type,
    buy_plans_list_partial,
    sales_order_create,
    sales_order_new,
)
