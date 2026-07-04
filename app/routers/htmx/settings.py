"""routers/htmx/settings.py — Settings / ops / user-management partials (HTMX + Alpine).

Server-rendered HTML partials for the settings surface: ops verification group, users,
scorecard, sources, system, profile + the per-user toggle endpoints, inbox scan-now,
data-ops + connectors tabs, connector test-all, the CRM vendor/company merge + dedup
admin actions, and the admin api-health + data-ops partials. Extracted verbatim from
htmx_views.py (same `/v2/partials/settings`, `/api/user`, `/v2/partials/admin` paths,
same `htmx-views` tag).

Called by: app/main.py (router mount); htmx_views.py re-imports `settings_toast`
    (external importers in sources.py / admin.buy_plan_ops) and `_run_inbox_scan_now`
    (the staying poll-inbox route).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
"""

import asyncio
import json
import os
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    UserRole,
)
from ...database import get_db
from ...dependencies import (
    is_manager_or_admin,
    require_access,
    require_admin,
    require_user,
    user_has_access,
)
from ...models import (
    ApiSource,
    Company,
    User,
    VendorCard,
)
from ...services import clay_oauth
from ...template_env import template_response
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


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
    from ..admin.buy_plan_ops import ops_group_context

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
    from ..admin.users import users_context

    ctx = _base_ctx(request, user, "settings")
    ctx.update(users_context(db))
    return template_response("htmx/partials/settings/users.html", ctx)


