"""Clay OAuth token lifecycle (authorization_code + PKCE + DCR) for the headless MCP.

Clay's MCP (api.clay.com/v3/mcp) is OAuth-gated (no client_credentials grant), so the
backend holds an access+refresh token obtained via a one-time interactive login and
auto-refreshes it. Tokens are stored encrypted in ApiSource('clay_enrichment').

Called by: app/routers/clay_oauth.py (connect/callback), app/connectors/clay_mcp.py
(get_access_token). Depends on: app/http_client.py, app/services/credential_service,
app/database, app/models.ApiSource.
"""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from loguru import logger

from app.config import settings
from app.database import SessionLocal
from app.http_client import http
from app.models import ApiSource
from app.services import credential_service as cs

CLAY_AUTHORIZE_URL = "https://app.clay.com/oauth/authorize"
CLAY_TOKEN_URL = "https://api.clay.com/oauth/token"
CLAY_REGISTER_URL = "https://api.clay.com/oauth/register"
CLAY_SCOPE = "mcp"
_SOURCE = "clay_enrichment"
_REFRESH_BUFFER = timedelta(minutes=5)


def _redirect_uri() -> str:
    return f"{settings.app_url}/auth/clay/callback"


def _store(updates: dict[str, str | None]) -> None:
    """Encrypt+persist (or delete when value is None) CLAY_OAUTH_* keys; bust cred
    cache."""
    db = SessionLocal()
    try:
        s = db.query(ApiSource).filter_by(name=_SOURCE).first()
        if s is None:
            s = ApiSource(name=_SOURCE, credentials={})
            db.add(s)
        creds = dict(s.credentials or {})
        for k, v in updates.items():
            if v is None:
                creds.pop(k, None)
            else:
                creds[k] = cs.encrypt_value(v)
        s.credentials = creds
        db.commit()
    finally:
        db.close()
    cs._cred_cache.clear()


def _load(key: str) -> str | None:
    return cs.get_credential_cached(_SOURCE, key)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(client_id: str, state: str, code_challenge: str) -> str:
    return f"{CLAY_AUTHORIZE_URL}?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _redirect_uri(),
            "scope": CLAY_SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )


async def register_client() -> str:
    existing = _load("CLAY_OAUTH_CLIENT_ID")
    if existing:
        return existing
    resp = await http.post(
        CLAY_REGISTER_URL,
        json={
            "client_name": "AvailAI",
            "redirect_uris": [_redirect_uri()],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": CLAY_SCOPE,
        },
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        logger.error("Clay DCR failed: {}", resp.status_code)
        raise RuntimeError(f"Clay client registration failed: {resp.status_code}")
    cid = resp.json().get("client_id")
    if not cid:
        raise RuntimeError("Clay DCR response missing client_id")
    _store({"CLAY_OAUTH_CLIENT_ID": cid})
    return cid


def _persist_tokens(tok: dict) -> None:
    if not tok.get("access_token"):
        return
    expires_at = datetime.now(UTC) + timedelta(seconds=int(tok.get("expires_in", 3600)))
    updates: dict[str, str | None] = {
        "CLAY_OAUTH_ACCESS_TOKEN": tok.get("access_token"),
        "CLAY_OAUTH_EXPIRES_AT": expires_at.isoformat(),
        "CLAY_OAUTH_NEEDS_RECONNECT": None,
    }
    if tok.get("refresh_token"):  # rotation-aware: keep old if not returned
        updates["CLAY_OAUTH_REFRESH_TOKEN"] = tok["refresh_token"]
    _store(updates)


async def exchange_code(code: str, code_verifier: str, client_id: str) -> bool:
    resp = await http.post(
        CLAY_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _redirect_uri(),
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    if resp.status_code != 200 or not resp.json().get("access_token"):
        logger.error("Clay code exchange failed: {}", resp.status_code)
        return False
    _persist_tokens(resp.json())
    return True


async def refresh() -> str | None:
    rt = _load("CLAY_OAUTH_REFRESH_TOKEN")
    cid = _load("CLAY_OAUTH_CLIENT_ID")
    if not rt or not cid:
        return None
    resp = await http.post(
        CLAY_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": cid,
        },
        timeout=15,
    )
    if resp.status_code != 200 or not resp.json().get("access_token"):
        logger.warning("Clay refresh failed ({}) — needs reconnect", resp.status_code)
        _store({"CLAY_OAUTH_ACCESS_TOKEN": None, "CLAY_OAUTH_NEEDS_RECONNECT": "1"})
        return None
    _persist_tokens(resp.json())
    return _load("CLAY_OAUTH_ACCESS_TOKEN")


async def get_access_token() -> str | None:
    at = _load("CLAY_OAUTH_ACCESS_TOKEN")
    exp = _load("CLAY_OAUTH_EXPIRES_AT")
    if not at:
        return await refresh() if _load("CLAY_OAUTH_REFRESH_TOKEN") else None
    if not exp:
        return await refresh()
    try:
        if datetime.fromisoformat(exp) - _REFRESH_BUFFER <= datetime.now(UTC):
            return await refresh()
    except ValueError:
        return await refresh()
    return at


def is_connected() -> bool:
    return bool(_load("CLAY_OAUTH_REFRESH_TOKEN")) and not needs_reconnect()


def needs_reconnect() -> bool:
    return _load("CLAY_OAUTH_NEEDS_RECONNECT") == "1"


def disconnect() -> None:
    _store(
        {
            k: None
            for k in (
                "CLAY_OAUTH_CLIENT_ID",
                "CLAY_OAUTH_ACCESS_TOKEN",
                "CLAY_OAUTH_REFRESH_TOKEN",
                "CLAY_OAUTH_EXPIRES_AT",
                "CLAY_OAUTH_NEEDS_RECONNECT",
            )
        }
    )
