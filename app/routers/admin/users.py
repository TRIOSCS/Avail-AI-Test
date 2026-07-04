"""Admin router — Users tab CRUD (invite, change-role, activate/deactivate) + per-user
access editor.

Routes (all admin-only via Depends(require_admin); the agent service account is
blocked by require_admin and additionally excluded/uneditable here):
- POST /api/admin/users/invite                    — create a new interactive user
- POST /api/admin/users/{user_id}/role            — change a user's role
- POST /api/admin/users/{user_id}/manager         — set/clear a user's manager (reports_to)
- POST /api/admin/users/{user_id}/active          — activate / deactivate a user
- GET  /api/admin/users/{user_id}/access-panel    — render the per-user Access editor
- POST /api/admin/users/{user_id}/access          — grant/revoke/reset one access key
- GET  /api/admin/users/audit                     — render the user-management audit log

Each mutation writes an append-only UserAdminAudit row (services.user_admin) and
returns the refreshed users.html partial (HTMX swaps #users-content). Validation
failures re-render the partial with an inline `error` banner under a 400 status.
The access editor instead re-renders user_access_panel.html (the modal swaps itself).

Self-protection invariants enforced here (never let an admin lock themselves /
everyone out): an admin can't demote or deactivate themselves, and the last
active admin can't be demoted or deactivated by anyone.

Called by: app/routers/admin/__init__.py (included via router); module_access_map is
           imported by htmx_views._base_ctx to gate the bottom nav.
Depends on: models.User, models.buy_plan.VerificationGroupMember, dependencies.require_admin
            / user_has_access, services.user_admin.record_user_audit,
            template_env.template_response, constants (AccessKey / MODULE_ACCESS_KEYS /
            CAPABILITY_ACCESS_KEYS / UserRole / UserAuditAction)
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import (
    CAPABILITY_ACCESS_KEYS,
    MODULE_ACCESS_KEYS,
    AccessKey,
    UserAuditAction,
    UserRole,
)
from ...database import get_db
from ...dependencies import _AGENT_EMAIL, require_admin, user_has_access
from ...models import User, UserAdminAudit
from ...models.buy_plan import VerificationGroupMember
from ...services.user_admin import record_user_audit
from ...template_env import template_response

router = APIRouter(tags=["admin"])

# Hyphenated bottom-nav id → the AccessKey that gates that nav section. Single source
# of truth shared by module_access_map (nav gate) and the access panel. The nav-ids
# match mobile_nav.html's nav_items loop variable; the values are the module keys (a
# subset of AccessKey, exactly MODULE_ACCESS_KEYS).
NAV_ID_TO_ACCESS: dict[str, AccessKey] = {
    "requisitions": AccessKey.REQUISITIONS,
    "sightings": AccessKey.SIGHTINGS,
    "materials": AccessKey.MATERIALS,
    "search": AccessKey.SEARCH,
    "buy-plans": AccessKey.BUY_PLANS,
    "resell": AccessKey.RESELL,
    "crm": AccessKey.CRM,
    "proactive": AccessKey.PROACTIVE,
    "prospecting": AccessKey.PROSPECTING,
    "my-day": AccessKey.MY_DAY,
}

# Human-friendly labels for every access key, shown in the access editor. Keyed by the
# AccessKey value so both module and capability rows resolve through one map.
_ACCESS_KEY_LABELS: dict[AccessKey, str] = {
    AccessKey.REQUISITIONS: "Sales Hub",
    AccessKey.SIGHTINGS: "Sightings",
    AccessKey.MATERIALS: "Materials",
    AccessKey.SEARCH: "Search",
    AccessKey.BUY_PLANS: "Buy Plans",
    AccessKey.RESELL: "Resell",
    AccessKey.CRM: "CRM",
    AccessKey.PROACTIVE: "Proactive",
    AccessKey.PROSPECTING: "Prospecting",
    AccessKey.MY_DAY: "My Day",
    AccessKey.SEND_RFQ: "Send RFQs",
    AccessKey.APPROVE_OFFERS: "Approve offers",
    AccessKey.EXPORT_DATA: "Export data",
    AccessKey.MANAGE_CONNECTORS: "Manage connectors",
    AccessKey.OPS_VERIFICATION: "Ops verification group",
}


def module_access_map(user: User | None, db: Session | None = None) -> dict[str, bool]:
    """Return {hyphenated-nav-id: bool} — whether *user* may see each nav module.

    Powers the bottom-nav gate (htmx_views._base_ctx). Module access never needs the db
    (user_has_access handles admin→all and the role default for non-ops keys). A None
    user (logged-out shell render) defaults every module to True so the nav is never
    accidentally blanked.
    """
    if user is None:
        return {nav_id: True for nav_id in NAV_ID_TO_ACCESS}
    return {nav_id: user_has_access(user, key, db) for nav_id, key in NAV_ID_TO_ACCESS.items()}


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
    name/email, each as {user, status, last_login_at, reports_to_id,
    can_approve_buy_plans, can_approve_prepayments, prepayment_approval_limit,
    can_approve_qp_sales, can_approve_qp_purchasing, can_approve_purchase_orders,
    purchase_order_approval_limit}. Also returns manager_options — the active
    MANAGER/ADMIN users assignable as a rep's manager (the per-row Manager <select>
    excludes self). Shared by the GET tab (htmx_views.settings_users_tab) and the
    mutation POSTs below.
    """
    users = db.query(User).filter(User.email != _AGENT_EMAIL).order_by(User.name.is_(None), User.name, User.email).all()
    rows = [
        {
            "user": u,
            "status": _user_status(u),
            "last_login_at": u.last_login_at,
            "reports_to_id": u.reports_to_id,
            "can_approve_buy_plans": u.can_approve_buy_plans,
            "can_approve_prepayments": u.can_approve_prepayments,
            "prepayment_approval_limit": u.prepayment_approval_limit,
            "can_approve_qp_sales": u.can_approve_qp_sales,
            "can_approve_qp_purchasing": u.can_approve_qp_purchasing,
            "can_approve_purchase_orders": u.can_approve_purchase_orders,
            "purchase_order_approval_limit": u.purchase_order_approval_limit,
        }
        for u in users
    ]
    manager_options = [u for u in users if u.is_active and u.role in (UserRole.MANAGER, UserRole.ADMIN)]
    return {
        "rows": rows,
        "roles": _ASSIGNABLE_ROLES,
        "manager_options": manager_options,
        "active_admin_count": _active_admin_count(db),
    }


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


