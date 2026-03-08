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

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_admin, require_settings_access
from ...models import ApiSource, User
from ...models.config import ApiUsageLog
from ...rate_limit import limiter
from ...services.admin_service import get_all_config, get_system_health, set_config_value

router = APIRouter(tags=["admin"])


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


@router.put("/api/admin/config/{key}")
@limiter.limit("10/minute")
def api_set_config(
    key: str,
    request: Request,
    body: ConfigUpdateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = set_config_value(db, key, body.value, user.email)
    if "error" in result:
        raise HTTPException(result.get("status", 400), result["error"])
    return result


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
        status = src.status or "pending"
        if errors_24h >= 4 and total > 0:
            recent_success = max(0, total - errors_24h)
            if errors_24h > recent_success:
                status = "degraded"
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
                "last_success": src.last_success.isoformat() if src.last_success else None,
                "last_error": src.last_error,
                "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
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
                "last_success": src.last_success.isoformat() if src.last_success else None,
                "last_error": src.last_error,
                "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
                "error_count_24h": src.error_count_24h or 0,
                "avg_response_ms": src.avg_response_ms or 0,
                "total_searches": src.total_searches or 0,
                "monthly_quota": quota,
                "calls_this_month": calls,
                "usage_pct": usage_pct,
                "last_ping_at": src.last_ping_at.isoformat() if src.last_ping_at else None,
                "last_deep_test_at": src.last_deep_test_at.isoformat() if src.last_deep_test_at else None,
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
                logger.warning("Credential decryption failed for %s", var_name, exc_info=True)
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
    body: dict,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Set credential values for a source. Body: {VAR_NAME: "plaintext_value", ...}"""
    from . import encrypt_value

    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "Source not found")
    valid_vars = set(src.env_vars or [])
    creds = dict(src.credentials or {})
    updated = []
    for var_name, value in body.items():
        if var_name not in valid_vars:
            continue
        value = (value or "").strip()
        if value:
            creds[var_name] = encrypt_value(value)
            updated.append(var_name)
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
    if env_vars and src.status == "live":
        all_set = all(credential_is_set(db, src.name, v) for v in env_vars)
        if not all_set:
            src.status = "pending"
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
    limit = min(int(request.query_params.get("limit", "100")), 500)
    offset = max(int(request.query_params.get("offset", "0")), 0)

    query = db.query(MaterialCardAudit)
    if card_id:
        query = query.filter(MaterialCardAudit.material_card_id == int(card_id))
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
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "created_by": e.created_by,
            }
            for e in entries
        ],
    }
