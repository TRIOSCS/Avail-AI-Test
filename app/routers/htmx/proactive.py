"""routers/htmx/proactive.py — Proactive part-match partials (HTMX + Alpine).

Server-rendered HTML partials for the proactive-selling surface: the match list,
refresh/scan, batch-dismiss, the prepare page + draft + send flow, the legacy
send/convert routes, scorecard, badge, and do-not-offer. Extracted verbatim from
htmx_views.py (same `/v2/partials/proactive` + `/v2/proactive` paths, same
`htmx-views` tag).

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
"""

import asyncio
import html as html_mod
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    ProactiveMatchStatus,
)
from ...database import get_db
from ...dependencies import (
    can_manage_account,
    require_access,
    require_user,
)
from ...models import (
    Company,
    SiteContact,
    User,
)
from ...template_env import template_response
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


def _render_matches_tab(request: Request, user: User, db: Session, *, scope: str = "mine", success_msg: str = ""):
    """One renderer for every endpoint that lands back on the Matches tab.

    ``scope=all`` (manager/admin only) shows every rep's matches with rep
    attribution; everyone else always gets their own. The prepared strip
    (staged per-customer draft offers from Process) rides along.
    """
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_service import get_matches_for_user, list_draft_offers

    can_see_all = is_manager_or_admin(user)
    admin_all = scope == "all" and can_see_all
    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW, admin_all=admin_all)
    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = result.get("groups", []) if isinstance(result, dict) else result
    ctx["sent"] = []
    ctx["tab"] = "matches"
    ctx["match_count"] = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0
    ctx["success_msg"] = success_msg
    ctx["scope"] = "all" if admin_all else "mine"
    ctx["can_see_all"] = can_see_all
    ctx["prepared"] = list_draft_offers(db, user, allow_all=admin_all)
    return template_response("htmx/partials/proactive/list.html", ctx)


# ── Proactive Part Match ─────────────────────────────────────────────


