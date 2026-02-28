"""Clay OAuth2 client — company enrichment and contact discovery.

Clay deprecated their REST API key auth. This connector uses OAuth2
authorization code flow with PKCE to obtain and refresh tokens.

Token lifecycle:
  1. Admin clicks "Connect Clay" → redirected to Clay's authorize URL
  2. Callback exchanges auth code for access + refresh tokens
  3. Tokens stored in clay_oauth_tokens table (single row)
  4. On each API call, check expiry and refresh if needed

Called by: enrichment_service._clay_find_company/contacts,
           customer_enrichment_service._step_clay
Depends on: app.config.settings, app.http_client.http, app.database
"""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from loguru import logger

from app.config import settings
from app.http_client import http

CLAY_AUTH_URL = "https://app.clay.com/oauth/authorize"
CLAY_TOKEN_URL = "https://api.clay.com/oauth/token"
CLAY_API_BASE = "https://api.clay.com/v3/sources"

_semaphore = asyncio.Semaphore(5)

# In-memory PKCE verifier cache (short-lived, cleared after callback)
_pkce_verifiers: dict[str, str] = {}


def generate_pkce_challenge() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    import base64
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(state: str | None = None) -> tuple[str, str]:
    """Build the Clay OAuth2 authorization URL with PKCE.

    Returns (authorize_url, state).
    """
    if not settings.clay_client_id:
        raise ValueError("CLAY_CLIENT_ID not configured")

    verifier, challenge = generate_pkce_challenge()
    if not state:
        state = secrets.token_urlsafe(32)

    _pkce_verifiers[state] = verifier

    params = {
        "response_type": "code",
        "client_id": settings.clay_client_id,
        "redirect_uri": settings.clay_redirect_uri,
        "scope": "mcp mcp:run-enrichment",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{CLAY_AUTH_URL}?{urlencode(params)}"
    return url, state


async def exchange_code_for_tokens(code: str, state: str) -> dict | None:
    """Exchange an authorization code for access + refresh tokens.

    Returns dict with access_token, refresh_token, expires_in, scope
    or None on failure.
    """
    verifier = _pkce_verifiers.pop(state, None)
    if not verifier:
        logger.error("No PKCE verifier found for state=%s", state)
        return None

    try:
        resp = await http.post(
            CLAY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.clay_redirect_uri,
                "client_id": settings.clay_client_id,
                "client_secret": settings.clay_client_secret,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Clay token exchange failed: %s %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except Exception as e:
        logger.error("Clay token exchange error: %s", e)
        return None


async def refresh_clay_token(refresh_token: str) -> dict | None:
    """Refresh an expired Clay access token.

    Returns dict with new access_token, refresh_token, expires_in
    or None on failure.
    """
    try:
        resp = await http.post(
            CLAY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.clay_client_id,
                "client_secret": settings.clay_client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Clay token refresh failed: %s %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()
    except Exception as e:
        logger.error("Clay token refresh error: %s", e)
        return None


def _get_token_from_db(db) -> "ClayOAuthToken | None":
    """Fetch the current Clay OAuth token row from DB."""
    from app.models.enrichment import ClayOAuthToken
    return db.query(ClayOAuthToken).first()


def _save_token_to_db(db, token_data: dict) -> None:
    """Upsert Clay OAuth token row."""
    from app.models.enrichment import ClayOAuthToken

    expires_in = token_data.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    existing = db.query(ClayOAuthToken).first()
    if existing:
        existing.access_token = token_data["access_token"]
        existing.refresh_token = token_data.get("refresh_token", existing.refresh_token)
        existing.expires_at = expires_at
        existing.scope = token_data.get("scope", existing.scope)
        existing.updated_at = datetime.now(timezone.utc)
    else:
        row = ClayOAuthToken(
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=expires_at,
            scope=token_data.get("scope", "mcp mcp:run-enrichment"),
        )
        db.add(row)
    db.flush()


async def get_valid_token(db) -> str | None:
    """Get a valid Clay access token, refreshing if expired.

    Returns the access_token string or None if no token is stored
    or refresh fails.
    """
    token_row = _get_token_from_db(db)
    if not token_row:
        logger.debug("No Clay OAuth token stored — skipping")
        return None

    # Refresh 5 minutes before expiry (handle naive datetimes from SQLite)
    expires = token_row.expires_at
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires and expires < datetime.now(timezone.utc) + timedelta(minutes=5):
        logger.info("Clay token expired, refreshing...")
        new_tokens = await refresh_clay_token(token_row.refresh_token)
        if not new_tokens:
            logger.warning("Clay token refresh failed — connection may need re-authorization")
            return None
        _save_token_to_db(db, new_tokens)
        db.flush()
        return new_tokens["access_token"]

    return token_row.access_token


async def find_contacts(
    domain: str, title_keywords: str = "", limit: int = 10, *, db=None
) -> list[dict]:
    """Find contacts at a company via Clay's find-people endpoint.

    Returns list of contact dicts or [] on failure.
    """
    if not db:
        return []

    token = await get_valid_token(db)
    if not token:
        return []

    async with _semaphore:
        try:
            payload: dict = {"domain": domain}
            if title_keywords:
                payload["title"] = title_keywords
            if limit:
                payload["limit"] = limit

            resp = await http.post(
                f"{CLAY_API_BASE}/find-people",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )
            if resp.status_code != 200:
                logger.warning("Clay find_contacts failed: %s %s", resp.status_code, resp.text[:200])
                return []

            people = resp.json().get("people") or resp.json().get("contacts") or []
            return [
                {
                    "source": "clay",
                    "full_name": p.get("name") or p.get("full_name"),
                    "title": p.get("title") or p.get("latest_experience_title"),
                    "email": p.get("email"),
                    "phone": p.get("phone"),
                    "linkedin_url": p.get("linkedin_url") or p.get("url"),
                    "location": p.get("location_name") or p.get("location"),
                    "company": p.get("company") or p.get("latest_experience_company"),
                }
                for p in people
                if p.get("name") or p.get("full_name")
            ]
        except Exception as e:
            logger.error("Clay find_contacts error: %s", e)
            return []


async def enrich_company(domain: str, *, db=None) -> dict | None:
    """Enrich a company by domain via Clay.

    Returns normalized company dict or None on failure.
    """
    if not db:
        return None

    token = await get_valid_token(db)
    if not token:
        return None

    async with _semaphore:
        try:
            resp = await http.post(
                f"{CLAY_API_BASE}/enrich-company",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"domain": domain},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("Clay enrich_company failed: %s %s", resp.status_code, resp.text[:200])
                return None

            data = resp.json()
            return {
                "source": "clay",
                "legal_name": data.get("name"),
                "domain": domain,
                "linkedin_url": data.get("linkedin_url") or data.get("url"),
                "industry": data.get("industry"),
                "employee_size": data.get("size"),
                "hq_city": data.get("locality", "").split(",")[0].strip()
                if data.get("locality")
                else None,
                "hq_state": data.get("locality", "").split(",")[-1].strip()
                if data.get("locality") and "," in data.get("locality", "")
                else None,
                "hq_country": data.get("country"),
                "website": data.get("website"),
            }
        except Exception as e:
            logger.error("Clay enrich_company error: %s", e)
            return None
