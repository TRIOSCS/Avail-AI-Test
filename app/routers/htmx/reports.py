"""Reports page router — GET /v2/partials/reports (Phase 5 / STREAM 1, Decision M).

Thin: coerces the four query params to the service vocabularies, gates the All scope,
calls gp_rollup, renders. ONE route — the full page, or only the #gp-panel fragment
when HX-Target says so (the app/routers/htmx/my_day.py:119-121 pattern). The full page
GET /v2/reports is a decorator on htmx_views.v2_page; its else-fallback threads the
whole query string here. The All scope is role-based is_manager_or_admin (the resell
PR #933 lens precedent) — the approvals workspace's ungated scope=all is deliberately
NOT copied: GP by rep is the oversight view the resell ruling reserved for managers.

Called by: HTMX (pill taps target #gp-panel), v2_page's lazy shell, the More menu,
           the Sales Hub eyebrow link
Depends on: app.services.gp_report_service, app.services.forecast_service,
            app.dependencies (require_access, is_manager_or_admin), ._shared._base_ctx
"""

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...constants import AccessKey
from ...database import get_db
from ...dependencies import is_manager_or_admin, require_access
from ...models import User
from ...services import forecast_service
from ...services.gp_report_service import (
    BASES,
    BASIS_LABELS,
    BASIS_SHORT_LABELS,
    DEFAULT_BASIS,
    DEFAULT_GROUP_BY,
    DEFAULT_PERIOD,
    GROUP_BYS,
    GROUP_LABELS,
    GROUP_PILL_LABELS,
    MANAGER_DEFAULT_SCOPE,
    PERIOD_LABELS,
    PERIODS,
    SCOPE_LABELS,
    coerce,
    gp_rollup,
)
from ...template_env import template_response
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


def _normalize_scope(scope: str, user: User) -> str:
    """'all' requires is_manager_or_admin; a non-manager asking for it silently gets
    'mine' — never a 403 that confirms the lens exists, never a leak (the shape of
    app/routers/resell.py:455-468 _normalize_lens; fallback adapted to 'mine', the only
    lens a non-manager holds here).

    Absent/unknown → MANAGER_DEFAULT_SCOPE for managers/admins (owner question f),
    'mine' for everyone else.
    """
    if scope == "mine":
        return "mine"
    if not is_manager_or_admin(user):
        return "mine"
    if scope == "all":
        return "all"
    return MANAGER_DEFAULT_SCOPE


@router.get("/v2/partials/reports", response_class=HTMLResponse)
async def reports_partial(
    request: Request,
    basis: str = Query(""),
    group_by: str = Query(""),
    period: str = Query(""),
    scope: str = Query(""),
    user: User = Depends(require_access(AccessKey.REPORTS)),
    db: Session = Depends(get_db),
):
    """The full Reports page, or only the #gp-panel fragment (HX-Target: gp-panel)."""
    basis = coerce(basis, BASES, DEFAULT_BASIS)
    group_by = coerce(group_by, GROUP_BYS, DEFAULT_GROUP_BY)
    period = coerce(period, PERIODS, DEFAULT_PERIOD)
    scope = _normalize_scope(scope, user)
    report = gp_rollup(db, user_id=user.id, scope=scope, group_by=group_by, basis=basis, period=period)
    ctx = _base_ctx(request, user, "reports")
    ctx.update(
        report=report,
        is_manager=is_manager_or_admin(user),
        bases=BASES,
        group_bys=GROUP_BYS,
        periods=PERIODS,
        basis_labels=BASIS_LABELS,
        basis_short_labels=BASIS_SHORT_LABELS,
        group_labels=GROUP_LABELS,
        group_pill_labels=GROUP_PILL_LABELS,
        period_labels=PERIOD_LABELS,
        scope_labels=SCOPE_LABELS,
    )
    if request.headers.get("HX-Target") == "gp-panel":
        return template_response("htmx/partials/reports/_gp_panel.html", ctx)
    ctx["pipeline"] = forecast_service.pipeline_summary(db)  # tenant-wide, no owner_id — as on the Sales Hub
    return template_response("htmx/partials/reports/page.html", ctx)
