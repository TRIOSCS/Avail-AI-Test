"""AI contact finder — background find/status poller + prospect save/promote/delete.

W4.8 split of the 1,475-line app/routers/htmx/vendors.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from fastapi import BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_prospect_site_access, require_user
from ....models import User, VendorCard
from ....models.enrichment import ProspectContact
from ....models.vendors import VendorContact
from ....template_env import template_response
from ..._lookup_helpers import get_vendor_card_or_404
from .._shared import _base_ctx
from .common import router

# ── AI Contact Finder actions (Phase 3A) ───────────────────────────────


async def _run_vendor_find_contacts(vendor_id: int, keywords: str | None) -> None:
    """Background worker: run the AI web-search contact finder for one vendor.

    Scheduled by ``vendor_find_contacts`` so the click never blocks on the >15s Claude +
    web-search call. Opens its own session (FastAPI has already returned the response and
    closed the request session by the time this runs), deduplicates and persists the
    discovered contacts as ``ProspectContact`` rows, and records the run's outcome in
    ``vendor_contact_runs`` so the find-contacts poller can swap in the results (new count),
    the "none found" state, or the error/retry state. Because a legitimate no-results run
    saves no rows — indistinguishable from "never ran" by the table alone — the registry
    outcome is what the poller trusts.

    Must NEVER raise: it is a fire-and-forget task.
    """
    from ....database import SessionLocal
    from ....services.vendor_contact_runs import VendorContactRunOutcome, vendor_contact_runs

    db = SessionLocal()
    outcome = VendorContactRunOutcome()
    try:
        vendor = db.get(VendorCard, vendor_id)
        if not vendor:
            outcome = VendorContactRunOutcome(error="Vendor not found.")
            return

        from app.services.ai_service import enrich_contacts_websearch
        from app.services.contact_dedup import existing_contact_keys, is_existing_contact

        web_results = await enrich_contacts_websearch(vendor.display_name, vendor.domain, keywords, limit=10)

        # Existing contacts for this vendor — both prior AI-discovered ProspectContact
        # rows and real VendorContact rows — so re-running discovery never re-suggests
        # (or re-persists) someone already on file (ISS-025).
        existing_rows = list(db.query(ProspectContact).filter(ProspectContact.vendor_card_id == vendor_id)) + list(
            db.query(VendorContact).filter(VendorContact.vendor_card_id == vendor_id)
        )
        existing_emails, existing_names = existing_contact_keys(existing_rows)

        # Dedup within this batch and against existing contacts, then save the
        # survivors as ProspectContact records.
        seen: set[str] = set()
        new_count = 0
        for c in web_results:
            email = (c.get("email") or "").lower()
            key = email if email else c.get("full_name", "").lower()
            if key and key in seen:
                continue
            if is_existing_contact(c.get("email"), c.get("full_name"), existing_emails, existing_names):
                continue
            seen.add(key)

            db.add(
                ProspectContact(
                    vendor_card_id=vendor_id,
                    full_name=c["full_name"],
                    title=c.get("title"),
                    email=c.get("email"),
                    email_status=c.get("email_status"),
                    phone=c.get("phone"),
                    linkedin_url=c.get("linkedin_url"),
                    source=c.get("source", "web_search"),
                    confidence=c.get("confidence", "low"),
                )
            )
            new_count += 1

        db.commit()
        outcome = VendorContactRunOutcome(new_count=new_count)
    except Exception as exc:
        logger.error("AI contact finder error for vendor {}: {}", vendor_id, exc)
        db.rollback()
        outcome = VendorContactRunOutcome(error=f"AI search failed: {exc}")
    finally:
        db.close()
        vendor_contact_runs.finish(vendor_id, outcome)


@router.post("/v2/partials/vendors/{vendor_id}/ai/find-contacts", response_class=HTMLResponse)
async def vendor_find_contacts(
    request: Request,
    vendor_id: int,
    background_tasks: BackgroundTasks,
    title_keywords: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Queue the AI web-search contact finder and return the "Finding contacts…" poller.

    The Claude + web-search call (``enrich_contacts_websearch``, commonly >15s) used to run
    INLINE before responding, so it blew past htmx's 15s client timeout and the Find
    Contacts tab spun then errored out. It now runs in a FastAPI background task and this
    handler returns the polling partial immediately; the find-contacts-status poller swaps
    in the discovered contacts (or the none-found / error state) when the task finishes.

    Called by: HTMX "Find Contacts" button on the vendor detail Find Contacts tab.
    Depends on: _run_vendor_find_contacts (background worker), vendor_contact_runs
        (double-enqueue guard + outcome carrier).
    """
    vendor = get_vendor_card_or_404(db, vendor_id)

    from ....config import settings as app_settings
    from ....services.vendor_contact_runs import vendor_contact_runs

    # Check AI feature gate
    if app_settings.ai_features_enabled == "off":
        return HTMLResponse(
            '<div class="p-4 text-center text-sm text-amber-600 bg-amber-50 rounded-lg border border-amber-200">'
            "AI features are currently disabled. Contact your admin to enable them.</div>"
        )

    keywords = title_keywords.strip() if title_keywords else None

    # Guard double-enqueue: a search already in flight for this vendor must not stack another.
    if vendor_contact_runs.begin(vendor_id):
        background_tasks.add_task(_run_vendor_find_contacts, vendor_id, keywords)

    # Return the polling in-progress state immediately (no inline >15s block).
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    return template_response("htmx/partials/vendors/find_contacts_status.html", ctx)


