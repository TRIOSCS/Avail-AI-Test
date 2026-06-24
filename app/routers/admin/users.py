"""Admin router — Users tab CRUD (invite, change-role, activate/deactivate).

Routes (all admin-only via Depends(require_admin); the agent service account is
blocked by require_admin and additionally excluded/uneditable here):
- POST /api/admin/users/invite             — create a new interactive user
- POST /api/admin/users/{user_id}/role     — change a user's role
- POST /api/admin/users/{user_id}/active   — activate / deactivate a user

Each mutation writes an append-only UserAdminAudit row (services.user_admin) and
returns the refreshed users.html partial (HTMX swaps #users-content). Validation
failures re-render the partial with an inline `error` banner under a 400 status.

Self-protection invariants enforced here (never let an admin lock themselves /
everyone out): an admin can't demote or deactivate themselves, and the last
active admin can't be demoted or deactivated by anyone.

Called by: app/routers/admin/__init__.py (included via router)
Depends on: models.User, dependencies.require_admin, services.user_admin.record_user_audit,
            template_env.template_response, constants.UserRole/UserAuditAction
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import UserAuditAction, UserRole
from ...database import get_db
from ...dependencies import _AGENT_EMAIL, require_admin
from ...models import User
from ...services.user_admin import record_user_audit
from ...template_env import template_response

router = APIRouter(tags=["admin"])

# Roles an admin may assign — every interactive role plus admin, but NOT the
# non-interactive AGENT service role (assigning it would grant a human the
# service identity / strip their interactive access).
_ASSIGNABLE_ROLES = [r for r in UserRole if r != UserRole.AGENT]


def _active_admin_count(db: Session, *, lock: bool = False) -> int:
    """Count active admins, excluding the agent service account.

    When *lock* is True the matching admin rows are selected FOR UPDATE so a concurrent
    demote/deactivate of the last admin(s) blocks until this transaction commits,
    instead of both requests racing past the last-admin guard and locking everyone out.
    No-op on SQLite (the test DB); enforced on Postgres. The display path
    (users_context) reads lock-free.
    """
    q = db.query(User.id).filter(User.role == UserRole.ADMIN, User.is_active.is_(True), User.email != _AGENT_EMAIL)
    if lock:
        return len(q.with_for_update().all())
    return q.count()


def _is_last_active_admin(db: Session, user: User) -> bool:
    """True if *user* is an active admin and the only one left.

    Uses a row-locking count so the guard is race-safe against concurrent last-admin
    demote/deactivate requests (see _active_admin_count).
    """
    return bool(user.role == UserRole.ADMIN and user.is_active and _active_admin_count(db, lock=True) <= 1)


def _user_status(user: User) -> str:
    """Derived lifecycle status shown in the Users table."""
    if not user.is_active:
        return "Disabled"
    if user.azure_id is None and user.last_login_at is None:
        return "Invited"
    return "Active"


def users_context(db: Session) -> dict:
    """Build {rows, roles, active_admin_count} for the Users settings partial.

    rows: every real user (excluding the agent service account), ordered by
    name/email, each as {user, status, last_login_at}. Shared by the GET tab
    (htmx_views.settings_users_tab) and the mutation POSTs below.
    """
    users = db.query(User).filter(User.email != _AGENT_EMAIL).order_by(User.name.is_(None), User.name, User.email).all()
    rows = [{"user": u, "status": _user_status(u), "last_login_at": u.last_login_at} for u in users]
    return {"rows": rows, "roles": _ASSIGNABLE_ROLES, "active_admin_count": _active_admin_count(db)}


def _render(db: Session, request: Request, *, error: str | None = None, status_code: int = 200):
    """Render the refreshed users partial, optionally with an inline error banner."""
    ctx = {"request": request, "is_admin": True, "error": error, **users_context(db)}
    return template_response("htmx/partials/settings/users.html", ctx, status_code=status_code)


def _validate_assignable_role(role: str) -> str | None:
    """Return the canonical role string if assignable, else None."""
    try:
        parsed = UserRole(role)
    except ValueError:
        return None
    if parsed == UserRole.AGENT:
        return None
    return str(parsed)


@router.post("/api/admin/users/invite", response_class=HTMLResponse)
async def invite_user(
    request: Request,
    email: str = Form(...),
    role: str = Form(...),
    name: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new interactive user; returns the refreshed Users partial."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return _render(db, request, error="Enter a valid email address.", status_code=400)

    valid_role = _validate_assignable_role(role)
    if valid_role is None:
        return _render(db, request, error="Choose a valid role.", status_code=400)

    if db.query(User).filter(User.email == email).first() is not None:
        return _render(db, request, error=f"{email} is already a user.", status_code=400)

    user = User(
        email=email,
        name=(name or "").strip() or email.split("@")[0],
        role=valid_role,
        is_active=True,
        invited_by_id=admin.id,
    )
    db.add(user)
    db.flush()  # assign user.id for the audit row
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=user.id,
        action=UserAuditAction.INVITE,
        detail={"email": email, "role": valid_role},
    )
    db.commit()
    logger.info("User {} invited as {} by {}", email, valid_role, admin.email)
    return _render(db, request)


def _editable_target(db: Session, user_id: int) -> User:
    """Load an editable target user or raise 404 (agent account is not editable).

    404 (not 403) for the agent so its existence isn't leaked as a special case.
    """
    target = db.get(User, user_id)
    if target is None or target.email == _AGENT_EMAIL:
        raise HTTPException(404, "User not found")
    return target


@router.post("/api/admin/users/{user_id}/role", response_class=HTMLResponse)
async def change_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Change a user's role; returns the refreshed Users partial."""
    target = _editable_target(db, user_id)

    valid_role = _validate_assignable_role(role)
    if valid_role is None:
        return _render(db, request, error="Choose a valid role.", status_code=400)

    if target.id == admin.id and valid_role != UserRole.ADMIN:
        return _render(db, request, error="You can't change your own admin role.", status_code=400)

    if valid_role != UserRole.ADMIN and _is_last_active_admin(db, target):
        return _render(db, request, error="Can't remove the last admin.", status_code=400)

    old_role = str(target.role)
    if old_role == valid_role:
        return _render(db, request)  # no-op, nothing to audit

    target.role = valid_role
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=UserAuditAction.ROLE_CHANGE,
        detail={"from": old_role, "to": valid_role},
    )
    db.commit()
    logger.info("User {} role {} -> {} by {}", target.email, old_role, valid_role, admin.email)
    return _render(db, request)


@router.post("/api/admin/users/{user_id}/active", response_class=HTMLResponse)
async def set_user_active(
    request: Request,
    user_id: int,
    is_active: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Activate or deactivate a user; returns the refreshed Users partial."""
    target = _editable_target(db, user_id)
    activate = str(is_active).strip().lower() in {"true", "1", "on", "yes"}

    if not activate:
        if target.id == admin.id:
            return _render(db, request, error="You can't deactivate yourself.", status_code=400)
        if _is_last_active_admin(db, target):
            return _render(db, request, error="Can't deactivate the last admin.", status_code=400)

    if target.is_active == activate:
        return _render(db, request)  # no-op, nothing to audit

    target.is_active = activate
    action = UserAuditAction.ACTIVATE if activate else UserAuditAction.DEACTIVATE
    record_user_audit(db, actor_id=admin.id, target_user_id=target.id, action=action)
    db.commit()
    logger.info("User {} {} by {}", target.email, "activated" if activate else "deactivated", admin.email)
    return _render(db, request)
