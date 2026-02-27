"""token_manager.py — Azure AD token lifecycle management.

Provides get_valid_token() for background jobs and refresh_user_token()
for foreground request auth. Extracted from scheduler.py for cleaner imports.

Called by: dependencies.py, routers (admin, enrichment, proactive, sources),
          services (buyplan, buyplan_v3_notifications, deep_enrichment,
          ownership, teams, webhook)
Depends on: config.py, http_client.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger

from ..http_client import http


def _utc(dt):
    """Make a naive datetime UTC-aware (no-op if already aware)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def get_valid_token(user, db) -> str | None:
    """Get a valid Graph API token for user, refreshing if expired/near-expiry.

    Use this before EVERY Graph API call (background or foreground).
    Returns access_token string or None if refresh fails.
    """
    # Check if current token is still valid (with 5-min buffer)
    if user.access_token and user.token_expires_at:
        if datetime.now(timezone.utc) < _utc(user.token_expires_at) - timedelta(minutes=5):
            return user.access_token

    # Token expired or near-expiry — refresh it
    token = await refresh_user_token(user, db)
    if token:
        user.m365_last_healthy = datetime.now(timezone.utc)
        user.m365_error_reason = None
        db.commit()
    else:
        user.m365_error_reason = "Token refresh failed"
        db.commit()
    return token


async def refresh_user_token(user, db) -> str | None:
    """Refresh a single user's Azure token. Returns new access_token or None."""
    from ..config import settings

    if not user.refresh_token:
        return None

    result = await _refresh_access_token(
        user.refresh_token,
        settings.azure_client_id,
        settings.azure_client_secret,
        settings.azure_tenant_id,
    )
    if not result:
        user.m365_connected = False
        db.commit()
        logger.warning(f"Token refresh failed for {user.email}")
        return None

    access_token, new_refresh = result
    user.access_token = access_token
    user.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    user.m365_connected = True
    if new_refresh:
        user.refresh_token = new_refresh
    db.commit()
    logger.info(f"Token refreshed for {user.email}")
    return access_token


async def _refresh_access_token(
    refresh_token: str, client_id: str, client_secret: str, tenant_id: str
) -> tuple[str, str | None] | None:
    """Use a refresh token to get a new access token from Azure AD.

    Returns (access_token, new_refresh_token_or_None) or None on failure.
    """
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    try:
        r = await http.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": "openid profile email offline_access Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read Calendars.Read ChannelMessage.Send Team.ReadBasic.All",
            },
            timeout=15,
        )

        if r.status_code != 200:
            logger.warning(f"Token refresh failed: {r.status_code} — {r.text[:200]}")
            return None

        tokens = r.json()
        return (tokens.get("access_token"), tokens.get("refresh_token"))

    except Exception as e:
        logger.warning(f"Token refresh error: {e}")
        return None
