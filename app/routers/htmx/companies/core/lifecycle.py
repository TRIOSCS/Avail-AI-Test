"""routers/htmx/companies/core/lifecycle.py — company create/typeahead/dup-check,
tier/disposition, deactivate/reactivate, park-in-prospecting, AI dup/name suggestions
(W4.8 split of core.py).

Everything here that needs the rendered detail partial goes through
``_pkg._render_company_detail`` / ``_pkg.company_detail_partial`` (the package
attribute, NOT a static ``from ..detail import ...``) for two reasons: (1) core's
own static routes (account-list, create-form, typeahead, check-duplicate,
archived) MUST register on the shared router before ``..detail``'s
``/v2/partials/customers/{company_id}`` catch-all — a module-level import of
``..detail`` would trigger its route registration as a side effect of importing
THIS module, before core's own routes finish registering; and (2) it preserves
the monkeypatch on ``app.routers.htmx.companies.company_detail_partial`` a test
relies on. The same invariant holds in .editing.

Called by: app.routers.htmx.companies.core (package __init__ re-export, route
    registration)
Depends on: app.services.crm_service, app.services.prospect_reclamation,
    app.company_utils, app.cache.decorators, ..._shared, .common
"""

import html as html_mod
import json
from datetime import UTC, datetime

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import app.routers.htmx.companies as _pkg

from .....cache.decorators import invalidate_prefix
from .....company_utils import find_company_dup_match, suggest_clean_company_name
from .....constants import UserRole
from .....database import get_db
from .....dependencies import can_manage_account, can_manage_account_team, is_manager_or_admin, require_user
from .....models import Company, CustomerSite, User
from .....services.crm_service import cadence_state, next_best_touch
from .....services.prospect_reclamation import park_company_in_prospecting
from .....template_env import template_response
from .....utils.column_limits import ensure_fits_column
from .....utils.normalization_helpers import normalize_country, normalize_phone_e164, normalize_us_state
from .....utils.search_builder import SearchBuilder
from ..._shared import _base_ctx
from .common import router

# NOTE: _render_company_detail is deliberately NOT imported at module scope here.
# This subpackage's static routes (account-list, create-form, typeahead,
# check-duplicate, archived) must register on the shared router BEFORE ..detail's
# own `/v2/partials/customers/{company_id}` catch-all — a module-level `from
# ..detail import ...` would trigger ..detail's import (and route registration) as
# a side effect of importing THIS module, before core's own @router...
# decorators run. Every call site below goes through `_pkg._render_company_detail`
# instead (same package-attribute indirection already used for
# `_pkg.company_detail_partial` in send_company_to_prospecting_htmx).

_VALID_TIERS = frozenset({"key", "core", "standard", "prospect"})

# ── Sprint 4: Company CRUD (static routes — must precede {company_id}) ──


