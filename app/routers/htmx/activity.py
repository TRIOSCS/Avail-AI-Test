"""routers/htmx/activity.py — Activity tracking and enrichment HTMX partials.

Handles activity timeline views for companies/vendors/contacts,
click-to-call logging, and on-demand entity enrichment.

Called by: htmx router package (imported via htmx_views.py)
Depends on: services.activity_service, services.enrichment_orchestrator,
            models (ActivityLog, Company, VendorCard, User)
"""

from html import escape

from fastapi import Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import ActivityLog, Company, User, VendorCard
from ...services import activity_service
from ._helpers import _timesince_filter, router

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


_CHANNEL_ICONS = {
    "phone": '<svg class="w-4 h-4 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg>',
    "email": '<svg class="w-4 h-4 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>',
    "manual": '<svg class="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>',
    "system": '<svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>',
}


def _icon_for(channel: str | None) -> str:
    """Return an SVG icon string for the given activity channel."""
    return _CHANNEL_ICONS.get(channel or "", _CHANNEL_ICONS["manual"])


def _activity_row(a: ActivityLog) -> str:
    """Render a single activity log row as an HTML <tr>."""
    icon = _icon_for(a.channel)
    contact = escape(a.contact_name or "")
    summary = escape(a.summary or a.subject or a.notes or "")
    time_str = _timesince_filter(a.created_at)
    return (
        f'<tr class="hover:bg-brand-50">'
        f'<td class="px-4 py-2">{icon}</td>'
        f'<td class="px-4 py-2 text-sm">{contact}</td>'
        f'<td class="px-4 py-2 text-sm text-gray-500">{summary}</td>'
        f'<td class="px-4 py-2 text-sm text-gray-500">{time_str}</td>'
        f"</tr>"
    )


