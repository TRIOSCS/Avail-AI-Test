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

from fastapi import APIRouter, Depends, HTTPException, Request
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


# ── Proactive Part Match ─────────────────────────────────────────────


@router.get("/v2/partials/proactive", response_class=HTMLResponse)
async def proactive_list_partial(
    request: Request,
    tab: str = "matches",
    user: User = Depends(require_access(AccessKey.PROACTIVE)),
    db: Session = Depends(get_db),
):
    """Proactive matches list partial — shows matches and sent offers."""
    from ...services.proactive_service import get_matches_for_user, get_sent_offers

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
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/refresh", response_class=HTMLResponse)
async def proactive_refresh(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger a proactive scan then return the matches list partial."""
    from ...services.proactive_matching import run_proactive_scan
    from ...services.proactive_service import get_matches_for_user

    await asyncio.to_thread(run_proactive_scan, db)

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    groups = result.get("groups", []) if isinstance(result, dict) else result
    match_count = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0

    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = groups
    ctx["sent"] = []
    ctx["tab"] = "matches"
    ctx["match_count"] = match_count
    ctx["success_msg"] = ""
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/batch-dismiss", response_class=HTMLResponse)
async def proactive_batch_dismiss(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Batch dismiss selected proactive matches and reload the list."""
    from ...models import ProactiveMatch

    form = await request.form()
    match_ids_raw = form.getlist("match_ids")
    match_ids = [int(mid) for mid in match_ids_raw if mid and str(mid).isdigit()]

    if match_ids:
        db.query(ProactiveMatch).filter(
            ProactiveMatch.id.in_(match_ids),
            ProactiveMatch.salesperson_id == user.id,
            ProactiveMatch.status == ProactiveMatchStatus.NEW,
        ).update(
            {"status": ProactiveMatchStatus.DISMISSED, "dismiss_reason": "batch_dismiss"}, synchronize_session=False
        )
        db.commit()

    # Re-render list
    from ...services.proactive_service import get_matches_for_user

    result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
    groups = result.get("groups", []) if isinstance(result, dict) else result
    match_count = result.get("stats", {}).get("total", 0) if isinstance(result, dict) else 0
    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = groups
    ctx["sent"] = []
    ctx["tab"] = "matches"
    ctx["match_count"] = match_count
    ctx["success_msg"] = ""
    return template_response("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/proactive/prepare/{site_id}", response_class=HTMLResponse)
async def proactive_prepare_page(
    site_id: int,
    request: Request,
    user: User = Depends(require_user),
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

    matches = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.id.in_(match_ids), ProactiveMatch.salesperson_id == user.id)
        .all()
    )
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

    match_data = []
    for m in matches:
        offer = m.offer
        match_data.append(
            {
                "id": m.id,
                "mpn": m.mpn,
                "vendor_name": offer.vendor_name if offer else "",
                "manufacturer": offer.manufacturer if offer else "",
                "qty_available": offer.qty_available if offer else 0,
                "unit_price": float(offer.unit_price) if offer and offer.unit_price else None,
                "margin_pct": m.margin_pct,
                "match_score": m.match_score or 0,
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
            "match_ids_json": json.dumps([m["id"] for m in match_data]),
            "contacts": contact_data,
            "error_msg": "",
        }
    )
    return template_response("htmx/partials/proactive/prepare.html", ctx)


@router.post("/v2/partials/proactive/draft", response_class=HTMLResponse)
async def proactive_draft_for_prepare(
    request: Request,
    user: User = Depends(require_user),
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

    matches = (
        db.query(ProactiveMatch)
        .filter(ProactiveMatch.id.in_(match_ids), ProactiveMatch.salesperson_id == user.id)
        .all()
    )
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
    parts = []
    for m in matches:
        offer = m.offer
        cost = float(offer.unit_price) if offer and offer.unit_price else 0
        sell = draft_sell_prices.get(str(m.id), cost * 1.3)
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

    salesperson_name = user.name or user.email.split("@")[0]

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
    user: User = Depends(require_user),
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

        result = await send_proactive_offer(
            db=db,
            user=user,
            token=token or "no-token",
            match_ids=match_ids,
            contact_ids=contact_ids,
            sell_prices=sell_prices,
            subject=subject or None,
            email_html=email_html,
        )

        # Success — reload matches list with success banner
        from ...services.proactive_service import get_matches_for_user

        match_result = get_matches_for_user(db, user.id, status=ProactiveMatchStatus.NEW)
        groups = match_result.get("groups", []) if isinstance(match_result, dict) else match_result
        match_count = match_result.get("stats", {}).get("total", 0) if isinstance(match_result, dict) else 0
        parts_count = len(result.get("line_items", []))
        contacts_count = len(result.get("recipient_emails", []))

        ctx = _base_ctx(request, user, "proactive")
        ctx["matches"] = groups
        ctx["sent"] = []
        ctx["tab"] = "matches"
        ctx["match_count"] = match_count
        ctx["success_msg"] = f"Offer sent to {contacts_count} contact(s) ({parts_count} parts)."
        return template_response("htmx/partials/proactive/list.html", ctx)

    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as exc:
        logger.error("Proactive send failed: {}", exc)
        raise HTTPException(500, "Send failed. Please try again or contact support.")


# ── Sprint 8: Proactive Selling + Prospecting Completion (legacy routes kept for compat) ──


@router.post("/v2/partials/proactive/{match_id}/send", response_class=HTMLResponse)
async def proactive_send_legacy(
    request: Request,
    match_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send a proactive offer email."""
    from ...models import ProactiveMatch

    match = (
        db.query(ProactiveMatch).filter(ProactiveMatch.id == match_id, ProactiveMatch.salesperson_id == user.id).first()
    )
    if not match:
        raise HTTPException(404, "Match not found")

    form = await request.form()
    body = form.get("body", "").strip()
    if not body:
        raise HTTPException(400, "Email body is required")

    # Mark as sent
    match.status = ProactiveMatchStatus.SENT
    db.commit()
    logger.info("Proactive match {} sent by {}", match_id, user.email)

    # Redirect to list with success message (send_success.html removed in redesign)
    return template_response(
        "htmx/partials/proactive/list.html",
        _base_ctx(request, user, "proactive")
        | {
            "matches": [],
            "sent": [],
            "tab": "matches",
            "match_count": 0,
            "success_msg": f"Offer for {match.mpn} marked as sent",
        },
    )


@router.post("/v2/partials/proactive/{offer_id}/convert", response_class=HTMLResponse)
async def proactive_convert(
    request: Request,
    offer_id: int,
    user: User = Depends(require_user),
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
            raise HTTPException(409, "This offer has already been converted.")
        raise HTTPException(403, str(exc))
    except Exception as exc:
        logger.error("Proactive conversion failed: {}", exc)
        raise HTTPException(500, "Conversion failed. Please try again.")


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
    except (ImportError, RuntimeError, Exception):
        stats = {"total_sent": 0, "total_converted": 0, "conversion_rate": 0, "total_revenue": 0}

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
    user: User = Depends(require_user),
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
    except (ValueError, TypeError):
        raise HTTPException(400, "company_id must be an integer")

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