@router.post("/api/admin/users/{user_id}/manager", response_class=HTMLResponse)
async def set_user_manager(
    request: Request,
    user_id: int,
    reports_to_id: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set or clear a user's manager (User.reports_to_id); returns the refreshed Users
    partial.

    reports_to_id="" (or "none") clears the manager. Otherwise it must be the id of an
    active MANAGER/ADMIN user other than the target itself (and never the agent service
    account). The manager routes account-park alerts to that specific supervisor;
    clearing it restores the all-managers fallback. Admin-only (require_admin); each
    change writes a MANAGER_CHANGE audit row. A no-op (state unchanged) re-renders
    without auditing, mirroring change_user_role.
    """
    target = _editable_target(db, user_id)

    raw = (reports_to_id or "").strip().lower()
    if raw in {"", "none"}:
        new_manager_id: int | None = None
    else:
        try:
            new_manager_id = int(reports_to_id)
        except ValueError:
            return _render(db, request, error="Choose a valid manager.", status_code=400)
        if new_manager_id == target.id:
            return _render(db, request, error="A user can't be their own manager.", status_code=400)
        manager = db.get(User, new_manager_id)
        if (
            manager is None
            or manager.email == _AGENT_EMAIL
            or not manager.is_active
            or manager.role not in (UserRole.MANAGER, UserRole.ADMIN)
        ):
            return _render(db, request, error="Manager must be an active manager or admin.", status_code=400)

    old_manager_id = target.reports_to_id
    if old_manager_id == new_manager_id:
        return _render(db, request)  # no-op, nothing to audit

    target.reports_to_id = new_manager_id
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=UserAuditAction.MANAGER_CHANGE,
        detail={"from": old_manager_id, "to": new_manager_id},
    )
    db.commit()
    logger.info("User {} manager {} -> {} by {}", target.email, old_manager_id, new_manager_id, admin.email)
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


@router.post("/api/admin/users/{user_id}/buyplan-approver", response_class=HTMLResponse)
async def set_buyplan_approver(
    request: Request,
    user_id: int,
    can_approve: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grant or revoke a user's buy-plan approval right; returns the refreshed Users
    partial.

    The right is the per-user User.can_approve_buy_plans column enforced by
    dependencies.require_buyplan_approver. Admin-only (require_admin); each change
    writes an APPROVAL_GRANT / APPROVAL_REVOKE audit row. A no-op (state unchanged) re-
    renders without auditing, mirroring change_user_role / set_user_active.
    """
    target = _editable_target(db, user_id)
    grant = str(can_approve).strip().lower() in {"true", "1", "on", "yes"}

    if target.can_approve_buy_plans == grant:
        return _render(db, request)  # no-op, nothing to audit

    target.can_approve_buy_plans = grant
    action = UserAuditAction.APPROVAL_GRANT if grant else UserAuditAction.APPROVAL_REVOKE
    record_user_audit(db, actor_id=admin.id, target_user_id=target.id, action=action)
    db.commit()
    logger.info("User {} buy-plan approval {} by {}", target.email, "granted" if grant else "revoked", admin.email)
    return _render(db, request)


@router.post("/api/admin/users/{user_id}/prepayment-approver", response_class=HTMLResponse)
async def set_prepayment_approver(
    request: Request,
    user_id: int,
    can_approve: str = Form(...),
    limit: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grant or revoke a user's prepayment approval right + optional dollar limit.

    Sets User.can_approve_prepayments and User.prepayment_approval_limit. limit="" or
    "unlimited" → NULL (no cap). A positive decimal sets the cap. Admin-only; each
    change writes an APPROVAL_GRANT / APPROVAL_REVOKE audit row. A no-op (state
    unchanged) re-renders without auditing.
    """
    target = _editable_target(db, user_id)
    grant = str(can_approve).strip().lower() in {"true", "1", "on", "yes"}

    # Parse the optional dollar limit
    limit_str = (limit or "").strip()
    if limit_str in {"", "unlimited"}:
        new_limit: Decimal | None = None
    else:
        try:
            new_limit = Decimal(limit_str)
            if new_limit <= 0:
                return _render(db, request, error="Dollar limit must be a positive number.", status_code=400)
        except InvalidOperation:
            return _render(db, request, error="Enter a valid dollar limit (e.g. 1000.00).", status_code=400)

    # No-op guard
    if target.can_approve_prepayments == grant and target.prepayment_approval_limit == new_limit:
        return _render(db, request)

    target.can_approve_prepayments = grant
    target.prepayment_approval_limit = new_limit
    action = UserAuditAction.APPROVAL_GRANT if grant else UserAuditAction.APPROVAL_REVOKE
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=action,
        detail={"gate": "prepayment", "limit": str(new_limit) if new_limit is not None else None},
    )
    db.commit()
    logger.info(
        "User {} prepayment approval {} (limit={}) by {}",
        target.email,
        "granted" if grant else "revoked",
        new_limit,
        admin.email,
    )
    return _render(db, request)


@router.post("/api/admin/users/{user_id}/sales-order-approver", response_class=HTMLResponse)
async def set_sales_order_approver(
    request: Request,
    user_id: int,
    can_approve: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grant or revoke a user's QP Sales-Order approval right; returns the refreshed
    Users partial.

    The right is the per-user User.can_approve_qp_sales column; the QP Sales section
    (QP_SALES gate) routes to its holders. Admin-only (require_admin); each change
    writes an APPROVAL_GRANT / APPROVAL_REVOKE audit row. A no-op (state unchanged) re-
    renders without auditing, mirroring set_buyplan_approver.
    """
    target = _editable_target(db, user_id)
    grant = str(can_approve).strip().lower() in {"true", "1", "on", "yes"}

    if target.can_approve_qp_sales == grant:
        return _render(db, request)  # no-op, nothing to audit

    target.can_approve_qp_sales = grant
    action = UserAuditAction.APPROVAL_GRANT if grant else UserAuditAction.APPROVAL_REVOKE
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=action,
        detail={"gate": "qp_sales"},
    )
    db.commit()
    logger.info("User {} sales-order approval {} by {}", target.email, "granted" if grant else "revoked", admin.email)
    return _render(db, request)


@router.post("/api/admin/users/{user_id}/po-approver", response_class=HTMLResponse)
async def set_po_approver(
    request: Request,
    user_id: int,
    can_approve: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grant or revoke a user's QP Purchasing-section approval right; returns the
    refreshed Users partial.

    The right is the per-user User.can_approve_qp_purchasing column; the QP Purchasing
    section (QP_PURCHASING gate) routes to its holders. Admin-only (require_admin); each
    change writes an APPROVAL_GRANT / APPROVAL_REVOKE audit row. A no-op (state
    unchanged) re-renders without auditing, mirroring set_buyplan_approver. (The route
    name stays /po-approver; SP-3 only repointed the column it governs.)
    """
    target = _editable_target(db, user_id)
    grant = str(can_approve).strip().lower() in {"true", "1", "on", "yes"}

    if target.can_approve_qp_purchasing == grant:
        return _render(db, request)  # no-op, nothing to audit

    target.can_approve_qp_purchasing = grant
    action = UserAuditAction.APPROVAL_GRANT if grant else UserAuditAction.APPROVAL_REVOKE
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=action,
        detail={"gate": "qp_purchasing"},
    )
    db.commit()
    logger.info("User {} QP-purchasing approval {} by {}", target.email, "granted" if grant else "revoked", admin.email)
    return _render(db, request)


@router.post("/api/admin/users/{user_id}/purchase-order-approver", response_class=HTMLResponse)
async def set_purchase_order_approver(
    request: Request,
    user_id: int,
    can_approve: str = Form(...),
    limit: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grant or revoke a user's purchase-order approval right + optional dollar limit.

    Sets User.can_approve_purchase_orders and User.purchase_order_approval_limit. The
    right gates the per-line PO sign-off (``verify_po`` — Verify/Reject on a cut PO
    awaiting verification); the limit caps the dollar amount of an individual PO the
    holder may verify — NULL (unlimited) or ≥ the line amount, mirroring the prepayment
    grant. limit="" or "unlimited" → NULL (no cap); a positive decimal sets the cap.
    Admin-only; each change writes an APPROVAL_GRANT / APPROVAL_REVOKE audit row. A
    no-op (state unchanged) re-renders without auditing.
    """
    target = _editable_target(db, user_id)
    grant = str(can_approve).strip().lower() in {"true", "1", "on", "yes"}

    # Parse the optional dollar limit (mirrors set_prepayment_approver).
    limit_str = (limit or "").strip()
    if limit_str in {"", "unlimited"}:
        new_limit: Decimal | None = None
    else:
        try:
            new_limit = Decimal(limit_str)
            if new_limit <= 0:
                return _render(db, request, error="Dollar limit must be a positive number.", status_code=400)
        except InvalidOperation:
            return _render(db, request, error="Enter a valid dollar limit (e.g. 1000.00).", status_code=400)

    if target.can_approve_purchase_orders == grant and target.purchase_order_approval_limit == new_limit:
        return _render(db, request)  # no-op, nothing to audit

    target.can_approve_purchase_orders = grant
    target.purchase_order_approval_limit = new_limit
    action = UserAuditAction.APPROVAL_GRANT if grant else UserAuditAction.APPROVAL_REVOKE
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=action,
        detail={"gate": "purchase_order", "limit": str(new_limit) if new_limit is not None else None},
    )
    db.commit()
    logger.info(
        "User {} PO approval {} (limit={}) by {}",
        target.email,
        "granted" if grant else "revoked",
        new_limit,
        admin.email,
    )
    return _render(db, request)


# ── Per-user access editor ───────────────────────────────────────────


def _access_row(target: User, key: AccessKey, db: Session) -> dict:
    """Build one editor row {key, label, effective, state} for *key*.

    `state` is the dropdown's current selection: "on"/"off" for an explicit override,
    else "default" (follow the role). ops_verification is special-cased — its
    state/effective come from VerificationGroupMember (not access_overrides) and it
    never has a "default" state.
    """
    label = _ACCESS_KEY_LABELS[key]
    if key == AccessKey.OPS_VERIFICATION:
        effective = user_has_access(target, key, db)
        return {"key": str(key), "label": label, "effective": effective, "state": "on" if effective else "off"}

    overrides = target.access_overrides or {}
    key_str = str(key)
    if key_str in overrides:
        state = "on" if overrides[key_str] else "off"
    else:
        state = "default"
    return {"key": key_str, "label": label, "effective": user_has_access(target, key, db), "state": state}


def user_access_context(target: User, db: Session) -> dict:
    """Build {target, module_rows, capability_rows} for the access editor partial."""
    return {
        "target": target,
        "module_rows": [_access_row(target, k, db) for k in MODULE_ACCESS_KEYS],
        "capability_rows": [_access_row(target, k, db) for k in CAPABILITY_ACCESS_KEYS],
    }


def _render_access_panel(target: User, db: Session, request: Request):
    """Render the access-editor partial for *target* (HTMX swaps the modal root)."""
    ctx = {"request": request, **user_access_context(target, db)}
    return template_response("htmx/partials/settings/user_access_panel.html", ctx)


@router.get("/api/admin/users/{user_id}/access-panel", response_class=HTMLResponse)
async def user_access_panel(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Render the per-user Access editor modal — admin only."""
    target = _editable_target(db, user_id)
    return _render_access_panel(target, db, request)


@router.post("/api/admin/users/{user_id}/access", response_class=HTMLResponse)
async def set_user_access(
    request: Request,
    user_id: int,
    key: str = Form(...),
    value: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Grant / revoke / reset one access key for a user; re-renders the access panel.

    value ∈ {"on", "off", "default"}. For ops_verification the override store is ignored
    and the VerificationGroupMember membership is driven instead (default→off). For
    every other key, access_overrides is reassigned to a NEW dict (the column is a plain
    JSON, not MutableDict — in-place mutation would not flush).
    """
    target = _editable_target(db, user_id)

    try:
        access_key = AccessKey(key)
    except ValueError:
        raise HTTPException(400, "Unknown access key")
    if value not in {"on", "off", "default"}:
        raise HTTPException(400, "Invalid value")

    if access_key == AccessKey.OPS_VERIFICATION:
        # default acts as off — membership is curated explicitly, never "follow role".
        want_active = value == "on"
        member = db.query(VerificationGroupMember).filter_by(user_id=target.id).first()
        if member is None:
            db.add(
                VerificationGroupMember(user_id=target.id, is_active=want_active, added_at=datetime.now(timezone.utc))
            )
        else:
            member.is_active = want_active
        effective_after = want_active
    else:
        # Reassign a NEW dict — User.access_overrides is a plain JSON column, so an
        # in-place mutation of the existing dict would not be detected / flushed.
        overrides = dict(target.access_overrides or {})
        if value == "default":
            overrides.pop(str(access_key), None)
        else:
            overrides[str(access_key)] = value == "on"
        target.access_overrides = overrides
        effective_after = user_has_access(target, access_key, db)

    action = UserAuditAction.ACCESS_GRANT if effective_after else UserAuditAction.ACCESS_REVOKE
    record_user_audit(
        db,
        actor_id=admin.id,
        target_user_id=target.id,
        action=action,
        detail={"key": str(access_key), "value": value},
    )
    db.commit()
    db.refresh(target)
    logger.info("Access {}={} for {} by {}", access_key, value, target.email, admin.email)
    return _render_access_panel(target, db, request)


# ── Audit log viewer ─────────────────────────────────────────────────


def users_audit_context(db: Session, limit: int = 200) -> dict:
    """Build {audit_rows, limit, truncated, total} for the audit-log partial.

    Loads the latest *limit* UserAdminAudit rows newest-first and resolves each row's
    actor + target User. Both users are batch-loaded by id into one dict (single query)
    so the view never N+1s; a missing actor (None / SET NULL'd) or target renders as
    "system" / "—" in the template. `truncated` is True when more rows exist than shown.
    """
    total = db.query(UserAdminAudit).count()
    audits = (
        db.query(UserAdminAudit).order_by(UserAdminAudit.created_at.desc(), UserAdminAudit.id.desc()).limit(limit).all()
    )

    user_ids = {a.actor_id for a in audits if a.actor_id} | {a.target_user_id for a in audits if a.target_user_id}
    users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}

    audit_rows = [
        {
            "when": a.created_at,
            "actor": users_by_id.get(a.actor_id),
            "target": users_by_id.get(a.target_user_id),
            "action": a.action,
            "detail": a.detail or {},
        }
        for a in audits
    ]
    return {"audit_rows": audit_rows, "limit": limit, "truncated": total > limit, "total": total}


@router.get("/api/admin/users/audit", response_class=HTMLResponse)
async def users_audit(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Render the user-management audit log modal — admin only."""
    ctx = {"request": request, **users_audit_context(db)}
    return template_response("htmx/partials/settings/users_audit.html", ctx)
