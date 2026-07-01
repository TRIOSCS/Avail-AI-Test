"""routers/htmx/companies.py — Company/customer & contact partial views (HTMX + Alpine).

Server-rendered HTML partials for the company/customer + contact CRM surface: the
customers (account) list, the global customer-contacts list, company CRUD, CSV
import (companies + contacts), bulk actions, segment/contact tags, the inline-edit
field registry + account/contact inline editors, custom fields, company + contact
merge, contact move, sites & site-contacts CRUD, account collaborators, the company
detail shell and tabs, the contacts-tab/suggested-contacts loops, and contact
notes/files. Extracted verbatim from htmx_views.py (same ``/v2/partials/customers``
+ ``/v2/partials/companies`` + ``/v2/partials/contacts`` paths, same ``htmx-views``
tag) as part of the CRM-cluster domain split.

Called by: app/main.py (router mount); htmx_views.py re-imports ``company_tab`` for
    its company activity add-note route; tests import ``_staleness_tier``.
Depends on: app.models, app.dependencies, app.database, app.services.crm_service,
    app.services.tagging, app.services.company_merge_service,
    app.services.contact_merge_service, ._shared
"""

import html as html_mod
import json
from collections.abc import Iterable
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ...constants import CRM_INDUSTRIES, ActivityType, ContactRole, RequisitionStatus, UserRole
from ...database import get_db
from ...dependencies import can_manage_account, can_manage_account_team, is_manager_or_admin, require_user
from ...models import (
    AccountCollaborator,
    BuyPlan,
    Company,
    CustomerSite,
    Quote,
    Requisition,
    SiteContact,
    User,
)
from ...services.crm_completeness import company_completeness as _company_completeness
from ...services.crm_field_history import (
    ENTITY_COMPANY as _ENTITY_COMPANY,
)
from ...services.crm_field_history import (
    ENTITY_CONTACT as _ENTITY_CONTACT,
)
from ...services.crm_field_history import (
    field_history_for as _field_history_for,
)
from ...services.crm_field_history import (
    record_field_change as _record_field_change,
)
from ...services.crm_service import cadence_state as _cadence_state
from ...services.crm_service import cdm_list_ctx as _cdm_list_ctx
from ...services.crm_service import company_commercial_stats as _company_commercial_stats
from ...services.crm_service import company_contact_rows as _company_contact_rows
from ...services.crm_service import next_best_touch as _next_best_touch
from ...services.crm_service import staleness_tier as _staleness_tier  # noqa: F401
from ...services.tagging import assign_segment_tag as _assign_segment_tag
from ...services.tagging import get_or_create_segment_tag as _get_or_create_segment_tag
from ...services.tagging import list_all_segment_tags as _list_all_segment_tags
from ...services.tagging import list_company_segment_tags as _list_company_segment_tags
from ...services.tagging import unassign_segment_tag as _unassign_segment_tag
from ...template_env import template_response
from ...utils.normalization_helpers import normalize_country, normalize_phone_e164, normalize_us_state
from ...utils.search_builder import SearchBuilder
from ...utils.sql_helpers import escape_like
from ._shared import _DASH, _base_ctx

router = APIRouter(tags=["htmx-views"])


# ── Company/Customer partials ──────────────────────────────────────────


# Redirect old /v2/companies URLs to /v2/customers
@router.get("/v2/companies", response_class=HTMLResponse)
@router.get("/v2/companies/{path:path}", response_class=HTMLResponse)
async def companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/companies URLs to /v2/customers."""
    from fastapi.responses import RedirectResponse

    new_url = f"/v2/customers/{path}" if path else "/v2/customers"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=301)


# Redirect old /v2/partials/companies URLs to /v2/partials/customers
@router.get("/v2/partials/companies", response_class=HTMLResponse)
@router.get("/v2/partials/companies/{path:path}", response_class=HTMLResponse)
async def partials_companies_redirect(request: Request, path: str = ""):
    """Redirect old /v2/partials/companies URLs to /v2/partials/customers."""
    from fastapi.responses import RedirectResponse

    new_url = f"/v2/partials/customers/{path}" if path else "/v2/partials/customers"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=301)


@router.get("/v2/partials/customers", response_class=HTMLResponse)
async def companies_list_partial(
    request: Request,
    search: str = "",
    staleness: str = "",
    account_type: str = "",
    my_only: bool = False,
    sort: str = "oldest",
    segment: int = Query(0, ge=0),
    disposition: str = "",
    has_open_reqs: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the CDM account workspace (split-panel list + detail) as HTML partial."""
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        _cdm_list_ctx(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            my_only=my_only,
            sort=sort,
            segment=segment,
            disposition=disposition or None,
            has_open_reqs=has_open_reqs,
            limit=limit,
            offset=offset,
            include_overdue=True,
            include_users=is_manager_or_admin(user),
        )
    )
    ctx.update(_saved_views_ctx(request, user, db, "customers"))
    return template_response("htmx/partials/customers/list.html", ctx)


@router.get("/v2/partials/customers/account-list", response_class=HTMLResponse)
async def companies_account_list_partial(
    request: Request,
    search: str = "",
    staleness: str = "",
    account_type: str = "",
    my_only: bool = False,
    sort: str = "oldest",
    segment: int = Query(0, ge=0),
    disposition: str = "",
    has_open_reqs: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return only the CDM left-panel account list (filter/sort/pagination refresh).

    The overdue chip lives in the filter bar (not re-rendered here), so this route skips
    the overdue COUNT query.
    """
    ctx = {"request": request, "user": user}
    ctx.update(
        _cdm_list_ctx(
            db,
            user,
            search=search,
            staleness=staleness,
            account_type=account_type,
            my_only=my_only,
            sort=sort,
            segment=segment,
            disposition=disposition or None,
            has_open_reqs=has_open_reqs,
            limit=limit,
            offset=offset,
            include_users=is_manager_or_admin(user),
        )
    )
    return template_response("htmx/partials/customers/_account_list.html", ctx)


# ── Global customer-contacts list (cross-company, role-scoped) ─────────────
# /v2/contacts is cross-tenant PII: SALES/TRADER reps see ONLY contacts in
# accounts they can manage (shared company_visibility_predicate); MANAGER/ADMIN
# see all. Scoping lives in crm_service.customer_contacts_query — this route is
# thin HTTP glue.


@router.get("/v2/partials/contacts", response_class=HTMLResponse)
async def customer_contacts_partial(
    request: Request,
    search: str = "",
    company_id: int = Query(0, ge=0),
    contact_role: str = "",
    cadence_state: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the cross-company customer-contacts workspace as an HTML partial."""
    from ...services.crm_service import customer_contacts_list_ctx

    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        customer_contacts_list_ctx(
            db,
            user,
            search=search,
            company_id=company_id,
            contact_role=contact_role,
            cadence_state=cadence_state,
            limit=limit,
            offset=offset,
        )
    )
    ctx["contact_roles"] = CANONICAL_ROLES
    ctx.update(_saved_views_ctx(request, user, db, "contacts"))
    return template_response("htmx/partials/customers/contacts_list.html", ctx)


# ── Bulk actions (static — must precede /{company_id}) ────────────────────
# Every bulk action FILTERS selected ids to only those the caller can act on.
# Sales/trader reps may only act on companies where can_manage_account() is True.
# Manager/admin can act on all. "assign-owner" is MANAGER/ADMIN ONLY.
# Non-manageable companies are silently skipped; the summary reports both counts.

_VALID_BULK_COMPANY_ACTIONS = frozenset({"deactivate", "send-to-prospecting", "assign-owner"})
_BULK_MAX_IDS = 200


def _manageable_company_ids(user: User, companies: Iterable[Company], db: Session) -> set[int]:
    """Return the subset of *companies*' ids that this rep may manage — batched.

    Batched equivalent of calling ``can_manage_account`` once per company for a
    non-manager: a company is manageable when the user is its ``account_owner``, owns
    one of its sites, or is a named collaborator. Runs in at most two ownership queries
    total regardless of how many companies are passed — the per-row alternative issues
    up to 2*N DB round-trips (one site + one collaborator EXISTS per company). Managers
    and admins manage everything, so callers must gate on ``is_manager_or_admin`` first
    and skip this entirely.
    """
    company_list = list(companies)
    ids = {c.id for c in company_list}
    if not ids:
        return set()
    # account-owner: resolved from the already-loaded Company rows (no query).
    manageable = {c.id for c in company_list if c.account_owner_id == user.id}
    remaining = ids - manageable
    if remaining:
        manageable.update(
            cid
            for (cid,) in db.query(CustomerSite.company_id)
            .filter(CustomerSite.company_id.in_(remaining), CustomerSite.owner_id == user.id)
            .distinct()
        )
        remaining = ids - manageable
    if remaining:
        manageable.update(
            cid
            for (cid,) in db.query(AccountCollaborator.company_id)
            .filter(AccountCollaborator.company_id.in_(remaining), AccountCollaborator.user_id == user.id)
            .distinct()
        )
    return manageable


@router.post("/v2/partials/customers/bulk/{action}", response_class=HTMLResponse)
async def customers_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a bulk action to selected companies.

    Auth: deactivate + send-to-prospecting gate per-company via can_manage_account;
    assign-owner is MANAGER/ADMIN ONLY (403 for reps).
    Selected ids that the caller cannot act on are silently skipped; the returned
    partial includes a summary of applied vs skipped counts.

    Actions:
    - deactivate: set is_active=False
    - send-to-prospecting: clear ownership (ownership_cleared_at + account_owner_id=NULL)
    - assign-owner: set account_owner_id to owner_id form param (MANAGER/ADMIN only)
    """
    from ...dependencies import is_manager_or_admin

    if action not in _VALID_BULK_COMPANY_ACTIONS:
        raise HTTPException(400, f"Invalid action '{action}'. Allowed: {sorted(_VALID_BULK_COMPANY_ACTIONS)}")

    # assign-owner is manager/admin only — 403 before reading IDs to avoid timing oracle
    if action == "assign-owner" and not is_manager_or_admin(user):
        raise HTTPException(403, "assign-owner requires MANAGER or ADMIN role")

    form = await request.form()
    ids_str = form.get("ids", "") or ""
    try:
        ids = [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    except ValueError:
        raise HTTPException(400, "Invalid ID list")

    if len(ids) > _BULK_MAX_IDS:
        raise HTTPException(400, f"Maximum {_BULK_MAX_IDS} companies per bulk action")

    if not ids:
        # No-op: return refreshed list
        ctx = {"request": request, "user": user}
        ctx.update(
            _cdm_list_ctx(
                db,
                user,
                search="",
                staleness="",
                account_type="",
                my_only=False,
                sort="oldest",
                segment=0,
                disposition=None,
                has_open_reqs=False,
                limit=50,
                offset=0,
                include_users=is_manager_or_admin(user),
            )
        )
        return template_response("htmx/partials/customers/_account_list.html", ctx)

    companies = db.query(Company).filter(Company.id.in_(ids)).all()

    # Filter to only companies this user can act on
    if is_manager_or_admin(user):
        authorised = companies
        skipped = 0
    else:
        manageable_ids = _manageable_company_ids(user, companies, db)
        authorised = [c for c in companies if c.id in manageable_ids]
        skipped = len(companies) - len(authorised)

    applied = 0
    if action == "deactivate":
        for co in authorised:
            co.is_active = False
            applied += 1
    elif action == "send-to-prospecting":
        for co in authorised:
            co.account_owner_id = None
            co.ownership_cleared_at = datetime.now(timezone.utc)
            applied += 1
    elif action == "assign-owner":
        owner_id_raw = form.get("owner_id")
        if not owner_id_raw:
            raise HTTPException(400, "owner_id is required for assign-owner")
        try:
            new_owner_id = int(owner_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "owner_id must be an integer")
        new_owner = db.get(User, new_owner_id)
        if not new_owner or not new_owner.is_active:
            raise HTTPException(400, "owner_id does not correspond to an active user")
        for co in authorised:
            co.account_owner_id = new_owner_id
            applied += 1

    if applied:
        db.commit()
        logger.info(
            "Bulk {} applied to {} companies ({} skipped) by {}",
            action,
            applied,
            skipped,
            user.email,
        )

    action_label = {
        "deactivate": "Deactivated",
        "send-to-prospecting": "Sent to prospecting",
        "assign-owner": "Reassigned",
    }.get(action, action.title())

    if skipped:
        msg = f"{action_label} {applied} of {applied + skipped} ({skipped} skipped — not yours)"
    else:
        msg = f"{action_label} {applied} account{'s' if applied != 1 else ''}"

    ctx = {"request": request, "user": user}
    ctx.update(
        _cdm_list_ctx(
            db,
            user,
            search="",
            staleness="",
            account_type="",
            my_only=False,
            sort="oldest",
            segment=0,
            disposition=None,
            has_open_reqs=False,
            limit=50,
            offset=0,
            include_users=is_manager_or_admin(user),
        )
    )
    resp = template_response("htmx/partials/customers/_account_list.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps(
        {
            "showToast": {"message": msg},
            "clearSelection": True,
        }
    )
    return resp


# ── Saved views (filter presets) — static, MUST precede /{company_id} ─────
# Per-user named snapshots of a list's filter query params (list_key='customers'
# | 'contacts'). The control lives in both filter bars (list.html /
# contacts_list.html); these routes refresh the #saved-views-<list_key> wrapper.

# Map list_key → the filter <form> id its presets apply onto (used by the partial).
_SAVED_VIEW_FORMS = {"customers": "cdm-filters", "contacts": "contacts-filters"}


def _saved_views_ctx(request: Request, user: User, db: Session, list_key: str) -> dict:
    """Context for the _saved_views.html partial (and the list shells that embed it)."""
    from ...services.saved_views_service import list_saved_views

    return {
        "request": request,
        "user": user,
        "saved_views": list_saved_views(db, user, list_key),
        "list_key": list_key,
        "target_form": _SAVED_VIEW_FORMS.get(list_key, "cdm-filters"),
    }


@router.get("/v2/partials/customers/saved-views", response_class=HTMLResponse)
async def saved_views_list(
    request: Request,
    list_key: str = "customers",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the saved-views control for *list_key* ('customers' | 'contacts')."""
    from ...services.saved_views_service import valid_list_key

    if not valid_list_key(list_key):
        raise HTTPException(400, "Unknown list_key")
    ctx = _saved_views_ctx(request, user, db, list_key)
    return template_response("htmx/partials/customers/_saved_views.html", ctx)