@router.get("/v2/partials/vendors/{vendor_id}/ai/find-contacts-status", response_class=HTMLResponse)
async def vendor_find_contacts_status(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll the in-flight AI contact search and swap in the results when it lands.

    While the background search is running this returns the "Finding contacts…" polling
    partial (keep polling). On the terminal outcome it reloads the vendor's prospects and
    returns ``find_contacts_results.html`` (discovered contacts, the "none found" state, or
    — on a failed run — the error/retry state) with HTTP 286 so htmx swaps the results in
    and STOPS polling. If no run is tracked (e.g. the process restarted mid-run) it stops
    polling and renders the current prospects rather than spinning forever.
    """
    from ....services.vendor_contact_runs import vendor_contact_runs

    vendor = db.get(VendorCard, vendor_id)
    if not vendor:
        # Polling sub-resource, not a navigable page: htmx neither swaps nor cancels an
        # `every 3s` poll on a 4xx, so a 404 would hammer this route forever after the
        # vendor is gone. 286 stops the poll; the empty body clears the spinner.
        return HTMLResponse("", status_code=286)

    outcome = vendor_contact_runs.consume_outcome(vendor_id)
    if outcome is None and vendor_contact_runs.is_running(vendor_id):
        # Still running → keep polling.
        ctx = _base_ctx(request, user, "vendors")
        ctx["vendor"] = vendor
        return template_response("htmx/partials/vendors/find_contacts_status.html", ctx)

    # Terminal (done / error) or no tracked run → reload prospects, swap in, stop polling.
    prospects = (
        db.query(ProspectContact)
        .filter(ProspectContact.vendor_card_id == vendor_id)
        .order_by(ProspectContact.created_at.desc())
        .limit(50)
        .all()
    )
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["prospects"] = prospects
    ctx["search_count"] = outcome.new_count if outcome else 0
    ctx["error"] = outcome.error if outcome else None
    response = template_response("htmx/partials/vendors/find_contacts_results.html", ctx)
    response.status_code = 286  # htmx's stop-polling status — the results still swap in.
    return response


@router.post(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/save",
    response_class=HTMLResponse,
)
async def vendor_prospect_save(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a prospect contact as saved."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    require_prospect_site_access(db, user, pc)

    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return template_response("htmx/partials/vendors/prospect_card.html", ctx)


@router.post(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}/promote",
    response_class=HTMLResponse,
)
async def vendor_prospect_promote(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a prospect contact to a VendorContact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    require_prospect_site_access(db, user, pc)

    # Dedup: check if email already exists on this vendor
    existing = None
    if pc.email:
        existing = db.query(VendorContact).filter_by(vendor_card_id=vendor_id, email=pc.email).first()

    if existing:
        if pc.full_name and not existing.full_name:
            existing.full_name = pc.full_name
        if pc.title and not existing.title:
            existing.title = pc.title
        if pc.phone and not existing.phone:
            existing.phone = pc.phone
        if pc.linkedin_url and not existing.linkedin_url:
            existing.linkedin_url = pc.linkedin_url
        vc = existing
    else:
        vc = VendorContact(
            vendor_card_id=vendor_id,
            full_name=pc.full_name,
            title=pc.title,
            email=pc.email,
            phone=pc.phone,
            linkedin_url=pc.linkedin_url,
            source="prospect_promote",
        )
        db.add(vc)
        db.flush()

    pc.promoted_to_type = "vendor_contact"
    pc.promoted_to_id = vc.id
    pc.is_saved = True
    pc.saved_by_id = user.id
    db.commit()

    vendor = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    ctx = _base_ctx(request, user, "vendors")
    ctx["vendor"] = vendor
    ctx["p"] = pc
    return template_response("htmx/partials/vendors/prospect_card.html", ctx)


@router.delete(
    "/v2/partials/vendors/{vendor_id}/ai/prospect/{prospect_id}",
    response_class=HTMLResponse,
)
async def vendor_prospect_delete(
    request: Request,
    vendor_id: int,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a prospect contact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == prospect_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    require_prospect_site_access(db, user, pc)
    db.delete(pc)
    db.commit()
    # Return empty string to remove the card from DOM
    return HTMLResponse("")
