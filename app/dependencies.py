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
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .constants import RESTRICTED_ROLES, UserRole
from .database import get_db
from .models import AccountCollaborator, Company, CustomerSite, Quote, Requisition, User

# Non-interactive service account seeded by startup.py. It authenticates via the
# x-agent-key header and is barred from admin/settings/buyer (RFQ) endpoints.
_AGENT_EMAIL = "agent@availai.local"

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
            user = db.query(User).filter_by(email=_AGENT_EMAIL).first()
            if not user:
                logger.error("Agent API key valid but {} user not found in DB — seed it first", _AGENT_EMAIL)
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


def is_manager_or_admin(user: User) -> bool:
    """True for MANAGER or ADMIN roles (the supervisor/oversight tier).

    Use this to gate visibility and management actions that a supervisor should always
    be able to perform regardless of account ownership.
    """
    return user.role in (UserRole.MANAGER, UserRole.ADMIN)


def can_manage_account(user: User, company: Company, db: "Session") -> bool:  # noqa: F821
    """True if *user* may act on *company* as an account manager.

    Allowed when ANY of the following holds:
    - is_manager_or_admin(user)  — supervisors see/manage everything
    - company.account_owner_id == user.id  — primary account owner
    - user owns at least one CustomerSite under this company
    - user is an AccountCollaborator (helper role) on this company  [Phase 3]

    NOTE: this does NOT gate team-management actions (add/remove collaborators,
    reassign ownership). Use can_manage_account_team() for those.
    """
    if is_manager_or_admin(user):
        return True
    if company.account_owner_id is not None and company.account_owner_id == user.id:
        return True
    # Site-owner check: efficient exists() subquery — index-backed via ix_cs_owner/ix_cs_company
    site_exists = (
        select(CustomerSite.id)
        .where(
            CustomerSite.company_id == company.id,
            CustomerSite.owner_id == user.id,
        )
        .exists()
    )
    if bool(db.scalar(select(site_exists))):
        return True
    # Collaborator check (Phase 3): helper collaborators can view + work the account.
    collab_exists = (
        select(AccountCollaborator.id)
        .where(
            AccountCollaborator.company_id == company.id,
            AccountCollaborator.user_id == user.id,
        )
        .exists()
    )
    return bool(db.scalar(select(collab_exists)))


def can_manage_account_team(user: User, company: Company) -> bool:
    """True if *user* may add/remove collaborators or change the primary owner.

    This is a STRICTER gate than can_manage_account. Helper collaborators and
    site-owners are excluded — only the primary account owner and manager/admin
    may alter the team roster.

    Allowed when:
    - is_manager_or_admin(user)  — supervisors manage all teams
    - company.account_owner_id == user.id  — primary owner manages their own team

    Intentionally does NOT accept collaborators, site-owners, or buyers.
    """
    return is_manager_or_admin(user) or (company.account_owner_id is not None and company.account_owner_id == user.id)


def _require_admin_user(request: Request, db: Session, *, agent_msg: str, role_msg: str) -> User:
    """Resolve an admin-only user, blocking the agent service account.

    Raises 403 with *agent_msg* if the caller is the agent account, or *role_msg* if the
    caller is not an admin.
    """
    user = require_user(request, db)
    if user.email == _AGENT_EMAIL:
        raise HTTPException(403, agent_msg)
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, role_msg)
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 403 if user is not an admin. Blocks agent service account."""
    return _require_admin_user(
        request,
        db,
        agent_msg="Agent keys cannot access admin endpoints",
        role_msg="Admin access required",
    )


def require_settings_access(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: allows admin only. Blocks agent service account."""
    return _require_admin_user(
        request,
        db,
        agent_msg="Agent keys cannot access settings",
        role_msg="Settings access required",
    )


# Roles allowed through require_buyer. Single source of truth — templates that hide
# buyer-only UI (e.g. the materials workspace "Add part" button) check the SAME set
# via has_buyer_role, so the surface can never show an action the POST would 403.
BUYER_ROLES = frozenset({UserRole.BUYER, UserRole.SALES, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN})


def require_requisition_access(
    db: Session,
    req_id: int | None,
    user: User,
    *,
    owner_id: int | None = None,
    label: str = "Requisition",
) -> None:
    """Enforce role-scoped ownership for an action on a requisition-scoped resource.

    No-op for unrestricted roles (buyer/manager/admin). For SALES/TRADER, allows the
    action only when the user owns the requisition (created_by) or, for unscoped/scratch
    resources where ``req_id`` is None, when ``owner_id`` matches the user. Raises
    HTTPException(404) otherwise (404 not 403 so existence isn't leaked).
    """
    if getattr(user, "role", None) not in RESTRICTED_ROLES:
        return
    if req_id is not None:
        req = db.get(Requisition, req_id)
        if req is not None and req.created_by == user.id:
            return
    if owner_id is not None and owner_id == user.id:
        return
    raise HTTPException(status_code=404, detail=f"{label} not found")


def has_buyer_role(user: User | None) -> bool:
    """True when *user* holds a buyer-tier role (require_buyer's allowed set)."""
    return user is not None and user.role in BUYER_ROLES


def require_buyer(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: requires a buyer-tier role for RFQ actions.

    UserRole.AGENT is intentionally absent from the allowed set: the agent
    service account is non-interactive and must reach only non-privileged
    (require_user-level) endpoints — never RFQ/buyer actions.
    """
    user = require_user(request, db)
    if not has_buyer_role(user):
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
    if user.role in RESTRICTED_ROLES:
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
    if user.role in RESTRICTED_ROLES:
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

    # The background scheduler job (_job_token_refresh) refreshes proactively within the
    # 15-min buffer, so the only failure case to handle inline is a truly-expired token
    # (background job missed it or no refresh token) — everything else uses the DB token.
    if user.token_expires_at:
        expiry = (
            user.token_expires_at
            if user.token_expires_at.tzinfo
            else user.token_expires_at.replace(tzinfo=timezone.utc)
        )
        if datetime.now(timezone.utc) > expiry:
            user.m365_connected = False
            db.commit()
            raise HTTPException(401, "Session expired — please log in again")

    return str(token)