@router.post("/v2/partials/customers/saved-views", response_class=HTMLResponse)
async def saved_views_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the submitted filter set as a named view (upsert on name).

    Reads list_key + name + the hx-included filter form fields; the service whitelists
    which keys are persisted.
    """
    from ...services.saved_views_service import create_saved_view, valid_list_key

    form = await request.form()
    list_key = (form.get("list_key") or "customers").strip()
    if not valid_list_key(list_key):
        raise HTTPException(400, "Unknown list_key")
    name = form.get("name") or ""
    raw_filters = {k: v for k, v in form.items() if k not in ("list_key", "name")}
    try:
        view = create_saved_view(db, user, list_key, name, raw_filters)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    logger.info("Saved view {!r} ({}) created by {}", view.name, list_key, user.email)
    ctx = _saved_views_ctx(request, user, db, list_key)
    resp = template_response("htmx/partials/customers/_saved_views.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": f"Saved view '{view.name}'"}})
    return resp


@router.delete("/v2/partials/customers/saved-views/{view_id}", response_class=HTMLResponse)
async def saved_views_delete(
    request: Request,
    view_id: int,
    list_key: str = "customers",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete one of the caller's saved views, then re-render the control."""
    from ...services.saved_views_service import delete_saved_view, valid_list_key

    if not valid_list_key(list_key):
        raise HTTPException(400, "Unknown list_key")
    deleted = delete_saved_view(db, user, view_id)
    ctx = _saved_views_ctx(request, user, db, list_key)
    resp = template_response("htmx/partials/customers/_saved_views.html", ctx)
    if deleted:
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "View deleted"}})
    return resp


# ── Contacts bulk actions (static — global cross-company contacts list) ────
# Mirrors the accounts bulk pattern (customers_bulk_action): per-contact auth via
# can_manage_account on the owning company; manager/admin act on all. Selected
# contacts the caller cannot manage are silently skipped (summary reports both).
_VALID_BULK_CONTACT_ACTIONS = frozenset({"archive", "dnc"})


def _form_int(form, key: str, default: int = 0) -> int:
    raw = (form.get(key) or "").strip()
    return int(raw) if raw.isdigit() else default


def _contacts_list_response(request: Request, user: User, db: Session, form) -> HTMLResponse:
    """Re-render the global contacts list from the hx-included filter fields."""
    from ...services.crm_service import customer_contacts_list_ctx

    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        customer_contacts_list_ctx(
            db,
            user,
            search=(form.get("search") or "").strip(),
            company_id=_form_int(form, "company_id"),
            contact_role=(form.get("contact_role") or "").strip(),
            cadence_state=(form.get("cadence_state") or "").strip(),
            limit=_form_int(form, "limit", 50),
            offset=_form_int(form, "offset", 0),
        )
    )
    ctx["contact_roles"] = CANONICAL_ROLES
    ctx.update(_saved_views_ctx(request, user, db, "contacts"))
    return template_response("htmx/partials/customers/contacts_list.html", ctx)


@router.post("/v2/partials/contacts/bulk/{action}", response_class=HTMLResponse)
async def contacts_bulk_action(
    request: Request,
    action: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply a bulk action (archive | dnc) to selected contacts.

    Auth: per-contact via can_manage_account on the owning company (manager/admin
    act on all). Non-manageable contacts are silently skipped; the summary reports
    applied vs skipped. Re-renders the contacts list scoped to the included filters.
    """
    if action not in _VALID_BULK_CONTACT_ACTIONS:
        raise HTTPException(400, f"Invalid action '{action}'. Allowed: {sorted(_VALID_BULK_CONTACT_ACTIONS)}")

    form = await request.form()
    ids_str = form.get("ids", "") or ""
    ids = [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    if len(ids) > _BULK_MAX_IDS:
        raise HTTPException(400, f"Maximum {_BULK_MAX_IDS} contacts per bulk action")

    applied = 0
    skipped = 0
    if ids:
        rows = (
            db.query(SiteContact, Company)
            .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
            .join(Company, CustomerSite.company_id == Company.id)
            .filter(SiteContact.id.in_(ids))
            .all()
        )
        is_mgr = is_manager_or_admin(user)
        manageable_ids = set() if is_mgr else _manageable_company_ids(user, [company for _, company in rows], db)
        for contact, company in rows:
            if is_mgr or company.id in manageable_ids:
                if action == "archive":
                    contact.is_archived = True
                elif action == "dnc":
                    contact.do_not_contact = True
                applied += 1
            else:
                skipped += 1
        if applied:
            db.commit()
            logger.info(
                "Bulk contact {} applied to {} contacts ({} skipped) by {}",
                action,
                applied,
                skipped,
                user.email,
            )

    label = {"archive": "Archived", "dnc": "Marked Do-Not-Contact"}.get(action, action.title())
    if skipped:
        msg = f"{label} {applied} of {applied + skipped} ({skipped} skipped — not yours)"
    else:
        msg = f"{label} {applied} contact{'s' if applied != 1 else ''}"

    resp = _contacts_list_response(request, user, db, form)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}, "clearSelection": True})
    return resp


# ── Company / contact CSV import: preview + confirm ───────────────────────
# Auth: require_user (same gate as all CDM mutations).
# Preview: parse CSV → return table of rows with per-row status flags
#          (valid / duplicate / invalid).
# Confirm: create Company rows for non-duplicate valid rows; assign importer
#          as account_owner_id.
# Contact preview: parse CSV → flag emails that already exist in site_contacts.

_IMPORT_MAX_ROWS = 1000


@router.post("/v2/partials/customers/import/preview", response_class=HTMLResponse)
async def import_companies_preview(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse company CSV and return a preview table (no writes).

    Expected columns: name (required), website, account_type.
    Flags: duplicate (normalized_name collision), invalid (missing name).
    """
    import csv
    import io as _io

    from ...vendor_utils import normalize_vendor_name

    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "A CSV file is required")

    try:
        content_bytes = await file.read() if hasattr(file, "read") else file.file.read()
        text = content_bytes.decode("utf-8", errors="replace")
        reader = csv.DictReader(_io.StringIO(text))
        raw_rows = list(reader)
    except Exception:
        return HTMLResponse(
            '<div class="text-rose-700 text-sm p-3 bg-rose-50 rounded border border-rose-200">'
            "Could not parse CSV — please check the file format.</div>"
        )

    if len(raw_rows) > _IMPORT_MAX_ROWS:
        raise HTTPException(400, f"CSV exceeds {_IMPORT_MAX_ROWS} row limit")

    # Build set of existing normalized names for dedup check
    existing_norm_names = {
        row[0] for row in db.query(Company.normalized_name).filter(Company.normalized_name.isnot(None)).all()
    }

    rows = []
    for raw in raw_rows:
        name = (raw.get("name") or raw.get("Name") or "").strip()
        website = (raw.get("website") or raw.get("Website") or "").strip()
        account_type = (raw.get("account_type") or raw.get("Account Type") or "").strip()
        norm = normalize_vendor_name(name) if name else None

        if not name:
            status = "invalid"
            status_label = "Missing name"
        elif norm and norm in existing_norm_names:
            status = "duplicate"
            status_label = "Already exists"
        else:
            status = "valid"
            status_label = "OK"

        rows.append(
            {
                "name": name,
                "website": website,
                "account_type": account_type,
                "status": status,
                "status_label": status_label,
            }
        )

    valid_count = sum(1 for r in rows if r["status"] == "valid")
    dup_count = sum(1 for r in rows if r["status"] == "duplicate")
    invalid_count = sum(1 for r in rows if r["status"] == "invalid")

    import json as _json

    rows_json = _json.dumps(
        [
            {"name": r["name"], "website": r["website"], "account_type": r["account_type"]}
            for r in rows
            if r["status"] == "valid"
        ]
    )

    return template_response(
        "htmx/partials/customers/_import_preview.html",
        {
            "request": request,
            "rows": rows,
            "valid_count": valid_count,
            "dup_count": dup_count,
            "invalid_count": invalid_count,
            "rows_json": rows_json,
        },
    )


@router.post("/v2/partials/customers/import/confirm", response_class=HTMLResponse)
async def import_companies_confirm(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create Company rows from a confirmed import (validated rows_json payload).

    Each row: {name, website?, account_type?}. Deduplicates by normalized_name.
    Sets account_owner_id to the importing user.
    """
    import json as _json

    from ...vendor_utils import normalize_vendor_name

    form = await request.form()
    rows_json_str = form.get("rows_json", "")
    if not rows_json_str:
        raise HTTPException(400, "rows_json is required")

    try:
        rows = _json.loads(rows_json_str)
        if not isinstance(rows, list):
            raise ValueError("Expected a list")
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid rows_json — must be a JSON array")

    if len(rows) > _IMPORT_MAX_ROWS:
        raise HTTPException(400, f"rows_json exceeds {_IMPORT_MAX_ROWS} row limit")

    # Re-fetch existing normalized names to guard against race conditions
    existing_norm = {
        row[0] for row in db.query(Company.normalized_name).filter(Company.normalized_name.isnot(None)).all()
    }

    created = 0
    skipped_dup = 0
    skipped_invalid = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name:
            skipped_invalid += 1
            continue
        norm = normalize_vendor_name(name)
        if norm and norm in existing_norm:
            skipped_dup += 1
            continue

        co = Company(
            name=name,
            website=str(row.get("website", "")).strip() or None,
            account_type=str(row.get("account_type", "")).strip() or None,
            account_owner_id=user.id,
            is_active=True,
            source="import",
            created_at=now,
        )
        db.add(co)
        if norm:
            existing_norm.add(norm)  # prevent intra-batch duplicates
        created += 1

    if created:
        db.commit()
        logger.info("CSV import: {} companies created by {}", created, user.email)

    parts = [f"Imported {created} compan{'y' if created == 1 else 'ies'}"]
    if skipped_dup:
        parts.append(f"{skipped_dup} duplicate{'s' if skipped_dup != 1 else ''} skipped")
    if skipped_invalid:
        parts.append(f"{skipped_invalid} invalid row{'s' if skipped_invalid != 1 else ''} skipped")
    summary = "; ".join(parts)

    resp = template_response(
        "htmx/partials/customers/_import_confirm_summary.html",
        {"request": request, "summary": summary},
    )
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": summary}})
    return resp


# ── Contact CSV import: preview ────────────────────────────────────────────
# Parses a CSV with columns: company_name, contact_name, email, phone, role.
# Flags email duplicates (already in site_contacts).


@router.post("/v2/partials/customers/import/contacts/preview", response_class=HTMLResponse)
async def import_contacts_preview(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse contact CSV and return a preview table (no writes).

    Expected columns: company_name (required), contact_name (required), email, phone, role.
    Flags: duplicate (email collision in site_contacts), invalid (missing required fields).
    """
    import csv
    import io as _io

    from ...models.crm import SiteContact

    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "A CSV file is required")

    try:
        content_bytes = await file.read() if hasattr(file, "read") else file.file.read()
        text = content_bytes.decode("utf-8", errors="replace")
        reader = csv.DictReader(_io.StringIO(text))
        raw_rows = list(reader)
    except Exception:
        return HTMLResponse(
            '<div class="text-rose-700 text-sm p-3 bg-rose-50 rounded border border-rose-200">'
            "Could not parse CSV — please check the file format.</div>"
        )

    if len(raw_rows) > _IMPORT_MAX_ROWS:
        raise HTTPException(400, f"CSV exceeds {_IMPORT_MAX_ROWS} row limit")

    # Build set of existing contact emails for duplicate check
    existing_emails = {
        row[0].lower()
        for row in db.query(SiteContact.email).filter(SiteContact.email.isnot(None), SiteContact.email != "").all()
    }

    # Build company lookup for authz check (same logic as confirm)
    import re as _re

    from ...vendor_utils import normalize_vendor_name as _normalize_vendor_name

    all_companies = db.query(Company).filter(Company.is_active.is_(True)).all()
    _norm_to_company: dict[str, Company] = {}
    _domain_to_company: dict[str, Company] = {}
    for _co in all_companies:
        if _co.normalized_name:
            _norm_to_company[_co.normalized_name] = _co
        if _co.website:
            _dom = _re.sub(r"^https?://", "", _co.website.strip().lower())
            _dom = _re.sub(r"^www\.", "", _dom).split("/")[0].strip()
            if _dom:
                _domain_to_company[_dom] = _co

    # Precompute the manageable-company set once (batched) instead of per-row round-trips.
    _is_mgr = is_manager_or_admin(user)
    _manageable_ids = set() if _is_mgr else _manageable_company_ids(user, all_companies, db)

    rows = []
    for raw in raw_rows:
        company_name = (raw.get("company_name") or "").strip()
        contact_name = (raw.get("contact_name") or "").strip()
        email = (raw.get("email") or "").strip().lower()
        phone = (raw.get("phone") or "").strip()
        role = (raw.get("role") or "").strip()

        if not company_name or not contact_name:
            status = "invalid"
            status_label = "Missing required field"
        elif email and email in existing_emails:
            status = "duplicate"
            status_label = "Email already exists"
        else:
            # Check if the matched company is manageable by this user
            _norm = _normalize_vendor_name(company_name)
            _matched_co = _norm_to_company.get(_norm) if _norm else None
            if _matched_co is None and email and "@" in email:
                _matched_co = _domain_to_company.get(email.split("@", 1)[1])
            if _matched_co is not None and not (_is_mgr or _matched_co.id in _manageable_ids):
                status = "unauthorized"
                status_label = "Company not yours"
            else:
                status = "valid"
                status_label = "OK"

        rows.append(
            {
                "company_name": company_name,
                "contact_name": contact_name,
                "email": email,
                "phone": phone,
                "role": role,
                "status": status,
                "status_label": status_label,
            }
        )

    valid_count = sum(1 for r in rows if r["status"] == "valid")
    dup_count = sum(1 for r in rows if r["status"] == "duplicate")
    invalid_count = sum(1 for r in rows if r["status"] == "invalid")
    unauthorized_count = sum(1 for r in rows if r["status"] == "unauthorized")

    import json as _json

    contacts_rows_json = _json.dumps(
        [
            {
                "company_name": r["company_name"],
                "contact_name": r["contact_name"],
                "email": r["email"],
                "phone": r["phone"],
                "role": r["role"],
            }
            for r in rows
            if r["status"] == "valid"
        ]
    )

    return template_response(
        "htmx/partials/customers/_import_contacts_preview.html",
        {
            "request": request,
            "rows": rows,
            "valid_count": valid_count,
            "dup_count": dup_count,
            "invalid_count": invalid_count,
            "unauthorized_count": unauthorized_count,
            "rows_json": contacts_rows_json,
        },
    )


