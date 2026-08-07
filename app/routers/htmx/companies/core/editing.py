"""routers/htmx/companies/core/editing.py — primary-contact/parent-company setters,
account collaborators, account edit forms + inline field editing (W4.8 split of
core.py).

Rendered-detail responses go through ``_pkg._render_company_detail`` (the package
attribute, never a module-scope ``from ..detail import ...``) — the same
load-bearing invariant as .lifecycle (see the NOTE there): importing ``..detail``
at module scope would register its ``/v2/partials/customers/{company_id}``
catch-all before core's static routes.

Called by: app.routers.htmx.companies.core (package __init__ re-export, route
    registration)
Depends on: app.services.crm_field_history, .._registries, ..._shared, .common
"""

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import app.routers.htmx.companies as _pkg

from .....constants import UserRole
from .....database import get_db
from .....dependencies import can_manage_account, can_manage_account_team, is_manager_or_admin, require_user
from .....models import AccountCollaborator, Company, CustomerSite, SiteContact, User
from .....services.crm_field_history import ENTITY_COMPANY, record_field_change
from .....template_env import template_response
from .....utils.column_limits import ensure_fits_column
from ..._shared import _base_ctx
from .._registries import EDITABLE_ACCOUNT_FIELDS, apply_company_field
from .common import router


