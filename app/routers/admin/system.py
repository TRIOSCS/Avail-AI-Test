"""Admin system configuration, health, credentials, integrity, and audit endpoints.

Business rules:
- System config reads require settings_access; writes require admin.
- Credential values are encrypted at rest via Fernet (credential_service).
- Connector health auto-flags "degraded" when >50% failure rate over 24h.
- Material audit log supports filtering by card_id and action.

Called by: app/routers/admin/__init__.py (included via router)
Depends on: app/services/admin_service.py, app/services/credential_service.py,
            app/models, app/dependencies
"""

import json
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...constants import ApiSourceStatus
from ...database import get_db
from ...dependencies import require_admin, require_settings_access
from ...models import ApiSource, User
from ...models.config import ApiUsageLog, GraphSubscription
from ...rate_limit import limiter
from ...schemas.admin import SourceCredentialsUpdate
from ...services.admin_service import get_all_config, get_system_health, set_config_value

router = APIRouter(tags=["admin"])


# -- Curated system-settings catalog ---------------------------------------
#
# The System settings tab edits these user-facing keys via typed controls (bool
# toggles, an int number input, and string text inputs). The meta map is the single
# source of truth for label/help/type, owned in code here. `restart` flags the
# scheduler-read settings whose change only takes full effect after the next app
# restart (the DB value is still saved immediately). `email_mining_enabled` resolves
# per-request, so it has no restart note. `min` bounds the integer control + write
# validation. The three `type: "string"` keys are the prepayment-notification
# recipients (accounting/AP group inboxes + a Teams incoming-webhook URL) read at
# notify time by prepayment_notifications; each defaults to empty (channel skipped).

INTERVAL_MIN_MINUTES = 5

SYSTEM_SETTINGS_META: dict[str, dict] = {
    "email_mining_enabled": {
        "type": "bool",
        "label": "Email mining",
        "help": "Mine connected inboxes for parts demand & vendor offers.",
        "restart": False,
    },
    "proactive_matching_enabled": {
        "type": "bool",
        "label": "Proactive offer matching",
        "help": "Auto-match inbound offers to open requirements.",
        "restart": True,
    },
    "activity_tracking_enabled": {
        "type": "bool",
        "label": "CRM activity tracking",
        "help": "Log emails/calls onto company & contact timelines.",
        "restart": True,
    },
    "inbox_scan_interval_min": {
        "type": "int",
        "label": "Inbox scan interval (minutes)",
        "help": "How often connected inboxes are scanned.",
        "restart": True,
        "min": INTERVAL_MIN_MINUTES,
    },
    "accounting_group_email": {
        "type": "string",
        "label": "Accounting group email",
        "help": "Distribution list emailed when a prepayment is requested or approved.",
        "restart": False,
        "default": "",
    },
    "ap_group_email": {
        "type": "string",
        "label": "Accounts-payable (AP) group email",
        "help": "Distribution list emailed alongside accounting on prepayment events.",
        "restart": False,
        "default": "",
    },
    "prepayment_teams_webhook": {
        "type": "string",
        "label": "Prepayment Teams webhook",
        "help": "Incoming-webhook URL for the Teams channel card on prepayment events.",
        "restart": False,
        "default": "",
    },
}

# Internal watermark/job-state keys: never editable; optionally shown read-only.
SYSTEM_JOB_STATE_KEYS: tuple[str, ...] = (
    "teams_calls_last_poll",
    "8x8_last_poll",
    "proactive_last_scan",
)


def _iso(dt):
    """Return a datetime as an ISO string, or None if unset."""
    return dt.isoformat() if dt else None


# -- Schemas ---------------------------------------------------------------


class ConfigUpdateRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=500)


# -- System Config (admin for writes, settings_access for reads) -----------