@router.post("/v2/partials/customers/import/contacts/confirm", response_class=HTMLResponse)
async def import_contacts_confirm(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create SiteContact rows from a confirmed contacts import.

    Per row: matches company by normalized_name or domain; attaches contact to
    the company's first ACTIVE site (creates an HQ site if none exists);
    deduplicates by email within the site; skips rows whose company isn't found.
    Reports created, skipped_no_company, and skipped_dup counts.
    """
    import json as _json

    from ...models.crm import CustomerSite, SiteContact
    from ...utils.phone import normalize_e164
    from ...vendor_utils import normalize_vendor_name

    form = await request.form()
    rows_json_str = form.get("rows_json", "")
    if not rows_json_str:
        raise HTTPException(400, "rows_json is required")

    try:
        rows = _json.loads(rows_json_str)
        if not isinstance(rows, list):
            raise ValueError("Expected a list")
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid rows_json — must be a JSON array")

    if len(rows) > _IMPORT_MAX_ROWS:
        raise HTTPException(400, f"rows_json exceeds {_IMPORT_MAX_ROWS} row limit")

    # Build company lookup: normalized_name → Company (active preferred)
    all_companies = db.query(Company).filter(Company.is_active.is_(True)).all()
    norm_to_company: dict[str, Company] = {}
    domain_to_company: dict[str, Company] = {}
    for co in all_companies:
        if co.normalized_name:
            norm_to_company[co.normalized_name] = co
        if co.website:
            # extract domain: strip scheme + www
            import re as _re

            domain = _re.sub(r"^https?://", "", co.website.strip().lower())
            domain = _re.sub(r"^www\.", "", domain).split("/")[0].strip()
            if domain:
                domain_to_company[domain] = co

    # Precompute the manageable-company set once (batched) instead of per-row round-trips.
    is_mgr = is_manager_or_admin(user)
    manageable_ids = set() if is_mgr else _manageable_company_ids(user, all_companies, db)

    now = datetime.now(timezone.utc)
    created = 0
    skipped_no_company = 0
    skipped_dup = 0
    skipped_unauthorized = 0

    for row in rows:
        company_name = str(row.get("company_name", "")).strip()
        contact_name = str(row.get("contact_name", "")).strip()
        email = str(row.get("email", "")).strip().lower() or None
        phone = str(row.get("phone", "")).strip() or None
        role = str(row.get("role", "")).strip() or None

        if not company_name or not contact_name:
            skipped_no_company += 1
            continue

        # Match company by normalized name, then by domain
        norm = normalize_vendor_name(company_name)
        co = norm_to_company.get(norm) if norm else None
        if co is None and email and "@" in email:
            email_domain = email.split("@", 1)[1]
            co = domain_to_company.get(email_domain)

        if co is None:
            skipped_no_company += 1
            continue

        # AUTHZ: rep may only attach contacts to companies they manage
        if not (is_mgr or co.id in manageable_ids):
            skipped_unauthorized += 1
            continue

        # Find or create the first ACTIVE site for this company
        site = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == co.id, CustomerSite.is_active.is_(True))
            .order_by(CustomerSite.id)
            .first()
        )
        if site is None:
            site = CustomerSite(
                company_id=co.id,
                site_name="HQ",
                is_active=True,
                created_at=now,
            )
            db.add(site)
            db.flush()  # get site.id

        # Deduplicate by email within the site
        if email:
            existing = (
                db.query(SiteContact)
                .filter(SiteContact.customer_site_id == site.id, SiteContact.email == email)
                .first()
            )
            if existing:
                skipped_dup += 1
                continue

        contact = SiteContact(
            customer_site_id=site.id,
            full_name=contact_name,
            email=email,
            phone=normalize_e164(phone) if phone else None,
            contact_role=role,
            is_active=True,
            created_at=now,
        )
        db.add(contact)
        created += 1

    if created:
        db.commit()
        logger.info("Contact CSV import: {} contacts created by {}", created, user.email)

    parts = [f"Imported {created} contact{'s' if created != 1 else ''}"]
    if skipped_no_company:
        parts.append(f"{skipped_no_company} skipped (company not found)")
    if skipped_dup:
        parts.append(f"{skipped_dup} duplicate{'s' if skipped_dup != 1 else ''} skipped")
    if skipped_unauthorized:
        parts.append(f"{skipped_unauthorized} skipped — not your account{'s' if skipped_unauthorized != 1 else ''}")
    summary = "; ".join(parts)

    resp = template_response(
        "htmx/partials/customers/_import_confirm_summary.html",
        {"request": request, "summary": summary},
    )
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": summary}})
    return resp


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
        phone=normalize_phone_e164(raw_phone) if raw_phone else None,
        credit_terms=form.get("credit_terms", "").strip() or None,
        tax_id=form.get("tax_id", "").strip() or None,
        source=form.get("source", "").strip() or "manual",
    )
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

    return await _render_company_detail(request, company.id, user, db)


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


def _company_quotes_query(db: Session, company):
    """Quotes belonging to an account: union of quotes linked via the
    company's customer sites OR via the company's requisitions (the latter
    catches quotes whose customer_site_id is NULL). Returns a Query, or None
    when the account can own no quotes (no sites and no requisitions).
    Called by: company_detail_partial (count), company_tab (quotes + activity).
    """
    site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company.id).all()]
    req_ids = [
        r.id
        for r in db.query(Requisition.id)
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            )
        )
        .all()
    ]
    conds = []
    if site_ids:
        conds.append(Quote.customer_site_id.in_(site_ids))
    if req_ids:
        conds.append(Quote.requisition_id.in_(req_ids))
    if not conds:
        return None
    return db.query(Quote).filter(or_(*conds)).options(joinedload(Quote.requisition))


def _company_buy_plans_query(db: Session, company):
    """Buy plans belonging to an account: all buy-plans whose requisition links
    to the company (via company_id FK or customer_name match). Returns a Query,
    or None when the account has no requisitions.
    Called by: company_detail_partial (count), company_tab (buy_plans).
    """
    req_ids = [
        r.id
        for r in db.query(Requisition.id)
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            )
        )
        .all()
    ]
    if not req_ids:
        return None
    return (
        db.query(BuyPlan)
        .options(joinedload(BuyPlan.lines), joinedload(BuyPlan.requisition))
        .filter(BuyPlan.requisition_id.in_(req_ids))
    )


@router.get("/v2/partials/customers/{company_id}/segment-tags", response_class=HTMLResponse)
async def company_segment_tags_partial(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the segment-tag chips + editor partial for a company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.post("/v2/partials/customers/{company_id}/segment-tags", response_class=HTMLResponse)
async def company_assign_segment_tag(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assign a segment tag to a company.

    Accepts tag_id= (existing) or tag_name= (creates new).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    tag_id_raw = form.get("tag_id", "").strip()
    tag_name_raw = form.get("tag_name", "").strip()

    if tag_name_raw:
        tag = _get_or_create_segment_tag(tag_name_raw, db)
    elif tag_id_raw:
        try:
            tag_id = int(tag_id_raw)
        except ValueError:
            raise HTTPException(400, "tag_id must be an integer")
        from ...models.tags import Tag as _Tag

        tag = db.query(_Tag).filter_by(id=tag_id).first()
        if not tag:
            raise HTTPException(404, "Tag not found")
    else:
        raise HTTPException(400, "Provide tag_id or tag_name")

    _assign_segment_tag(company_id=company_id, tag_id=tag.id, db=db)
    db.commit()

    tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.delete("/v2/partials/customers/{company_id}/segment-tags/{tag_id}", response_class=HTMLResponse)
async def company_unassign_segment_tag(
    request: Request,
    company_id: int,
    tag_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a segment tag from a company."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    _unassign_segment_tag(company_id=company_id, tag_id=tag_id, db=db)
    db.commit()

    tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_segment_tags.html",
        {
            "request": request,
            "company": company,
            "segment_tags": tags,
            "all_segment_tags": all_segment_tags,
        },
    )


# ── Contact tag routes ─────────────────────────────────────────────────────


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tags",
    response_class=HTMLResponse,
)
async def contact_assign_tag(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Assign a segment tag to a site contact.

    Accepts tag_id= (existing) or tag_name= (creates new tag_type='segment'). Returns
    the contact tags chips partial.
    """
    from ...models.tags import EntityTag as _EntityTag
    from ...models.tags import Tag as _Tag

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    form = await request.form()
    tag_id_raw = (form.get("tag_id") or "").strip()
    tag_name_raw = (form.get("tag_name") or "").strip()

    if tag_name_raw:
        tag = _get_or_create_segment_tag(tag_name_raw, db)
    elif tag_id_raw:
        try:
            tag_id = int(tag_id_raw)
        except ValueError:
            raise HTTPException(400, "tag_id must be an integer")
        tag = db.query(_Tag).filter_by(id=tag_id).first()
        if not tag:
            raise HTTPException(404, "Tag not found")
    else:
        raise HTTPException(400, "Provide tag_id or tag_name")

    existing = db.query(_EntityTag).filter_by(entity_type="site_contact", entity_id=contact_id, tag_id=tag.id).first()
    if existing:
        existing.is_visible = True
    else:
        et = _EntityTag(
            entity_type="site_contact",
            entity_id=contact_id,
            tag_id=tag.id,
            is_visible=True,
            interaction_count=0,
            total_entity_interactions=0,
        )
        db.add(et)
    db.commit()

    contact_tags = (
        db.query(_Tag)
        .join(_EntityTag, _EntityTag.tag_id == _Tag.id)
        .filter(
            _EntityTag.entity_type == "site_contact",
            _EntityTag.entity_id == contact_id,
            _EntityTag.is_visible.is_(True),
        )
        .order_by(_Tag.name)
        .all()
    )
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_contact_tags.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
            "contact_tags": contact_tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.delete(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/tags/{tag_id}",
    response_class=HTMLResponse,
)
async def contact_unassign_tag(
    request: Request,
    company_id: int,
    contact_id: int,
    tag_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a segment tag from a site contact."""
    from ...models.tags import EntityTag as _EntityTag
    from ...models.tags import Tag as _Tag

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    et = db.query(_EntityTag).filter_by(entity_type="site_contact", entity_id=contact_id, tag_id=tag_id).first()
    if et:
        db.delete(et)
        db.commit()

    contact_tags = (
        db.query(_Tag)
        .join(_EntityTag, _EntityTag.tag_id == _Tag.id)
        .filter(
            _EntityTag.entity_type == "site_contact",
            _EntityTag.entity_id == contact_id,
            _EntityTag.is_visible.is_(True),
        )
        .order_by(_Tag.name)
        .all()
    )
    all_segment_tags = _list_all_segment_tags(db=db)
    return template_response(
        "htmx/partials/customers/_contact_tags.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
            "contact_tags": contact_tags,
            "all_segment_tags": all_segment_tags,
        },
    )


@router.get("/v2/partials/customers/{company_id}/contacts/for-select")
async def get_company_contacts_for_select(
    company_id: int,
    exclude_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return active site contacts for a company as JSON for the reports_to select.

    Excludes the contact with exclude_id (self-exclusion for reports_to picker).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    q = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.is_active.is_(True),
        )
    )
    if exclude_id:
        q = q.filter(SiteContact.id != exclude_id)
    contacts = q.order_by(SiteContact.full_name).all()
    return [{"id": c.id, "name": c.full_name or c.first_name or "—"} for c in contacts]


_VALID_TIERS = frozenset({"key", "core", "standard", "prospect"})

# Canonical buying-role taxonomy — sourced from the ContactRole StrEnum (single
# source of truth in app/constants.py; mirrored as the `roles` Jinja2 global in
# app/template_env.py). Legacy DB values (buyer_po/specifier/ap_payer/logistics/
# exec/technical/decision_maker/operations) remain in the DB but can only be cleared
# via the "— clear —" option; they are not in this set.
CANONICAL_ROLES = tuple(ContactRole)
_VALID_ROLES = frozenset(CANONICAL_ROLES)

# ── Inline-editable field registry (WS1) ────────────────────────────────────
# Each field maps to {label, kind, choices (for select)}. tier/disposition/owner
# have dedicated controls and are excluded. owner inline deferred to WS2.

EDITABLE_ACCOUNT_FIELDS: dict[str, dict] = {
    "industry": {"label": "Industry", "kind": "select", "choices": list(CRM_INDUSTRIES)},
    "phone": {"label": "Phone", "kind": "text"},
    "employee_size": {"label": "Employees", "kind": "text"},
    "credit_terms": {"label": "Credit Terms", "kind": "text"},
    "website": {"label": "Website", "kind": "text"},
    "legal_name": {"label": "Legal Name", "kind": "text"},
    "revenue_range": {"label": "Revenue Range", "kind": "text"},
    "hq_city": {"label": "HQ City", "kind": "text"},
    "hq_state": {"label": "HQ State", "kind": "text"},
    "hq_country": {"label": "HQ Country", "kind": "text"},
    "account_type": {
        "label": "Account Type",
        "kind": "select",
        "choices": ["Customer", "Prospect", "Partner", "Competitor"],
    },
    "domain": {"label": "Domain", "kind": "text"},
    "linkedin_url": {"label": "LinkedIn URL", "kind": "text"},
    "tax_id": {"label": "Tax ID", "kind": "text"},
    "source": {"label": "Source", "kind": "text"},
    "notes": {"label": "Notes", "kind": "text"},
}

EDITABLE_CONTACT_FIELDS: dict[str, dict] = {
    # first_name + last_name replace the old single full_name inline editor.
    # apply_contact_field recomposes full_name when either is saved.
    "first_name": {"label": "First Name", "kind": "text"},
    "last_name": {"label": "Last Name", "kind": "text"},
    "title": {"label": "Title", "kind": "text"},
    "email": {"label": "Email", "kind": "text"},
    "phone": {"label": "Phone", "kind": "text"},
    "secondary_email": {"label": "Secondary Email", "kind": "text"},
    "secondary_phone": {"label": "Secondary Phone", "kind": "text"},
    "wechat_id": {"label": "WeChat ID", "kind": "text"},
    "linkedin_url": {"label": "LinkedIn", "kind": "text"},
    "contact_role": {
        "label": "Role",
        "kind": "select",
        "choices": list(CANONICAL_ROLES),
    },
    # contact_owner_id is intentionally NOT listed here — ownership flows via
    # site → account owner (per-contact picker removed in Phase 1).
}

# Ordered list: (field, label, kind, choices) — used by the detail template to render the
# always-visible known-fields grid. Every field here MUST also be in EDITABLE_ACCOUNT_FIELDS
# so the "Add <field>" affordance has a working edit endpoint behind it.
KNOWN_ACCOUNT_FIELDS: list[tuple[str, str, str, list[str] | None]] = [
    ("legal_name", "Legal Name", "text", None),
    ("website", "Website", "text", None),
    ("domain", "Domain", "text", None),
    ("phone", "Phone", "text", None),
    ("employee_size", "Employees", "text", None),
    ("revenue_range", "Revenue Range", "text", None),
    ("hq_city", "HQ City", "text", None),
    ("hq_state", "HQ State", "text", None),
    ("hq_country", "HQ Country", "text", None),
    ("tax_id", "Tax ID", "text", None),
    ("account_type", "Account Type", "select", ["Customer", "Prospect", "Partner", "Competitor"]),
    ("source", "Source", "text", None),
    ("notes", "Notes", "text", None),
]


# Field-name → human label, for rendering the field-history surfaces (company
# History tab + contact History modal). Merges both inline-edit registries so a
# history row's raw field_name resolves to its display label.
FIELD_LABELS: dict[str, str] = {
    field: meta["label"] for field, meta in {**EDITABLE_ACCOUNT_FIELDS, **EDITABLE_CONTACT_FIELDS}.items()
}


def apply_company_field(company: Company, field: str, value: str) -> None:
    """Apply a single inline-edited account field to *company* (does NOT commit).

    Validates/normalizes each field the same way edit_company does. Raises
    HTTPException(400) for invalid values, HTTPException(404) for unknown field. Called
    by both the inline-edit POST endpoint and edit_company (DRY).
    """
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    v = value.strip()
    if field == "phone":
        company.phone = normalize_phone_e164(v) if v else None
    elif field == "hq_state":
        company.hq_state = (normalize_us_state(v) or v) if v else None
    elif field == "hq_country":
        company.hq_country = (normalize_country(v) or v) if v else None
    elif field == "account_type":
        choices = EDITABLE_ACCOUNT_FIELDS["account_type"]["choices"]
        if v and v not in choices:
            raise HTTPException(400, f"Invalid account_type '{v}'. Valid: {choices}")
        company.account_type = v or None
    elif field == "industry":
        # Constrained pick-list (CRM_INDUSTRIES). Accept a canonical value, a blank
        # (clear), OR the unchanged current value — the last clause preserves legacy
        # free-text industries on no-op saves while constraining every NEW value.
        choices = EDITABLE_ACCOUNT_FIELDS["industry"]["choices"]
        if v and v not in choices and v != (company.industry or ""):
            raise HTTPException(400, f"Invalid industry '{v}'. Valid: {choices}")
        company.industry = v or None
    elif field == "website":
        # Reuse the Company schema's website validator so the inline-edit + edit_company
        # paths reject bad URLs the same way the create form does.
        from ...schemas.crm import normalize_website

        try:
            company.website = normalize_website(v)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        setattr(company, field, v or None)
    company.updated_at = datetime.now(timezone.utc)


def _recompose_full_name(contact: SiteContact) -> None:
    """Recompose contact.full_name from first_name + last_name (in-place).

    Rule: full_name is always derived from first_name/last_name when either is written
    via the form or inline-edit path. Direct full_name writers (legacy) leave first/last
    unchanged; this function is NOT called for those paths.
    """
    contact.full_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or (contact.full_name or "")


def apply_contact_field(
    contact: SiteContact,
    field: str,
    value: str,
    site_id: int,
    db: Session,
) -> None:
    """Apply a single inline-edited contact field to *contact* (does NOT commit).

    first_name / last_name edits recompose full_name automatically. At least one of
    first_name / last_name must be non-empty (enforced here). Raises HTTPException for
    invalid values. Called by both the inline-edit POST endpoint and edit_site_contact
    (DRY).
    """
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    v = value.strip()
    if field in ("first_name", "last_name"):
        setattr(contact, field, v or None)
        # After updating, verify at least one name part remains.
        if not contact.first_name and not contact.last_name:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        _recompose_full_name(contact)
    elif field == "email":
        if v and "@" not in v:
            raise HTTPException(400, "Invalid email address")
        if v:
            dup = (
                db.query(SiteContact)
                .filter(
                    SiteContact.customer_site_id == site_id,
                    sqlfunc.lower(SiteContact.email) == v.lower(),
                    SiteContact.id != contact.id,
                )
                .first()
            )
            if dup:
                raise HTTPException(409, f"Another contact at this site already uses {v}")
        contact.email = v or None
    elif field in ("phone", "secondary_phone"):
        # Normalize to E.164 on save, mirroring the account phone path
        # (apply_company_field) — reuses the shared normalize_phone_e164 util.
        setattr(contact, field, (normalize_phone_e164(v) or v) if v else None)
    elif field == "contact_role":
        contact.contact_role = _validate_role(v)
    else:
        setattr(contact, field, v or None)
    contact.updated_at = datetime.now(timezone.utc)


def _validate_role(role_raw: str) -> str | None:
    """Validate a contact_role value: blank → None, unknown → raises HTTPException 400.

    Used by set_contact_role chip endpoint AND edit_site_contact form endpoint so
    both paths share one source of truth for canonical-role enforcement.
    """
    cleaned = (role_raw or "").strip()
    if not cleaned:
        return None
    if cleaned not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid contact_role '{cleaned}'. Valid: {sorted(_VALID_ROLES)}")
    return cleaned


def _render_contacts_list(request: Request, user: User, company: Company, db: Session) -> HTMLResponse:
    """Build and return the contacts grouped-list partial for the Contacts tab.

    Shared by create, add-suggested, delete, set-primary, and edit endpoints so the five
    swap paths stay in sync with one another.
    """
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "contact_rows": _company_contact_rows(db, company.id, viewer=user),
            "now_utc": datetime.now(timezone.utc),
            "roles": CANONICAL_ROLES,
        }
    )
    return template_response("htmx/partials/customers/tabs/_contacts_grouped_list.html", ctx)


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

    _cadence = _cadence_state(company.tier, company.last_outbound_at)
    _nbt = _next_best_touch(company.tier, company.last_outbound_at)
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


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/role",
    response_class=HTMLResponse,
)
async def set_contact_role(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set SiteContact.contact_role; re-renders the role chip editor.

    Accepts contact_role= from the inline select.  Blank value clears the role (NULL).
    Invalid value → 400 (legacy values that pre-exist in the DB are not accepted via
    this endpoint; rep must choose a canonical role).
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    contact.contact_role = _validate_role(form.get("contact_role") or "")
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} role set to {} by {} (company {})",
        contact_id,
        contact.contact_role,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/do-not-contact",
    response_class=HTMLResponse,
)
async def set_contact_dnc(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set or clear SiteContact.do_not_contact; re-renders the DNC toggle partial.

    Accepts do_not_contact= from the inline form.  Non-empty value → True. Empty string
    → False (clear the flag).
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    dnc_raw = (form.get("do_not_contact") or "").strip()

    contact.do_not_contact = bool(dnc_raw)
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} do_not_contact set to {} by {} (company {})",
        contact_id,
        contact.do_not_contact,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


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
    company.disposition_set_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(company)

    from app.cache.decorators import invalidate_prefix

    invalidate_prefix("company_list")
    invalidate_prefix("companies_typeahead")

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
    company.ownership_cleared_at = datetime.now(timezone.utc)
    company.disposition_reason = disposition_reason
    db.commit()
    db.refresh(company)

    from app.cache.decorators import invalidate_prefix

    invalidate_prefix("company_list")
    invalidate_prefix("companies_typeahead")

    logger.info("Company {} archived (DNC) by {}, reason={!r}", company_id, user.email, disposition_reason)
    return await _render_company_detail(request, company_id, user, db)


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

    return await _render_company_detail(request, company_id, user, db)


@router.get("/v2/partials/customers/archived", response_class=HTMLResponse)
async def archived_companies_list(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the list of archived (DNC) companies.

    Gate: require_user — any logged-in user may VIEW the archived list.
    Reactivate button is only rendered for manager/admin.

    Called by: a future "Archived" tab or "View Archived" link in the CDM workspace.
    """
    from ...dependencies import is_manager_or_admin as _is_mgr_admin

    companies = db.query(Company).filter(Company.is_active.is_(False)).order_by(Company.name).all()
    ctx = {
        "request": request,
        "user": user,
        "companies": companies,
        "can_reactivate": _is_mgr_admin(user),
    }
    return template_response("htmx/partials/customers/archived_list.html", ctx)


@router.post("/v2/partials/customers/{company_id}/send-to-prospecting", response_class=HTMLResponse)
async def send_company_to_prospecting_htmx(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send an owned account back to the prospecting pool.

    Owner-or-admin only. Clears ownership, surfaces the account as a SUGGESTED prospect
    (by domain), and re-renders the company detail partial with a toast.
    """
    from ...services.prospect_claim import send_company_to_prospecting

    try:
        result = send_company_to_prospecting(company_id, user.id, db, is_admin=(user.role == UserRole.ADMIN))
    except LookupError:
        raise HTTPException(404, "Company not found")
    except ValueError as e:
        raise HTTPException(403, str(e))

    from app.cache.decorators import invalidate_prefix

    invalidate_prefix("company_list")
    invalidate_prefix("companies_typeahead")

    msg = f"Sent {result['company_name']} back to prospecting"
    if not result["pooled"]:
        msg += " (no domain — ownership cleared, not pooled)"
    response = await company_detail_partial(request, company_id, user=user, db=db)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    return response


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/priority",
    response_class=HTMLResponse,
)
async def set_contact_priority(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set or clear SiteContact.is_priority; re-renders the priority toggle partial.

    IDOR-safe: the contact must belong to a site under this company. Non-empty
    is_priority= → True; empty → False (clear).
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    contact.is_priority = bool((form.get("is_priority") or "").strip())
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} is_priority set to {} by {} (company {})",
        contact_id,
        contact.is_priority,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/archive",
    response_class=HTMLResponse,
)
async def set_contact_archive(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set or clear SiteContact.is_archived; re-renders the archive toggle partial.

    IDOR-safe: the contact must belong to a site under this company. Non-empty
    is_archived= → True; empty → False (restore). Archived contacts stay visible
    (sorted to the bottom) — is_active is never touched here.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")

    form = await request.form()
    contact.is_archived = bool((form.get("is_archived") or "").strip())
    db.commit()
    db.refresh(contact)

    logger.info(
        "Contact {} is_archived set to {} by {} (company {})",
        contact_id,
        contact.is_archived,
        user.email,
        company_id,
    )
    return _render_contacts_list(request, user, company, db)


# ── Increment 3: AI-organization surfaces (per-account) ───────────────────────
# STATIC trailing segments (dup-suggestion / name-suggestion / apply-name), so they
# MUST precede the GET /v2/partials/customers/{company_id} catch-all below. ADDITIVE —
# the merge engine (merge_companies) and the dedup scanner are reused as-is; no merge
# logic lives here. The dup banner reuses the existing merge-form → preview → POST
# .../merge flow. Naming is suggest-only — never a silent rewrite.


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
    from ...company_utils import find_company_dedup_candidates

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    # Scan, then pick the highest-scoring pair that INVOLVES this company. The scanner
    # already honors the auto_keep heuristic; here we only need the OTHER side as the
    # merge-away candidate.
    try:
        candidates = find_company_dedup_candidates(db, threshold=85, limit=50)
    except Exception as e:  # pragma: no cover - defensive, mirrors data-ops scan guard
        logger.warning("dup-suggestion scan failed for company {}: {}", company_id, e)
        return HTMLResponse("")

    match = None
    for pair in candidates:
        a, b = pair["company_a"], pair["company_b"]
        if a["id"] == company_id:
            match = {"id": b["id"], "name": b["name"], "score": pair["score"]}
            break
        if b["id"] == company_id:
            match = {"id": a["id"], "name": a["name"], "score": pair["score"]}
            break

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
    from ...company_utils import suggest_clean_company_name

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
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

    from ...cache.decorators import invalidate_prefix

    invalidate_prefix("company_list")
    invalidate_prefix("companies_typeahead")
    logger.info("Company {} renamed to '{}' (suggested) by {}", company_id, new_name, user.email)
    return HTMLResponse("")


_VALID_CUSTOMER_TABS = frozenset(
    {"contacts", "sites", "requisitions", "activity", "quotes", "buy_plans", "files", "history"}
)


def _get_next_account_task(db: Session, company_id: int):
    """Return the soonest open task for an account, or None."""
    from app.services.task_service import get_next_task_for_company

    return get_next_task_for_company(db, company_id)


@router.get("/v2/partials/customers/{company_id}", response_class=HTMLResponse)
async def company_detail_partial(
    request: Request,
    company_id: int,
    tab: str = Query("contacts"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return company detail as HTML partial with tabs.

    ``tab`` deep-links to the specified tab on first load (default: contacts).
    Invalid tab values silently fall back to contacts.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")  # scope detail to match the contacts list
    return await _render_company_detail(request, company_id, user, db, tab=tab)


async def _render_company_detail(
    request: Request, company_id: int, user: User, db: Session, *, tab: str = "contacts"
) -> HTMLResponse:
    """Render company detail (NO access gate).

    company_detail_partial gates with can_manage_account then calls this; create_company
    / edit_company call it directly after authorizing their own mutation (the actor just
    created/edited the account, so the post-mutation render is trusted).
    """
    active_tab = tab if tab in _VALID_CUSTOMER_TABS else "contacts"
    company = (
        db.query(Company)
        .options(joinedload(Company.account_owner), joinedload(Company.sites))
        .filter(Company.id == company_id)
        .first()
    )
    if not company:
        raise HTTPException(404, "Company not found")

    sites = [s for s in (company.sites or []) if s.is_active]

    # Count open requisitions — use company_id FK if available, fall back to name match
    from sqlalchemy import or_

    open_req_count = (
        db.query(sqlfunc.count(Requisition.id))
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            ),
            Requisition.status.in_(
                [
                    RequisitionStatus.OPEN,
                    RequisitionStatus.DRAFT,
                ]
            ),
        )
        .scalar()
        or 0
    )

    _cq = _company_quotes_query(db, company)
    quote_count = _cq.count() if _cq is not None else 0

    _bpq = _company_buy_plans_query(db, company)
    buy_plan_count = _bpq.count() if _bpq is not None else 0

    # Cadence card + commercial strip context
    from datetime import timezone as _tz

    _stats = _company_commercial_stats(db, [company.id]).get(company.id, {})
    _cadence = _cadence_state(company.tier, company.last_outbound_at)
    _nbt = _next_best_touch(company.tier, company.last_outbound_at)
    contact_rows = _company_contact_rows(db, company_id, sites=sites, viewer=user)
    segment_tags = _list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = _list_all_segment_tags(db=db)
    # Active sites (name-sorted) for the inlined Contacts site filter — same source
    # the /tab/contacts route uses, so the default surface and the tab match.
    active_sites = sorted(sites, key=lambda s: (s.site_name or "").lower())

    # Phase 3: collaborators for the header chip list
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
            "sites": sites,
            "open_req_count": open_req_count,
            "quote_count": quote_count,
            "buy_plan_count": buy_plan_count,
            # Pass the active-only sites list — contacts on deactivated sites must
            # not be shown (clicking them would log outreach against, and bump,
            # a deactivated entity).
            "contact_rows": contact_rows,
            "user": user,
            # Cadence card
            "cadence_state": _cadence,
            "next_best_touch": _nbt,
            "contact_count": sum(1 for r in contact_rows if not (r.get("contact") and r["contact"].is_archived)),
            "site_count": len(sites),
            # Inlined Contacts surface (default tab) needs the site filter + roles.
            "active_sites": active_sites,
            "roles": CANONICAL_ROLES,
            # Commercial strip
            "win_rate": _stats.get("win_rate"),
            "revenue_90d": _stats.get("revenue_90d", 0.0),
            "last_req_date": _stats.get("last_req_date"),
            # Clock day calculations
            "now_utc": datetime.now(_tz.utc),
            # Segment tags
            "segment_tags": segment_tags,
            "all_segment_tags": all_segment_tags,
            # Deep-link: which tab to activate on first render (validated above).
            "active_tab": active_tab,
            # WS2: known-field grid for the account card.
            "known_account_fields": KNOWN_ACCOUNT_FIELDS,
            # Next open task for the "Next step" summary line.
            "next_account_task": _get_next_account_task(db, company_id),
            # Phase 3: account collaborators for the header chip list
            "collaborators": collaborators,
            "all_users": all_users,
            "can_manage_team": can_manage_team,
            # Gate for the "Reactivate" button in the archived banner.
            # Computed server-side (mirrors archived_list.html pattern) so the
            # template never inspects raw role strings.
            "can_reactivate": is_manager_or_admin(user),
            # CRM P5 trust: data-completeness score for the header badge. The
            # adjacent Enrich button is the "enrich to fill" affordance.
            "account_completeness": _company_completeness(company),
        }
    )
    return template_response("htmx/partials/customers/detail.html", ctx)


@router.get("/v2/partials/customers/{company_id}/tab/{tab}", response_class=HTMLResponse)
async def company_tab(
    request: Request,
    company_id: int,
    tab: str,
    site_id: int | None = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a specific tab partial for company detail."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")  # scope detail to match the contacts list

    valid_tabs = {"sites", "contacts", "requisitions", "activity", "quotes", "buy_plans", "files", "history"}
    if tab not in valid_tabs:
        raise HTTPException(404, f"Unknown tab: {tab}")

    if tab == "sites":
        from sqlalchemy.orm import joinedload

        sites = (
            db.query(CustomerSite)
            .options(joinedload(CustomerSite.owner), joinedload(CustomerSite.site_contacts))
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .all()
        )
        users = db.query(User).order_by(User.name).all()
        ctx = _base_ctx(request, user, "customers")
        ctx["company"] = company
        ctx["sites"] = sites
        ctx["users"] = users
        return template_response("htmx/partials/customers/tabs/sites_tab.html", ctx)

    elif tab == "contacts":
        active_sites = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .order_by(CustomerSite.site_name)
            .all()
        )
        # IDOR-safe: only honor site_id when it belongs to this company's active sites.
        preselect_site_id = site_id if site_id and any(s.id == site_id for s in active_sites) else None
        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "contact_rows": _company_contact_rows(db, company_id, viewer=user),
                "now_utc": datetime.now(timezone.utc),
                "active_sites": active_sites,
                "roles": CANONICAL_ROLES,
                "preselect_site_id": preselect_site_id,
            }
        )
        return template_response("htmx/partials/customers/tabs/contacts_tab.html", ctx)

    elif tab == "requisitions":
        from sqlalchemy import or_

        reqs = (
            db.query(Requisition)
            .filter(
                or_(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .order_by(Requisition.created_at.desc().nullslast())
            .limit(50)
            .all()
        )
        rows = []
        for r in reqs:
            date_str = r.created_at.strftime("%b %d, %Y") if r.created_at else "\u2014"
            rows.append(f"""<tr class="hover:bg-brand-50 cursor-pointer"
                hx-get="/v2/partials/requisitions/{r.id}"
                hx-target="#main-content"
                hx-push-url="/v2/requisitions/{r.id}">
              <td class="px-4 py-2 text-sm font-medium text-brand-500">{html_mod.escape(r.name or "")}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{html_mod.escape(r.status or _DASH)}</td>
              <td class="px-4 py-2 text-sm text-gray-500">{date_str}</td>
            </tr>""")
        if rows:
            html = f"""<div class="overflow-x-auto">
              <table class="min-w-full divide-y divide-gray-200">
                <thead class="bg-gray-50">
                  <tr>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Name</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th class="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Created</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200">{"".join(rows)}</tbody>
              </table>
            </div>"""
        else:
            html = '<div class="p-8 text-center"><p class="text-sm text-gray-500">No requisitions for this company.</p></div>'
        return HTMLResponse(html)

    elif tab == "quotes":
        cq = _company_quotes_query(db, company)
        quotes = cq.order_by(Quote.created_at.desc().nullslast()).all() if cq is not None else []
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "quotes": quotes})
        return template_response("htmx/partials/customers/tabs/quotes_tab.html", ctx)

    elif tab == "buy_plans":
        bpq = _company_buy_plans_query(db, company)
        buy_plans = bpq.order_by(BuyPlan.created_at.desc().nullslast()).all() if bpq is not None else []
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "buy_plans": buy_plans})
        return template_response("htmx/partials/customers/tabs/buy_plans_tab.html", ctx)

    elif tab == "files":
        ctx = _base_ctx(request, user, "customers")
        ctx["company"] = company
        return template_response("htmx/partials/customers/tabs/files_tab.html", ctx)

    elif tab == "history":
        history = _field_history_for(db, _ENTITY_COMPANY, company_id)
        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "history": history,
                "field_labels": FIELD_LABELS,
                "now_utc": datetime.now(timezone.utc),
            }
        )
        return template_response("htmx/partials/customers/tabs/history_tab.html", ctx)

    else:  # activity
        from sqlalchemy import or_ as or_clause

        from ...models.intelligence import ActivityLog
        from ...models.offers import Contact as RfqContact

        # Find all requisition IDs linked to this company (via FK or name match)
        req_ids = [
            r.id
            for r in db.query(Requisition.id)
            .filter(
                or_clause(
                    Requisition.company_id == company.id,
                    sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
                )
            )
            .all()
        ]

        # RFQ contacts across company's requisitions (canonical RFQ source)
        rfq_contacts: list = []
        req_map: dict = {}
        if req_ids:
            rfq_contacts = (
                db.query(RfqContact)
                .filter(RfqContact.requisition_id.in_(req_ids))
                .order_by(RfqContact.created_at.desc())
                .limit(30)
                .all()
            )
            if rfq_contacts:
                linked_req_ids = {c.requisition_id for c in rfq_contacts}
                for r in db.query(Requisition).filter(Requisition.id.in_(linked_req_ids)).all():
                    req_map[r.id] = r

        # Direct activity logs on this company + its requisitions (newest-first).
        # Exclude rfq_sent: RfqContact rows are the canonical source; showing both
        # would double-show the same RFQ.
        _RFQ_SENT = ActivityType.RFQ_SENT
        activity_filters = [ActivityLog.company_id == company.id]
        if req_ids:
            activity_filters.append(ActivityLog.requisition_id.in_(req_ids))
        activities = (
            db.query(ActivityLog)
            .filter(or_clause(*activity_filters))
            .filter(ActivityLog.activity_type != _RFQ_SENT)
            .order_by(ActivityLog.created_at.desc())
            .limit(50)
            .all()
        )

        activities_truncated = len(activities) >= 50

        # Bucket activities into type-sections (template renders by section).
        # Emails section also carries RFQ contact items (tagged with _is_rfq=True);
        # they are merged and sorted newest-first in the template.
        _CALLS = frozenset({ActivityType.CALL_LOGGED})
        _EMAILS = frozenset({ActivityType.EMAIL_SENT, ActivityType.EMAIL_RECEIVED})
        _MEETINGS = frozenset({ActivityType.TEAMS_MESSAGE, ActivityType.WECHAT_MESSAGE, ActivityType.MEETING})
        _NOTES = frozenset({ActivityType.NOTE, ActivityType.SALES_NOTE, ActivityType.CONTACT_NOTE})

        sections: dict[str, list] = {"Calls": [], "Emails": [], "Meetings": [], "Notes": [], "Other": []}

        # Wrap RFQ contacts as tagged dicts so the template can branch on _is_rfq
        for c in rfq_contacts:
            sections["Emails"].append({"_is_rfq": True, "raw": c, "req": req_map.get(c.requisition_id)})

        for a in activities:
            at = a.activity_type
            if at in _CALLS:
                sections["Calls"].append(a)
            elif at in _EMAILS:
                sections["Emails"].append(a)
            elif at in _MEETINGS:
                sections["Meetings"].append(a)
            elif at in _NOTES:
                sections["Notes"].append(a)
            else:
                sections["Other"].append(a)

        # Sort Emails section: RFQ dicts use raw.created_at; ActivityLog uses created_at
        import datetime as _dt_mod

        _epoch = _dt_mod.datetime(1970, 1, 1, tzinfo=_dt_mod.timezone.utc)

        def _email_ts(item):
            if isinstance(item, dict):
                c = item["raw"]
                return c.created_at or _epoch
            return item.created_at or _epoch

        sections["Emails"].sort(key=_email_ts, reverse=True)

        # has_any_activity: drives empty-state vs. sections in the template
        has_any_activity = bool(activities) or any(sections.values())

        ctx = _base_ctx(request, user, "customers")
        ctx.update(
            {
                "company": company,
                "activities": activities,
                "sections": sections,
                "activities_truncated": activities_truncated,
                "req_map": req_map,
                "has_any_activity": has_any_activity,
            }
        )
        return template_response("htmx/partials/customers/tabs/activity_tab.html", ctx)


# ── Contacts-tab management (C2) ───────────────────────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/contacts/add-form",
    response_class=HTMLResponse,
)
async def contacts_tab_add_form(
    request: Request,
    company_id: int,
    site_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the shared _contact_form.html in add mode for the Contacts tab modal.

    Optional site_id pre-selects that site in the form's Site dropdown — set by the "+
    add here" affordance on a per-site section header (Contacts surface).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    # Only honor site_id when it belongs to THIS company's active sites (IDOR-safe).
    preselect_site_id = site_id if any(s.id == site_id for s in active_sites) else None
    users = db.query(User).order_by(User.name).all()
    return template_response(
        "htmx/partials/customers/tabs/_contact_form.html",
        {
            "request": request,
            "mode": "add",
            "company": company,
            "contact": None,
            "site": None,
            "sites": active_sites,
            "preselect_site_id": preselect_site_id,
            "roles": CANONICAL_ROLES,
            "users": users,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts",
    response_class=HTMLResponse,
)
async def contacts_tab_create(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a contact from the Contacts tab modal and return the grouped list.

    Site resolution order:
      1. site_id == '__new__' + new_site_name → create site first using a single
         SQLAlchemy transaction — db.flush() sends the site INSERT to PG but does
         not commit; if db.commit() fails the entire transaction (site + contact)
         rolls back atomically.
      2. site_id valid int → use that site
      3. site_id blank/missing → auto-create an 'HQ' site for zero-site companies;
         for companies with sites default to the first active HQ-typed site.

    After resolving the site, creates SiteContact with email dedup per-site.
    Duplicate email on the same site returns HTTP 409 with a user-visible error.
    Returns the grouped list HTML for swap into #contacts-tab-list.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    # Step 4: accept first_name + last_name (new form) or full_name (legacy fallback).
    first_name_val = (form.get("first_name") or "").strip() or None
    last_name_val = (form.get("last_name") or "").strip() or None
    full_name_legacy = (form.get("full_name") or "").strip()

    if first_name_val or last_name_val:
        # New form: compose full_name from parts
        if not first_name_val and not last_name_val:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        full_name = f"{first_name_val or ''} {last_name_val or ''}".strip()
    elif full_name_legacy:
        # Legacy: full_name submitted directly; split into parts
        full_name = full_name_legacy
        parts = full_name.split(" ", 1)
        first_name_val = parts[0] or None
        last_name_val = parts[1].strip() if len(parts) > 1 else None
    else:
        raise HTTPException(400, "full_name is required")

    email_val = (form.get("email") or "").strip().lower() or None
    if email_val and "@" not in email_val:
        raise HTTPException(400, "Invalid email address")

    site_id_raw = (form.get("site_id") or "").strip()
    new_site_name = (form.get("new_site_name") or "").strip()

    # ── Resolve or pre-validate the site (no writes yet) ───────────────
    # For existing sites, resolve + validate before any writes so dedup
    # can return cleanly without needing a rollback.
    existing_site: CustomerSite | None = None  # already-persisted site

    if site_id_raw == "__new__":
        if not new_site_name:
            raise HTTPException(400, "new_site_name is required when site_id=__new__")
        # Site will be created in the commit below
    elif site_id_raw:
        try:
            sid = int(site_id_raw)
        except ValueError:
            raise HTTPException(400, "Invalid site_id")
        existing_site = (
            db.query(CustomerSite).filter(CustomerSite.id == sid, CustomerSite.company_id == company_id).first()
        )
        if not existing_site:
            raise HTTPException(404, "Site not found")
    else:
        # No site_id provided — resolve default or mark for auto-create
        current_sites = (
            db.query(CustomerSite)
            .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
            .order_by(CustomerSite.site_name)
            .all()
        )
        if current_sites:
            hq_site = next((s for s in current_sites if (s.site_type or "") == "hq"), None)
            existing_site = hq_site or current_sites[0]
        # else: zero-site → will auto-create below

    # ── Per-site email dedup (only possible for existing sites) ─────────
    # For __new__ + zero-site cases the target site has no ID yet, so no dedup needed.
    if email_val and existing_site:
        dup = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == existing_site.id,
                sqlfunc.lower(SiteContact.email) == email_val,
            )
            .first()
        )
        if dup:
            # Dedup: 409 so the user knows the contact was not created
            raise HTTPException(409, f"A contact with email {email_val} already exists at this site")

    # ── Create site if needed (inside one transaction with the contact) ──
    if existing_site:
        site = existing_site
    elif site_id_raw == "__new__":
        site = CustomerSite(company_id=company_id, site_name=new_site_name, is_active=True)
        db.add(site)
        db.flush()  # get site.id before creating contact
    else:
        # Zero-site auto-create HQ
        site = CustomerSite(company_id=company_id, site_name="HQ", site_type="hq", is_active=True)
        db.add(site)
        db.flush()

    # ── Validate role ───────────────────────────────────────────────────
    role = _validate_role(form.get("contact_role") or "")
    is_priority = bool((form.get("is_priority") or "").strip())

    # SiteContact.wechat_id is String(100); SQLite (tests) ignores VARCHAR lengths
    # but Postgres 500s on over-length. Reject here, matching the legacy
    # create_site_contact guard so the canonical add path is consistent.
    wechat_id_val = (form.get("wechat_id") or "").strip()
    if len(wechat_id_val) > 100:
        raise HTTPException(400, "WeChat ID must be 100 characters or fewer.")

    # ── reports_to_id (self-FK — not in EDITABLE_CONTACT_FIELDS) ────────────
    reports_to_id_raw = (form.get("reports_to_id") or "").strip()
    reports_to_id = int(reports_to_id_raw) if reports_to_id_raw.isdigit() else None
    if reports_to_id is not None:
        mgr = (
            db.query(SiteContact)
            .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
            .filter(SiteContact.id == reports_to_id, CustomerSite.company_id == company_id)
            .first()
        )
        if not mgr:
            raise HTTPException(400, "reports_to must be a contact in the same company")

    # ── Create contact ──────────────────────────────────────────────────
    # contact_owner_id is intentionally NOT read from the form — ownership
    # flows via site → account owner (per-contact picker removed in Phase 1).
    contact = SiteContact(
        customer_site_id=site.id,
        full_name=full_name,
        first_name=first_name_val,
        last_name=last_name_val,
        email=email_val,
        title=(form.get("title") or "").strip() or None,
        phone=(form.get("phone") or "").strip() or None,
        secondary_email=(form.get("secondary_email") or "").strip() or None,
        secondary_phone=(form.get("secondary_phone") or "").strip() or None,
        wechat_id=wechat_id_val or None,
        notes=(form.get("notes") or "").strip() or None,
        linkedin_url=(form.get("linkedin_url") or "").strip() or None,
        contact_role=role,
        is_priority=is_priority,
        reports_to_id=reports_to_id,
    )
    db.add(contact)
    db.commit()
    logger.info(
        "Contact created for company {} site {} by {}",
        company_id,
        site.id,
        user.email,
    )
    return _render_contacts_list(request, user, company, db)


# ── Suggested-contacts UI (account-building loop) ──────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/suggested-contacts",
    response_class=HTMLResponse,
)
async def contacts_tab_suggested(
    request: Request,
    company_id: int,
    domain: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return suggested-contacts partial for the Contacts tab.

    Calls the enrichment waterfall and renders _suggested_contacts.html with:
    - per-row Add buttons (hx-post to /suggested-contacts/add)
    - zero results + no errors → neutral "No contacts found"
    - zero results + provider errors → amber "Couldn't reach <provider>" state
    """
    from app.enrichment_service import find_suggested_contacts_with_errors

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not domain:
        domain = company.domain or company.website or ""
    if not domain:
        raise HTTPException(400, "No domain available for this company")

    # Normalize (strip scheme/www/path)
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )

    try:
        contacts, errored = await find_suggested_contacts_with_errors(domain, company.name or "")
    except Exception as exc:
        import httpx

        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            logger.warning("find_suggested_contacts_with_errors connectivity error for company {}: {}", company_id, exc)
        else:
            logger.error(
                "find_suggested_contacts_with_errors unexpected error for company {}: {}",
                company_id,
                exc,
                exc_info=True,
            )
        contacts = []
        errored = ["all"]

    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "suggested": contacts,
            "errored_providers": errored,
            "active_sites": active_sites,
        }
    )
    return template_response("htmx/partials/customers/tabs/_suggested_contacts.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/suggested-contacts/add",
    response_class=HTMLResponse,
)
async def contacts_tab_add_suggested(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a single suggested contact to a site and return the grouped list with toast.

    Form fields: site_id (int), full_name, email, title, phone, linkedin_url,
    source (default "enrichment"), email_verified ("1" / "true" = True),
    from_enrich ("1" when posted from the header Enrich result panel).

    Dedup: if the email already exists on the site, returns a "already on file"
    toast — never a silent no-op or 409 error.
    Returns _contacts_grouped_list.html + HX-Trigger toast for the Contacts tab; when
    from_enrich=1, returns a self-contained "✓ Added" <li> fragment (the enrich panel
    lives outside the Contacts tab, so it self-swaps the clicked row via outerHTML).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    form = await request.form()
    full_name = (form.get("full_name") or "").strip()
    if not full_name:
        raise HTTPException(400, "full_name is required")

    site_id_raw = (form.get("site_id") or "").strip()
    email_val = (form.get("email") or "").strip().lower() or None
    title_val = (form.get("title") or "").strip() or None
    phone_val = (form.get("phone") or "").strip() or None
    linkedin_val = (form.get("linkedin_url") or "").strip() or None
    source_val = (form.get("source") or "").strip() or "enrichment"
    email_verified = (form.get("email_verified") or "").strip().lower() in ("1", "true", "yes")

    # Resolve site
    if site_id_raw:
        try:
            sid = int(site_id_raw)
        except ValueError:
            raise HTTPException(400, "Invalid site_id")
        site = db.query(CustomerSite).filter(CustomerSite.id == sid, CustomerSite.company_id == company_id).first()
        if not site:
            raise HTTPException(404, "Site not found")
    else:
        # Default to HQ site
        sites = (
            db.query(CustomerSite).filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True)).all()
        )
        hq = next((s for s in sites if (s.site_type or "") == "hq"), None)
        site = hq or (sites[0] if sites else None)
        if not site:
            raise HTTPException(400, "No site available — create a site first")

    # Per-site email dedup
    deduped = False
    if email_val:
        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                sqlfunc.lower(SiteContact.email) == email_val,
            )
            .first()
        )
        if existing:
            deduped = True
    elif full_name:
        existing_name = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site.id,
                SiteContact.email.is_(None),
                sqlfunc.lower(SiteContact.full_name) == full_name.lower(),
            )
            .first()
        )
        if existing_name:
            deduped = True

    if not deduped:
        sc = SiteContact(
            customer_site_id=site.id,
            full_name=full_name,
            email=email_val,
            title=title_val,
            phone=phone_val,
            linkedin_url=linkedin_val,
            enrichment_source=source_val,
            email_verified=email_verified,
        )
        db.add(sc)
        db.commit()
        logger.info(
            "add_suggested: created SiteContact for company {} site {} by {}",
            company_id,
            site.id,
            user.email,
        )
        toast_msg = f"Added {full_name}"
        toast_kind = "success"
    else:
        toast_msg = f"{full_name} is already on file"
        toast_kind = "info"

    # Enrich-panel Add: the result panel lives outside the Contacts tab (no
    # #contacts-tab-list to re-render), so when the post is flagged from_enrich return a
    # self-contained confirmation row that swaps the clicked <li> in place (hx-swap=outerHTML).
    if (form.get("from_enrich") or "") == "1":
        return HTMLResponse(
            '<li class="px-4 py-3 bg-emerald-50 text-sm text-emerald-700 flex items-center gap-2">'
            '<svg class="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" '
            'stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
            f"{html_mod.escape(toast_msg)}</li>",
            headers={"HX-Trigger": json.dumps({"showToast": {"message": toast_msg, "type": toast_kind}})},
        )

    response = _render_contacts_list(request, user, company, db)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": toast_msg, "type": toast_kind}})
    return response


