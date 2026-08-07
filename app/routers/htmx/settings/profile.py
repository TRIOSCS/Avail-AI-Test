"""Profile tab + per-user toggle endpoints + inbox scan-now.

W4.8 split of the 1,032-line app/routers/htmx/settings.py — pure structural move: URLs
and behavior unchanged (same /v2/partials/settings*, /api/user/*, /v2/profile/timezone
paths, same htmx-views tag); routes attach to the shared router imported from .common.

Called by: app/main.py (via the package router mount); app/routers/htmx/
    requisitions_edit.py imports _run_inbox_scan_now (the staying poll-inbox route,
    via the package __init__ re-export).
Depends on: app.database, app.dependencies, app.models, app.template_env,
    routers.htmx._shared, .common
"""

import asyncio
import os

from fastapi import Depends, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_user
from ....models import User
from ....template_env import template_response
from .._shared import _base_ctx
from .common import router, settings_toast


@router.get("/v2/partials/settings/profile", response_class=HTMLResponse)
async def settings_profile_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User profile tab."""
    from ....services.activity_service import get_inbox_sync_status
    from ....utils.timezones import DEFAULT_DISPLAY_TZ, grouped_timezones

    ctx = _base_ctx(request, user, "settings")
    ctx["profile_user"] = user
    ctx["inbox_status"] = get_inbox_sync_status(db, user)
    ctx["tz_groups"] = grouped_timezones()
    ctx["default_display_tz"] = DEFAULT_DISPLAY_TZ
    return template_response("htmx/partials/settings/profile.html", ctx)


async def _run_inbox_scan_now(user: User, db: Session) -> None:
    """Run a real on-demand inbox scan for the current user, unless under TESTING."""
    if os.getenv("TESTING") == "1":
        return  # hermetic tests: do not touch Graph
    from ....jobs.email_jobs import _scan_user_inbox

    try:
        # stay under the HTMX client timeout (app/static/htmx_app.js); scan is idempotent + scheduler-backed
        await asyncio.wait_for(_scan_user_inbox(user, db), timeout=12)
    except TimeoutError:
        logger.warning("Manual inbox scan timed out for {}", user.email)


@router.post("/v2/partials/settings/inbox/scan-now", response_class=HTMLResponse)
async def settings_scan_now(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manual inbox scan from the Settings mailbox-sync card."""
    from ....services.activity_service import get_inbox_sync_status

    await _run_inbox_scan_now(user, db)
    db.refresh(user)
    ctx = _base_ctx(request, user, "settings")
    ctx["inbox_status"] = get_inbox_sync_status(db, user)
    return template_response("htmx/partials/settings/_mailbox_sync_card.html", ctx)


@router.post("/api/user/toggle-8x8", response_class=HTMLResponse)
async def toggle_8x8(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle 8x8 click-to-call preference for the current user."""
    user.eight_by_eight_enabled = not user.eight_by_eight_enabled
    db.commit()
    state = "enabled" if user.eight_by_eight_enabled else "disabled"
    logger.info("8x8 click-to-call toggled", user_id=user.id, enabled=user.eight_by_eight_enabled)
    return HTMLResponse(
        status_code=200,
        headers={"HX-Trigger": '{"showToast": "8x8 click-to-call ' + state + '"}'},
    )


@router.post("/api/user/profile", response_class=HTMLResponse)
async def update_user_profile(
    request: Request,
    name: str = Form(""),
    extension: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update the current user's display name and 8x8 extension.

    Validates name (non-empty, ≤255 chars) and extension (≤20 chars). Returns 400 JSON
    on bad input; on success commits and emits a showToast trigger.
    """
    from fastapi.responses import JSONResponse

    name = name.strip()
    extension = extension.strip()

    if not name or len(name) > 255:
        req_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=400,
            content={"error": "Name is required.", "status_code": 400, "request_id": req_id},
        )
    if len(extension) > 20:
        req_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=400,
            content={"error": "Extension must be 20 characters or fewer.", "status_code": 400, "request_id": req_id},
        )

    user.name = name
    user.eight_by_eight_extension = extension
    db.commit()
    logger.info("Profile updated", user_id=user.id)
    response = HTMLResponse(status_code=200)
    settings_toast(response, "Profile updated.")
    return response


@router.post("/v2/profile/timezone", response_class=HTMLResponse)
async def update_display_timezone(
    request: Request,
    timezone: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set the current user's display timezone (an IANA zone name).

    Written from BOTH the base-layout auto-detect (a background ``fetch`` of the browser's
    ``Intl`` zone, once per session when it differs from the stored value) AND the profile
    ``<select>`` override (an HTMX post). Validates the value is a real IANA zone; stores it
    only when unset or changed (so the auto-detect is a cheap no-op on repeat visits). The
    success HX-Trigger toast is consumed by the HTMX select; the fetch auto-detect ignores
    the response body, so it stays silent.
    """
    from fastapi.responses import JSONResponse

    from ....utils.timezones import is_valid_timezone

    tz = timezone.strip()
    if not is_valid_timezone(tz):
        req_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=400,
            content={"error": "Not a valid timezone.", "status_code": 400, "request_id": req_id},
        )

    if user.display_timezone == tz:
        # Unset-vs-changed guard: no write, no toast — the common auto-detect repeat case.
        return HTMLResponse(status_code=200)

    user.display_timezone = tz
    db.commit()
    # Invalidate the TTL cache so AuditUserMiddleware re-reads the new zone on the NEXT
    # request, and reflect it immediately for any rendering later in THIS request (the
    # middleware set the contextvar from the pre-change value at request start).
    from ....request_context import current_user_display_tz_var, invalidate_display_tz

    invalidate_display_tz(user.id)
    current_user_display_tz_var.set(tz)
    logger.info("Display timezone updated", user_id=user.id, timezone=tz)
    response = HTMLResponse(status_code=200)
    settings_toast(response, "Timezone updated.")
    return response


@router.post("/api/user/toggle-buyplan-email", response_class=HTMLResponse)
async def toggle_buyplan_email(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle buy-plan email notifications for the current user."""
    user.notify_buyplan_email_enabled = not user.notify_buyplan_email_enabled
    db.commit()
    state = "enabled" if user.notify_buyplan_email_enabled else "disabled"
    logger.info("Buy-plan email notifications toggled", user_id=user.id, enabled=user.notify_buyplan_email_enabled)
    response = HTMLResponse(status_code=200)
    settings_toast(response, f"Buy-plan email notifications {state}.")
    return response


@router.post("/api/user/toggle-new-offer-alert", response_class=HTMLResponse)
async def toggle_new_offer_alert(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle new-offer alerts for the current user."""
    user.notify_new_offer_alert_enabled = not user.notify_new_offer_alert_enabled
    db.commit()
    state = "enabled" if user.notify_new_offer_alert_enabled else "disabled"
    logger.info("New-offer alerts toggled", user_id=user.id, enabled=user.notify_new_offer_alert_enabled)
    response = HTMLResponse(status_code=200)
    settings_toast(response, f"New-offer alerts {state}.")
    return response


@router.post("/api/user/toggle-resource-alert", response_class=HTMLResponse)
async def toggle_resource_alert(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle urgent re-source backfill alerts (email + Teams DM) for the current
    user."""
    user.notify_resource_alert_enabled = not user.notify_resource_alert_enabled
    db.commit()
    state = "enabled" if user.notify_resource_alert_enabled else "disabled"
    logger.info("Re-source alerts toggled", user_id=user.id, enabled=user.notify_resource_alert_enabled)
    response = HTMLResponse(status_code=200)
    settings_toast(response, f"Re-source alerts {state}.")
    return response