@router.get("/v2/partials/settings/scorecard", response_class=HTMLResponse)
async def settings_scorecard_tab(
    request: Request,
    time_range: str = "this_month",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Activity Scorecard tab — per-user activity leaderboard. Manager/admin only.

    A leaderboard of all users' activity is oversight/performance data, so it is gated
    to the supervisor tier (MANAGER + ADMIN) via is_manager_or_admin — buyers/sales/
    traders never see it. On an HX-Request triggered by the time-range selector only the
    table fragment is swapped; the first paint (and a direct hit) renders the full tab.
    """
    if not is_manager_or_admin(user):
        raise HTTPException(403, "Managers and admins only")
    from ...services.activity_scorecard import (
        DEFAULT_TIME_RANGE,
        TALK_TIME_BUCKET_SECONDS,
        TIME_RANGE_LABELS,
        TIME_RANGES,
        compute_scorecard,
        scoring_formula_parts,
    )

    if time_range not in TIME_RANGES:
        time_range = DEFAULT_TIME_RANGE

    ctx = _base_ctx(request, user, "settings")
    ctx.update(
        {
            "rows": compute_scorecard(db, time_range),
            "time_range": time_range,
            "time_ranges": TIME_RANGES,
            "time_range_labels": TIME_RANGE_LABELS,
            "formula_parts": scoring_formula_parts(),
            "talk_bucket_min": TALK_TIME_BUCKET_SECONDS // 60,
        }
    )

    # Time-range selector swaps only the table fragment; full-tab on first paint.
    if request.headers.get("HX-Request") == "true" and request.headers.get("HX-Trigger-Name") == "time_range":
        return template_response("htmx/partials/settings/_scorecard_table.html", ctx)
    return template_response("htmx/partials/settings/scorecard.html", ctx)


def settings_toast(response, message: str, kind: str = "success") -> None:
    """Attach a showToast HX-Trigger for settings mutation responses.

    Called by settings mutation handlers to surface success/error feedback via the
    Alpine $store.toast. Mirrors _prospect_toast but is scoped to settings so later
    tasks can import it cleanly.
    """
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})


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
    # Supervisor-tier flag — gates the Activity Scorecard tab (manager + admin).
    ctx["is_manager"] = is_manager_or_admin(user)
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
    from ...config import settings as app_settings
    from ...services.admin_service import (
        get_all_config,
        get_config_value,
        get_effective_flag,
        get_effective_int,
    )
    from ..admin.system import SYSTEM_JOB_STATE_KEYS, SYSTEM_SETTINGS_META

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


@router.get("/v2/partials/settings/profile", response_class=HTMLResponse)
async def settings_profile_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User profile tab."""
    from ...services.activity_service import get_inbox_sync_status
    from ...utils.timezones import DEFAULT_DISPLAY_TZ, grouped_timezones

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
    from ...jobs.email_jobs import _scan_user_inbox

    try:
        # stay under the HTMX client timeout (app/static/htmx_app.js); scan is idempotent + scheduler-backed
        await asyncio.wait_for(_scan_user_inbox(user, db), timeout=12)
    except asyncio.TimeoutError:
        logger.warning("Manual inbox scan timed out for {}", user.email)


@router.post("/v2/partials/settings/inbox/scan-now", response_class=HTMLResponse)
async def settings_scan_now(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manual inbox scan from the Settings mailbox-sync card."""
    from ...services.activity_service import get_inbox_sync_status

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

    from ...utils.timezones import is_valid_timezone

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
    from ...request_context import current_user_display_tz_var, invalidate_display_tz

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


def _render_data_ops(request: Request, user: User, db: Session):
    """Render the Data Ops tab partial — vendor/company dedup suggestions.

    Each scan is guarded independently. A scan that RAISES sets a per-scan
    ``*_scan_failed`` flag so the template can render a distinct error block instead
    of swallowing the failure into the reassuring "no duplicates found" empty state
    (a crashed scan must never look like a clean dataset). Reused by the merge
    endpoints so a successful merge re-renders the surrounding list and stale pairs
    drop without a manual refresh.
    """
    vendor_dupes: list = []
    company_dupes: list = []
    vendor_scan_failed = False
    company_scan_failed = False
    try:
        from ...vendor_utils import find_vendor_dedup_candidates

        vendor_dupes = find_vendor_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        vendor_scan_failed = True
        logger.warning(f"Vendor dedup scan failed: {e}")
    try:
        from ...company_utils import find_company_dedup_candidates

        company_dupes = find_company_dedup_candidates(db, threshold=85, limit=30)
    except Exception as e:
        company_scan_failed = True
        logger.warning(f"Company dedup scan failed: {e}")

    ctx = _base_ctx(request, user, "settings")
    ctx["vendor_dupes"] = vendor_dupes
    ctx["company_dupes"] = company_dupes
    ctx["vendor_scan_failed"] = vendor_scan_failed
    ctx["company_scan_failed"] = company_scan_failed
    return template_response("htmx/partials/settings/data_ops.html", ctx)


@router.get("/v2/partials/settings/data-ops", response_class=HTMLResponse)
async def settings_data_ops_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Admin data operations tab — vendor/company dedup suggestions."""
    from ...dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    return _render_data_ops(request, user, db)


@router.get("/v2/partials/settings/api-keys", response_class=HTMLResponse)
async def settings_api_keys_tab(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """API keys tab — redirects to unified Connectors tab."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/v2/partials/settings/connectors", status_code=302)


# Retired data providers — excluded from the connectors tab and the Test-all sweep.
# Single source of truth referenced by both _build_connector_groups and connectors_test_all.
_DEAD_CONNECTORS = frozenset({"rocketreach_enrichment", "clearbit_enrichment"})


def _build_connector_field(source, env_var: str, *, mask_fully: bool = False) -> dict:
    """Return {is_set, masked} for one env-var credential field.

    Reads directly from the already-loaded ``source`` (ApiSource) row rather than
    re-querying per env var. ``_build_connector_groups`` loads every ApiSource once, so
    the old ``credential_is_set``/``get_credential`` calls (a fresh SELECT each) were
    ~2 redundant queries per field, i.e. ~70 across a full render (O5). ``is_set_for`` /
    ``decrypt_from`` take the row and fall back to env vars without touching the DB.

    ``mask_fully`` renders dots ONLY (no last-4 tail) for password-type credentials —
    used for browser_login account logins (TBF/ICS). The default ``mask_value`` shows the
    last 4 chars to help identify an API key, but for a reused human account password even
    a 4-char tail in the DOM is a leak, so those are fully masked.
    """
    from ...services.credential_service import decrypt_from, is_set_for, mask_value

    is_set = is_set_for(source, env_var)
    masked = ""
    if is_set:
        if mask_fully:
            masked = "••••••••"
        else:
            plain = decrypt_from(source, env_var)
            masked = mask_value(plain) if plain else "••••••••"
    return {"is_set": is_set, "masked": masked}


def _worker_status_row(source_name: str, db):
    """Return the worker-status singleton for a worker-backed source (or None).

    Maps an ApiSource.name (thebrokersite/netcomponents/icsource) to its heartbeat model
    via connector_service.WORKER_BACKED_SOURCES, reading the id=1 singleton.
    """
    from ...models import IcsWorkerStatus, NcWorkerStatus, TbfWorkerStatus
    from ...services import connector_service

    worker_key = connector_service.WORKER_BACKED_SOURCES.get(source_name)
    model = {"tbf": TbfWorkerStatus, "nc": NcWorkerStatus, "ics": IcsWorkerStatus}.get(worker_key)
    if model is None:
        return None
    return db.get(model, 1)


def _enrich_source(source, db) -> dict:
    """Build the per-source context dict for the connectors tab."""
    from ...services import connector_service

    name = source.name
    ct = connector_service.control_type(source)
    keyless = connector_service.is_keyless(source)

    # Credential fields. browser_login logins (TBF/ICS account passwords) are fully
    # masked — no last-4 tail in the DOM.
    env_vars = source.env_vars or []
    mask_fully = ct == "browser_login"
    creds = {ev: _build_connector_field(source, ev, mask_fully=mask_fully) for ev in env_vars}
    credential_set = any(c["is_set"] for c in creds.values())

    # Clay OAuth state
    if name == "clay_enrichment":
        oauth_connected = clay_oauth.is_connected()
        needs_reconnect = clay_oauth.needs_reconnect()
    else:
        oauth_connected = False
        needs_reconnect = False

    # Worker-backed sources: derive status from the worker heartbeat, not a direct API.
    worker = None
    if connector_service.is_worker_backed(source):
        worker = connector_service.worker_health(_worker_status_row(name, db))

    state = connector_service.connector_state(
        source,
        credential_set=credential_set,
        oauth_connected=oauth_connected,
        needs_reconnect=needs_reconnect,
        keyless=keyless,
        worker=worker,
    )

    # Keyless note
    if ct == "keyless":
        if name == "ai_live_web":
            keyless_note = "No key required — uses your Anthropic key."
        elif name == "email_mining":
            # Flag connector: no credential to enter. Enablement lives in the
            # Email Mining setting on the System tab, not a key field here.
            keyless_note = "No key required — turn Email Mining on in System settings."
        else:
            keyless_note = "No key required — switch it on to use it."
    else:
        keyless_note = ""

    # Testability:
    #  - planned: never (no implementation yet)
    #  - worker-backed: never via the API-probe Test button — health is the heartbeat,
    #    not a synchronous search (the worker runs out-of-process on a schedule)
    #  - keyless: only when a real test path exists — some keyless sources
    #    (sam_gov_enrichment, stock_list_import) have no connector/test hook, so their
    #    Test button was a cosmetic no-op that falsely reported OK. Derive it from
    #    whether _get_connector_for_source can actually build a probe.
    #  - else (credentialed / oauth): has some form of access
    if ct == "planned" or worker is not None:
        testable = False
    elif keyless:
        from ..sources import source_has_test_path

        testable = source_has_test_path(name, db)
    else:
        testable = bool(credential_set or oauth_connected)

    return {
        "id": source.id,
        "name": name,
        "display_name": source.display_name or name,
        "description": source.description or "",
        "is_active": source.is_active,
        "state": state,
        "control_type": ct,
        "env_vars": env_vars,
        "creds": creds,
        "oauth_connected": oauth_connected,
        "needs_reconnect": needs_reconnect,
        "status": source.status or "pending",
        "last_error": source.last_error or "",
        "last_success": source.last_success,
        "error_count_24h": getattr(source, "error_count_24h", 0) or 0,
        "keyless_note": keyless_note,
        "testable": testable,
        # Worker-backed health (None for direct-API/keyless/oauth sources).
        "worker": worker,
    }


def _build_connector_groups(db, request) -> list[dict]:
    """Return connector_groups list-of-group-dicts for the connectors tab context.

    Each group: {key, label, sources: [enriched source dict]}.
    Sources are bucketed by connector_service.connector_group, emitted in GROUP_ORDER,
    empty groups are dropped. Dead providers (rocketreach, clearbit) are excluded.
    """
    from ...services import connector_service

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()

    buckets: dict[str, list[dict]] = {key: [] for key, _ in connector_service.GROUP_ORDER}

    for src in sources:
        if src.name in _DEAD_CONNECTORS:
            continue
        group_key = connector_service.connector_group(src)
        if group_key not in buckets:
            group_key = "part_sourcing"
        buckets[group_key].append(_enrich_source(src, db))

    groups = []
    for key, label in connector_service.GROUP_ORDER:
        members = buckets.get(key, [])
        if members:
            groups.append({"key": key, "label": label, "sources": members})

    return groups


@router.get("/v2/partials/settings/connectors", response_class=HTMLResponse)
async def settings_connectors_tab(
    request: Request,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Unified Connectors tab — admins + MANAGE_CONNECTORS capability holders (SET-06).

    Replaces sources + api-keys tabs.
    """
    ctx = _base_ctx(request, user, "settings")
    ctx["connector_groups"] = _build_connector_groups(db, request)
    return template_response("htmx/partials/settings/connectors.html", ctx)


@router.get("/v2/partials/settings/connector-card/{source_id}", response_class=HTMLResponse)
async def connector_card_partial(
    source_id: int,
    request: Request,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Single connector card partial — used as the swap unit for toggle/test/save.

    Returns the rendered card macro for one source, or 404 if not found. Gated on
    MANAGE_CONNECTORS (admins always qualify) — this is re-GET after every card action,
    so it must honor the same gate as the tab (SET-06).
    """
    source = db.query(ApiSource).filter(ApiSource.id == source_id).first()
    if not source:
        raise HTTPException(404, f"Connector {source_id!r} not found")

    enriched = _enrich_source(source, db)
    ctx = _base_ctx(request, user, "settings")
    ctx["s"] = enriched
    return template_response("htmx/partials/settings/_connector_card_partial.html", ctx)


# Test-all budgets. Each probe is a real live search — most connectors finish in
# 15-30s, AI web search up to ~60s. Run them CONCURRENTLY (was: sequential, so >4 live
# connectors blew the client's 15s htmx timeout → the XHR aborted, every OOB card/summary
# was discarded, and the server kept burning paid quota). Bound each probe, bound the
# whole sweep under the button's raised hx-request timeout, and poll for client
# disconnect so an abandoned sweep stops burning quota.
_TEST_ALL_PROBE_TIMEOUT_S = 60.0
_TEST_ALL_OVERALL_BUDGET_S = 90.0
_TEST_ALL_DISCONNECT_POLL_S = 0.5
# Per-user Test-all cap. A sweep probes every connector at once (far heavier than a single
# 5/min per-source Test), so it gets a tighter per-minute budget to protect paid quota.
_TEST_ALL_MAX_PER_MIN = 3


@router.post("/v2/partials/settings/connectors/test-all", response_class=HTMLResponse)
async def connectors_test_all(
    request: Request,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Run Test for every testable + active source CONCURRENTLY and return an OOB bundle
    of refreshed cards.

    Gated on MANAGE_CONNECTORS (admins always qualify) — matches the per-source Test
    endpoint (SET-06). Non-testable / inactive / dead sources are skipped. Each probe is
    bounded by a per-probe timeout, the whole sweep by an overall budget, and the loop
    aborts early if the client disconnects — so an abandoned sweep stops burning paid
    quota. Per-source failures are tolerated (recorded as Error) and never abort the
    sweep. Network I/O overlaps across probes; status is persisted sequentially on this
    one session afterward (concurrent commits on a shared session would race).
    """
    from ...rate_limit import check_rate_limit
    from ..sources import _persist_test_result, _probe_source

    # Cost guard: a sweep fires a live probe at every connector, spending real paid quota.
    # The per-source Test is capped at 5/min (slowapi); Test-all previously had NO cap, so
    # it bypassed that entirely. Cap it per-user (a sweep is far heavier than one probe).
    if not check_rate_limit(user.id, "connectors_test_all", limit=_TEST_ALL_MAX_PER_MIN, window_seconds=60):
        resp = HTMLResponse("")
        settings_toast(
            resp,
            "Test-all is rate-limited — wait a minute before retrying (each run spends live API quota).",
            kind="error",
        )
        return resp

    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    candidates = [
        src
        for src in sources
        if src.name not in _DEAD_CONNECTORS and src.is_active and _enrich_source(src, db)["testable"]
    ]

    async def _guarded(src):
        """Probe one source, bounded by the per-probe timeout.

        Never raises (except on outer cancellation, which marks the task cancelled).
        """
        try:
            return await asyncio.wait_for(_probe_source(src, db), timeout=_TEST_ALL_PROBE_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError):
            ms = int(_TEST_ALL_PROBE_TIMEOUT_S * 1000)
            return {"results": [], "elapsed_ms": ms, "error": f"Test exceeded {int(_TEST_ALL_PROBE_TIMEOUT_S)}s"}

    tasks = {src.id: asyncio.create_task(_guarded(src)) for src in candidates}
    pending = set(tasks.values())
    deadline = time.monotonic() + _TEST_ALL_OVERALL_BUDGET_S
    while pending:
        if await request.is_disconnected():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _done, pending = await asyncio.wait(
            pending,
            timeout=min(remaining, _TEST_ALL_DISCONNECT_POLL_S),
            return_when=asyncio.FIRST_COMPLETED,
        )
    # Cancel any probes still running (budget hit or client gone) and drain them.
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # Persist sequentially on the shared session (no concurrent commits).
    tested: list[dict] = []
    for src in candidates:
        task = tasks[src.id]
        if task.cancelled() or not task.done():
            continue
        outcome = task.result()  # _guarded never raises for a completed task
        _persist_test_result(
            src, db, results=outcome["results"], elapsed_ms=outcome["elapsed_ms"], error=outcome["error"]
        )
        tested.append(_enrich_source(src, db))

    failed = sum(1 for s in tested if s["state"] == "error")
    ctx = _base_ctx(request, user, "settings")
    ctx["tested_sources"] = tested
    ctx["tested_count"] = len(tested)
    ctx["failed_count"] = failed
    return template_response("htmx/partials/settings/_connectors_testall.html", ctx)


@router.post("/v2/partials/admin/vendor-merge", response_class=HTMLResponse)
async def admin_vendor_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two vendor cards via HTMX."""
    from ...dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ...services.vendor_merge_service import merge_vendor_cards as _merge

    try:
        result = _merge(keep_id, remove_id, db)
        db.commit()
    except Exception as e:
        # Align with company-merge: the service raises ValueError on validation, but an
        # unexpected SQLAlchemy error here must surface as a toast, not a 500.
        db.rollback()
        message, kind = f"Vendor merge failed: {e}", "error"
    else:
        kept = db.get(VendorCard, result.get("kept", keep_id))
        kept_name = kept.display_name if kept and kept.display_name else "vendor"
        message = f"Merged into {kept_name}. {result.get('reassigned', 0)} records reassigned."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/company-merge", response_class=HTMLResponse)
async def admin_company_merge(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge two companies via HTMX."""
    from ...dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ...services.company_merge_service import merge_companies

    try:
        result = merge_companies(keep_id, remove_id, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Company merge failed: {e}", "error"
    else:
        kept = db.get(Company, result.get("kept", keep_id))
        kept_name = kept.name if kept and kept.name else "company"
        message, kind = f"Merged into {kept_name}.", "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/vendor-delete-both", response_class=HTMLResponse)
async def admin_vendor_delete_both(
    request: Request,
    id_a: int = Form(...),
    id_b: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete BOTH vendor cards in a dedup pair (neither is worth keeping)."""
    from ...dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ...services.vendor_merge_service import delete_vendor_cards

    try:
        result = delete_vendor_cards(id_a, id_b, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Vendor delete failed: {e}", "error"
    else:
        message = f"Deleted both vendors. {result.get('detached', 0)} records detached."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


@router.post("/v2/partials/admin/company-delete-both", response_class=HTMLResponse)
async def admin_company_delete_both(
    request: Request,
    id_a: int = Form(...),
    id_b: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete BOTH companies in a dedup pair (neither is worth keeping)."""
    from ...dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    from ...services.company_merge_service import delete_companies

    try:
        result = delete_companies(id_a, id_b, db)
        db.commit()
    except Exception as e:
        db.rollback()
        message, kind = f"Company delete failed: {e}", "error"
    else:
        message = f"Deleted both companies. {result.get('detached', 0)} records detached."
        kind = "success"

    resp = _render_data_ops(request, user, db)
    settings_toast(resp, message, kind=kind)
    return resp


# Mass dedup actions accept a comma-joined "pairs" token list where each token is
# "<id_a>-<id_b>" (the two ids of one candidate pair). Mirrors the requisitions2 /
# customers bulk convention (one hidden field, server-side parse + per-item gate),
# but the dedup unit is a PAIR, not a single row, so the token carries both ids.
_MAX_DEDUP_PAIRS = 200


def _parse_dedup_pairs(raw: str) -> list[tuple[int, int]]:
    """Parse a "a-b,c-d" pair-token string into [(a, b), ...]; skip malformed tokens."""
    pairs: list[tuple[int, int]] = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        a, _, b = tok.partition("-")
        if a.lstrip("-").isdigit() and b.lstrip("-").isdigit():
            pairs.append((int(a), int(b)))
    return pairs


async def _dedup_bulk(request, user, db, entity: str) -> HTMLResponse:
    """Shared body for vendor/company bulk dedup actions (merge | delete | dismiss).

    ``merge`` keeps the FIRST id of each pair (the template emits keeper-first tokens);
    ``delete`` removes both; ``dismiss`` is a view-only clear (no durable state yet — the
    rows just drop from this render and reappear on the next scan). Per-pair failures don't
    abort the batch, but each is logged at error level and the failing pair tokens are
    surfaced in the toast — any failure makes the toast an ``error`` (never green success).
    """
    from ...dependencies import is_admin

    if not is_admin(user):
        raise HTTPException(403, "Admin only")

    form = await request.form()
    action = (form.get("action") or "").strip()
    if action not in {"merge", "delete", "dismiss"}:
        raise HTTPException(400, f"Invalid action {action!r}")

    pairs = _parse_dedup_pairs(form.get("pairs", ""))
    if len(pairs) > _MAX_DEDUP_PAIRS:
        raise HTTPException(400, f"Maximum {_MAX_DEDUP_PAIRS} pairs per bulk action")

    if not pairs or action == "dismiss":
        # Dismiss is purely client-side (the row was already hidden); just re-render.
        resp = _render_data_ops(request, user, db)
        if pairs:
            settings_toast(resp, f"Dismissed {len(pairs)} pair(s) for now.", kind="success")
        return resp

    if entity == "vendor":
        from ...services.vendor_merge_service import delete_vendor_cards, merge_vendor_cards

        merge_fn, delete_fn, noun = merge_vendor_cards, delete_vendor_cards, "vendor"
    else:
        from ...services.company_merge_service import delete_companies, merge_companies

        merge_fn, delete_fn, noun = merge_companies, delete_companies, "company"

    done = 0
    failed_tokens: list[str] = []
    for a, b in pairs:
        try:
            if action == "merge":
                merge_fn(a, b, db)
            else:
                delete_fn(a, b, db)
            db.commit()
            done += 1
        except Exception as e:
            db.rollback()
            failed_tokens.append(f"{a}-{b}")
            logger.error("Bulk {} {}: pair {}-{} failed: {}", noun, action, a, b, e)

    verb = "Merged" if action == "merge" else "Deleted"
    failed = len(failed_tokens)
    message = f"{verb} {done} {noun} pair(s)."
    if failed:
        message += f" {failed} failed: {', '.join(failed_tokens)}."
    resp = _render_data_ops(request, user, db)
    # Any failure surfaces as an error toast — a partial failure must not look green.
    settings_toast(resp, message, kind="error" if failed else "success")
    return resp


@router.post("/v2/partials/admin/vendor-bulk", response_class=HTMLResponse)
async def admin_vendor_bulk(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk merge/delete/dismiss selected vendor dedup pairs."""
    return await _dedup_bulk(request, user, db, "vendor")


@router.post("/v2/partials/admin/company-bulk", response_class=HTMLResponse)
async def admin_company_bulk(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk merge/delete/dismiss selected company dedup pairs."""
    return await _dedup_bulk(request, user, db, "company")


@router.get("/v2/partials/admin/api-health", response_class=HTMLResponse)
async def admin_api_health(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return connector health dashboard."""
    try:
        from ...services.connector_health import get_health_dashboard

        health = get_health_dashboard(db)
    except (ImportError, RuntimeError, Exception):
        health = {"connectors": [], "overall_status": "unknown"}

    return template_response(
        "htmx/partials/admin/api_health.html",
        {"request": request, "health": health},
    )


@router.get("/v2/partials/admin/data-ops", response_class=HTMLResponse)
async def admin_data_ops(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return admin data operations panel."""
    from ...models.intelligence import MaterialCard

    vendor_count = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    company_count = db.query(sqlfunc.count(Company.id)).filter(Company.is_active.is_(True)).scalar() or 0
    material_count = db.query(sqlfunc.count(MaterialCard.id)).scalar() or 0

    return template_response(
        "htmx/partials/admin/data_ops.html",
        {
            "request": request,
            "vendor_count": vendor_count,
            "company_count": company_count,
            "material_count": material_count,
        },
    )
