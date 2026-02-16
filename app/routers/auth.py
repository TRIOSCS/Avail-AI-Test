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

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings, APP_VERSION
from ..database import get_db
from ..dependencies import get_user
from ..models import User

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

AZURE_AUTH = f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0"
SCOPES = "openid profile email offline_access Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read Files.ReadWrite Chat.ReadWrite"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    is_admin = user.email.lower() in settings.admin_emails if user else False
    return templates.TemplateResponse("index.html", {
        "request": request,
        "logged_in": user is not None,
        "user_name": user.name if user else "",
        "user_email": user.email if user else "",
        "is_admin": is_admin,
        "app_version": APP_VERSION,
    })


@router.get("/auth/login")
async def login():
    return RedirectResponse(
        f"{AZURE_AUTH}/authorize?client_id={settings.azure_client_id}"
        f"&response_type=code&redirect_uri={settings.app_url}/auth/callback"
        f"&scope={SCOPES}&response_mode=query"
    )


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", db: Session = Depends(get_db)):
    if not code:
        return RedirectResponse("/")
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{AZURE_AUTH}/token", data={
            "client_id": settings.azure_client_id,
            "client_secret": settings.azure_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": f"{settings.app_url}/auth/callback",
            "scope": SCOPES,
        })
    if resp.status_code != 200:
        return RedirectResponse("/")
    tokens = resp.json()
    access_token = tokens["access_token"]
    request.session["access_token"] = access_token
    request.session["token_issued_at"] = datetime.now(timezone.utc).timestamp()

    # Calculate token expiry
    expires_in = tokens.get("expires_in", 3600)
    token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    async with httpx.AsyncClient() as client:
        me = await client.get("https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"})
    profile = me.json()
    email = (profile.get("mail") or profile.get("userPrincipalName", "")).strip().lower()
    user = db.query(User).filter_by(email=email).first()
    if not user:
        user = User(email=email, name=profile.get("displayName", email),
                     azure_id=profile.get("id"))
        db.add(user)
        db.commit()

    # Store tokens in DB (not just session) for background jobs
    user.access_token = access_token
    user.token_expires_at = token_expires_at
    user.m365_connected = True
    if tokens.get("refresh_token"):
        user.refresh_token = tokens["refresh_token"]

    db.commit()

    # Trigger first-time backfill if user has never been scanned
    if not user.last_inbox_scan:
        log.info(f"New M365 connection for {user.email} — backfill will run on next scheduler tick")

    request.session["user_id"] = user.id
    return RedirectResponse("/")


@router.post("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


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
        elif u.token_expires_at and u.token_expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            status = "expired"
        users_status.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "role": u.role or "buyer",
            "status": status,
            "m365_error": u.m365_error_reason,
            "m365_last_healthy": u.m365_last_healthy.isoformat() if u.m365_last_healthy else None,
            "last_inbox_scan": u.last_inbox_scan.isoformat() if u.last_inbox_scan else None,
            "last_contacts_sync": u.last_contacts_sync.isoformat() if u.last_contacts_sync else None,
        })

    return JSONResponse({
        "connected": user.m365_connected,
        "user_id": user.id,
        "user_email": user.email,
        "user_name": user.name or user.email.split("@")[0],
        "user_role": user.role or "buyer",
        "m365_error": user.m365_error_reason,
        "m365_last_healthy": user.m365_last_healthy.isoformat() if user.m365_last_healthy else None,
        "users": users_status,
    })