@router.get("/v2/partials/proactive", response_class=HTMLResponse)
async def proactive_list_partial(
    request: Request,
    tab: str = "matches",
    scope: str = "mine",
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Proactive matches list partial — Matches / Sent / Digests tabs."""
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_service import get_matches_for_user, get_sent_offers

    if tab == "matches":
        return _render_matches_tab(
            request, user, db, scope=scope, success_msg=request.query_params.get("success_msg", "")
        )

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    groups = result.get("groups", []) if isinstance(result, dict) else result
    match_count = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0
    sent = get_sent_offers(db, user.id) if tab == "sent" else []

    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = groups
    ctx["sent"] = sent
    ctx["tab"] = tab
    ctx["match_count"] = match_count
    ctx["success_msg"] = request.query_params.get("success_msg", "")
    if tab == "digests":
        from ...services.proactive_digest import get_digests_for_view, weekly_outreach_summary

        can_send = is_manager_or_admin(user)
        ctx["digests"] = get_digests_for_view(db, viewer=user, can_see_all=can_send)
        ctx["outreach_summary"] = weekly_outreach_summary(db)
        ctx["can_send"] = can_send
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/refresh", response_class=HTMLResponse)
async def proactive_refresh(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Trigger a proactive scan then return the matches list partial."""
    from ...services.part_equivalence import classify_new_pairs, windowed_spellings_by_key
    from ...services.proactive_matching import run_proactive_scan

    try:
        spellings = await asyncio.to_thread(windowed_spellings_by_key, db)
        await classify_new_pairs(db, spellings, limit=10)
    except Exception as exc:  # classifier down ≠ scan broken
        logger.warning("Part-equivalence classification on refresh failed: {}", exc)
        db.rollback()
    await asyncio.to_thread(run_proactive_scan, db)
    return _render_matches_tab(request, user, db)


@router.post("/v2/partials/proactive/batch-dismiss", response_class=HTMLResponse)
async def proactive_batch_dismiss(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Batch dismiss selected proactive matches and reload the list."""
    from ...models import ProactiveMatch

    form = await request.form()
    match_ids_raw = form.getlist("match_ids")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]

    if match_ids:
        from ...dependencies import is_manager_or_admin

        query = db.query(ProactiveMatch).filter(
            ProactiveMatch.id.in_(match_ids),
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
        )
        if not is_manager_or_admin(user):
            query = query.filter(ProactiveMatch.salesperson_id == user.id)
        query.update(
            {"status": ProactiveMatchStatus.DISMISSED, "dismiss_reason": "batch_dismiss"}, synchronize_session=False
        )
        db.commit()

    return _render_matches_tab(request, user, db)


@router.post("/v2/proactive/prepare/{site_id}", response_class=HTMLResponse)
async def proactive_prepare_page(
    site_id: int,
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Full-page prepare/send workflow for proactive offers."""
    import json

    from ...models import ProactiveMatch
    from ...models.crm import CustomerSite as _CS

    form = await request.form()
    match_ids_raw = form.getlist("match_ids")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]

    if not match_ids:
        from starlette.responses import RedirectResponse

        return RedirectResponse("/v2/proactive", status_code=303)

    from ...dependencies import is_manager_or_admin

    match_query = db.query(ProactiveMatch).filter(ProactiveMatch.id.in_(match_ids))
    if not is_manager_or_admin(user):
        match_query = match_query.filter(ProactiveMatch.salesperson_id == user.id)
    matches = match_query.all()
    if not matches:
        from starlette.responses import RedirectResponse

        return RedirectResponse("/v2/proactive", status_code=303)

    site = db.get(_CS, site_id)
    company = site.company if site else None
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id)
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )

    from ...services.proactive_matching import compute_offer_rollups
    from ...services.proactive_service import DEFAULT_PROACTIVE_MARKUP

    # AI-variant flags for the row chips + Task 5's server-side send gate.
    # ONE batch call for every distinct part (PERF guard pattern —
    # services/proactive_service.py:105-109).
    rollups = compute_offer_rollups(db, parts={(m.mpn or "").strip().upper() for m in matches if m.mpn})

    match_data = []
    for m in matches:
        offer = m.offer
        unit_price = float(offer.unit_price) if offer and offer.unit_price else None
        match_data.append(
            {
                "id": m.id,
                "mpn": m.mpn,
                "vendor_name": offer.vendor_name if offer else "",
                "manufacturer": offer.manufacturer if offer else "",
                "qty_available": offer.qty_available if offer else 0,
                "unit_price": unit_price,
                # Seeded sell price shown in the table — the service's markup rule,
                # computed here so the template never carries pricing logic.
                "suggested_sell": f"{(unit_price * DEFAULT_PROACTIVE_MARKUP) if unit_price else 0:.4f}",
                "margin_pct": m.margin_pct,
                "match_score": m.match_score or 0,
                "has_ai_variants": (rollups.get((m.mpn or "").strip().upper()) or {}).get("has_ai_variants", False),
            }
        )

    contact_data = [
        {
            "id": c.id,
            "full_name": c.full_name,
            "email": c.email,
            "title": c.title,
            "is_primary": c.is_primary,
            "has_email": bool(c.email),
        }
        for c in contacts
    ]

    ctx = _base_ctx(request, user, "proactive")
    ctx.update(
        {
            "site_id": site_id,
            "company_name": company.name if company else "Customer",
            "site_name": site.site_name if site else "",
            "matches": match_data,
            "any_ai_variants": any(m["has_ai_variants"] for m in match_data),
            "match_ids_json": json.dumps([m["id"] for m in match_data]),
            "contacts": contact_data,
            "error_msg": "",
        }
    )
    return template_response("htmx/partials/proactive/prepare.html", ctx)


def _render_contact_picker(request, db, site_id, added_contact_id=None):
    """Re-render the Prepare "Send To" contact picker for one site.

    Shared by proactive_add_contact so the swapped-in list mirrors the initial
    prepare.html render exactly (same fields, same ordering).
    """
    contacts = (
        db.query(SiteContact)
        .filter(SiteContact.customer_site_id == site_id)
        .order_by(SiteContact.is_primary.desc(), SiteContact.full_name)
        .all()
    )
    contact_data = [
        {
            "id": c.id,
            "full_name": c.full_name,
            "email": c.email,
            "title": c.title,
            "is_primary": c.is_primary,
            "has_email": bool(c.email),
        }
        for c in contacts
    ]
    return template_response(
        "htmx/partials/proactive/_contact_picker.html",
        {"request": request, "contacts": contact_data, "added_contact_id": added_contact_id},
    )