@router.post(
    "/v2/partials/customers/{company_id}/primary-contact/{contact_id}",
    response_class=HTMLResponse,
)
async def set_account_primary_contact(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.primary_contact_id to contact_id (account-level primary contact).

    IDOR-safe: verifies contact belongs to a site under company_id.
    Owner-or-admin gate. Returns refreshed company detail partial.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")

    # IDOR-safe: verify contact belongs to a site under this company.
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company.primary_contact_id = contact_id
    company.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(company)
    logger.info("Company {} primary contact set to {} by {}", company_id, contact_id, user.email)

    return await _pkg._render_company_detail(request, company_id, user, db)


@router.post(
    "/v2/partials/customers/{company_id}/parent",
    response_class=HTMLResponse,
)
async def set_parent_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.parent_company_id; validates no cycle.

    Accepts parent_company_id= form field (empty → clear).
    Cycle guard: rejects self-parent and any descendant as parent.
    Owner-or-admin gate.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not (is_manager_or_admin(user) or company.account_owner_id == user.id):
        raise HTTPException(403, "Only the account owner or a manager can change company hierarchy")

    form = await request.form()
    raw = (form.get("parent_company_id") or "").strip()

    _set_parent_company(db, company, raw)
    company.updated_at = datetime.now(UTC)
    db.commit()
    db.refresh(company)
    logger.info("Company {} parent set to {} by {}", company_id, raw or "None", user.email)

    return await _pkg._render_company_detail(request, company_id, user, db)


# ── Shared helper — parent-company validation used by set_parent_company + edit_company ──


def _set_parent_company(db: Session, company: Company, raw_parent_id: str) -> None:
    """Validate and set company.parent_company_id from the raw form string.

    ``raw_parent_id`` is the stripped string value from the submitted form:
      - empty string → clear the parent (set to None)
      - integer string → validate, cycle-check, then set

    Raises HTTPException(400) for bad input or cycle; HTTPException(404) for missing
    parent. Does NOT commit — caller owns the transaction.
    """
    if not raw_parent_id:
        company.parent_company_id = None
        return

    if not raw_parent_id.isdigit():
        raise HTTPException(400, "parent_company_id must be an integer")

    parent_id = int(raw_parent_id)
    if parent_id == company.id:
        raise HTTPException(400, "A company cannot be its own parent (would create a cycle)")

    parent = db.get(Company, parent_id)
    if not parent:
        raise HTTPException(404, "Parent company not found")

    # Cycle guard: walk ancestor chain of proposed parent; reject if we reach company.id.
    visited: set[int] = set()
    cursor = parent
    while cursor.parent_company_id is not None:
        if cursor.parent_company_id in visited:
            break  # existing cycle in DB — stop walking
        visited.add(cursor.id)
        if cursor.parent_company_id == company.id:
            raise HTTPException(400, "Setting this parent would create a cycle in the company hierarchy")
        cursor = db.get(Company, cursor.parent_company_id)
        if cursor is None:
            break

    company.parent_company_id = parent_id


# ── Phase 3: Account Collaborators (add/remove helpers) ──────────────────


@router.post("/v2/partials/customers/{company_id}/collaborators", response_class=HTMLResponse)
async def add_account_collaborator(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a helper collaborator to this account.

    Gate: can_manage_account_team (primary owner OR manager/admin ONLY).
    Helpers, site-owners, and unrelated reps are denied (403).
    Validates: user_id exists, is not the primary owner, is not already a collaborator.
    Returns the refreshed collaborators partial.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Only the account owner or a manager can manage the team")

    form = await request.form()
    raw_user_id = (form.get("user_id") or "").strip()
    if not raw_user_id or not raw_user_id.isdigit():
        raise HTTPException(400, "user_id is required and must be an integer")

    target_user_id = int(raw_user_id)
    target_user = db.get(User, target_user_id)
    if not target_user:
        raise HTTPException(404, "User not found")

    if target_user_id == company.account_owner_id:
        raise HTTPException(400, "The primary account owner cannot be added as a collaborator")

    existing = db.query(AccountCollaborator).filter_by(company_id=company_id, user_id=target_user_id).first()
    if existing:
        raise HTTPException(409, "This user is already a collaborator on this account")

    collaborator = AccountCollaborator(company_id=company_id, user_id=target_user_id, role="helper")
    db.add(collaborator)
    db.commit()
    logger.info(
        "Collaborator added: company={} user={} by {}",
        company_id,
        target_user_id,
        user.email,
    )

    return await _collaborators_partial(request, company_id=company_id, user=user, db=db, company=company)


@router.delete(
    "/v2/partials/customers/{company_id}/collaborators/{collab_user_id}",
    response_class=HTMLResponse,
)
async def remove_account_collaborator(
    request: Request,
    company_id: int,
    collab_user_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a helper collaborator from this account.

    Gate: can_manage_account_team (primary owner OR manager/admin ONLY).
    Helpers and unrelated reps are denied (403).
    Returns the refreshed collaborators partial.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Only the account owner or a manager can manage the team")

    # Validate the target user exists (prevents state-probing via silent 200 on garbage ids)
    if not db.get(User, collab_user_id):
        raise HTTPException(404, "User not found")

    collaborator = db.query(AccountCollaborator).filter_by(company_id=company_id, user_id=collab_user_id).first()
    if collaborator:
        db.delete(collaborator)
        db.commit()
        logger.info(
            "Collaborator removed: company={} user={} by {}",
            company_id,
            collab_user_id,
            user.email,
        )

    return await _collaborators_partial(request, company_id=company_id, user=user, db=db, company=company)


async def _collaborators_partial(
    request: Request,
    company_id: int,
    user: User,
    db: Session,
    company: "Company | None" = None,
):
    """Render the collaborators partial for a given company.

    *company* may be passed by callers that already hold the loaded object to avoid a
    second DB fetch.  If omitted, it is fetched here.
    """
    if company is None:
        company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    collaborators = (
        db.query(AccountCollaborator, User)
        .join(User, AccountCollaborator.user_id == User.id)
        .filter(AccountCollaborator.company_id == company_id)
        .order_by(User.name)
        .all()
    )
    can_manage_team = can_manage_account_team(user, company)
    all_users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all() if can_manage_team else []

    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "collaborators": collaborators,
            "all_users": all_users,
            "can_manage_team": can_manage_team,
        }
    )
    return template_response("htmx/partials/customers/_collaborators.html", ctx)


# ── Sprint 4: Company CRUD (parameterized routes) ──────────────────────


@router.get("/v2/partials/customers/{company_id}/edit-form", response_class=HTMLResponse)
async def company_edit_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return inline edit form for company fields."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    users = (
        db.query(User).filter(User.role.in_((UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN))).all()
    )
    all_companies = (
        db.query(Company.id, Company.name)
        .filter(Company.id != company_id, Company.is_active.is_(True))
        .order_by(Company.name)
        .all()
    )
    return template_response(
        "htmx/partials/customers/edit_form.html",
        {"request": request, "company": company, "users": users, "all_companies": all_companies},
    )


