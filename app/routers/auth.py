"""
routers/auth.py — Authentication & Session Routes

Handles Azure AD OAuth login/callback, logout, session status,
and the main index page. All M365 token management lives here.

Business Rules:
- Login via Azure AD OAuth2 code flow
- Tokens stored in DB (not just session) for background job access
- Token refresh handled proactively (15-min buffer) in dependencies.py
- New users auto-created on first login
- Email normalized to lowercase on login

Called by: main.py (router mount)
Depends on: dependencies, models, config
"""

from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session

from ..config import APP_VERSION, settings
from ..database import get_db
from ..dependencies import get_user
from ..http_client import http
from ..models import User
from ..vite import vite_css_tags, vite_js_tags, vite_app_url, vite_crm_url

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

AZURE_AUTH = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0"
SCOPES = "openid profile email offline_access Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read Files.ReadWrite Chat.ReadWrite Calendars.Read ChannelMessage.Send Team.ReadBasic.All"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    is_admin = user.role == "admin" if user else False
    is_manager = user.role == "manager" if user else False
    user_role = user.role if user else ""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logged_in": user is not None,
            "user_name": user.name if user else "",
            "user_email": user.email if user else "",
            "is_admin": is_admin,
            "is_manager": is_manager,
            "user_role": user_role,
            "app_version": APP_VERSION,
            "vite_css_tags": vite_css_tags(APP_VERSION),
            "vite_js_tags": vite_js_tags(APP_VERSION),
            "vite_app_url": vite_app_url(APP_VERSION),
            "vite_crm_url": vite_crm_url(APP_VERSION),
        },
    )


@router.get("/auth/login")
async def login():
    return RedirectResponse(
        f"{AZURE_AUTH}/authorize?client_id={settings.azure_client_id}"
        f"&response_type=code&redirect_uri={settings.app_url}/auth/callback"
        f"&scope={SCOPES}&response_mode=query"
    )


from ..rate_limit import limiter


