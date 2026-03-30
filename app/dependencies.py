"""dependencies.py — Shared FastAPI Dependencies.

Reusable dependency functions for authentication, authorization,
and common query patterns. All routers import from here instead
of defining their own auth logic.

Business Rules:
- get_user returns None if not logged in (non-throwing)
- require_user raises 401 if not logged in, 403 if deactivated
- require_buyer raises 403 if user is not buyer/sales/trader/manager/admin
- require_admin raises 403 if user.role != "admin"
- require_settings_access allows admin only
- require_fresh_token handles M365 token refresh with 15-min buffer

Called by: all routers
Depends on: models, database, config
"""

import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session, selectinload

from .constants import UserRole
from .database import get_db
from .models import Quote, Requisition, User

# ── Authentication ────────────────────────────────────────────────────


def get_user(request: Request, db: Session) -> User | None:
    """Return current user from session, or None if not logged in."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    try:
        return db.get(User, uid)
    except Exception:
        logger.warning("Failed to load user from session (uid={})", uid, exc_info=True)
        request.session.clear()
        return None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 401 if no authenticated user, 403 if deactivated."""
    user = get_user(request, db)
    if not user:
        # Check for agent API key (service-to-service auth)
        from .config import settings

        agent_key = request.headers.get("x-agent-key")
        if agent_key and settings.agent_api_key and hmac.compare_digest(agent_key, settings.agent_api_key):
            logger.info("Agent API access: method={} path={}", request.method, request.url.path)
            user = db.query(User).filter_by(email="agent@availai.local").first()
            if not user:
                logger.error("Agent API key valid but agent@availai.local user not found in DB — seed it first")
                raise HTTPException(503, "Agent user not provisioned")
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not getattr(user, "is_active", True):
        request.session.clear()
        raise HTTPException(403, "Account deactivated — contact admin")
    return user


def is_admin(user: User) -> bool:
    """Check if user has admin privileges (by role)."""
    return bool(user.role == UserRole.ADMIN)


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 403 if user is not an admin. Blocks agent service account."""
    user = require_user(request, db)
    if user.email == "agent@availai.local":
        raise HTTPException(403, "Agent keys cannot access admin endpoints")
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Admin access required")
    return user


def require_settings_access(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: allows admin only. Blocks agent service account."""
    user = require_user(request, db)
    if user.email == "agent@availai.local":
        raise HTTPException(403, "Agent keys cannot access settings")
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, "Settings access required")
    return user


def require_buyer(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: requires buyer role for RFQ actions."""
    user = require_user(request, db)
    if user.role not in (UserRole.BUYER, UserRole.SALES, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN):
        raise HTTPException(403, "Buyer role required for this action")
    return user


# ── Query Helpers ─────────────────────────────────────────────────────


def get_req_for_user(db: Session, user: User, req_id: int, options=None) -> Requisition:
    """Get a single requisition with role-based access check.

    Args:
        options: Additional SQLAlchemy loader options (e.g., joinedload).
                 Defaults to selectinload(Requisition.requirements).
    """
    load_opts = options or [selectinload(Requisition.requirements)]
    q = db.query(Requisition).options(*load_opts).filter_by(id=req_id)
    if user.role == UserRole.SALES:
        q = q.filter_by(created_by=user.id)
    req = q.first()
    if not req:
        raise HTTPException(status_code=404, detail="Requisition not found")
    return req


def get_quote_for_user(db: Session, user: User, quote_id: int, options=None) -> Quote:
    """Get a single quote with role-based requisition ownership checks."""
    load_opts = options or []
    q = (
        db.query(Quote)
        .options(*load_opts)
        .join(Requisition, Quote.requisition_id == Requisition.id)
        .filter(Quote.id == quote_id)
    )
    if user.role == UserRole.SALES:
        q = q.filter(Requisition.created_by == user.id)
    quote = q.first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    return quote


# ── Token Management ──────────────────────────────────────────────────


async def require_fresh_token(request: Request, db: Session = Depends(get_db)) -> str:
    """Return a valid M365 access token, raising 401 only if truly expired.

    Tokens stored in DB (not just session) so background jobs can use them. The
    background scheduler job (_job_token_refresh) refreshes tokens proactively when
    within 15 min of expiry — no inline refresh is done here to avoid latency spikes on
    request handlers.
    """
    user = get_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated — please log in")

    token = user.access_token
    if not token:
        raise HTTPException(401, "No access token — please log in")

    # Check if token needs refresh (15-min buffer)
    needs_refresh = False
    if user.token_expires_at:
        expiry = (
            user.token_expires_at
            if user.token_expires_at.tzinfo
            else user.token_expires_at.replace(tzinfo=timezone.utc)
        )
        if datetime.now(timezone.utc) > expiry - timedelta(minutes=15):
            needs_refresh = True

    if needs_refresh:
        # Background scheduler job refreshes tokens proactively.
        # If within buffer but not expired, continue with current token.
        if datetime.now(timezone.utc) > expiry:
            # Truly expired — background job missed it or no refresh token
            user.m365_connected = False
            db.commit()
            raise HTTPException(401, "Session expired — please log in again")
        # Within buffer, not yet expired — use current token
        return str(token)

    return str(token)
