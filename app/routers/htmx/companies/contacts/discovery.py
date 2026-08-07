"""routers/htmx/companies/contacts/discovery.py — suggested-contacts discovery
waterfall: background runner, kickoff, poller, add (W4.8 split of contacts.py).

The account-building loop: ``contacts_tab_suggested`` schedules
``_run_contact_discovery`` (external-provider waterfall) as a background task,
the status poller renders results from ``contact_discovery_runs``, and
``contacts_tab_add_suggested`` persists picked suggestions. Pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in contacts/__init__).

PATCH SEAMS (do not refactor): tests monkeypatch
``app.routers.htmx.companies._run_contact_discovery`` — the scheduled callable
is resolved off the PACKAGE attribute via the module-level ``_pkg`` alias at
call time, so the patch takes effect. ``find_suggested_contacts_with_errors``
stays a function-local import inside ``_run_contact_discovery`` for the same
reason (patched at app.enrichment_service).

Called by: app.routers.htmx.companies.contacts (package __init__ re-export,
    route registration); app.routers.htmx.companies (package attr re-export)
Depends on: app.enrichment_service (function-local),
    app.services.contact_discovery_runs, app.services.contact_dedup
    (function-local), app.services.crm_service, .._registries, .common
"""

import html as html_mod
import json

import httpx
from fastapi import BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

import app.routers.htmx.companies as _pkg
from app.services.contact_discovery_runs import ContactDiscoveryOutcome, contact_discovery_runs

from .....database import get_db
from .....dependencies import can_manage_account, require_user
from .....models import Company, CustomerSite, SiteContact, User
from .....template_env import template_response
from ..._shared import _base_ctx
from .common import _render_contacts_list, router

# ── Suggested-contacts UI (account-building loop) ──────────────────────


async def _run_contact_discovery(company_id: int, domain: str, name: str) -> None:
    """Background worker: run the contact-discovery waterfall for one company.

    Scheduled by ``contacts_tab_suggested`` (the "Find contacts" button) so the click never
    blocks on the ~10-40s of external-provider calls (``find_suggested_contacts_with_errors``:
    Hunter/Clay/Lusha/Explorium). The transient result (discovered contacts + which providers
    errored) is recorded in ``contact_discovery_runs`` so the status poller can render the same
    ``_suggested_contacts.html`` panel the old synchronous path produced.

    This is a pure external-API call — it never touches the DB — so, unlike the account-enrich
    runner, it opens no session. It degrades gracefully: any failure is folded into
    ``errored_providers`` (the amber "couldn't reach" banner), mirroring the old inline
    behavior. Must NEVER raise: it is a fire-and-forget task.
    """
    # Import kept function-local (not hoisted) — tests monkeypatch
    # app.enrichment_service.find_suggested_contacts_with_errors via that exact
    # module-attribute path, which only takes effect on a fresh lookup per call.
    from app.enrichment_service import find_suggested_contacts_with_errors

    suggested: list[dict] = []
    errored: list[str] = []
    try:
        suggested, errored = await find_suggested_contacts_with_errors(domain, name)
    except Exception as exc:
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            logger.warning("Contact discovery connectivity error for company {}: {}", company_id, exc)
        else:
            logger.error("Contact discovery unexpected error for company {}: {}", company_id, exc, exc_info=True)
        suggested = []
        errored = ["all"]
    finally:
        contact_discovery_runs.finish(
            company_id,
            ContactDiscoveryOutcome(suggested=suggested, errored_providers=errored),
        )