@router.get("/auth/callback")
@limiter.limit("10/minute")
async def callback(request: Request, code: str = "", db: Session = Depends(get_db)):
    if not code:
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
        return RedirectResponse("/")
    tokens = resp.json()
    access_token = tokens.get("access_token")
    if not access_token:
        logger.error("Azure token response missing access_token")
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
    email = (
        (profile.get("mail") or profile.get("userPrincipalName", "")).strip().lower()
    )
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(
            email=email,
            name=profile.get("displayName", email),
            azure_id=profile.get("id"),
        )
        db.add(user)
        db.commit()

    # Bootstrap admin: auto-promote users in admin_emails env var
    if user.email.lower() in settings.admin_emails and user.role != "admin":
        user.role = "admin"
        logger.info(f"Auto-promoted {user.email} to admin via admin_emails bootstrap")

    # Store tokens in DB (not just session) for background jobs
    user.access_token = access_token
    user.token_expires_at = token_expires_at
    user.m365_connected = True
    if tokens.get("refresh_token"):
        user.refresh_token = tokens["refresh_token"]

    db.commit()

    # Trigger first-time backfill if user has never been scanned
    if not user.last_inbox_scan:
        logger.info(
            f"New M365 connection for {user.email} — backfill will run on next scheduler tick"
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/")


@router.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


@router.post("/auth/login")
@limiter.limit("10/minute")
async def password_login(request: Request, db: Session = Depends(get_db)):
    """Simple email/password login. Accepts JSON or form-encoded body."""
    email = ""
    password = ""
    body_was_form = False
    body_was_json = False
    # Try JSON body first
    try:
        data = await request.json()
        if isinstance(data, dict):
            email = (data.get("email") or "").strip().lower()
            password = data.get("password") or ""
            body_was_json = True
    except Exception:
        # Fallback to form-encoded (from regular HTML form submit)
        try:
            form = await request.form()
            email = (form.get("email") or "").strip().lower()
            password = form.get("password") or ""
            body_was_form = True
        except Exception:
            pass
    print(f"email: {email}, password: {password}")
    if email:
        # Allow shortcut: if DEFAULT_USER_EMAIL is set and matches the provided email,
        # permit login even without a password (development convenience).
        import os
        default_email = os.environ.get("DEFAULT_USER_EMAIL")
        print(f"default_email: {default_email}")
        print(f"1default_email and email match: {default_email} and {email}")
        if default_email and email and email.lower() == default_email.strip().lower():
            print(f"2default_email and email match: {default_email} and {email}")
            # create user if missing
            from ..models.auth import User as UserModel
            user = db.query(User).filter_by(email=email).first()
            if not user:
                role = os.environ.get("DEFAULT_USER_ROLE", "admin")
                user = UserModel(email=email.lower(), name=email.split("@")[0], role=role)
                db.add(user)
                db.commit()
                db.refresh(user)
                print("Created default login user %s via DEFAULT_USER_EMAIL", email)
                logger.info("Created default login user %s via DEFAULT_USER_EMAIL", email)
            request.session["user_id"] = user.id
            # If the browser submitted a form (or accepts HTML), redirect to the UI.
            accept = request.headers.get("accept", "")
            content_type = request.headers.get("content-type", "")
            if body_was_form or "text/html" in accept.lower() or content_type.startswith("application/x-www-form-urlencoded"):
                from fastapi.responses import RedirectResponse

                return RedirectResponse("/", status_code=303)
            return JSONResponse({"ok": True, "user": {"id": user.id, "email": user.email, "role": user.role}})
        return JSONResponse({"ok": False, "error": "email_and_password_required"}, status_code=400)

    user = db.query(User).filter_by(email=email).first()
    if not user:
        logger.info("Password login failed: user not found %s", email)
        return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)

    pw_field = getattr(user, "password_hash", None)
    if not pw_field:
        logger.info("Password login failed: user %s has no password_hash", email)
        return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)

    # verify password: stored format "<salt_b64>$<hash_b64>"
    import base64, hashlib, hmac
    try:
        raw = str(pw_field).strip()
        if "$" not in raw:
            logger.warning("Password hash format invalid for user %s: %s", email, raw[:30])
            return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)
        salt_b64, hash_b64 = raw.split("$", 1)
        try:
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
        except Exception:
            logger.exception("Failed base64 decoding stored hash for user %s", email)
            return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)

        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        if not hmac.compare_digest(dk, expected):
            logger.info(
                "Password mismatch for user %s: dk_len=%d expected_len=%d",
                email,
                len(dk),
                len(expected),
            )
            return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)
    except Exception:
        logger.exception("Unexpected error during password verification for %s", email)
        return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)

    request.session["user_id"] = user.id
    return JSONResponse({"ok": True, "user": {"id": user.id, "email": user.email, "role": user.role}})


@router.get("/auth/status")
async def auth_status(request: Request, db: Session = Depends(get_db)):
    """Return M365 connection health for current user + all connected users."""
    user = get_user(request, db)
    if not user:
        return JSONResponse({"connected": False, "users": []})

    all_users = db.query(User).filter(User.refresh_token.isnot(None)).all()
    users_status = []
    for u in all_users:
        status = "connected"
        if not u.m365_connected:
            status = "disconnected"
        elif u.token_expires_at and u.token_expires_at.replace(
            tzinfo=timezone.utc
        ) < datetime.now(timezone.utc):
            status = "expired"
        users_status.append(
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role or "buyer",
                "status": status,
                "m365_error": u.m365_error_reason,
                "m365_last_healthy": u.m365_last_healthy.isoformat()
                if u.m365_last_healthy
                else None,
                "last_inbox_scan": u.last_inbox_scan.isoformat()
                if u.last_inbox_scan
                else None,
                "last_contacts_sync": u.last_contacts_sync.isoformat()
                if u.last_contacts_sync
                else None,
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
            "m365_last_healthy": user.m365_last_healthy.isoformat()
            if user.m365_last_healthy
            else None,
            "users": users_status,
        }
    )