@router.post("/v2/partials/proactive/prepare/{site_id}/add-contact", response_class=HTMLResponse)
async def proactive_add_contact(
    site_id: int,
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    title: str = Form(""),
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Add a SiteContact from the Prepare page, then re-render the contact picker.

    Fixes PROACTIVE-04: when a site has no emailable contact, Send was disabled with
    no way forward. This adds one inline. The refreshed picker auto-selects the new
    contact (client-side) so Send unblocks in a single step.

    Authorization: the caller must be the assigned salesperson on at least one
    ProactiveMatch at this site — the same relationship that lets them reach Prepare.
    Validation failures raise 4xx (surfaced by the global htmx toast handler); the
    form stays open with the entered values on error.
    """
    from ...models import ProactiveMatch
    from ...models.crm import CustomerSite as _CS

    site = db.get(_CS, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")

    from ...dependencies import is_manager_or_admin

    owns_query = db.query(ProactiveMatch.id).filter(ProactiveMatch.customer_site_id == site_id)
    if not is_manager_or_admin(user):
        owns_query = owns_query.filter(ProactiveMatch.salesperson_id == user.id)
    if not owns_query.first():
        raise HTTPException(403, "Not authorized to add a contact for this site")

    full_name = full_name.strip()
    email = email.strip().lower()
    if not full_name:
        raise HTTPException(400, "Name is required.")
    if not email:
        raise HTTPException(400, "Email is required to send an offer.")
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address.")

    # Per-site email dedup (case-insensitive) — mirrors the canonical add path.
    dup = (
        db.query(SiteContact)
        .filter(
            SiteContact.customer_site_id == site_id,
            sqlfunc.lower(SiteContact.email) == email,
        )
        .first()
    )
    if dup:
        raise HTTPException(409, f"A contact with email {email} already exists at this site.")

    # Split full_name into first/last so the new contact matches the canonical
    # contacts_tab_create shape (full_name stays the derived source of truth).
    parts = full_name.split(" ", 1)
    contact = SiteContact(
        customer_site_id=site_id,
        full_name=full_name,
        first_name=parts[0] or None,
        last_name=parts[1].strip() if len(parts) > 1 else None,
        email=email,
        title=title.strip() or None,
    )
    db.add(contact)
    db.commit()
    logger.info(
        "Proactive add-contact: created contact {} at site {} by {}",
        contact.id,
        site_id,
        user.email,
    )
    return _render_contact_picker(request, db, site_id, added_contact_id=contact.id)


@router.post("/v2/partials/proactive/draft", response_class=HTMLResponse)
async def proactive_draft_for_prepare(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """AI-draft a proactive offer email for the prepare page."""
    from ...models import ProactiveMatch
    from ...models.crm import CustomerSite as _CS

    form = await request.form()
    match_ids_raw = form.getlist("match_ids") or (form.get("match_ids", "") or "").split(",")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]
    contact_ids_raw = form.getlist("contact_ids") or (form.get("contact_ids", "") or "").split(",")
    contact_ids = [int(cid) for cid in contact_ids_raw if cid and str(cid).isdigit()]

    if not match_ids:
        return HTMLResponse('<div class="text-sm text-rose-600">No matches selected.</div>')

    from ...dependencies import is_manager_or_admin

    draft_query = db.query(ProactiveMatch).filter(ProactiveMatch.id.in_(match_ids))
    if not is_manager_or_admin(user):
        draft_query = draft_query.filter(ProactiveMatch.salesperson_id == user.id)
    matches = draft_query.all()
    if not matches:
        return HTMLResponse('<div class="text-sm text-rose-600">No valid matches found.</div>')

    site_id = matches[0].customer_site_id
    site = db.get(_CS, site_id)
    company = site.company if site else None
    company_name = company.name if company else "Customer"

    # Resolve contact name
    contact_name = None
    if contact_ids:
        primary = db.get(SiteContact, contact_ids[0])
        if primary and primary.full_name:
            _fn_parts = primary.full_name.split()
            contact_name = _fn_parts[0] if _fn_parts else None

    # Parse rep-entered sell prices from form (sell_price_<match_id>)
    draft_sell_prices: dict[str, float] = {}
    for key in form:
        if key.startswith("sell_price_"):
            match_id_str = key[len("sell_price_") :]
            raw_val = form.get(key, "").strip()
            if match_id_str.isdigit() and raw_val:
                try:
                    draft_sell_prices[match_id_str] = float(raw_val)
                except ValueError:
                    pass

    # Build parts list for AI
    from ...services.proactive_service import DEFAULT_PROACTIVE_MARKUP

    parts = []
    for m in matches:
        offer = m.offer
        cost = float(offer.unit_price) if offer and offer.unit_price else 0
        sell = draft_sell_prices.get(str(m.id), cost * DEFAULT_PROACTIVE_MARKUP)
        parts.append(
            {
                "mpn": m.mpn,
                "manufacturer": offer.manufacturer if offer else "",
                "qty": offer.qty_available if offer else 0,
                "sell_price": float(sell),
                "condition": offer.condition if offer else "",
                "lead_time": offer.lead_time if offer else "",
                "customer_purchase_count": m.customer_purchase_count or 0,
                "customer_last_purchased_at": (
                    m.customer_last_purchased_at.strftime("%b %Y") if m.customer_last_purchased_at else None
                ),
            }
        )

    salesperson_name = user.display_name

    try:
        from ...services.proactive_email import draft_proactive_email

        result = await draft_proactive_email(
            company_name=company_name,
            contact_name=contact_name,
            parts=parts,
            salesperson_name=salesperson_name,
        )
        if result:
            subject = result.get("subject", f"Parts Available — {company_name}")
            body = result.get("body", "")
            safe_subject_attr = html_mod.escape(subject)
            subject_json = json.dumps(subject, ensure_ascii=True).replace("</", "<\\/")
            body_json = json.dumps(body, ensure_ascii=True).replace("</", "<\\/")
            return HTMLResponse(f"""
                <input type="hidden" name="ai_subject" id="ai-subject" value="{safe_subject_attr}">
                <input type="hidden" name="ai_body" id="ai-body" value="">
                <script>
                    document.getElementById('subject-input').value = {subject_json};
                    document.getElementById('body-input').value = {body_json};
                </script>
                <div class="text-sm text-emerald-600 flex items-center gap-1">
                    <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
                    </svg>
                    Draft generated — edit as needed
                </div>
            """)
    except Exception as exc:
        logger.warning("Proactive AI draft failed: {}", exc)

    return HTMLResponse("""
        <div class="text-sm text-amber-600 flex items-center gap-1">
            Auto-draft unavailable. Write your message manually.
            <button type="button"
                    hx-post="/v2/partials/proactive/draft"
                    hx-target="#draft-status"
                    hx-include="[name='match_ids'],[name='contact_ids'],[name^='sell_price_']"
                    class="ml-2 text-brand-600 underline text-xs">Retry</button>
        </div>
    """)


@router.post("/v2/proactive/send", response_class=HTMLResponse)
async def proactive_send_offer(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Send a proactive offer email from the prepare page."""
    form = await request.form()
    match_ids_raw = form.getlist("match_ids") or (form.get("match_ids", "") or "").split(",")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]
    contact_ids_raw = form.getlist("contact_ids") or (form.get("contact_ids", "") or "").split(",")
    contact_ids = [int(cid) for cid in contact_ids_raw if cid and str(cid).isdigit()]
    subject = form.get("subject", "").strip()
    body = form.get("body", "").strip()

    # Parse rep-entered sell prices keyed as sell_price_<match_id>
    sell_prices: dict[str, float] = {}
    for key in form:
        if key.startswith("sell_price_"):
            match_id_str = key[len("sell_price_") :]
            raw_val = form.get(key, "").strip()
            if match_id_str.isdigit() and raw_val:
                try:
                    sell_prices[match_id_str] = float(raw_val)
                except ValueError:
                    pass  # ignore non-numeric input; service will apply default

    if not match_ids:
        raise HTTPException(400, "No matches selected")
    if not contact_ids:
        raise HTTPException(400, "No contacts selected")

    # Get token
    from ...scheduler import get_valid_token

    token = await get_valid_token(user, db)

    try:
        from ...services.proactive_service import send_proactive_offer

        # Build email HTML from body text
        email_html = None
        if body:
            import html as html_mod

            body_html = html_mod.escape(body).replace("\n", "<br>")
            email_html = f'<div style="font-family:Arial,sans-serif;max-width:700px"><p>{body_html}</p></div>'

        from ...dependencies import is_manager_or_admin

        result = await send_proactive_offer(
            db=db,
            user=user,
            token=token or "no-token",
            match_ids=match_ids,
            contact_ids=contact_ids,
            sell_prices=sell_prices,
            subject=subject or None,
            email_html=email_html,
            allow_all=is_manager_or_admin(user),
        )

        # Honest outcome (QC 2026-08-13): send_proactive_offer swallows a Graph
        # rejection and marks the offer + matches FAILED (persisted), returning
        # normally. Don't show a phantom "sent" banner — surface the failure so the
        # HTMX error toast fires, matching the prepared-send path.
        from ...constants import ProactiveOfferStatus

        if result.get("status") == ProactiveOfferStatus.FAILED:
            raise HTTPException(
                502, "Send failed — Microsoft rejected the message. The offer was not delivered; try again."
            )

        # Success — reload matches list with success banner
        parts_count = len(result.get("line_items", []))
        contacts_count = len(result.get("recipient_emails", []))
        return _render_matches_tab(
            request, user, db, success_msg=f"Offer sent to {contacts_count} contact(s) ({parts_count} parts)."
        )

    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except HTTPException:
        raise  # our own 502 phantom-send guard — don't remap it to a generic 500
    except Exception as exc:
        logger.error("Proactive send failed: {}", exc)
        raise HTTPException(500, "Send failed. Please try again or contact support.") from exc