# ── Sites & Site Contacts CRUD (Phase 4) ───────────────────────────────


@router.post("/v2/partials/customers/{company_id}/sites", response_class=HTMLResponse)
async def create_site(
    request: Request,
    company_id: int,
    site_name: str = Form(""),
    site_type: str = Form(""),
    city: str = Form(""),
    country: str = Form(""),
    owner_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new site for a company, return the site card partial."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if not site_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Site name is required.</div>')

    try:
        parsed_owner_id = int(owner_id) if owner_id else None
    except (ValueError, TypeError):
        parsed_owner_id = None

    site = CustomerSite(
        company_id=company_id,
        site_name=site_name.strip(),
        site_type=site_type or None,
        city=city or None,
        country=country or None,
        owner_id=parsed_owner_id,
        is_active=True,
    )
    db.add(site)
    db.commit()
    db.refresh(site)

    # Eager load owner for template
    if site.owner_id:
        _ = site.owner

    # Avoid lazy-load during render: new site has zero contacts by definition.
    site.site_contacts = []

    ctx = _base_ctx(request, user, "customers")
    ctx["company"] = company
    ctx["s"] = site
    return template_response("htmx/partials/customers/tabs/site_card.html", ctx)


@router.delete("/v2/partials/customers/{company_id}/sites/{site_id}", response_class=HTMLResponse)
async def delete_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Soft-delete a site (set is_active=False).

    Gate: can_manage_account — mirrors edit_site.  Any authenticated user can hit
    this route, so we must check ownership before mutating.
    """
    from ...dependencies import can_manage_account

    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    site.is_active = False
    db.commit()
    return HTMLResponse("")


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/mark-dnc",
    response_class=HTMLResponse,
)
async def mark_site_dnc(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle do_not_contact on a CustomerSite.

    Gate: can_manage_account — account owner, collaborator, or manager/admin.
    A DNC site is excluded from call-list surfaces but NOT deleted.
    Returns an updated site_card partial.

    Called by: "Mark DNC" / "Clear DNC" toggle in site_card.html.
    """
    from ...dependencies import can_manage_account

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")
    site = db.get(CustomerSite, site_id)
    if not site or site.company_id != company_id:
        raise HTTPException(404, "Site not found")
    site.do_not_contact = not site.do_not_contact
    db.commit()
    db.refresh(site)
    logger.info(
        "Site {} do_not_contact={} by {}",
        site_id,
        site.do_not_contact,
        user.email,
    )
    return template_response(
        "htmx/partials/customers/tabs/site_card.html",
        {"request": request, "s": site, "company": company, "user": user},
    )


@router.get(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def site_contacts_list(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Redirect site-scoped contact list to the canonical grouped contacts surface.

    site_contacts.html has been retired — all contact management now lives on the
    canonical #contacts-tab-list surface (contacts_tab.html /
    _contacts_grouped_list.html).
    """
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts",
    response_class=HTMLResponse,
)
async def create_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    full_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    phone: str = Form(""),
    wechat_id: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a site contact and return refreshed contacts list."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if not full_name.strip():
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">Name is required.</div>')

    # SiteContact.wechat_id is String(100) — reject over-length input here (the
    # in-memory SQLite test engine ignores VARCHAR lengths, but Postgres 500s).
    if len(wechat_id.strip()) > 100:
        return HTMLResponse('<div class="p-2 text-xs text-rose-600">WeChat ID must be 100 characters or fewer.</div>')

    # Dedup by email
    if email:
        from sqlalchemy import func

        existing = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == site_id,
                func.lower(SiteContact.email) == email.strip().lower(),
            )
            .first()
        )
        if existing:
            # Deprecated route: dedup silently returns the list; legacy path kept for
            # backwards-compat only (contacts_tab_create is the canonical add endpoint).
            logger.info(
                "Dedup [legacy create_site_contact]: email {} already exists at site {} (company {})",
                email,
                site_id,
                company_id,
            )
        else:
            contact = SiteContact(
                customer_site_id=site_id,
                full_name=full_name.strip(),
                email=email.strip() or None,
                title=title.strip() or None,
                phone=phone.strip() or None,
                wechat_id=wechat_id.strip() or None,
            )
            db.add(contact)
            db.commit()
    else:
        contact = SiteContact(
            customer_site_id=site_id,
            full_name=full_name.strip(),
            title=title.strip() or None,
            phone=phone.strip() or None,
            wechat_id=wechat_id.strip() or None,
        )
        db.add(contact)
        db.commit()

    # Return canonical grouped contacts list (site_contacts.html is retired).
    company = db.query(Company).filter(Company.id == company_id).first()
    return _render_contacts_list(request, user, company, db)


@router.delete(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}",
    response_class=HTMLResponse,
)
async def delete_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a site contact and re-render the Contacts tab grouped list.

    The Sites tab no longer carries a contact editor — the only render target is
    #contacts-tab-list (the canonical Contacts tab surface).
    """
    # Validate site belongs to company BEFORE mutating — closes IDOR gap and
    # guarantees company is available for the contacts-tab-list render path.
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    db.delete(contact)
    db.commit()
    logger.info("Contact {} deleted by {}", contact_id, user.email)

    return _render_contacts_list(request, user, company, db)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/primary",
    response_class=HTMLResponse,
)
async def set_primary_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Set a contact as primary for the site (unsets others)."""
    # Validate the site belongs to the company BEFORE mutating — a mismatched
    # URL must not flip the primary flag and then 500 on render.
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized")

    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Unset all other primary contacts on this site
    db.query(SiteContact).filter(
        SiteContact.customer_site_id == site_id,
        SiteContact.is_primary.is_(True),
        SiteContact.id != contact_id,
    ).update({"is_primary": False})
    contact.is_primary = True
    db.commit()

    # The Sites tab no longer carries a contact editor — always render the
    # canonical Contacts tab surface (#contacts-tab-list).
    return _render_contacts_list(request, user, company, db)


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
    company.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(company)
    logger.info("Company {} primary contact set to {} by {}", company_id, contact_id, user.email)

    return await _render_company_detail(request, company_id, user, db)


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
    company.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(company)
    logger.info("Company {} parent set to {} by {}", company_id, raw or "None", user.email)

    return await _render_company_detail(request, company_id, user, db)


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
    users = (
        db.query(User).filter(User.role.in_((UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN))).all()
    )
    all_companies = (
        db.query(Company).filter(Company.id != company_id, Company.is_active.is_(True)).order_by(Company.name).all()
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
        company.name = name
    notes = form.get("notes", "").strip()
    company.notes = notes or company.notes
    source = form.get("source", "").strip()
    company.source = source or company.source
    tax_id = form.get("tax_id", "").strip()
    company.tax_id = tax_id or None
    # Owner reassignment is a TEAM action — only the primary owner / a manager may seize
    # primary ownership (can_manage_account admits collaborators + site-owners, who must
    # NOT be able to lock out the real owner). Gate only when the value actually changes.
    owner_id = form.get("owner_id", "")
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

    parent_company_id_raw = form.get("parent_company_id", "").strip()
    # Parent-company (hierarchy) edits are also a team action — match set_parent_company,
    # which gates on owner/manager — so a collaborator can't restructure the hierarchy.
    if parent_company_id_raw != (str(company.parent_company_id or "")):
        if not can_manage_account_team(user, company):
            raise HTTPException(403, "Only the account owner or a manager can change company hierarchy")
    _set_parent_company(db, company, parent_company_id_raw)

    # Registry fields — DRY via apply_company_field.
    # notes/source use blank-sentinel "preserve current value" semantics above; tax_id is
    # explicitly clear-on-blank (a submitted blank sets it to NULL). All three are handled
    # above, so skip them here to keep that behaviour — the registry loop must not re-touch
    # notes/source (its clear-on-blank would wipe a value the user left untouched).
    _form_handled = {"notes", "source", "tax_id"}
    for f in EDITABLE_ACCOUNT_FIELDS:
        if f in _form_handled:
            continue
        raw = form.get(f)
        if raw is not None:  # field was submitted
            apply_company_field(company, f, raw)
    # updated_at set inside apply_company_field; ensure it's set for non-registry writes too
    company.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Company {} edited by {}", company_id, user.email)

    return await _render_company_detail(request, company_id, user, db)


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
    _record_field_change(
        db,
        entity_type=_ENTITY_COMPANY,
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


# ── Inline Field Edit — Contact (WS1) ──────────────────────────────────────


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/field/edit/{field}",
    response_class=HTMLResponse,
)
async def contact_field_edit_form(
    request: Request,
    company_id: int,
    contact_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline edit widget for a single contact field."""
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    meta = EDITABLE_CONTACT_FIELDS[field]
    extra: dict = {}
    return template_response(
        "htmx/partials/customers/_field_edit.html",
        {
            **_base_ctx(request, user),
            "obj": contact,
            "field": field,
            "entity": "contact",
            "meta": meta,
            "post_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field",
            "display_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field/display/{field}",
            **extra,
        },
    )


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/field/display/{field}",
    response_class=HTMLResponse,
)
async def contact_field_display(
    request: Request,
    company_id: int,
    contact_id: int,
    field: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the display span for a single contact field (cancel path)."""
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    meta = EDITABLE_CONTACT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": contact,
            "field": field,
            "entity": "contact",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field/edit/{field}",
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/field",
    response_class=HTMLResponse,
)
async def contact_field_post(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save a single inline-edited contact field; return the display span.

    IDOR-safe: the contact must belong to a site under {company_id}. Owner-or-admin only.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")
    form = await request.form()
    field = (form.get("field") or "").strip()
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    value = form.get("value") or ""
    old_value = getattr(contact, field, None)
    apply_contact_field(contact, field, value, contact.customer_site_id, db)
    _record_field_change(
        db,
        entity_type=_ENTITY_CONTACT,
        entity_id=contact.id,
        field_name=field,
        old_value=old_value,
        new_value=getattr(contact, field, None),
        user_id=user.id,
    )
    db.commit()
    logger.info("Contact {} field {} edited inline by {}", contact_id, field, user.email)
    meta = EDITABLE_CONTACT_FIELDS[field]
    return template_response(
        "htmx/partials/customers/_field_display.html",
        {
            **_base_ctx(request, user),
            "obj": contact,
            "field": field,
            "entity": "contact",
            "meta": meta,
            "edit_url": f"/v2/partials/customers/{company_id}/contacts/{contact_id}/field/edit/{field}",
        },
    )


# ── Custom Fields — Account + Contact (WS3) ────────────────────────────────


def _render_custom_fields(request: Request, entity: str, obj, company_id: int):
    """Render the _custom_fields.html partial for a company or contact."""
    return template_response(
        "htmx/partials/customers/_custom_fields.html",
        {"request": request, "entity": entity, "obj": obj, "company_id": company_id},
    )


@router.post("/v2/partials/customers/{company_id}/custom-fields", response_class=HTMLResponse)
async def company_add_custom_field(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or overwrite a label:value pair in company.custom_fields."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")
    form = await request.form()
    label = (form.get("label") or "").strip()
    value = (form.get("value") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    existing = company.custom_fields or {}
    updated = {**existing, label: value}
    try:
        company.custom_fields = updated
    except ValueError as e:
        raise HTTPException(400, str(e))
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(company, "custom_fields")
    db.commit()
    db.refresh(company)
    logger.info("Company {} custom field '{}' set by {}", company_id, label, user.email)
    return _render_custom_fields(request, "company", company, company_id)


@router.delete(
    "/v2/partials/customers/{company_id}/custom-fields/{label:path}",
    response_class=HTMLResponse,
)
async def company_delete_custom_field(
    request: Request,
    company_id: int,
    label: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a label from company.custom_fields."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this account")
    existing = dict(company.custom_fields or {})
    existing.pop(label, None)
    company.custom_fields = existing
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(company, "custom_fields")
    db.commit()
    db.refresh(company)
    logger.info("Company {} custom field '{}' removed by {}", company_id, label, user.email)
    return _render_custom_fields(request, "company", company, company_id)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/custom-fields",
    response_class=HTMLResponse,
)
async def contact_add_custom_field(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add or overwrite a label:value pair in contact.custom_fields.

    IDOR-safe: verifies the contact belongs to a site under the path company.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")
    form = await request.form()
    label = (form.get("label") or "").strip()
    value = (form.get("value") or "").strip()
    if not label:
        raise HTTPException(400, "label is required")
    existing = contact.custom_fields or {}
    updated = {**existing, label: value}
    try:
        contact.custom_fields = updated
    except ValueError as e:
        raise HTTPException(400, str(e))
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(contact, "custom_fields")
    db.commit()
    db.refresh(contact)
    logger.info("Contact {} custom field '{}' set by {}", contact_id, label, user.email)
    return _render_custom_fields(request, "contact", contact, company_id)


@router.delete(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/custom-fields/{label:path}",
    response_class=HTMLResponse,
)
async def contact_delete_custom_field(
    request: Request,
    company_id: int,
    contact_id: int,
    label: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a label from contact.custom_fields.

    IDOR-safe: verifies the contact belongs to a site under the path company.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can edit this contact")
    existing = dict(contact.custom_fields or {})
    existing.pop(label, None)
    contact.custom_fields = existing
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(contact, "custom_fields")
    db.commit()
    db.refresh(contact)
    logger.info("Contact {} custom field '{}' removed by {}", contact_id, label, user.email)
    return _render_custom_fields(request, "contact", contact, company_id)


# ── Merge Duplicate ─────────────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/merge-preview", response_class=HTMLResponse)
async def company_merge_preview(
    request: Request,
    company_id: int,
    remove_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a preview of what will happen when remove_id is merged into company_id.

    Shows counts: sites, contacts, activities that will be reassigned; confirms the
    loser will be deleted. Used by the merge-confirm modal before the user commits.
    """
    from ...models.intelligence import ActivityLog as _AL

    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, keep, db):
        raise HTTPException(403, "Not authorized to manage this account")

    remove = db.query(Company).filter(Company.id == remove_id).first()
    if not remove:
        raise HTTPException(400, "Duplicate company not found")
    if not can_manage_account(user, remove, db):
        raise HTTPException(403, "Not authorized to manage the duplicate company")

    if keep.id == remove.id:
        raise HTTPException(400, "Cannot merge a company with itself")

    # Count what will move
    site_count = db.query(sqlfunc.count(CustomerSite.id)).filter(CustomerSite.company_id == remove.id).scalar() or 0
    contact_count = (
        db.query(sqlfunc.count(SiteContact.id))
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(CustomerSite.company_id == remove.id)
        .scalar()
        or 0
    )
    activity_count = db.query(sqlfunc.count(_AL.id)).filter(_AL.company_id == remove.id).scalar() or 0
    req_count = db.query(sqlfunc.count(Requisition.id)).filter(Requisition.company_id == remove.id).scalar() or 0

    ctx = {
        "request": request,
        "keep": keep,
        "remove": remove,
        "site_count": site_count,
        "contact_count": contact_count,
        "activity_count": activity_count,
        "req_count": req_count,
    }
    return template_response("htmx/partials/customers/_merge_preview.html", ctx)


