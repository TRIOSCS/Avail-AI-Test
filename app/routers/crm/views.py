"""CRM shell views — unified CRM tab with Customers/Vendors sub-tabs.

Called by: app/routers/crm/__init__.py (included via router)
Depends on: app/dependencies (require_access), app/templates
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...constants import AccessKey
from ...database import get_db
from ...dependencies import require_access, require_user
from ...models.auth import User

router = APIRouter(tags=["crm"])


@router.get("/v2/partials/crm/shell", response_class=HTMLResponse)
async def crm_shell(
    request: Request,
    user: User = Depends(require_access(AccessKey.CRM)),
):
    """Render the CRM shell with Customers/Vendors/Activity tab bar."""
    from ...template_env import template_response

    ctx = {
        "request": request,
        "user": user,
        "default_tab": "customers",
    }
    return template_response("htmx/partials/crm/shell.html", ctx)


@router.get("/v2/partials/crm/scorecard", response_class=HTMLResponse)
async def crm_scorecard(
    request: Request,
    time_range: str = "this_month",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Activity Scorecard CRM tab — per-user activity leaderboard, visible to ALL users.

    Relocated from Settings (where it was manager/admin-gated) to a CRM tab at the owner's
    request so every logged-in user sees the team leaderboard — hence ``require_user`` with
    no supervisor gate. On an HX-Request triggered by the time-range selector only the
    table fragment is swapped; the first paint (and a direct hit) renders the full tab.
    """
    from ...services.activity_scorecard import (
        DEFAULT_TIME_RANGE,
        TIME_RANGE_LABELS,
        TIME_RANGES,
        compute_scorecard,
    )
    from ...template_env import template_response
    from ..htmx._shared import _base_ctx

    if time_range not in TIME_RANGES:
        time_range = DEFAULT_TIME_RANGE

    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "rows": compute_scorecard(db, time_range),
            "time_range": time_range,
            "time_ranges": TIME_RANGES,
            "time_range_labels": TIME_RANGE_LABELS,
        }
    )

    # Time-range selector swaps only the table fragment; full-tab on first paint.
    if request.headers.get("HX-Request") == "true" and request.headers.get("HX-Trigger-Name") == "time_range":
        return template_response("htmx/partials/crm/_scorecard_table.html", ctx)
    return template_response("htmx/partials/crm/scorecard.html", ctx)