@router.post("/v2/partials/customers/{company_id}/edit", response_class=HTMLResponse)
async def edit_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save company edits and return refreshed detail."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        ensure_fits_column(Company, "name", name, "Company name")
        # Duplicate-name guard — mirror create_company. Company.name is nullable=False
        # and NOT unique, so nothing else stops a rename colliding with another account.
        # Exclude self (Company.id != company_id) so a no-op or case-only save on the
        # same row doesn't false-positive.
        existing = (
            db.query(Company).filter(sqlfunc.lower(Company.name) == name.lower(), Company.id != company_id).first()
        )
        if existing:
            raise HTTPException(409, f"Company '{existing.name}' already exists (ID {existing.id})")
        company.name = name
    source = form.get("source", "").strip()
    # source is blank-sentinel (see registry-loop comment below) but still a bounded
    # String(50) — guard the submitted value; the preserved current value already fits.
    ensure_fits_column(Company, "source", source, "Source")
    company.source = source or company.source
    # Owner reassignment is a TEAM action — only the primary owner / a manager may seize
    # primary ownership (can_manage_account admits collaborators + site-owners, who must
    # NOT be able to lock out the real owner). Gate only when the value actually changes.
    owner_id_raw = form.get("owner_id")
    owner_id = (owner_id_raw or "").strip()
    if owner_id and owner_id.isdigit():
        new_owner_id = int(owner_id)
        if new_owner_id != company.account_owner_id:
            if not can_manage_account_team(user, company):
                raise HTTPException(403, "Only the account owner or a manager can change the primary owner")
            # The new owner must be a real active user — mirrors create_company and the
            # bulk assign-owner path, so a deactivated/non-existent id can't silently take
            # ownership (or raise an unhandled FK IntegrityError on commit).
            target = db.get(User, new_owner_id)
            if not target or not target.is_active:
                raise HTTPException(400, "Owner must be an active user")
            company.account_owner_id = new_owner_id
    elif owner_id_raw is not None and not owner_id and company.account_owner_id is not None:
        # Submitted-EMPTY owner ("— None —" selected) with an owner set = explicit
        # unassign. Same team gate as reassignment; an ABSENT field stays a no-op.
        if not can_manage_account_team(user, company):
            raise HTTPException(403, "Only the account owner or a manager can change the primary owner")
        company.account_owner_id = None

    parent_company_id_raw = form.get("parent_company_id", "").strip()
    # Parent-company (hierarchy) edits are also a team action — match set_parent_company,
    # which gates on owner/manager — so a collaborator can't restructure the hierarchy.
    if parent_company_id_raw != (str(company.parent_company_id or "")):
        if not can_manage_account_team(user, company):
            raise HTTPException(403, "Only the account owner or a manager can change company hierarchy")
    _set_parent_company(db, company, parent_company_id_raw)

    # Registry fields — DRY via apply_company_field: a field ABSENT from the form is a
    # partial edit (preserved); a SUBMITTED blank is an explicit clear (→ NULL). notes and
    # tax_id follow that rule (the edit form prefills both, so blank means "user cleared
    # it" — CRM Wave 3 item 3). ONLY source keeps blank-sentinel "preserve current value"
    # semantics above, because its select has a real "— Keep current —" option whose
    # submitted value is legitimately blank.
    _form_handled = {"source"}
    for f in EDITABLE_ACCOUNT_FIELDS:
        if f in _form_handled:
            continue
        raw = form.get(f)
        if raw is not None:  # field was submitted
            apply_company_field(company, f, raw)
    # updated_at set inside apply_company_field; ensure it's set for non-registry writes too
    company.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Company {} edited by {}", company_id, user.email)

    # Refreshed detail replaces the detail root in place (form hx-target=#company-detail-<id>,
    # outerHTML) so it works in both the workspace and a deep-linked full page. The HX-Trigger
    # refreshes the left account list too (name/owner/type edits change the row) when present.
    resp = await _pkg._render_company_detail(request, company_id, user, db)
    resp.headers["HX-Trigger"] = "cdmListRefresh"
    return resp


# ── Inline Field Edit — Account (WS1) ─────────────────────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/field/edit/{field}",
    response_class=HTMLResponse,
)
async def company_field_edit_form(
    request: Request,
    company_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline edit widget for a single account field."""
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    meta = EDITABLE_ACCOUNT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_edit.html",
        {
            **_base_ctx(request, user),
            "obj": company,
            "field": field,
            "entity": "company",
            "meta": meta,
            "post_url": f"/v2/partials/customers/{company_id}/field",
            "display_url": f"/v2/partials/customers/{company_id}/field/display/{field}",
        },
    )


@router.get(
    "/v2/partials/customers/{company_id}/field/display/{field}",
    response_class=HTMLResponse,
)
async def company_field_display(
    request: Request,
    company_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the display span for a single account field (cancel path)."""
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")
    meta = EDITABLE_ACCOUNT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": company,
            "field": field,
            "entity": "company",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/field/edit/{field}",
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/field",
    response_class=HTMLResponse,
)
async def company_field_post(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save a single inline-edited account field; return the display span."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")
    form = await request.form()
    field = (form.get("field") or "").strip()
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    value = form.get("value") or ""
    old_value = getattr(company, field, None)
    apply_company_field(company, field, value)
    record_field_change(
        db,
        entity_type=ENTITY_COMPANY,
        entity_id=company.id,
        field_name=field,
        old_value=old_value,
        new_value=getattr(company, field, None),
        user_id=user.id,
    )
    db.commit()
    logger.info("Company {} field {} edited inline by {}", company_id, field, user.email)
    meta = EDITABLE_ACCOUNT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": company,
            "field": field,
            "entity": "company",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/field/edit/{field}",
        },
    )