@router.post("/v2/partials/customers/{company_id}/merge", response_class=HTMLResponse)
async def company_merge(
    request: Request,
    company_id: int,
    remove_id: int = Form(...),
    confirmed: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge remove_id into company_id (the keeper).

    Requires confirmed="true" to prevent accidental submissions. Calls the
    canonical merge_companies() engine — no FK logic lives here.
    POST is mandatory: this is a destructive, irreversible operation.
    """
    from ...services.company_merge_service import merge_companies as _merge

    if confirmed.lower() != "true":
        raise HTTPException(400, "Merge requires explicit confirmation (confirmed=true)")

    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, keep, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if remove_id == company_id:
        raise HTTPException(400, "Cannot merge a company with itself")

    remove = db.query(Company).filter(Company.id == remove_id).first()
    if not remove:
        raise HTTPException(400, "Duplicate company not found")
    if not can_manage_account(user, remove, db):
        raise HTTPException(403, "Not authorized to manage the duplicate company")

    try:
        result = _merge(company_id, remove_id, db)
        db.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "Manual company merge: kept {} ({}), removed {} by {}",
        company_id,
        keep.name,
        remove_id,
        user.email,
    )

    # Redirect browser to keeper's detail page via HTMX redirect header
    safe_name = html_mod.escape(keep.name or "")
    response = HTMLResponse(
        f'<p class="text-sm text-emerald-600 py-2">Merged into <strong>{safe_name}</strong>. '
        f"{int(result.get('sites_moved', 0))} site(s) and {int(result.get('reassigned', 0))} record(s) reassigned.</p>",
        status_code=200,
    )
    response.headers["HX-Redirect"] = f"/v2/partials/customers/{company_id}"
    return response


@router.get("/v2/partials/customers/{company_id}/merge-form", response_class=HTMLResponse)
async def company_merge_form(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the merge-duplicate modal form for a company.

    Renders a search input to find the duplicate and a submit button that triggers the
    merge-preview step.
    """
    keep = db.query(Company).filter(Company.id == company_id).first()
    if not keep:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, keep, db):
        raise HTTPException(403, "Not authorized to manage this account")

    ctx = {"request": request, "keep": keep}
    return template_response("htmx/partials/customers/_merge_form.html", ctx)


