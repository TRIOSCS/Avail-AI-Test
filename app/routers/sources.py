"""routers/sources.py — Data Source Management.

Manages API source configuration: list, per-source live test, is_active
toggle, and encrypted credential saves.

Business Rules:
- Sources auto-detect status from env vars on list
- Test runs a live connector probe and persists real ok/error health

Called by: main.py (router mount)
Depends on: models, config, dependencies, connectors/, services/
"""

import json
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from sqlalchemy.orm import Session

from ..connectors.sources import run_health_probe
from ..constants import AccessKey, ApiSourceStatus
from ..database import get_db
from ..dependencies import require_access, require_user
from ..models import ApiSource, User
from ..rate_limit import limiter
from ..schemas.responses import (
    ApiTestResponse,
    SourceListResponse,
    ToggleActiveResponse,
)
from ..services.connector_registry import get_connector_for_source as _get_connector_for_source

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────
#
# Connector lookup (``get_connector_for_source``, imported here as
# ``_get_connector_for_source`` for this router's own call site below), the
# keyless Test-button connector shims (``AnthropicTestConnector`` etc.), and
# ``source_has_test_path`` all live in ``app.services.connector_registry`` now
# (P4.1 — a router must not own business logic another service needs; this
# router previously defined all of it and health_monitor.py lazily reached
# into it, an inverted-layering violation).


