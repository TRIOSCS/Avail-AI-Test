"""graph_app_auth.py — app-only (client-credentials) Microsoft Graph token.

For org-wide, user-independent Graph writes (the datasheet company library). Distinct
from the delegated per-user token in utils.token_manager. Requires the Azure app to hold
the Sites.Selected application permission (admin-consented), scoped to the library's
site.
"""

from __future__ import annotations

import time

from loguru import logger

from ..config import settings
from ..http_client import http

# {"token": str, "expires_at": float}
_TOKEN_CACHE: dict[str, object] = {}


async def get_app_graph_token() -> str | None:
    """Return a cached app-only Graph token, or None if unavailable."""
    now = time.monotonic()
    cached = _TOKEN_CACHE.get("token")
    if cached and now < float(_TOKEN_CACHE.get("expires_at", 0)) - 300:
        return str(cached)
    if not (settings.azure_client_id and settings.azure_client_secret and settings.azure_tenant_id):
        return None
    url = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0/token"
    try:
        r = await http.post(
            url,
            data={
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
    except Exception:
        logger.warning("app-only Graph token request errored", exc_info=True)
        return None
    if r.status_code != 200:
        logger.warning("app-only Graph token failed: {} {}", r.status_code, r.text[:200])
        return None
    body = r.json()
    token: str | None = body.get("access_token")  # OAuth JSON boundary
    if not token:
        return None
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expires_at"] = now + int(body.get("expires_in", 3600))
    return token