# ── Contact Merge Duplicate ──────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/contacts/search", response_class=HTMLResponse)
async def contact_search_typeahead(
    request: Request,
    company_id: int,
    q: str = "",
    exclude_id: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return contacts for a company as clickable typeahead results.

    Used by the contact merge form to pick the "loser" contact. Excludes the keeper
    (exclude_id=) so a contact cannot be merged with itself.
    """
    if not q.strip() or len(q.strip()) < 2:
        return HTMLResponse("")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company or not can_manage_account(user, company, db):
        return HTMLResponse("")

    contacts = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.id != exclude_id,
            SiteContact.full_name.ilike(f"%{escape_like(q.strip())}%", escape="\\"),
        )
        .order_by(SiteContact.full_name)
        .limit(10)
        .all()
    )
    rows = [
        f'<button type="button" data-contact-id="{c.id}" '
        f'class="w-full text-left px-3 py-2 text-sm text-gray-700 hover:bg-gray-50">'
        f"{html_mod.escape(c.full_name or '')}"
        f"{'  (' + html_mod.escape(c.email) + ')' if c.email else ''}"
        f"</button>"
        for c in contacts
    ]
    return HTMLResponse("\n".join(rows))


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/merge-form",
    response_class=HTMLResponse,
)
async def contact_merge_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the merge-duplicate modal form for a contact."""
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    return template_response(
        "htmx/partials/customers/_contact_merge_form.html",
        {"request": request, "keep": keep, "company": company},
    )


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/merge-preview",
    response_class=HTMLResponse,
)
async def contact_merge_preview(
    request: Request,
    company_id: int,
    contact_id: int,
    remove_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return a preview of what will happen when remove_id is merged into contact_id."""
    from ...models.intelligence import ActivityLog as _AL
    from ...models.task import RequisitionTask as _RT

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    remove = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == remove_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not remove:
        raise HTTPException(400, "Duplicate contact not found or not in this company")

    if keep.id == remove.id:
        raise HTTPException(400, "Cannot merge a contact with itself")

    activity_count = db.query(sqlfunc.count(_AL.id)).filter(_AL.site_contact_id == remove.id).scalar() or 0
    task_count = db.query(sqlfunc.count(_RT.id)).filter(_RT.site_contact_id == remove.id).scalar() or 0
    from ...models.crm import SiteContactAttachment as _SCA

    attachment_count = db.query(sqlfunc.count(_SCA.id)).filter(_SCA.site_contact_id == remove.id).scalar() or 0

    return template_response(
        "htmx/partials/customers/_contact_merge_preview.html",
        {
            "request": request,
            "keep": keep,
            "remove": remove,
            "company": company,
            "activity_count": activity_count,
            "task_count": task_count,
            "attachment_count": attachment_count,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/merge",
    response_class=HTMLResponse,
)
async def contact_merge(
    request: Request,
    company_id: int,
    contact_id: int,
    remove_id: int = Form(...),
    confirmed: str = Form(default=""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Merge remove_id into contact_id (the keeper).

    Requires confirmed="true". Calls merge_contacts() — no FK logic here.
    """
    from ...services.contact_merge_service import merge_contacts as _merge

    if confirmed.lower() != "true":
        raise HTTPException(400, "Merge requires explicit confirmation (confirmed=true)")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can merge contacts")

    keep = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not keep:
        raise HTTPException(404, "Contact not found")

    if remove_id == contact_id:
        raise HTTPException(400, "Cannot merge a contact with itself")

    remove = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == remove_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not remove:
        raise HTTPException(400, "Duplicate contact not found or not in this company")

    try:
        result = _merge(contact_id, remove_id, db)
        db.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info(
        "Manual contact merge: kept {} ({}), removed {} by {}",
        contact_id,
        keep.full_name,
        remove_id,
        user.email,
    )

    safe_name = html_mod.escape(keep.full_name or "")
    response = HTMLResponse(
        f'<p class="text-sm text-emerald-600 py-2">Merged into <strong>{safe_name}</strong>. '
        f"{int(result.get('reassigned', 0))} record(s) reassigned.</p>",
        status_code=200,
    )
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": "Contact merged successfully", "type": "success"}}
    )
    return response


