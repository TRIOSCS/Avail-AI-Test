"""Settings tab shells — index, ops-group, users, system, data-export + legacy
redirects.

W4.8 split of the 1,032-line app/routers/htmx/settings.py — pure structural move: URLs
and behavior unchanged (same /v2/partials/settings* paths, same htmx-views tag); routes
attach to the shared router imported from .common.

Called by: app/main.py (via the package router mount).
Depends on: app.constants, app.database, app.dependencies, app.models, app.template_env,
    routers.htmx._shared, .common
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ....constants import (
    AccessKey,
    UserRole,
)
from ....database import get_db
from ....dependencies import (
    can_export_bulk_data,
    require_access,
    require_user,
    user_has_access,
)
from ....models import User
from ....template_env import template_response
from .._shared import _base_ctx
from .common import router

# ── Settings: Ops verification group ─────────────────────────────────


@router.get("/v2/partials/settings/ops-group", response_class=HTMLResponse)
async def settings_ops_group_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ops verification group management tab — admin only."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from ...admin.buy_plan_ops import ops_group_context

    ctx = _base_ctx(request, user, "settings")
    ctx.update(ops_group_context(db))
    return template_response("htmx/partials/settings/ops_group.html", ctx)


@router.get("/v2/partials/settings/users", response_class=HTMLResponse)
async def settings_users_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Users management tab (invite / role / activate) — admin only."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from ...admin.users import users_context

    ctx = _base_ctx(request, user, "settings")
    ctx.update(users_context(db))
    return template_response("htmx/partials/settings/users.html", ctx)


# ── Settings partials ────────────────────────────────────────────────


@router.get("/v2/partials/settings", response_class=HTMLResponse)
async def settings_partial(
    request: Request,
    tab: str = "connectors",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Settings page — renders index with active tab."""
    is_admin = user.role == UserRole.ADMIN
    # Connectors is gated on MANAGE_CONNECTORS (admins always qualify via user_has_access).
    # A user without that capability hitting Settings with the default 'connectors' tab
    # landed on an empty 403 page — send them to Profile (available to everyone) instead
    # (SET-04). Making the tab honor the capability is the SET-06 fix.
    can_manage_connectors = user_has_access(user, AccessKey.MANAGE_CONNECTORS, db)
    if tab == "connectors" and not can_manage_connectors:
        tab = "profile"
    ctx = _base_ctx(request, user, "settings")
    ctx["active_tab"] = tab
    ctx["is_admin"] = is_admin
    ctx["can_manage_connectors"] = can_manage_connectors
    # Data-export tab visibility: the SAME EXPORT_BULK_DATA predicate the tab route and
    # the five export routes enforce (admins by default, or an explicit per-user
    # override), so the button is never a dead 403 — mirrors can_manage_connectors.
    ctx["can_export_bulk"] = can_export_bulk_data(user)
    return template_response("htmx/partials/settings/index.html", ctx)


@router.get("/v2/partials/settings/sources", response_class=HTMLResponse)
async def settings_sources_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Sources tab — redirects to unified Connectors tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/v2/partials/settings/connectors", status_code=302)


@router.get("/v2/partials/settings/system", response_class=HTMLResponse)
async def settings_system_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """System config tab — admin only.

    Renders the curated typed controls (3 toggles + 1 number input) for the four user-
    facing flags. Effective values come from the Task-10 resolver (DB row wins, else the
    env-backed default) so each control reflects reality. Internal watermark keys are
    surfaced read-only in a collapsed "Job state" disclosure, never as editable
    controls.
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin only")
    from ....config import settings as app_settings
    from ....services.admin_service import (
        get_all_config,
        get_config_value,
        get_effective_flag,
        get_effective_int,
    )
    from ...admin.system import SYSTEM_JOB_STATE_KEYS, SYSTEM_SETTINGS_META

    # Resolve each curated setting's effective value, threading the env default so a
    # missing DB row falls back to the same value the background jobs read today.
    env_defaults = {
        "email_mining_enabled": app_settings.email_mining_enabled,
        "proactive_matching_enabled": app_settings.proactive_matching_enabled,
        "activity_tracking_enabled": app_settings.activity_tracking_enabled,
        "inbox_scan_interval_min": app_settings.inbox_scan_interval_min,
    }
    settings_view = []
    for key, meta in SYSTEM_SETTINGS_META.items():
        if meta["type"] == "bool":
            value: object = get_effective_flag(db, key, env_defaults[key])
        elif meta["type"] == "int":
            value = get_effective_int(db, key, env_defaults[key])
        else:  # string (e.g. prepayment-notification recipients) — DB row or empty default.
            value = get_config_value(db, key) or meta.get("default", "")
        settings_view.append({"key": key, "value": value, **meta})

    # Read-only job-state watermark rows (collapsed disclosure).
    all_config = get_all_config(db)
    job_state = [row for row in all_config if row["key"] in SYSTEM_JOB_STATE_KEYS]

    ctx = _base_ctx(request, user, "settings")
    ctx["system_settings"] = settings_view
    ctx["job_state"] = job_state
    return template_response("htmx/partials/settings/system.html", ctx)


@router.get("/v2/partials/settings/data-export", response_class=HTMLResponse)
async def settings_data_export_tab(
    request: Request,
    user: User = Depends(require_access(AccessKey.EXPORT_BULK_DATA)),
    db: Session = Depends(get_db),
):
    """Data export tab — bulk dataset export links (ISS-028).

    Links to the existing, unchanged EXPORT_BULK_DATA-gated download routes; this tab is
    the ONLY place bulk export controls appear in the app. Gated on the SAME
    require_access(AccessKey.EXPORT_BULK_DATA) the five export routes enforce (admins by
    default, or a non-admin granted an explicit per-user access_overrides escape hatch)
    — a role-only admin gate would let an overridden manager pass the export routes yet
    reach no UI that links them (mirrors the SET-06 Connectors pattern).
    """
    ctx = _base_ctx(request, user, "settings")
    return template_response("htmx/partials/settings/data_export.html", ctx)


@router.get("/v2/partials/settings/api-keys", response_class=HTMLResponse)
async def settings_api_keys_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """API keys tab — redirects to unified Connectors tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/v2/partials/settings/connectors", status_code=302)
