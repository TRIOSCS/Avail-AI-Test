"""routers/auth.py — Authentication & Session Routes.

Handles Azure AD OAuth login/callback, logout, session status,
and the main index page. All M365 token management lives here.

Business Rules:
- Login via Azure AD OAuth2 code flow
- Tokens stored in DB (not just session) for background job access
- Token refresh handled proactively (15-min buffer) in dependencies.py
- Login gated by an allowlist (ENABLE_USER_ALLOWLIST): unknown emails are rejected
  unless in ADMIN_EMAILS; invited rows adopt their azure_id on first login
- Email normalized to lowercase on login

Called by: main.py (router mount)
Depends on: dependencies, models, config
"""

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..config import GRAPH_SCOPES, settings
from ..constants import UserRole
from ..database import get_db
from ..dependencies import get_user
from ..http_client import http
from ..models import User
from ..rate_limit import limiter

router = APIRouter()

AZURE_AUTH = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0"
SCOPES = GRAPH_SCOPES


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect root to the HTMX frontend."""
    return RedirectResponse(url="/v2/requisitions", status_code=302)


@router.get("/auth/login")
async def login(request: Request):
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = urlencode(
        {
            "client_id": settings.azure_client_id,
            "response_type": "code",
            "redirect_uri": f"{settings.app_url}/auth/callback",
            "scope": SCOPES,
            "response_mode": "query",
            "state": state,
        }
    )
    return RedirectResponse(f"{AZURE_AUTH}/authorize?{params}")


@router.get("/auth/callback")
@limiter.limit("10/minute")
async def callback(request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)):
    # Log Azure error responses so auth failures aren't silent
    error = request.query_params.get("error", "")
    error_desc = request.query_params.get("error_description", "")
    if error:
        logger.warning(f"Azure OAuth error: {error} — {error_desc}")
    if not code:
        logger.warning(f"OAuth callback missing code param (query keys: {list(request.query_params.keys())})")
        return RedirectResponse("/")
    # Validate OAuth state (CSRF protection)
    expected_state = request.session.pop("oauth_state", None)
    if not expected_state or not hmac.compare_digest(state, expected_state):
        logger.warning("OAuth callback state mismatch (possible CSRF)")
        return RedirectResponse("/")
    try:
        resp = await http.post(
            f"{AZURE_AUTH}/token",
            data={
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{settings.app_url}/auth/callback",
                "scope": SCOPES,
            },
            timeout=15,
        )
    except httpx.HTTPError as e:
        logger.error(f"Azure token exchange failed: {e}")
        return RedirectResponse("/")
    if resp.status_code != 200:
        logger.error(f"Azure token exchange returned {resp.status_code}")
        return RedirectResponse("/")
    tokens = resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        logger.error(f"Azure token response missing access_token: {list(tokens.keys())}")
        return RedirectResponse("/")

    # Calculate token expiry
    expires_in = tokens.get("expires_in", 3600)
    token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    try:
        me = await http.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if me.status_code != 200:
            logger.error(f"Graph /me returned {me.status_code}")
            return RedirectResponse("/")
    except httpx.HTTPError as e:
        logger.error(f"Graph /me request failed: {e}")
        return RedirectResponse("/")
    profile = me.json()
    email = (profile.get("mail") or profile.get("userPrincipalName", "")).strip().lower()
    user = db.query(User).filter_by(email=email).first()
    if not user:
        # Allowlist gate: an unknown email is only auto-provisioned when the allowlist
        # is off (legacy posture) or the email is an admin. ADMIN_EMAILS always bypass.
        allowed = (email in settings.admin_emails) or (not settings.enable_user_allowlist)
        if not allowed:
            logger.warning("Login rejected — email not provisioned: {}", email)
            return RedirectResponse("/auth/access-denied")
        user = User(
            email=email,
            name=profile.get("displayName", email),
            azure_id=profile.get("id"),
        )
        db.add(user)
        db.commit()
    elif not user.is_active:
        logger.warning("Login rejected — account disabled: {}", email)
        return RedirectResponse("/auth/access-denied?reason=disabled")
    elif not user.azure_id:
        # Invite adoption: an invited row was pre-provisioned without an azure_id.
        # Bind it on first login. The user's role is left untouched (an invited
        # trader stays a trader).
        user.azure_id = profile.get("id")

    user.last_login_at = datetime.now(timezone.utc)

    # Bootstrap admin: auto-promote users in admin_emails env var
    if user.email.lower() in settings.admin_emails and user.role != UserRole.ADMIN:
        user.role = UserRole.ADMIN
        logger.info(f"Auto-promoted {user.email} to admin via admin_emails bootstrap")

    # Store tokens in DB (not just session) for background jobs
    user.access_token = access_token
    user.token_expires_at = token_expires_at
    user.m365_connected = True
    if tokens.get("refresh_token"):
        user.refresh_token = tokens["refresh_token"]

    db.commit()

    # Fetch mailbox settings (timezone, working hours) on login
    try:
        from ..services.mailbox_intelligence import fetch_and_store_mailbox_settings

        await fetch_and_store_mailbox_settings(access_token, user, db)
    except Exception as e:
        logger.warning(f"Mailbox settings fetch skipped for {user.email}: {e}")

    # Trigger first-time backfill if user has never been scanned
    if not user.last_inbox_scan:
        logger.info(f"New M365 connection for {user.email} — backfill will run on next scheduler tick")

    request.session["user_id"] = user.id
    return RedirectResponse("/")


@router.get("/auth/access-denied", response_class=HTMLResponse)
async def access_denied(request: Request):
    """Branded page shown when a login is rejected by the allowlist gate.

    Default copy covers an un-provisioned email; ``?reason=disabled`` switches to
    the disabled-account message. Self-contained (no app shell) so it renders even
    when there is no session.
    """
    if request.query_params.get("reason") == "disabled":
        heading = "Account disabled"
        body = "Your account has been disabled."
    else:
        heading = "Access not provisioned"
        body = "Your account isn't set up for AvailAI. Contact an administrator to be invited."
    return HTMLResponse(
        content=f"""