@router.get("/v2/partials/customers/create-form", response_class=HTMLResponse)
async def company_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return create company form."""
    users = (
        db.query(User).filter(User.role.in_((UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN))).all()
    )
    return template_response(
        "htmx/partials/customers/create_form.html",
        {"request": request, "users": users},
    )


@router.post("/v2/partials/customers/create", response_class=HTMLResponse)
async def create_company(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new company and redirect to its detail page."""
    form = await request.form()
    name = form.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Company name is required")

    # Duplicate check
    existing = db.query(Company).filter(sqlfunc.lower(Company.name) == name.lower()).first()
    if existing:
        raise HTTPException(409, f"Company '{existing.name}' already exists (ID {existing.id})")

    raw_phone = form.get("phone", "").strip() or None
    raw_hq_state = form.get("hq_state", "").strip() or None
    raw_hq_country = form.get("hq_country", "").strip() or None
    company = Company(
        name=name,
        website=form.get("website", "").strip() or None,
        industry=form.get("industry", "").strip() or None,
        notes=form.get("notes", "").strip() or None,
        is_active=True,
        legal_name=form.get("legal_name", "").strip() or None,
        employee_size=form.get("employee_size", "").strip() or None,
        revenue_range=form.get("revenue_range", "").strip() or None,
        hq_city=form.get("hq_city", "").strip() or None,
        hq_state=(normalize_us_state(raw_hq_state) or raw_hq_state) if raw_hq_state else None,
        hq_country=(normalize_country(raw_hq_country) or raw_hq_country) if raw_hq_country else None,
        phone=(normalize_phone_e164(raw_phone) or raw_phone) if raw_phone else None,
        credit_terms=form.get("credit_terms", "").strip() or None,
        tax_id=form.get("tax_id", "").strip() or None,
        source=form.get("source", "").strip() or "manual",
    )
    # Model-derived length guards (Wave 3 item 6) — reject over-length values with a clean
    # 400 instead of a Postgres StringDataRightTruncation 500 (SQLite tests mask it).
    for _field, _label in (
        ("name", "Company name"),
        ("website", "Website"),
        ("industry", "Industry"),
        ("legal_name", "Legal name"),
        ("employee_size", "Employee size"),
        ("revenue_range", "Revenue range"),
        ("hq_city", "HQ city"),
        ("hq_state", "HQ state"),
        ("hq_country", "HQ country"),
        ("phone", "Phone"),
        ("credit_terms", "Credit terms"),
        ("tax_id", "Tax ID"),
        ("source", "Source"),
    ):
        ensure_fits_column(Company, _field, getattr(company, _field), _label)
    # Assigning a NEW account to someone other than yourself is a manager action, and the
    # target must be a real active user (mirrors the bulk assign-owner path). A plain rep
    # assigning to self / leaving it blank keeps the current behaviour.
    owner_id = form.get("owner_id", "")
    if owner_id and owner_id.isdigit():
        new_owner_id = int(owner_id)
        if new_owner_id != user.id:
            if not is_manager_or_admin(user):
                raise HTTPException(403, "Only a manager can assign an account to another user")
            target = db.get(User, new_owner_id)
            if not target or not target.is_active:
                raise HTTPException(400, "Owner must be an active user")
        company.account_owner_id = new_owner_id
    db.add(company)
    db.flush()

    # Auto-create default site
    default_site = CustomerSite(
        company_id=company.id,
        site_name="HQ",
        site_type="hq",
        is_active=True,
    )
    db.add(default_site)
    db.commit()
    logger.info("Company {} created by {}", company.id, user.email)

    # Load the new account's detail into the CDM right panel (form hx-target=#cdm-detail);
    # the HX-Trigger tells the workspace's hidden listener to refresh the left account list
    # so the freshly created row appears. On deep-link contexts (no listener) it no-ops.
    resp = await _pkg._render_company_detail(request, company.id, user, db)
    resp.headers["HX-Trigger"] = "cdmListRefresh"
    return resp


