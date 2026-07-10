"""routers/htmx/companies/detail.py — the company detail shell + tab render path (P4.3
split).

``company_detail_partial`` (gated) → ``_render_company_detail`` (the actual assembly,
also called directly by ``.core`` after a mutation that already authorized itself) →
``detail.html``. ``company_tab`` is registered here on the shared router by wrapping
the implementation shared with ``vendor_tab`` / ``requisition_tab`` in
``app.routers.htmx._shared_tabs`` (unchanged since P4.1) — this module just re-applies
the ``@router.get(...)`` decorator so the route/URL/tag/importable name stay exactly
where they always were.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration),
    .core (``_render_company_detail``, ``company_detail_partial`` — the latter resolved
    dynamically off the package attribute by ``.core.send_company_to_prospecting_htmx``
    so a monkeypatch on it still takes effect), app.routers.htmx._shared_tabs (lazily
    imports ``CANONICAL_ROLES`` / ``FIELD_LABELS`` / ``_company_quotes_query`` /
    ``_company_buy_plans_query`` off the package to avoid a load-time cycle)
Depends on: app.models, app.dependencies, app.services.crm_service,
    app.services.crm_completeness, app.services.task_service, app.services.tagging,
    ._registries, .._shared, .._shared_tabs
"""

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ....constants import RequisitionStatus
from ....database import get_db
from ....dependencies import can_manage_account, can_manage_account_team, is_manager_or_admin, require_user
from ....models import AccountCollaborator, BuyPlan, Company, CustomerSite, Quote, Requisition, User
from ....services.crm_completeness import company_completeness
from ....services.crm_service import cadence_state, company_commercial_stats, company_contact_rows, next_best_touch
from ....services.tagging import list_all_segment_tags, list_company_segment_tags
from ....services.task_service import get_next_task_for_company
from ....template_env import template_response
from .._shared import _base_ctx
from .._shared_tabs import company_tab as _company_tab_impl
from . import router
from ._registries import CANONICAL_ROLES, KNOWN_ACCOUNT_FIELDS

_VALID_CUSTOMER_TABS = frozenset(
    {"contacts", "sites", "requisitions", "activity", "quotes", "buy_plans", "files", "history"}
)


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


def _get_next_account_task(db: Session, company_id: int):
    """Return the soonest open task for an account, or None."""
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

    _stats = company_commercial_stats(db, [company.id]).get(company.id, {})
    _cadence = cadence_state(company.tier, company.last_outbound_at)
    _nbt = next_best_touch(company.tier, company.last_outbound_at)
    contact_rows = company_contact_rows(db, company_id, sites=sites, viewer=user)
    segment_tags = list_company_segment_tags(company_id=company_id, db=db)
    all_segment_tags = list_all_segment_tags(db=db)
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
            "now_utc": datetime.now(UTC),
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
            "account_completeness": company_completeness(company),
        }
    )
    return template_response("htmx/partials/customers/detail.html", ctx)


# Implementation lives in ._shared_tabs (P4.1 — archive.py reused this tab render by
# importing it straight off this sibling router module; it's now a shared home both
# import from). Registered here, unchanged, so the route/URL/tag and the `company_tab`
# name importable off this module (and re-exported off the package) are exactly as before.
company_tab = router.get("/v2/partials/customers/{company_id}/tab/{tab}", response_class=HTMLResponse)(
    _company_tab_impl
)