# ── Sprint 8: Proactive Selling + Prospecting Completion ──
# (proactive_send_legacy, POST /v2/partials/proactive/{match_id}/send, was removed as a
# dead twin of POST /v2/proactive/send — no template/JS caller. The prepare form posts to
# /v2/proactive/send.)


@router.post("/v2/partials/proactive/{offer_id}/convert", response_class=HTMLResponse)
async def proactive_convert(
    request: Request,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Convert a won proactive offer into req+quote+buyplan."""
    from ...models import ProactiveOffer

    offer = db.query(ProactiveOffer).filter(ProactiveOffer.id == offer_id).first()
    if not offer:
        raise HTTPException(404, "Proactive offer not found")
    if offer.salesperson_id and offer.salesperson_id != user.id:
        raise HTTPException(403, "Not your proactive offer")

    try:
        from ...services.proactive_service import convert_proactive_to_win

        result = convert_proactive_to_win(db, offer.id, user)
        return template_response(
            "htmx/partials/proactive/convert_success.html",
            {"request": request, "offer": offer, "result": result},
        )
    except ValueError as exc:
        exc_str = str(exc).lower()
        if "already converted" in exc_str:
            raise HTTPException(409, "This offer has already been converted.") from exc
        raise HTTPException(403, str(exc)) from exc
    except Exception as exc:
        logger.error("Proactive conversion failed: {}", exc)
        raise HTTPException(500, "Conversion failed. Please try again.") from exc


@router.get("/v2/partials/proactive/scorecard", response_class=HTMLResponse)
async def proactive_scorecard(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return proactive offers scorecard/metrics panel."""
    try:
        from ...services.proactive_service import get_scorecard

        stats = get_scorecard(db, user.id)
    except Exception:
        # QC 2026-08-13: was a silent all-zeros swallow whose keys didn't even
        # match the template (total_revenue is unused; converted_revenue /
        # gross_profit / anticipated_revenue / total_quoted / total_po are what
        # scorecard.html reads). Log it, and mirror get_scorecard's real keys.
        logger.exception("Proactive scorecard failed for user {}", user.id)
        stats = {
            "total_sent": 0,
            "total_converted": 0,
            "total_quoted": 0,
            "total_po": 0,
            "conversion_rate": 0,
            "anticipated_revenue": 0,
            "converted_revenue": 0,
            "gross_profit": 0,
        }

    return template_response(
        "htmx/partials/proactive/scorecard.html",
        {"request": request, "stats": stats},
    )


@router.get("/v2/partials/proactive/badge", response_class=HTMLResponse)
async def proactive_badge(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return proactive match count badge for nav sidebar."""
    from ...models import ProactiveMatch

    count = (
        db.query(sqlfunc.count(ProactiveMatch.id))
        .filter(ProactiveMatch.salesperson_id == user.id, ProactiveMatch.status == ProactiveMatchStatus.NEW)
        .scalar()
        or 0
    )
    if count > 0:
        return HTMLResponse(
            f'<span class="ml-auto px-1.5 py-0.5 text-[10px] font-bold text-white bg-emerald-500 rounded-full">{count}</span>'
        )
    return HTMLResponse("")


@router.post("/v2/partials/proactive/do-not-offer", response_class=HTMLResponse)
async def proactive_do_not_offer(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Add an MPN+customer combo to the do-not-offer list (with dedup check)."""
    from ...models.intelligence import ProactiveDoNotOffer
    from ...services.proactive_helpers import is_do_not_offer

    form = await request.form()
    mpn = form.get("mpn", "").strip()
    company_id = form.get("customer_site_id", "") or form.get("company_id", "")

    if not mpn or not company_id:
        raise HTTPException(400, "MPN and company are required")

    try:
        cid = int(company_id)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "company_id must be an integer") from e

    # Authz: a do-not-offer rule is scoped to a customer account, so the actor must be
    # able to manage that account — otherwise a cross-account actor could suppress offers
    # for any company by passing an arbitrary company_id in the form.
    company = db.get(Company, cid)
    if not company or not can_manage_account(user, company, db):
        raise HTTPException(403, "Not authorized to manage this account")

    if not is_do_not_offer(db, mpn, cid):
        dno = ProactiveDoNotOffer(
            mpn=mpn.upper(),
            company_id=cid,
            created_by_id=user.id,
        )
        db.add(dno)
        db.commit()
        logger.info("Do-not-offer: {} for company {} by {}", mpn, company_id, user.email)

    # Return an empty collapsed row so the table structure stays valid
    return HTMLResponse('<tr style="display:none" aria-hidden="true"></tr>')


# ── Rollup drill-down + picks + digests (2026-08-06 augmentation) ────────


@router.get("/v2/partials/proactive/{match_id}/offers", response_class=HTMLResponse)
async def proactive_offers_drilldown(
    request: Request,
    match_id: int,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """The individual live offers behind one rolled-up match line."""
    from ...dependencies import is_manager_or_admin
    from ...models import ProactiveMatch
    from ...services.proactive_matching import compute_offer_rollup

    match = db.get(ProactiveMatch, match_id)
    if not match:
        raise HTTPException(404, "Match not found")
    if match.salesperson_id != user.id and not is_manager_or_admin(user):
        raise HTTPException(403, "Not your match")

    part = (match.mpn or "").strip().upper()
    rollup = compute_offer_rollup(db, part=part)
    variants = rollup.get("variants", {})
    ctx = _base_ctx(request, user, "proactive")
    ctx["part"] = part
    ctx["offers"] = []
    for o in rollup["offers"]:
        spelling = (o.mpn or "").strip().upper()
        variant = variants.get(spelling) if spelling != part else None
        ctx["offers"].append(
            {
                "spelling": spelling,
                "variant_kind": variant["kind"] if variant else None,
                "variant_reason": variant["reason"] if variant else "",
                "vendor_name": o.vendor_name,
                "qty_available": o.qty_available,
                "unit_price": float(o.unit_price) if o.unit_price is not None else None,
                "condition": o.condition,
                "lead_time": o.lead_time,
                "created_at_display": f"{o.created_at.month}/{o.created_at.day}/{o.created_at.year}"
                if o.created_at
                else "",
            }
        )
    return template_response("htmx/partials/proactive/_offers_drilldown.html", ctx)


@router.get("/v2/partials/proactive/picks", response_class=HTMLResponse)
async def proactive_picks(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Top-picks strip — highest-scored actionable lines, cached 24h per user."""
    from ...services.proactive_service import get_top_picks

    ctx = _base_ctx(request, user, "proactive")
    ctx["picks"] = get_top_picks(db, user.id)
    return template_response("htmx/partials/proactive/_picks.html", ctx)


def _render_digests_tab(request: Request, user: User, db: Session) -> HTMLResponse:
    """Shared re-render for the digests tab after generate/send."""
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_digest import get_digests_for_view, weekly_outreach_summary
    from ...services.proactive_service import get_matches_for_user

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    can_send = is_manager_or_admin(user)
    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = result.get("groups", [])
    ctx["sent"] = []
    ctx["tab"] = "digests"
    ctx["match_count"] = result.get("stats", {}).get("total", 0)
    ctx["success_msg"] = ""
    ctx["digests"] = get_digests_for_view(db, viewer=user, can_see_all=can_send)
    ctx["outreach_summary"] = weekly_outreach_summary(db)
    ctx["can_send"] = can_send
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/digests/generate", response_class=HTMLResponse)
async def proactive_digests_generate(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Build/replace draft digests from this week's matches (manager/admin).

    Never sends.
    """
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_digest import generate_digests

    if not is_manager_or_admin(user):
        raise HTTPException(403, "Only managers generate digests")
    stats = generate_digests(db)
    db.commit()
    logger.info("Proactive digests generated by {}: {}", user.email, stats)
    return _render_digests_tab(request, user, db)


@router.post("/v2/partials/proactive/digests/{digest_id}/send", response_class=HTMLResponse)
async def proactive_digest_send(
    request: Request,
    digest_id: int,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Email a reviewed draft to its salesperson with the reviewer's own mailbox."""
    from ...dependencies import is_manager_or_admin
    from ...scheduler import get_valid_token
    from ...services.proactive_digest import send_digest

    if not is_manager_or_admin(user):
        raise HTTPException(403, "Only managers send digests")
    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(400, "No valid Microsoft token — sign in again to send")
    try:
        await send_digest(db, digest_id, user, token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _render_digests_tab(request, user, db)


@router.post("/v2/partials/proactive/lines/{line_id}/tracking", response_class=HTMLResponse)
async def proactive_line_tracking(
    request: Request,
    line_id: int,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Record contact/outcome/SO-number on one sent digest line; returns the row
    cells."""
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_digest import update_line_tracking

    form = await request.form()
    contacted_raw = form.get("contacted")
    can_manage = is_manager_or_admin(user)
    try:
        line = update_line_tracking(
            db,
            line_id,
            viewer=user,
            can_manage=can_manage,
            contacted=(str(contacted_raw).lower() == "true") if contacted_raw is not None else None,
            outcome=str(form.get("outcome")) if form.get("outcome") is not None else None,
            sales_order_number=str(form.get("sales_order_number"))
            if form.get("sales_order_number") is not None
            else None,
        )
    except ValueError as e:
        raise HTTPException(403 if "Not your" in str(e) else 400, str(e)) from e
    db.commit()

    company = db.get(Company, line.company_id) if line.company_id else None
    ctx = _base_ctx(request, user, "proactive")
    ctx["line"] = {
        "id": line.id,
        "mpn": line.mpn,
        "company_name": company.name if company else None,
        "contacted": line.contacted,
        "outcome": line.outcome,
        "sales_order_number": line.sales_order_number,
        "produced_requisition_id": line.produced_requisition_id,
        "can_edit": can_manage or line.salesperson_id == user.id,
    }
    return template_response("htmx/partials/proactive/_outreach_line_cells.html", ctx)


@router.post("/v2/partials/proactive/lines/{line_id}/clone", response_class=HTMLResponse)
async def proactive_line_clone(
    request: Request,
    line_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """One-tap "Looking again" clone: copy the line's source part into a fresh OPEN
    requisition (clone_parts_to_active), stamp produced_requisition_id, fire a
    background sightings search on the clone, and re-render the row cells."""
    from ...dependencies import is_manager_or_admin
    from ...routers.sightings import run_search_and_publish
    from ...services.proactive_digest import clone_line_to_active

    can_manage = is_manager_or_admin(user)
    try:
        line, new_req = clone_line_to_active(db, line_id, viewer=user, can_manage=can_manage)
    except ValueError as e:
        raise HTTPException(403 if "Not your" in str(e) else 400, str(e)) from e

    new_requirement_ids = [r.id for r in new_req.requirements]
    if new_requirement_ids:
        background_tasks.add_task(run_search_and_publish, new_requirement_ids, user.id)

    company = db.get(Company, line.company_id) if line.company_id else None
    ctx = _base_ctx(request, user, "proactive")
    ctx["line"] = {
        "id": line.id,
        "mpn": line.mpn,
        "company_name": company.name if company else None,
        "contacted": line.contacted,
        "outcome": line.outcome,
        "sales_order_number": line.sales_order_number,
        "produced_requisition_id": line.produced_requisition_id,
        "can_edit": can_manage or line.salesperson_id == user.id,
    }
    response = template_response("htmx/partials/proactive/_outreach_line_cells.html", ctx)
    response.headers["HX-Trigger"] = json.dumps(
        {"showToast": {"message": f"Part cloned → REQ-{new_req.id:03d} (search running)", "type": "success"}}
    )
    return response


@router.post("/v2/partials/proactive/equivalence/verdict", response_class=HTMLResponse)
async def proactive_equivalence_verdict(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """The double-check affordance: a human confirms or rejects an AI variant pair.

    A 'different' verdict permanently un-pools the pair (outranks the AI)."""
    from ...services.part_equivalence import record_human_verdict

    form = await request.form()
    part_a = str(form.get("part_a") or "").strip()
    part_b = str(form.get("part_b") or "").strip()
    verdict = str(form.get("verdict") or "").strip().lower()
    try:
        record_human_verdict(db, user, part_a, part_b, verdict)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    word = "confirmed the same part" if verdict == "same" else "marked as different parts"
    # origin=search (idea #21): the demote came from the search banner / dossier
    # chip — swap just that chip with a tiny confirmation instead of teleporting
    # the user into the Proactive Matches tab.
    if str(form.get("origin") or "").strip() == "search":
        return HTMLResponse(
            '<span class="inline-flex items-center rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 '
            'text-[11px] text-gray-400">removed — won&#39;t pool again</span>'
        )
    return _render_matches_tab(request, user, db, success_msg=f"{part_a} / {part_b} {word}.")


# ── Process flow: check send/ignore → stage per-customer drafts → review → send ──


@router.post("/v2/partials/proactive/process", response_class=HTMLResponse)
async def proactive_process(
    request: Request,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Process the checked matches: dismiss the ignores, stage one draft offer
    per customer for review. Nothing is emailed here."""
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_service import process_matches

    form = await request.form()
    send_ids = [int(x) for x in form.getlist("send_ids") if str(x).isdigit()]
    ignore_ids = [int(x) for x in form.getlist("ignore_ids") if str(x).isdigit()]
    if not send_ids and not ignore_ids:
        return _render_matches_tab(request, user, db, scope=form.get("scope", "mine"))

    stats = process_matches(db, user, send_ids=send_ids, ignore_ids=ignore_ids, allow_all=is_manager_or_admin(user))
    db.commit()

    parts = []
    if stats.get("drafts_created"):
        parts.append(f"{stats['drafts_created']} offer(s) prepared for review")
    if stats.get("ignored"):
        parts.append(f"{stats['ignored']} ignored")
    if stats.get("needs_contact"):
        parts.append(f"{stats['needs_contact']} need a contact")
    if stats.get("skipped_backorder"):
        parts.append(f"{stats['skipped_backorder']} back-order line(s) skipped (no customer account)")
    if stats.get("skipped_already_queued"):
        parts.append(f"{stats['skipped_already_queued']} already staged")
    logger.info("Proactive process by {}: {}", user.email, stats)
    return _render_matches_tab(request, user, db, scope=form.get("scope", "mine"), success_msg="; ".join(parts) + ".")


@router.post("/v2/partials/proactive/prepared/{po_id}/send", response_class=HTMLResponse)
async def proactive_prepared_send(
    request: Request,
    po_id: int,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Send one staged per-customer draft from the review strip."""
    from ...dependencies import is_manager_or_admin
    from ...scheduler import get_valid_token
    from ...services.proactive_service import send_draft_offer

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(400, "No valid Microsoft token — sign in again to send")
    form = await request.form()
    confirm_ai_variants = str(form.get("confirm_ai_variants") or "") == "1"
    try:
        result = await send_draft_offer(
            db,
            user,
            token,
            po_id,
            allow_all=is_manager_or_admin(user),
            confirm_ai_variants=confirm_ai_variants,
        )
    except ValueError as e:
        raise HTTPException(403 if "Not your" in str(e) else 400, str(e)) from e
    recipients = ", ".join(result.get("recipient_emails", []))
    return _render_matches_tab(request, user, db, success_msg=f"Offer sent to {recipients}.")


@router.post("/v2/partials/proactive/prepared/{po_id}/discard", response_class=HTMLResponse)
async def proactive_prepared_discard(
    request: Request,
    po_id: int,
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Discard a staged draft; its matches stay in the list (nothing was sent)."""
    from ...dependencies import is_manager_or_admin
    from ...services.proactive_service import discard_draft_offer

    try:
        discard_draft_offer(db, user, po_id, allow_all=is_manager_or_admin(user))
    except ValueError as e:
        raise HTTPException(403 if "Not your" in str(e) else 400, str(e)) from e
    db.commit()
    return _render_matches_tab(request, user, db, success_msg="Draft discarded — matches back in the list.")