@router.get("/v2/partials/customers/typeahead", response_class=HTMLResponse)
async def company_typeahead(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return company typeahead results as HTML options."""
    if not q.strip() or len(q.strip()) < 2:
        return HTMLResponse("")

    sb = SearchBuilder(q.strip())
    companies = (
        db.query(Company)
        .filter(Company.is_active.is_(True), sb.ilike_filter(Company.name))
        .order_by(Company.name)
        .limit(10)
        .all()
    )
    rows = [f'<option value="{c.id}">{html_mod.escape(c.name or "")}</option>' for c in companies]
    return HTMLResponse("\n".join(rows))


@router.get("/v2/partials/customers/check-duplicate", response_class=HTMLResponse)
async def check_company_duplicate(
    request: Request,
    name: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Check for duplicate company name, return warning HTML if found."""
    if not name.strip():
        return HTMLResponse("")

    existing = (
        db.query(Company)
        .filter(
            Company.is_active.is_(True),
            sqlfunc.lower(Company.name) == name.strip().lower(),
        )
        .first()
    )
    if existing:
        return HTMLResponse(
            f'<p class="text-sm text-amber-600">A company named "{html_mod.escape(existing.name or "")}" already exists (ID {existing.id}).</p>'
        )
    return HTMLResponse("")


@router.post("/v2/partials/customers/{company_id}/tier", response_class=HTMLResponse)
async def set_company_tier(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.tier; re-renders the cadence hero with updated badge + NBT.

    Accepts tier= from the inline select.  Blank value clears the tier (NULL → behaves
    as 'standard').  Invalid value → 400.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    tier_raw = (form.get("tier") or "").strip()

    if tier_raw and tier_raw not in _VALID_TIERS:
        raise HTTPException(400, f"Invalid tier '{tier_raw}'. Valid: {sorted(_VALID_TIERS)}")

    company.tier = tier_raw or None
    db.commit()
    db.refresh(company)

    _cadence = cadence_state(company.tier, company.last_outbound_at)
    _nbt = next_best_touch(company.tier, company.last_outbound_at)
    logger.info("Company {} tier set to {} by {}", company_id, company.tier, user.email)
    return template_response(
        "htmx/partials/customers/_cadence_hero.html",
        {
            "request": request,
            "company": company,
            "cadence_state": _cadence,
            "next_best_touch": _nbt,
        },
    )


_VALID_DISPOSITIONS = frozenset({"active", "bucket"})


@router.post("/v2/partials/customers/{company_id}/disposition", response_class=HTMLResponse)
async def set_company_disposition(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set Company.disposition (active|bucket); re-renders the disposition control.

    Owner-or-admin only (mirrors release_prospect). Validates against the allowlist
    (invalid → 400). Writes disposition + optional reason + audit fields
    (set_by/set_at). Reversible — set back to 'active'. Invalidates the cached
    company_list / typeahead so the bucketed account drops out of the call-list.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can set disposition")

    form = await request.form()
    disp_raw = (form.get("disposition") or "").strip()
    reason_raw = (form.get("disposition_reason") or "").strip()

    if disp_raw not in _VALID_DISPOSITIONS:
        raise HTTPException(400, f"Invalid disposition '{disp_raw}'. Valid: {sorted(_VALID_DISPOSITIONS)}")

    company.disposition = disp_raw
    company.disposition_reason = reason_raw or None
    company.disposition_set_by = user.id
    company.disposition_set_at = datetime.now(UTC)
    db.commit()
    db.refresh(company)

    invalidate_prefix("company_list")

    logger.info("Company {} disposition set to {} by {}", company_id, company.disposition, user.email)
    return template_response(
        "htmx/partials/customers/_disposition_control.html",
        {"request": request, "company": company},
    )


@router.post("/v2/partials/customers/{company_id}/deactivate", response_class=HTMLResponse)
async def deactivate_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Archive (soft-delete) a company — sets is_active=False, clears ownership, stores
    reason.

    Gate: can_manage_account_team — primary owner or manager/admin only.
    On archive: unassigns account owner (ownership_cleared_at stamped) and stores
    optional disposition_reason from the form.
    Re-renders the full company detail partial so the archived banner appears immediately.

    Called by: kebab menu "Archive (Do Not Call)" button in detail.html.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Not authorized to deactivate accounts")
    form = await request.form()
    disposition_reason = form.get("disposition_reason", "").strip() or None
    company.is_active = False
    company.account_owner_id = None
    company.ownership_cleared_at = datetime.now(UTC)
    company.disposition_reason = disposition_reason
    db.commit()
    db.refresh(company)

    invalidate_prefix("company_list")

    logger.info("Company {} archived (DNC) by {}, reason={!r}", company_id, user.email, disposition_reason)
    return await _pkg._render_company_detail(request, company_id, user, db)


@router.post("/v2/partials/customers/{company_id}/reactivate", response_class=HTMLResponse)
async def reactivate_company(
    request: Request,
    company_id: int,
    from_archived: bool = Query(False, alias="from_archived"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Restore an archived company by setting is_active=True.

    Gate: is_manager_or_admin only.

    from_archived=true: called from the archived-list view → returns the refreshed
    archived_list partial (so the reactivated row disappears from the list).
    Default (false): called from the company detail banner → returns the detail
    partial (banner disappears after reactivate).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not is_manager_or_admin(user):
        raise HTTPException(403, "Only managers and admins may reactivate archived accounts")
    company.is_active = True
    db.commit()
    db.refresh(company)
    logger.info("Company {} reactivated by {}", company_id, user.email)

    if from_archived:
        # Return refreshed archived list — reactivated company will no longer appear.
        companies = db.query(Company).filter(Company.is_active.is_(False)).order_by(Company.name).all()
        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "companies": companies,
                "can_reactivate": True,  # gate already passed above
            }
        )
        return template_response("htmx/partials/customers/archived_list.html", ctx)

    return await _pkg._render_company_detail(request, company_id, user, db)


@router.post("/v2/partials/customers/{company_id}/send-to-prospecting", response_class=HTMLResponse)
async def send_company_to_prospecting_htmx(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Park an owned account into the prospecting pool (SP4 "Park in prospecting").

    Gate: the account owner OR a manager/admin (``can_manage_account_team``) — else 403.
    Clears ownership, surfaces the account as a SUGGESTED prospect (by domain) stamped with
    the SP4 sales-park provenance (``discovery_source="sales_park"``, ``parked_by_id``), and
    returns a toast. A manager/admin — who still oversees the now-unassigned account — gets
    the re-rendered detail partial; the former owner, who just relinquished access, is
    redirected back to the customers list (re-rendering the detail would 404 for them).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account_team(user, company):
        raise HTTPException(403, "Only the account owner or a manager can park this account")

    try:
        result = park_company_in_prospecting(company_id, user.id, db, is_admin=is_manager_or_admin(user))
    except LookupError as e:
        raise HTTPException(404, "Company not found") from e
    except ValueError as e:
        raise HTTPException(403, str(e)) from e

    invalidate_prefix("company_list")

    msg = f"Parked {result['company_name']} in prospecting"
    if not result["pooled"]:
        msg += " (no domain — ownership cleared, not pooled)"
    trigger = json.dumps({"showToast": {"message": msg}})

    if is_manager_or_admin(user):
        # Resolved off the PACKAGE attribute (not a static import of ..detail.company_detail_partial)
        # so a test that monkeypatches app.routers.htmx.companies.company_detail_partial
        # replaces what actually runs here — see the module docstring.
        response = await _pkg.company_detail_partial(request, company_id, user=user, db=db)
        response.headers["HX-Trigger"] = trigger
        return response

    # Former owner relinquished access — send them back to the customers list.
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = "/v2/customers"
    response.headers["HX-Trigger"] = trigger
    return response


@router.get("/v2/partials/customers/{company_id}/dup-suggestion", response_class=HTMLResponse)
async def company_dup_suggestion(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lazy per-account duplicate banner — top dedup match for THIS company + a Merge
    button reusing the merge-form/preview/merge flow.

    Renders nothing (empty 200) when there is no near-duplicate.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")

    # Anchored lookup for THIS company only (one trgm-index probe on PG, one O(n) fuzzy
    # anchor pass on SQLite) — never the global all-pairs scan, whose top-50 truncation
    # could silently hide a real near-dup. Inactive accounts get no banner (the lookup
    # filters partners to active companies; the anchor must be active too).
    match = None
    try:
        if company.is_active:
            match = find_company_dup_match(db, company.id, company.normalized_name, threshold=85)
    except Exception as e:  # pragma: no cover - defensive, mirrors data-ops scan guard
        logger.warning("dup-suggestion scan failed for company {}: {}", company_id, e)
        return HTMLResponse("")

    if not match:
        return HTMLResponse("")

    ctx = {"request": request, "company": company, "match": match}
    return template_response("htmx/partials/customers/_dup_suggestion.html", ctx)


@router.get("/v2/partials/customers/{company_id}/name-suggestion", response_class=HTMLResponse)
async def company_name_suggestion(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Suggest-only name-normalization chip: surface the suffix-stripped form of the
    current name as "Suggested name: X — Apply?".

    Uses the same normalizer the dedup scanner uses, re-cased from the original tokens
    (so we only strip the legal suffix / leading "the", never lowercase the whole name).
    Renders nothing (empty 200) when the current name is already clean.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")

    suggested = suggest_clean_company_name(company.name or "")
    if not suggested or suggested == (company.name or "").strip():
        return HTMLResponse("")

    ctx = {"request": request, "company": company, "suggested": suggested}
    return template_response("htmx/partials/customers/_name_suggestion.html", ctx)


@router.post("/v2/partials/customers/{company_id}/apply-name", response_class=HTMLResponse)
async def company_apply_name(
    request: Request,
    company_id: int,
    name: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a suggested company name (rep-initiated; the suggest-only counterpart to a
    silent rewrite).

    normalized_name follows automatically via Company._sync_normalized_name
    (@validates). Returns an empty 200 so the chip removes itself (hx-swap outerHTML).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    new_name = (name or "").strip()
    if not new_name:
        raise HTTPException(400, "Name is required")

    company.name = new_name  # @validates resyncs normalized_name
    db.commit()

    invalidate_prefix("company_list")
    logger.info("Company {} renamed to '{}' (suggested) by {}", company_id, new_name, user.email)
    return HTMLResponse("")
