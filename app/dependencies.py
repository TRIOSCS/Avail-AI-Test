"""
dependencies.py — Shared FastAPI Dependencies

Reusable dependency functions for authentication, authorization,
and common query patterns. All routers import from here instead
of defining their own auth logic.

Business Rules:
- get_user returns None if not logged in (non-throwing)
- require_user raises 401 if not logged in, 403 if deactivated
- require_buyer raises 403 if user is not buyer/trader/manager/admin
- require_admin raises 403 if user.role != "admin"
- require_settings_access allows admin or dev_assistant
- user_reqs_query enforces role-based access: sales/trader sees own, dev_assistant sees none
- require_fresh_token handles M365 token refresh with 15-min buffer

Called by: all routers
Depends on: models, database, config
"""

import logging
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .database import get_db
from .models import Requisition, User

log = logging.getLogger(__name__)


# ── Authentication ────────────────────────────────────────────────────


def get_user(request: Request, db: Session) -> User | None:
    """Return current user from session, or None if not logged in."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    try:
        return db.get(User, uid)
    except Exception:
        request.session.clear()
        return None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 401 if no authenticated user, 403 if deactivated."""
    user = get_user(request, db)
    if not user:
        # Check for agent API key (service-to-service auth)
        from .config import Settings

        settings = Settings()
        agent_key = request.headers.get("x-agent-key")
        if agent_key and settings.agent_api_key and agent_key == settings.agent_api_key:
            user = db.query(User).filter_by(email="agent@availai.local").first()
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not getattr(user, "is_active", True):
        request.session.clear()
        raise HTTPException(403, "Account deactivated — contact admin")
    return user


def is_admin(user: User) -> bool:
    """Check if user has admin privileges (by role)."""
    return user.role == "admin"


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 403 if user is not an admin."""
    user = require_user(request, db)
    if user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user


def require_settings_access(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: allows admin (full) or dev_assistant (limited)."""
    user = require_user(request, db)
    if user.role not in ("admin", "dev_assistant"):
        raise HTTPException(403, "Settings access required")
    return user


def require_buyer(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: requires buyer role for RFQ actions."""
    user = require_user(request, db)
    if user.role not in ("buyer", "trader", "manager", "admin"):
        raise HTTPException(403, "Buyer role required for this action")
    return user


# ── Query Helpers ─────────────────────────────────────────────────────


def user_reqs_query(db: Session, user: User):
    """Base requisition query respecting role-based access.
    Sales sees own reqs only; dev_assistant sees none; all other roles see all."""
    q = db.query(Requisition)
    if user.role == "dev_assistant":
        q = q.filter(sa.false())
    elif user.role == "sales":
        q = q.filter(Requisition.created_by == user.id)
    return q


def get_req_for_user(db: Session, user: User, req_id: int) -> Requisition:
    """Get a single requisition with role-based access check."""
    if user.role == "dev_assistant":
        return None
    if user.role == "sales":
        return db.query(Requisition).filter_by(id=req_id, created_by=user.id).first()
    return db.query(Requisition).filter_by(id=req_id).first()


# ── Token Management ──────────────────────────────────────────────────


async def require_fresh_token(request: Request, db: Session = Depends(get_db)) -> str:
    """Return a valid M365 access token, refreshing proactively if near expiry.

    Tokens stored in DB (not just session) so background jobs can use them.
    Refreshes when within 15 min of expiry.
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
        if datetime.now(timezone.utc) > user.token_expires_at - timedelta(minutes=15):
            needs_refresh = True

    if needs_refresh:
        if user.refresh_token:
            from .scheduler import refresh_user_token

            result = await refresh_user_token(user, db)
            if result:
                return result
        user.m365_connected = False
        db.commit()
        raise HTTPException(401, "Session expired — please log in again")

    return token