@router.get("/api/admin/config")
@limiter.limit("30/minute")
def api_get_config(
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    return get_all_config(db)


def _validate_typed_value(key: str, value: str) -> str:
    """Coerce/validate a curated typed setting; return the canonical string to store.

    Raises HTTPException(400) with a plain-language message on bad input. Keys not in
    the curated catalog are passed through unchanged (legacy free-text behaviour).
    """
    meta = SYSTEM_SETTINGS_META.get(key)
    if meta is None:
        return value
    raw = value.strip()
    if meta["type"] == "bool":
        normalized = raw.lower()
        if normalized in ("true", "false"):
            return normalized
        raise HTTPException(400, "Value must be true or false.")
    if meta["type"] == "int":
        try:
            n = int(raw)
        except (ValueError, TypeError):
            raise HTTPException(400, "Inbox scan interval must be a whole number.")
        if n < meta["min"]:
            raise HTTPException(400, "Inbox scan interval must be at least 5 minutes.")
        return str(n)
    return value


@router.put("/api/admin/config/{key}")
@limiter.limit("10/minute")
def api_set_config(
    key: str,
    request: Request,
    body: ConfigUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    canonical = _validate_typed_value(key, body.value)
    result = set_config_value(db, key, canonical, user.email)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    # Surface success via the shared toast (htmx_app.js bridges showToast → $store.toast).
    return JSONResponse(
        content=result,
        headers={"HX-Trigger": json.dumps({"showToast": {"message": "Setting saved.", "type": "success"}})},
    )


# -- System Health (settings_access) ---------------------------------------


@router.get("/api/admin/health")
@limiter.limit("30/minute")
def api_health(
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    return get_system_health(db)


# -- Connector Health Dashboard (admin) ------------------------------------


@router.get("/api/admin/connector-health")
@limiter.limit("30/minute")
def api_connector_health(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Return per-connector health metrics with auto-degraded status."""
    sources = db.query(ApiSource).order_by(ApiSource.name).all()
    result = []
    for src in sources:
        total = src.total_searches or 0
        total_results = src.total_results or 0
        errors_24h = src.error_count_24h or 0
        # Auto-flag degraded: >50% failure rate over last 24h (min 4 searches)
        status = src.status or ApiSourceStatus.PENDING
        if errors_24h >= 4 and total > 0:
            recent_success = max(0, total - errors_24h)
            if errors_24h > recent_success:
                status = ApiSourceStatus.DEGRADED
        result.append(
            {
                "id": src.id,
                "name": src.name,
                "display_name": src.display_name,
                "status": status,
                "is_active": src.is_active,
                "avg_response_ms": src.avg_response_ms or 0,
                "total_searches": total,
                "total_results": total_results,
                "last_success": _iso(src.last_success),
                "last_error": src.last_error,
                "last_error_at": _iso(src.last_error_at),
                "error_count_24h": errors_24h,
            }
        )
    return {"connectors": result}


@router.get("/api/admin/api-health/dashboard")
@limiter.limit("30/minute")
def api_health_dashboard(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Full API health dashboard data -- status, usage, recent check history."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)

    # Aggregate usage log stats per source (SQLite-compatible)
    check_stats_raw = (
        db.query(ApiUsageLog.source_id, ApiUsageLog.success).filter(ApiUsageLog.timestamp >= cutoff_24h).all()
    )
    stats_map = {}
    for row in check_stats_raw:
        sid = row.source_id
        if sid not in stats_map:
            stats_map[sid] = {"total": 0, "failures": 0}
        stats_map[sid]["total"] += 1
        if not row.success:
            stats_map[sid]["failures"] += 1

    result = []
    for src in sources:
        quota = src.monthly_quota
        calls = src.calls_this_month or 0
        usage_pct = round((calls / quota) * 100, 1) if quota and quota > 0 else None
        checks = stats_map.get(src.id, {"total": 0, "failures": 0})

        result.append(
            {
                "id": src.id,
                "name": src.name,
                "display_name": src.display_name,
                "category": src.category,
                "source_type": src.source_type,
                "status": src.status,
                "is_active": src.is_active,
                "last_success": _iso(src.last_success),
                "last_error": src.last_error,
                "last_error_at": _iso(src.last_error_at),
                "error_count_24h": src.error_count_24h or 0,
                "avg_response_ms": src.avg_response_ms or 0,
                "total_searches": src.total_searches or 0,
                "monthly_quota": quota,
                "calls_this_month": calls,
                "usage_pct": usage_pct,
                "last_ping_at": _iso(src.last_ping_at),
                "last_deep_test_at": _iso(src.last_deep_test_at),
                "recent_checks": checks["total"],
                "recent_failures": checks["failures"],
            }
        )

    return {"sources": result}


# -- Credential Management (admin) ----------------------------------------


@router.get("/api/admin/sources/{source_id}/credentials")
@limiter.limit("30/minute")
def api_get_credentials(
    source_id: int,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Return masked credential values for a source."""
    # Import from package __init__ so test patches on
    # "app.routers.admin.decrypt_value" are picked up.
    from . import decrypt_value, mask_value

    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    result = {}
    for var_name in src.env_vars or []:
        encrypted = (src.credentials or {}).get(var_name)
        if encrypted:
            try:
                plain = decrypt_value(encrypted)
                result[var_name] = {
                    "status": "set",
                    "masked": mask_value(plain),
                    "source": "db",
                }
            except (ValueError, TypeError):
                logger.warning("Credential decryption failed for {}", var_name, exc_info=True)
                result[var_name] = {"status": "error", "masked": "", "source": "db"}
        elif os.getenv(var_name):
            result[var_name] = {
                "status": "set",
                "masked": mask_value(os.getenv(var_name)),
                "source": "env",
            }
        else:
            result[var_name] = {"status": "empty", "masked": "", "source": "none"}
    return {"source_id": src.id, "source_name": src.name, "credentials": result}


@router.put("/api/admin/sources/{source_id}/credentials")
@limiter.limit("10/minute")
def api_set_credentials(
    source_id: int,
    request: Request,
    body: SourceCredentialsUpdate,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Set credential values for a source.

    Body: {VAR_NAME: "plaintext_value", ...}
    """
    from . import encrypt_value

    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    valid_vars = set(src.env_vars or [])
    creds = dict(src.credentials or {})
    updated = []
    for var_name, value in body.model_dump(exclude_unset=True).items():
        if var_name not in valid_vars:
            continue
        value = (value or "").strip()
        if value:
            creds[var_name] = encrypt_value(value)
        else:
            creds.pop(var_name, None)
        updated.append(var_name)
    src.credentials = creds
    db.commit()
    logger.info(f"Credentials updated for {src.name} by {user.email}: {updated}")
    return {"status": "ok", "updated": updated}


@router.delete("/api/admin/sources/{source_id}/credentials/{var_name}")
@limiter.limit("5/minute")
def api_delete_credential(
    source_id: int,
    var_name: str,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Remove a single credential from a source."""
    from ...services.credential_service import credential_is_set

    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    creds = dict(src.credentials or {})
    removed = creds.pop(var_name, None)
    src.credentials = creds
    # Recheck status -- if credentials are now incomplete, downgrade to pending
    env_vars = src.env_vars or []
    if env_vars and src.status == ApiSourceStatus.LIVE:
        all_set = all(credential_is_set(db, src.name, v) for v in env_vars)
        if not all_set:
            src.status = ApiSourceStatus.PENDING
    db.commit()
    logger.info(f"Credential {var_name} removed from {src.name} by {user.email}")
    return {"status": "removed" if removed else "not_found"}


# -- Material Card Integrity (admin) ---------------------------------------


@router.get("/api/admin/integrity")
@limiter.limit("10/minute")
def api_integrity_check(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Run material card integrity checks and return health report."""
    from ...services.integrity_service import run_integrity_check

    return run_integrity_check(db)


@router.get("/api/admin/material-audit")
@limiter.limit("10/minute")
def api_material_audit(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """View recent material card audit log entries."""
    from ...models import MaterialCardAudit

    card_id = request.query_params.get("card_id")
    action = request.query_params.get("action")
    try:
        limit = min(int(request.query_params.get("limit", "100")), 500)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except (ValueError, TypeError):
        raise HTTPException(400, "limit and offset must be integers")

    query = db.query(MaterialCardAudit)
    if card_id:
        try:
            card_id_int = int(card_id)
        except (ValueError, TypeError):
            raise HTTPException(400, "card_id must be an integer")
        query = query.filter(MaterialCardAudit.material_card_id == card_id_int)
    if action:
        query = query.filter(MaterialCardAudit.action == action)
    total = query.count()
    entries = query.order_by(MaterialCardAudit.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [
            {
                "id": e.id,
                "material_card_id": e.material_card_id,
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "old_card_id": e.old_card_id,
                "new_card_id": e.new_card_id,
                "normalized_mpn": e.normalized_mpn,
                "details": e.details,
                "created_at": _iso(e.created_at),
                "created_by": e.created_by,
            }
            for e in entries
        ],
    }


# -- Graph Subscription Health (settings_access) ---------------------------


@router.get("/api/admin/subscription-health")
@limiter.limit("30/minute")
def api_subscription_health(
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Return Graph subscription health data — counts, expiry, failure stats.

    Returns all graph_subscriptions rows with their health columns so the
    admin can observe renewal failures before they silently degrade email tracking.
    Read-only: no writes.
    """
    subs = db.query(GraphSubscription).order_by(GraphSubscription.expiration_dt.asc()).all()
    return {
        "subscriptions": [
            {
                "id": s.id,
                "user_id": s.user_id,
                "subscription_id": s.subscription_id,
                "resource": s.resource,
                "expiration_dt": _iso(s.expiration_dt),
                "renew_fail_count": s.renew_fail_count,
                "last_error": s.last_error,
                "last_renewed_at": _iso(s.last_renewed_at),
            }
            for s in subs
        ]
    }


@router.get("/api/admin/workers/status")
def get_workers_status(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Liveness + queue snapshot for the sourcing-engine workers (ICS, NC, enrichment).

    Glanceable "are they working?" surface: heartbeat age, stale flag, circuit-breaker
    state, today's counts, and queue depth.
    """
    from ...config import settings
    from ...models import EnrichmentWorkerStatus, IcsWorkerStatus, NcWorkerStatus, TbfWorkerStatus
    from ...services.ics_worker.queue_manager import get_queue_stats as ics_queue_stats
    from ...services.nc_worker.queue_manager import get_queue_stats as nc_queue_stats
    from ...services.tbf_worker.queue_manager import get_queue_stats as tbf_queue_stats

    now = datetime.now(timezone.utc)
    stale_secs = settings.worker_heartbeat_stale_minutes * 60

    def _age(dt):
        if dt is None:
            return None
        d = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return int((now - d).total_seconds())

    def _worker(name, row, queue=None):
        if row is None:
            return {"name": name, "present": False}
        age = _age(row.last_heartbeat)
        out = {
            "name": name,
            "present": True,
            "is_running": bool(row.is_running),
            "last_heartbeat": _iso(row.last_heartbeat),
            "heartbeat_age_seconds": age,
            "stale": bool(row.is_running and (age is None or age > stale_secs)),
            "circuit_breaker_open": bool(getattr(row, "circuit_breaker_open", False)),
            "circuit_breaker_reason": getattr(row, "circuit_breaker_reason", None),
        }
        if queue is not None:
            out["queue"] = queue
        return out

    return {
        "checked_at": _iso(now),
        "stale_threshold_seconds": stale_secs,
        "workers": [
            _worker("ics", db.get(IcsWorkerStatus, 1), ics_queue_stats(db)),
            _worker("netcomponents", db.get(NcWorkerStatus, 1), nc_queue_stats(db)),
            _worker("thebrokersite", db.get(TbfWorkerStatus, 1), tbf_queue_stats(db)),
            _worker("enrichment", db.get(EnrichmentWorkerStatus, 1)),
        ],
    }