def _activity_table(activities: list[ActivityLog], empty_msg: str = "No activity recorded") -> str:
    """Wrap activity rows in a full HTML table, or show empty state."""
    if not activities:
        return (
            '<div class="text-center text-gray-400 py-8">'
            f"{escape(empty_msg)}"
            "</div>"
        )
    rows = "\n".join(_activity_row(a) for a in activities)
    return (
        '<table class="w-full text-left">'
        "<thead>"
        '<tr class="border-b">'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">Type</th>'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">Contact</th>'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">Summary</th>'
        '<th class="px-4 py-2 text-xs text-gray-500 uppercase">When</th>'
        "</tr>"
        "</thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


# ═══════════════════════════════════════════════════════════════════════
#  1. POST /partials/activity/call — click-to-call logging
# ═══════════════════════════════════════════════════════════════════════


@router.post("/partials/activity/call", response_class=HTMLResponse)
def log_call_click(
    request: Request,
    phone_number: str = Form(...),
    company_id: int | None = Form(None),
    vendor_card_id: int | None = Form(None),
    origin: str | None = Form(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a click-to-call event. Routes to the appropriate service function
    depending on whether a company_id or vendor_card_id is provided."""
    try:
        if company_id:
            activity_service.log_company_call(
                user_id=user.id,
                company_id=company_id,
                direction="outbound",
                phone=phone_number,
                duration_seconds=None,
                contact_name=None,
                notes=f"Click-to-call from {origin}" if origin else "Click-to-call",
                db=db,
            )
        elif vendor_card_id:
            activity_service.log_vendor_call(
                user_id=user.id,
                vendor_card_id=vendor_card_id,
                vendor_contact_id=None,
                direction="outbound",
                phone=phone_number,
                duration_seconds=None,
                contact_name=None,
                notes=f"Click-to-call from {origin}" if origin else "Click-to-call",
                db=db,
            )
        else:
            activity_service.log_call_activity(
                user_id=user.id,
                direction="outbound",
                phone=phone_number,
                duration_seconds=None,
                external_id=None,
                contact_name=None,
                db=db,
            )
        db.commit()
        logger.info("Click-to-call logged for phone={} by user={}", phone_number, user.id)
    except Exception:
        db.rollback()
        logger.exception("Failed to log click-to-call for phone={}", phone_number)
        return HTMLResponse(
            '<span class="text-red-600 text-xs">Failed to log call</span>',
            status_code=500,
        )

    return HTMLResponse(
        '<span class="inline-flex items-center gap-1 text-green-700 text-xs">'
        '<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">'
        '<path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>'
        "</svg>"
        "Call logged"
        "</span>"
    )


# ═══════════════════════════════════════════════════════════════════════
#  2. GET /partials/companies/{company_id}/tab/activity — company timeline
# ═══════════════════════════════════════════════════════════════════════


@router.get("/partials/companies/{company_id}/tab/activity", response_class=HTMLResponse)
def company_activity_tab(
    request: Request,
    company_id: int,
    channel: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return HTML table of activity for a company."""
    company = db.get(Company, company_id)
    if not company:
        return HTMLResponse('<div class="text-red-600 p-4">Company not found</div>', status_code=404)

    channel_list = [channel] if channel else None
    activities, total = activity_service.get_account_timeline(
        db=db,
        company_id=company_id,
        channel=channel_list,
        limit=limit,
        offset=offset,
    )

    html = _activity_table(activities, empty_msg="No activity recorded for this company")

    # Pagination hint
    if total > offset + limit:
        next_offset = offset + limit
        html += (
            f'<div class="text-center py-2">'
            f'<button hx-get="/partials/companies/{company_id}/tab/activity?limit={limit}&offset={next_offset}'
            f'{"&channel=" + escape(channel) if channel else ""}"'
            f' hx-target="closest div" hx-swap="outerHTML"'
            f' class="text-brand-600 text-sm hover:underline">Load more</button>'
            f"</div>"
        )

    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════
#  3. GET /partials/vendors/{vendor_id}/activity — vendor timeline
# ═══════════════════════════════════════════════════════════════════════


@router.get("/partials/vendors/{vendor_id}/activity", response_class=HTMLResponse)
def vendor_activity_tab(
    request: Request,
    vendor_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return HTML table of activity for a vendor card."""
    vendor = db.get(VendorCard, vendor_id)
    if not vendor:
        return HTMLResponse('<div class="text-red-600 p-4">Vendor not found</div>', status_code=404)

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.vendor_card_id == vendor_id)
        .order_by(ActivityLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    html = _activity_table(activities, empty_msg="No activity recorded for this vendor")
    return HTMLResponse(html)


# ═══════════════════════════════════════════════════════════════════════
#  4. GET /partials/contacts/{contact_id}/timeline — contact timeline
# ═══════════════════════════════════════════════════════════════════════


@router.get("/partials/contacts/{contact_id}/timeline", response_class=HTMLResponse)
def contact_timeline(
    request: Request,
    contact_id: int,
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return HTML timeline for a specific contact (site or vendor contact)."""
    activities = (
        db.query(ActivityLog)
        .filter(
            or_(
                ActivityLog.site_contact_id == contact_id,
                ActivityLog.vendor_contact_id == contact_id,
            )
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )

    if not activities:
        return HTMLResponse(
            '<div class="text-center text-gray-400 py-8">No activity recorded for this contact</div>'
        )

    items = []
    for a in activities:
        icon = _icon_for(a.channel)
        summary = escape(a.summary or a.subject or a.notes or "")
        time_str = _timesince_filter(a.created_at)
        items.append(
            f'<li class="flex items-start gap-3 py-2 border-b border-gray-100">'
            f'<span class="mt-1">{icon}</span>'
            f'<div class="flex-1">'
            f'<p class="text-sm">{summary}</p>'
            f'<p class="text-xs text-gray-400">{time_str}</p>'
            f"</div>"
            f"</li>"
        )

    return HTMLResponse(f'<ul class="divide-y">{"".join(items)}</ul>')


# ═══════════════════════════════════════════════════════════════════════
#  5. POST /partials/companies/{company_id}/enrich — company enrichment
# ═══════════════════════════════════════════════════════════════════════


@router.post("/partials/companies/{company_id}/enrich", response_class=HTMLResponse)
async def enrich_company(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger on-demand enrichment for a company and return an HTML result card."""
    company = db.get(Company, company_id)
    if not company:
        return HTMLResponse('<div class="text-red-600 p-4">Company not found</div>', status_code=404)

    try:
        from ...services.enrichment_orchestrator import enrich_on_demand

        result = await enrich_on_demand("company", company_id, db)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Enrichment failed for company {}", company_id)
        return HTMLResponse(
            f'<div class="rounded-lg border border-red-200 bg-red-50 p-4">'
            f'<p class="text-red-700 font-medium">Enrichment failed</p>'
            f'<p class="text-red-600 text-sm mt-1">{escape(str(exc))}</p>'
            f"</div>",
            status_code=500,
        )

    if "error" in result:
        return HTMLResponse(
            f'<div class="rounded-lg border border-yellow-200 bg-yellow-50 p-4">'
            f'<p class="text-yellow-700 text-sm">{escape(result["error"])}</p>'
            f"</div>",
            status_code=404,
        )

    return HTMLResponse(_enrichment_result_card(result))


# ═══════════════════════════════════════════════════════════════════════
#  6. POST /partials/vendors/{vendor_id}/enrich — vendor enrichment
# ═══════════════════════════════════════════════════════════════════════


@router.post("/partials/vendors/{vendor_id}/enrich", response_class=HTMLResponse)
async def enrich_vendor(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger on-demand enrichment for a vendor and return an HTML result card."""
    vendor = db.get(VendorCard, vendor_id)
    if not vendor:
        return HTMLResponse('<div class="text-red-600 p-4">Vendor not found</div>', status_code=404)

    try:
        from ...services.enrichment_orchestrator import enrich_on_demand

        result = await enrich_on_demand("vendor", vendor_id, db)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Enrichment failed for vendor {}", vendor_id)
        return HTMLResponse(
            f'<div class="rounded-lg border border-red-200 bg-red-50 p-4">'
            f'<p class="text-red-700 font-medium">Enrichment failed</p>'
            f'<p class="text-red-600 text-sm mt-1">{escape(str(exc))}</p>'
            f"</div>",
            status_code=500,
        )

    if "error" in result:
        return HTMLResponse(
            f'<div class="rounded-lg border border-yellow-200 bg-yellow-50 p-4">'
            f'<p class="text-yellow-700 text-sm">{escape(result["error"])}</p>'
            f"</div>",
            status_code=404,
        )

    return HTMLResponse(_enrichment_result_card(result))


# ═══════════════════════════════════════════════════════════════════════
#  Enrichment result card builder
# ═══════════════════════════════════════════════════════════════════════


def _enrichment_result_card(result: dict) -> str:
    """Build an HTML card summarising enrichment results."""
    sources_fired = result.get("sources_fired", 0)
    sources_used = len(result.get("sources_used", []))
    applied = result.get("applied", {})
    rejected = result.get("rejected", {})

    # Header
    html = (
        '<div class="rounded-lg border border-green-200 bg-green-50 p-4">'
        '<p class="font-medium text-green-800">Enrichment complete</p>'
        f'<p class="text-sm text-green-700 mt-1">'
        f"{sources_used} of {sources_fired} sources returned data"
        f"</p>"
    )

    # Applied fields
    if applied:
        html += '<div class="mt-3"><p class="text-xs font-semibold text-green-700 uppercase">Applied fields</p><ul class="mt-1 space-y-1">'
        for field, value in applied.items():
            display_val = escape(str(value)) if value is not None else ""
            if len(display_val) > 80:
                display_val = display_val[:80] + "..."
            html += (
                f'<li class="text-sm flex justify-between">'
                f'<span class="text-gray-700">{escape(field)}</span>'
                f'<span class="text-green-700 font-medium">{display_val}</span>'
                f"</li>"
            )
        html += "</ul></div>"

    # Rejected fields
    if rejected:
        html += '<div class="mt-3"><p class="text-xs font-semibold text-yellow-700 uppercase">Rejected (low confidence)</p><ul class="mt-1 space-y-1">'
        for field, info in rejected.items():
            reason = escape(str(info)) if not isinstance(info, dict) else escape(info.get("reason", "low confidence"))
            html += (
                f'<li class="text-sm text-yellow-600">'
                f"{escape(field)}: {reason}"
                f"</li>"
            )
        html += "</ul></div>"

    # No data case
    if not applied and not rejected:
        html += '<p class="text-sm text-gray-500 mt-2">No new fields to apply.</p>'

    html += "</div>"
    return html