# ── Contact Move ─────────────────────────────────────────────────────────────


@router.get("/v2/partials/customers/{company_id}/sites-options")
async def company_sites_options(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return JSON list of active sites for a company for the move-contact site picker.

    Used by Alpine.js in _contact_move_form.html to populate the site select on
    company change. Returns [{"id": N, "name": "..."}].
    """
    from fastapi.responses import JSONResponse

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        return JSONResponse([])

    if not can_manage_account(user, company, db):
        return JSONResponse([])

    sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    return JSONResponse([{"id": s.id, "name": s.site_name or f"Site {s.id}"} for s in sites])


@router.get("/v2/partials/customers/{company_id}/contacts/{contact_id}/move-form", response_class=HTMLResponse)
async def contact_move_form(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the move-contact modal form.

    Lists all companies the user can manage so they can pick a target.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can move this contact")

    # Build list of companies the user can manage (for the target picker)
    if is_manager_or_admin(user):
        manageable = db.query(Company).filter(Company.is_active.is_(True)).order_by(Company.name).all()
    else:
        # owned companies + collaborator companies
        owned = db.query(Company).filter(Company.is_active.is_(True), Company.account_owner_id == user.id).all()
        collab_ids = [
            row[0]
            for row in db.query(AccountCollaborator.company_id).filter(AccountCollaborator.user_id == user.id).all()
        ]
        if collab_ids:
            collab_cos = db.query(Company).filter(Company.id.in_(collab_ids)).all()
        else:
            collab_cos = []
        seen = {c.id for c in owned}
        manageable = list(owned)
        for co in collab_cos:
            if co.id not in seen:
                manageable.append(co)
                seen.add(co.id)
        manageable.sort(key=lambda c: c.name or "")

    return template_response(
        "htmx/partials/customers/_contact_move_form.html",
        {
            "request": request,
            "contact": contact,
            "company": company,
            "companies": manageable,
        },
    )


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/move",
    response_class=HTMLResponse,
)
async def contact_move(
    request: Request,
    company_id: int,
    contact_id: int,
    target_site_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Move contact_id to target_site_id.

    Validates: source company accessible, target site exists + is active,
    target company accessible by the same user. Re-renders contacts-tab-list
    for the SOURCE company (contact is gone from here now).
    """
    # Source authz
    source_company = db.query(Company).filter(Company.id == company_id).first()
    if not source_company:
        raise HTTPException(404, "Company not found")

    if not can_manage_account(user, source_company, db):
        raise HTTPException(403, "Only the owner or an admin can move this contact")

    contact = (
        db.query(SiteContact)
        .join(CustomerSite)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")

    # Target site validation
    target_site = db.query(CustomerSite).filter(CustomerSite.id == target_site_id).first()
    if not target_site:
        raise HTTPException(400, "Target site not found")
    if not target_site.is_active:
        raise HTTPException(400, "Target site is inactive")

    # Target authz
    target_company = db.query(Company).filter(Company.id == target_site.company_id).first()
    if not target_company:
        raise HTTPException(400, "Target company not found")

    if not can_manage_account(user, target_company, db):
        raise HTTPException(403, "You do not have access to the target company")

    # Email collision guard: (customer_site_id, email) unique constraint
    if contact.email:
        collision = (
            db.query(SiteContact)
            .filter(
                SiteContact.customer_site_id == target_site_id,
                SiteContact.email == contact.email,
            )
            .first()
        )
        if collision:
            raise HTTPException(400, "A contact with this email already exists at the target site")

    # Execute move
    old_site_id = contact.customer_site_id
    contact.customer_site_id = target_site_id
    db.commit()

    logger.info(
        "Contact move: contact {} ({}) moved from site {} → site {} by {}",
        contact_id,
        contact.full_name,
        old_site_id,
        target_site_id,
        user.email,
    )

    return _render_contacts_list(request, user, source_company, db)


@router.get("/v2/partials/customers/{company_id}/sites/{site_id}/edit-form", response_class=HTMLResponse)
async def site_edit_form(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return modal edit form for a customer site."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    users = db.query(User).order_by(User.name).all()
    return template_response(
        "htmx/partials/customers/tabs/site_edit_modal.html",
        {"request": request, "site": site, "company_id": company_id, "users": users},
    )


@router.post("/v2/partials/customers/{company_id}/sites/{site_id}/edit", response_class=HTMLResponse)
async def edit_site(
    request: Request,
    company_id: int,
    site_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update site fields and return refreshed sites tab."""
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    form = await request.form()
    site_name = form.get("site_name", "").strip()
    if not site_name:
        raise HTTPException(400, "site_name is required")
    site.site_name = site_name
    site.address_line1 = form.get("address_line1", "").strip() or None
    site.address_line2 = form.get("address_line2", "").strip() or None
    site.city = form.get("city", "").strip() or None
    site.state = form.get("state", "").strip() or None
    site.zip = form.get("zip", "").strip() or None
    site.country = form.get("country", "").strip() or None
    site.site_type = form.get("site_type", "").strip() or None
    site.payment_terms = form.get("payment_terms", "").strip() or None
    site.shipping_terms = form.get("shipping_terms", "").strip() or None
    site.notes = form.get("notes", "").strip() or None
    owner_id = form.get("owner_id", "")
    if owner_id and str(owner_id).isdigit():
        site.owner_id = int(owner_id)
    site.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Site {} edited by {}", site_id, user.email)

    return await company_tab(request=request, company_id=company_id, tab="sites", user=user, db=db)


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/edit-form",
    response_class=HTMLResponse,
)
async def contact_edit_form_company_scoped(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the shared _contact_form.html in edit mode for the Contacts tab.

    This company-scoped route (no site_id in path) is called from the Contacts-tab kebab
    Edit button. It returns _contact_form.html in 'edit' mode so the form posts to
    /contacts/{contact_id}/edit and targets #contacts-tab-list — the canonical swap
    target for the Contacts tab. The former site-scoped edit-form route and
    contact_edit_modal.html have been retired.
    """
    # Validate the contact belongs to a site that belongs to this company
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    # Same-company contacts for reports_to select, excluding self
    site_contacts_for_select = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(
            CustomerSite.company_id == company_id,
            SiteContact.is_active.is_(True),
            SiteContact.id != contact_id,
        )
        .order_by(SiteContact.full_name)
        .all()
    )
    return template_response(
        "htmx/partials/customers/tabs/_contact_form.html",
        {
            "request": request,
            "mode": "edit",
            "company": company,
            "contact": contact,
            "site": site,
            "sites": [],
            "roles": CANONICAL_ROLES,
            "site_contacts_for_select": site_contacts_for_select,
        },
    )


@router.get("/v2/partials/contacts/{contact_id}/files-modal", response_class=HTMLResponse)
async def contact_files_modal(
    request: Request,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global-modal body hosting the shared attachments panel for a contact.

    Loaded by the contact-card kebab "Files" action via $dispatch('open-modal').
    Access mirrors the contact attachment endpoints: contact → site → company.
    """
    contact = db.get(SiteContact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.get(CustomerSite, contact.customer_site_id)
    if not site or not db.get(Company, site.company_id):
        raise HTTPException(404, "Contact not found")
    return template_response(
        "htmx/partials/customers/_contact_files_modal.html",
        {"request": request, "contact": contact},
    )


def _contact_under_company(db: Session, company_id: int, contact_id: int) -> SiteContact:
    """Load a SiteContact and verify it belongs to *company_id* (via its site).

    Raises HTTPException(404) if the contact does not exist or is not under that company
    — the contact-notes-modal endpoints share this lookup.
    """
    contact = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(SiteContact.id == contact_id, CustomerSite.company_id == company_id)
        .first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    return contact


def _render_contact_notes_modal(
    request: Request,
    company: Company,
    contact: SiteContact,
    db: Session,
    can_manage: bool,
    error: str | None = None,
) -> HTMLResponse:
    """Render the contact-notes modal body (feed + add form).

    Shared by GET + POST.
    """
    from ...services.activity_service import get_site_contact_notes

    notes = get_site_contact_notes(contact.id, db)
    return template_response(
        "htmx/partials/customers/_contact_notes_modal.html",
        {
            "request": request,
            "company": company,
            "contact": contact,
            "notes": notes,
            "can_manage": can_manage,
            "error": error,
        },
    )


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/notes-modal",
    response_class=HTMLResponse,
)
async def contact_notes_modal(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global-modal body with a contact's note feed + add form.

    Loaded by the contact-card drawer "See all notes" / "+ Add note" action via
    $dispatch('open-modal'). 404 if the contact is not under this company.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    contact = _contact_under_company(db, company_id, contact_id)
    return _render_contact_notes_modal(request, company, contact, db, can_manage=can_manage_account(user, company, db))


@router.get(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/history-modal",
    response_class=HTMLResponse,
)
async def contact_history_modal(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the global-modal body with a contact's field-change history.

    Loaded by the contact-card kebab "History" action via $dispatch('open-modal'). 404
    if the contact is not under this company (IDOR guard via the shared lookup).
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    contact = _contact_under_company(db, company_id, contact_id)
    history = _field_history_for(db, _ENTITY_CONTACT, contact.id)
    ctx = _base_ctx(request, user)
    ctx.update(
        {
            "company": company,
            "contact": contact,
            "history": history,
            "field_labels": FIELD_LABELS,
            "now_utc": datetime.now(timezone.utc),
        }
    )
    return template_response("htmx/partials/customers/_contact_history_modal.html", ctx)


@router.post(
    "/v2/partials/customers/{company_id}/contacts/{contact_id}/notes",
    response_class=HTMLResponse,
)
async def add_contact_note(
    request: Request,
    company_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a contact, then re-render the notes-modal body.

    can_manage_account gate (403 otherwise). Blank note → inline error (no write).
    """
    from ...services.activity_service import log_site_contact_note

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if not can_manage_account(user, company, db):
        raise HTTPException(403, "Only the owner or an admin can add notes for this contact")
    contact = _contact_under_company(db, company_id, contact_id)

    form = await request.form()
    notes_text = (form.get("notes") or "").strip()
    if not notes_text:
        return _render_contact_notes_modal(
            request, company, contact, db, can_manage=True, error="Note cannot be empty."
        )

    log_site_contact_note(
        user_id=user.id,
        site_contact_id=contact.id,
        customer_site_id=contact.customer_site_id,
        company_id=company_id,
        notes=notes_text,
        db=db,
    )
    db.commit()
    logger.info("Note added to contact {} by {} (company {})", contact_id, user.email, company_id)
    return _render_contact_notes_modal(request, company, contact, db, can_manage=True)


@router.post(
    "/v2/partials/customers/{company_id}/sites/{site_id}/contacts/{contact_id}/edit",
    response_class=HTMLResponse,
)
async def edit_site_contact(
    request: Request,
    company_id: int,
    site_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update editable contact fields and return refreshed Contacts tab grouped list.

    Writes contact_role (validated via _validate_role; blank→NULL, unknown→400),
    is_priority, and linkedin_url. Always renders #contacts-tab-list — the Sites tab no
    longer carries a contact editor.
    """
    contact = (
        db.query(SiteContact).filter(SiteContact.id == contact_id, SiteContact.customer_site_id == site_id).first()
    )
    if not contact:
        raise HTTPException(404, "Contact not found")
    site = db.query(CustomerSite).filter(CustomerSite.id == site_id, CustomerSite.company_id == company_id).first()
    if not site:
        raise HTTPException(404, "Site not found")
    company = db.get(Company, company_id)
    if company is None or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    form = await request.form()

    # Name fields — apply atomically so the "at least one required" check
    # sees both values together rather than one-at-a-time.
    first_name_raw = form.get("first_name")
    last_name_raw = form.get("last_name")
    if first_name_raw is not None or last_name_raw is not None:
        new_first = (first_name_raw or "").strip() or None
        new_last = (last_name_raw or "").strip() or None
        if not new_first and not new_last:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        contact.first_name = new_first
        contact.last_name = new_last
        _recompose_full_name(contact)

    # Remaining registry fields (skip first_name/last_name — handled above)
    for f in EDITABLE_CONTACT_FIELDS:
        if f in ("first_name", "last_name"):
            continue
        raw = form.get(f)
        if raw is not None:  # field was submitted
            apply_contact_field(contact, f, raw, site_id, db)

    # Non-registry fields
    contact.notes = (form.get("notes", "") or "").strip() or None
    contact.is_priority = bool((form.get("is_priority", "") or "").strip())
    # reports_to_id — self-FK, not in EDITABLE_CONTACT_FIELDS
    reports_to_id_raw = form.get("reports_to_id")
    if reports_to_id_raw is not None:
        v = reports_to_id_raw.strip()
        new_reports_to_id = int(v) if v.isdigit() else None
        if new_reports_to_id is not None:
            if new_reports_to_id == contact_id:
                raise HTTPException(400, "reports_to must be a contact in the same company")
            mgr = (
                db.query(SiteContact)
                .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
                .filter(SiteContact.id == new_reports_to_id, CustomerSite.company_id == company_id)
                .first()
            )
            if not mgr:
                raise HTTPException(400, "reports_to must be a contact in the same company")
        contact.reports_to_id = new_reports_to_id
    contact.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Contact {} edited by {}", contact_id, user.email)

    return _render_contacts_list(request, user, company, db)