@router.get(
    "/v2/partials/customers/{company_id}/suggested-contacts",
    response_class=HTMLResponse,
)
async def contacts_tab_suggested(
    request: Request,
    company_id: int,
    background_tasks: BackgroundTasks,
    domain: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The "Find contacts" button — **enqueue** the discovery waterfall, return a
    poller.

    The multi-provider suggested-contacts waterfall (Hunter/Clay/Lusha/Explorium, ~10-40s)
    used to run INLINE here, so the click felt hung. It now schedules that work as a FastAPI
    background task and returns the "Finding contacts…" poller immediately; the status route
    (:contacts_tab_suggested_status) swaps in the ``_suggested_contacts.html`` result panel
    once the run lands (or the amber "couldn't reach" banner if providers degraded).
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")
    # 404 (not 403) to match contact_edit_form_company_scoped: this trigger spends paid
    # enrichment credits and returns contact PII, so gate it to account managers and keep
    # out-of-scope accounts indistinguishable from missing ones.
    if not can_manage_account(user, company, db):
        raise HTTPException(404, "Company not found")

    if not domain:
        domain = company.domain or company.website or ""
    if not domain:
        raise HTTPException(400, "No domain available for this company")

    # Normalize (strip scheme/www/path)
    domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]

    # Double-enqueue guard: a run already in flight must not stack a second waterfall.
    # The scheduled callable is resolved off the PACKAGE attribute (not this module's own
    # global) so a test that monkeypatches app.routers.htmx.companies._run_contact_discovery
    # replaces what actually runs.
    if contact_discovery_runs.begin(company_id):
        background_tasks.add_task(_pkg._run_contact_discovery, company_id, domain, company.name or "")

    return template_response(
        "htmx/partials/customers/tabs/_suggested_contacts_finding.html",
        {"request": request, "company": company},
    )


@router.get(
    "/v2/partials/customers/{company_id}/suggested-contacts/status",
    response_class=HTMLResponse,
)
async def contacts_tab_suggested_status(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll target for the "Finding contacts…" panel — reflects the background run's
    state.

    While the run is in flight, re-renders the poller (keep polling). When it lands, returns
    the ``_suggested_contacts.html`` result panel (discovered contacts / neutral empty state /
    amber "couldn't reach" banner) and answers HTTP 286 to STOP polling. A deleted company or
    an already-consumed outcome stops polling with an empty body.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        # Polling sub-resource: htmx neither swaps nor cancels an `every 2s` poll on a 4xx,
        # so a 404 would leave the panel hammering this route. 286 stops it; empty clears it.
        return HTMLResponse("", status_code=286)
    if not can_manage_account(user, company, db):
        # Out-of-scope poller: this route streams discovered contact PII. Deny by stopping
        # the poll with an empty body — indistinguishable from a missing/consumed run (286),
        # never leaking whether a run exists for someone else's account.
        return HTMLResponse("", status_code=286)

    if contact_discovery_runs.is_running(company_id):
        return template_response(
            "htmx/partials/customers/tabs/_suggested_contacts_finding.html",
            {"request": request, "company": company},
        )

    outcome = contact_discovery_runs.consume_outcome(company_id)
    if outcome is None:
        # No run in flight and no pending outcome (already consumed, or lost on restart) —
        # stop polling and clear the panel.
        return HTMLResponse("", status_code=286)

    # ISS-025: drop any suggestion that already matches a saved SiteContact for this
    # company (case-insensitive email, or normalized-name fallback when the suggestion
    # has no email) — re-running discovery must not keep re-surfacing people already on
    # file.
    from .....services.contact_dedup import existing_contact_keys, is_existing_contact

    existing_contacts = (
        db.query(SiteContact)
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .filter(CustomerSite.company_id == company_id)
        .all()
    )
    existing_emails, existing_names = existing_contact_keys(existing_contacts)
    suggested = [
        c
        for c in outcome.suggested
        if not is_existing_contact(c.get("email"), c.get("full_name"), existing_emails, existing_names)
    ]

    active_sites = (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.site_name)
        .all()
    )
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "suggested": suggested,
            "errored_providers": outcome.errored_providers,
            "active_sites": active_sites,
        }
    )
    response = template_response("htmx/partials/customers/tabs/_suggested_contacts.html", ctx)
    response.status_code = 286  # htmx's stop-polling status — the result panel still swaps in.
    return response


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
        except ValueError as e:
            raise HTTPException(400, "Invalid site_id") from e
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