# ══════════════════════════════════════════════════════════════════════
# API SOURCES — Data Source Management & Tracking
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/sources", response_model=SourceListResponse, response_model_exclude_none=True)
async def list_api_sources(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Return all API sources grouped by status."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()

    from ..services.credential_service import credential_is_set, get_credential, mask_value

    for src in sources:
        env_vars = src.env_vars or []
        if env_vars:
            any_set = any(credential_is_set(db, src.name, v) for v in env_vars)
            # Only downgrade to pending if ALL credentials are missing
            # Never auto-upgrade to "live" — that's the health checker's job
            if not any_set and src.status not in (ApiSourceStatus.DISABLED, ApiSourceStatus.ERROR):
                src.status = ApiSourceStatus.PENDING
    db.commit()

    result = []
    for src in sources:
        env_status = {}
        credentials_masked = {}
        for v in src.env_vars or []:
            is_set = credential_is_set(db, src.name, v)
            env_status[v] = is_set
            if is_set:
                plain = get_credential(db, src.name, v)
                credentials_masked[v] = mask_value(plain) if plain else ""

        result.append(
            {
                "id": src.id,
                "name": src.name,
                "display_name": src.display_name,
                "category": src.category,
                "source_type": src.source_type,
                "status": src.status,
                "is_active": src.is_active,
                "description": src.description,
                "setup_notes": src.setup_notes,
                "signup_url": src.signup_url,
                "env_vars": src.env_vars or [],
                "env_status": env_status,
                "credentials_masked": credentials_masked,
                "last_success": src.last_success.isoformat() if src.last_success else None,
                "last_error": src.last_error,
                "total_searches": src.total_searches or 0,
                "total_results": src.total_results or 0,
                "avg_response_ms": src.avg_response_ms or 0,
                "created_at": src.created_at.isoformat() if src.created_at else None,
                "last_error_at": src.last_error_at.isoformat() if src.last_error_at else None,
                "error_count_24h": src.error_count_24h or 0,
                "monthly_quota": src.monthly_quota,
                "calls_this_month": src.calls_this_month or 0,
                "last_ping_at": src.last_ping_at.isoformat() if src.last_ping_at else None,
            }
        )

    return {"sources": result}


_TEST_MPN = "LM358N"


async def _probe_source(src: ApiSource, db: Session) -> dict:
    """Run the live connector search for one source WITHOUT persisting anything.

    Returns ``{"results": [...], "elapsed_ms": int, "error": str | None}`` and never
    raises. Split from persistence so the "Test all" sweep can fan these out
    concurrently (network I/O overlaps) and then write status sequentially on one
    session — running ``db.commit()`` from concurrent coroutines on a shared session
    would race. The connector is built here synchronously (credential reads only, no
    await) before the first ``await``, so concurrent probes never interleave DB reads.
    """
    start = time.time()
    try:
        connector = _get_connector_for_source(src.name, db)
        if not connector:
            raise ValueError(f"No connector available for {src.name}")
        # Bypass the open-circuit short-circuit so a transient in-search breaker trip
        # doesn't make Test falsely report ERROR — Test must reflect genuine upstream
        # health (see connectors.sources.run_health_probe / BaseConnector.health_probe).
        results = await run_health_probe(connector, _TEST_MPN)
        return {"results": results, "elapsed_ms": int((time.time() - start) * 1000), "error": None}
    except Exception as e:
        return {"results": [], "elapsed_ms": int((time.time() - start) * 1000), "error": str(e)[:500]}


def _persist_test_result(src: ApiSource, db: Session, *, results: list, elapsed_ms: int, error: str | None) -> dict:
    """Persist one probe outcome to the ApiSource row and return the result dict.

    Records the real ok/error status for every testable source — including keyless
    ones (ai_live_web, Clay, Teams). The old ``has_env_vars`` gate skipped persistence
    for keyless sources, so a keyless Test's result was silently discarded and the card
    stayed "all OK". A source only reaches here when a real test path exists, so
    recording its status is always meaningful.
    """
    if error is None:
        src.status = ApiSourceStatus.LIVE
        src.last_success = datetime.now(UTC)
        src.last_error = None
        src.avg_response_ms = elapsed_ms
    else:
        src.status = ApiSourceStatus.ERROR
        src.last_error = error
    db.commit()

    if results:
        status = "ok"
    elif not error:
        status = "no_results"
    else:
        status = "error"

    return {
        "source": src.display_name,
        "test_mpn": _TEST_MPN,
        "status": status,
        "results_count": len(results),
        "elapsed_ms": elapsed_ms,
        "error": error,
        "sample": results[:3] if results else [],
    }


async def run_source_test(src: ApiSource, db: Session) -> dict:
    """Run a live part-search probe against one source, persisting its health.

    Shared by the per-source Test endpoint and the Connectors "Test all" sweep.
    Tolerates connector failures (records them as `error`) and never raises.
    """
    outcome = await _probe_source(src, db)
    return _persist_test_result(
        src, db, results=outcome["results"], elapsed_ms=outcome["elapsed_ms"], error=outcome["error"]
    )


def _test_toast_header(result: dict) -> str:
    """Build the ``HX-Trigger`` payload (a ``showToast`` event) for a single Test.

    Gives the per-source Test button real pass/fail feedback — the JSON body is
    discarded by ``hx-swap="none"``, so without this a re-test on a Live source was
    zero-feedback. Bridged client-side by the ``showToast`` listener in htmx_app.js.
    """
    name = result["source"]
    if result["status"] == "ok":
        message = f"{name}: Live — {result['results_count']} result(s) in {result['elapsed_ms']}ms"
        kind = "success"
    elif result["status"] == "no_results":
        message = f"{name}: connected but returned no results ({result['elapsed_ms']}ms)"
        kind = "info"
    else:
        message = f"{name}: error — {result.get('error') or 'test failed'}"
        kind = "error"
    return json.dumps({"showToast": {"message": message, "type": kind}})


@router.post("/api/sources/{source_id}/test", response_model=ApiTestResponse)
@limiter.limit("5/minute")
async def test_api_source(
    source_id: int,
    request: Request,
    response: Response,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Test a specific API source with a known part number."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "API source not found")

    result = await run_source_test(src, db)
    response.headers["HX-Trigger"] = _test_toast_header(result)
    return result


@router.put("/api/sources/{source_id}/activate", response_model=ToggleActiveResponse)
async def toggle_source_active(
    source_id: int,
    response: Response,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Toggle is_active flag on a source (admins + MANAGE_CONNECTORS holders)."""
    from ..routers.htmx.settings import settings_toast

    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "API source not found")
    src.is_active = not src.is_active
    db.commit()
    name = src.display_name or src.name
    settings_toast(response, f"{name} {'enabled' if src.is_active else 'disabled'}.")
    return {"ok": True, "is_active": src.is_active}


# ══════════════════════════════════════════════════════════════════════
# CREDENTIAL MANAGEMENT
# ══════════════════════════════════════════════════════════════════════


@router.put("/api/sources/{source_name}/credentials")
async def update_source_credentials(
    source_name: str,
    request: Request,
    response: Response,
    user: User = Depends(require_access(AccessKey.MANAGE_CONNECTORS)),
    db: Session = Depends(get_db),
):
    """Save encrypted credentials for an API source (admins + MANAGE_CONNECTORS
    holders).

    Skips blank values (preserves existing).
    """
    from ..routers.htmx.settings import settings_toast
    from ..services.credential_service import _cred_cache, encrypt_value

    src = db.query(ApiSource).filter_by(name=source_name).first()
    if not src:
        raise HTTPException(
            status_code=404,
            detail={"error": f"Source '{source_name}' not found", "status_code": 404},
        )
    raw = await request.json()
    credentials = raw.get("credentials") if isinstance(raw, dict) else None
    if not credentials:
        raise HTTPException(
            status_code=400,
            detail={"error": "credentials field required", "status_code": 400},
        )
    current = dict(src.credentials or {})
    for key, value in credentials.items():
        if value and str(value).strip():
            current[key] = encrypt_value(str(value).strip())
    src.credentials = current
    db.commit()
    keys_to_clear = [k for k in list(_cred_cache) if k[0] == source_name]
    for k in keys_to_clear:
        _cred_cache.pop(k, None)
    logger.info("Credentials updated for source '{}' by user {}", source_name, user.email)
    # A single-key source ("Save key") vs. a multi-field one ("Save credentials").
    label = "Key saved." if len(src.env_vars or []) <= 1 else "Credentials saved."
    settings_toast(response, label)
    return {"saved": True, "source": source_name}