<!doctype html>
<html>
  <head>
    <title>{heading} — AvailAI</title>
    <style>
      body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0;
              display: flex; min-height: 100vh; align-items: center; justify-content: center; margin: 0; }}
      .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 2.5rem;
               max-width: 28rem; text-align: center; }}
      h1 {{ font-size: 1.25rem; margin: 0 0 0.75rem; }}
      p {{ color: #94a3b8; line-height: 1.5; margin: 0 0 1.5rem; }}
      a {{ color: #60a5fa; text-decoration: none; }}
      a:hover {{ text-decoration: underline; }}
    </style>
  </head>
  <body>
    <div class="card">
      <h1>{heading}</h1>
      <p>{body}</p>
      <a href="/auth/logout">Sign in with a different account</a>
    </div>
  </body>
</html>
        """,
        media_type="text/html",
    )


@router.post("/auth/logout")
@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/v2/requisitions", status_code=302)


def password_login_env_enabled() -> bool:
    """True iff ENABLE_PASSWORD_LOGIN is set truthy in the environment.

    Single canonical parse of this auth-critical flag — startup.py and the auth
    router all call this instead of re-implementing the ``.lower() == "true"``
    check (which previously lived, subtly copy-pasted, in three places). Read at
    call time (not via config.py's import-time Settings) so the flag can be
    toggled per-process — the behavior the tests and operator rely on.
    """
    return os.getenv("ENABLE_PASSWORD_LOGIN", "false").lower() == "true"


def _password_login_enabled() -> bool:
    """Return True when local/test password login should be allowed.

    Enabled when TESTING=1 or ENABLE_PASSWORD_LOGIN=true.
    """
    if os.getenv("TESTING") == "1":
        return True
    return password_login_env_enabled()


def _verify_password(stored: str, password: str) -> bool:
    """Verify PBKDF2-HMAC-SHA256 password hash stored as 'salt_b64$hash_b64'."""
    if not stored or "$" not in stored:
        return False
    try:
        salt_b64, hash_b64 = stored.split("$", 1)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(dk, expected)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Password verify failed: {e}")
        return False


@router.post("/auth/login")
@limiter.limit("5/minute")
async def password_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Local/test-only password login using DEFAULT_USER_* users.

    Guarded by TESTING=1 or ENABLE_PASSWORD_LOGIN=true.
    """
    if not _password_login_enabled():
        return JSONResponse({"error": "Password login disabled"}, status_code=403)

    email_norm = email.strip().lower()
    user = db.query(User).filter_by(email=email_norm).first()
    if not user or not user.password_hash:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    if not _verify_password(user.password_hash, password):
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    request.session["user_id"] = user.id
    return JSONResponse(
        {
            "ok": True,
            "user_email": user.email,
            "user_role": user.role or "buyer",
        }
    )


@router.get("/auth/login-form", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def password_login_form(request: Request):
    """Simple HTML form for local/test password login."""
    if not _password_login_enabled():
        return RedirectResponse("/auth/login")
    return HTMLResponse(
        content="""
<!doctype html>
<html>
  <head><title>Local Login</title></head>
  <body>
    <h1>Local Password Login</h1>
    <form method="post" action="/auth/login">
      <label>Email: <input type="email" name="email" required></label><br>
      <label>Password: <input type="password" name="password" required></label><br>
      <button type="submit">Login</button>
    </form>
  </body>
</html>
        """,
        media_type="text/html",
    )


@router.get("/auth/status")
async def auth_status(request: Request, db: Session = Depends(get_db)):
    """Return M365 connection health for current user + all connected users."""
    user = get_user(request, db)
    if not user:
        return JSONResponse({"connected": False, "users": []})

    is_admin = (user.role or "") == UserRole.ADMIN
    all_users = db.query(User).filter(User.refresh_token.isnot(None)).all() if is_admin else [user]
    users_status = []
    for u in all_users:
        status = "connected"
        if not u.m365_connected:
            status = "disconnected"
        elif u.token_expires_at and u.token_expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            status = "expired"
        users_status.append(
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role or "buyer",
                "status": status,
                "m365_error": u.m365_error_reason,
                "m365_last_healthy": u.m365_last_healthy.isoformat() if u.m365_last_healthy else None,
                "last_inbox_scan": u.last_inbox_scan.isoformat() if u.last_inbox_scan else None,
                "last_contacts_sync": u.last_contacts_sync.isoformat() if u.last_contacts_sync else None,
            }
        )

    return JSONResponse(
        {
            "connected": user.m365_connected,
            "user_id": user.id,
            "user_email": user.email,
            "user_name": user.name or user.email.split("@")[0],
            "user_role": user.role or "buyer",
            "m365_error": user.m365_error_reason,
            "m365_last_healthy": user.m365_last_healthy.isoformat() if user.m365_last_healthy else None,
            "users": users_status,
        }
    )
