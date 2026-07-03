"""Clay OAuth connect/callback/disconnect routes (admin-only).

One-time interactive flow to authorize the AvailAI backend against Clay's MCP.
Called by: the Settings → Connectors "Connect Clay" card. Depends on:
app/services/clay_oauth, app/cache/intel_cache (state store), app/dependencies (admin gate).

State lifecycle: /connect stores state→{verifier, client_id} with a 10-minute TTL in
intel_cache. /callback looks up and immediately expires the entry (1-second TTL re-set
— intel_cache.set_cached cannot store None, so this is the invalidation mechanism).
"""

import json
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from loguru import logger

from app.cache.intel_cache import get_cached, set_cached
from app.dependencies import require_admin
from app.services import clay_oauth

router = APIRouter()

# connect/callback are full browser navigations (the "Connect Clay" <a href>, and
# Clay's redirect back) — they must land on the full settings page, NOT the HTMX
# partial. A direct browser hit to a partial renders a bare fragment with no base
# layout (the "broken page" symptom). disconnect is an hx-post (hx-swap="none"), so
# its redirect target stays the partial — base.html would corrupt the swap.
_FULL_SETTINGS_URL = "/v2/settings"
_PARTIAL_SETTINGS_URL = "/v2/partials/settings/connectors"
_STATE_PREFIX = "clay:oauth:state:"
_STATE_TTL_DAYS = 10 / 1440  # 10 minutes
_EXPIRE_TTL_DAYS = 1 / 86400  # ~1 second — one-time consume (set_cached cannot store None)


@router.get("/auth/clay/connect")
async def connect(request: Request, _: object = Depends(require_admin)) -> RedirectResponse:
    """Kick off the Clay OAuth flow: DCR (or reuse) → PKCE → redirect to Clay."""
    try:
        client_id = await clay_oauth.register_client()
    except Exception as exc:
        logger.error("Clay connect (DCR) failed: {}", exc)
        return RedirectResponse(f"{_FULL_SETTINGS_URL}?clay=error", status_code=302)

    verifier, challenge = clay_oauth.pkce_pair()
    state = secrets.token_urlsafe(32)
    set_cached(
        f"{_STATE_PREFIX}{state}",
        {"verifier": verifier, "client_id": client_id},
        ttl_days=_STATE_TTL_DAYS,
    )
    return RedirectResponse(clay_oauth.build_authorize_url(client_id, state, challenge), status_code=302)


@router.get("/auth/clay/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    _: object = Depends(require_admin),
) -> RedirectResponse:
    """Handle Clay's authorization redirect; exchange code for tokens."""
    err = request.query_params.get("error")
    if err or not code or not state:
        logger.warning("Clay callback error/missing params: {}", err or "no code/state")
        return RedirectResponse(f"{_FULL_SETTINGS_URL}?clay=error", status_code=302)

    stash = get_cached(f"{_STATE_PREFIX}{state}")
    if not stash:
        logger.warning("Clay callback unknown/expired state")
        return RedirectResponse(f"{_FULL_SETTINGS_URL}?clay=error", status_code=302)

    verifier = stash.get("verifier")
    client_id = stash.get("client_id")
    if not verifier or not client_id:
        logger.warning("Clay callback state already consumed/invalid")
        return RedirectResponse(f"{_FULL_SETTINGS_URL}?clay=error", status_code=302)

    # Consume the state entry (one-time use) by expiring it to ~1 second.
    # intel_cache.set_cached does not accept None, so a near-zero TTL is the
    # invalidation mechanism.
    set_cached(f"{_STATE_PREFIX}{state}", {"consumed": True}, ttl_days=_EXPIRE_TTL_DAYS)

    ok = await clay_oauth.exchange_code(code, verifier, client_id)
    target = "connected" if ok else "error"
    return RedirectResponse(f"{_FULL_SETTINGS_URL}?clay={target}", status_code=302)


@router.post("/auth/clay/disconnect")
async def disconnect(request: Request, _: object = Depends(require_admin)) -> RedirectResponse:
    """Revoke stored Clay OAuth tokens and remove them from the DB."""
    clay_oauth.disconnect()
    resp = RedirectResponse(f"{_PARTIAL_SETTINGS_URL}?clay=disconnected", status_code=302)
    # Surface success feedback — htmx reads HX-Trigger off the redirect response
    # before following it, so the toast fires even though the body is a redirect.
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "Clay disconnected.", "type": "success"}})
    return resp
