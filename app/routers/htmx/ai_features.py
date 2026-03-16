"""routers/htmx/ai_features.py — AI-powered feature endpoints for HTMX frontend.

Surfaces AI contact discovery, email parsing, company intelligence,
and RFQ draft generation as HTMX partials.

Called by: htmx router package
Depends on: services.ai_service, services.response_parser, config.settings
"""

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...dependencies import require_user
from ...models import (
    Company,
    ProspectContact,
    Requirement,
    Requisition,
    User,
    VendorCard,
)
from ._helpers import _base_ctx, router, templates


def _ai_enabled(user: User) -> bool:
    """Check if AI features are enabled for this user."""
    flag = settings.ai_features_enabled
    if flag == "off":
        return False
    if flag == "all":
        return True
    if flag == "mike_only":
        allowed = {
            str(e).strip().lower()
            for e in (settings.admin_emails or [])
            if str(e).strip()
        }
        if not allowed:
            return False
        return (user.email or "").strip().lower() in allowed
    return False


def _error_html(message: str) -> HTMLResponse:
    """Return a styled error fragment for HTMX swaps."""
    html = (
        '<div class="ai-error" role="alert" '
        'style="padding:0.75rem;border:1px solid #e74c3c;border-radius:6px;'
        'background:#fdf0ef;color:#c0392b;margin:0.5rem 0;">'
        f"<strong>AI Error:</strong> {message}</div>"
    )
    return HTMLResponse(content=html, status_code=200)


# ── 1. Find contacts for a vendor via AI ─────────────────────────────


@router.post(
    "/v2/partials/vendors/{vendor_id}/find-contacts",
    response_class=HTMLResponse,
)
async def htmx_ai_find_contacts(
    vendor_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger AI contact search for a vendor, return HTML results card."""
    if not _ai_enabled(user):
        return _error_html("AI features are not enabled for your account.")

    card = db.query(VendorCard).filter(VendorCard.id == vendor_id).first()
    if not card:
        raise HTTPException(404, "Vendor not found")

    try:
        from ...services.ai_service import enrich_contacts_websearch

        contacts = await enrich_contacts_websearch(
            company_name=card.display_name,
            domain=card.domain,
            limit=5,
        )
    except Exception as exc:
        logger.error("AI contact search failed for vendor {}: {}", vendor_id, exc)
        return _error_html("Contact search failed. Please try again later.")

    # Save as ProspectContacts
    saved = []
    for c in contacts:
        pc = ProspectContact(
            vendor_card_id=card.id,
            full_name=c["full_name"],
            title=c.get("title"),
            email=c.get("email"),
            phone=c.get("phone"),
            linkedin_url=c.get("linkedin_url"),
            source=c.get("source", "web_search"),
            confidence=c.get("confidence", "low"),
        )
        db.add(pc)
        db.flush()
        saved.append(pc)
    db.commit()

    ctx = _base_ctx(request, user)
    ctx.update({"contacts": saved, "vendor": card, "total": len(saved)})
    return templates.TemplateResponse(
        "htmx/partials/ai/contact_results.html", ctx
    )


# ── 2. List AI-discovered prospect contacts ──────────────────────────


@router.get(
    "/v2/partials/ai/prospect-contacts",
    response_class=HTMLResponse,
)
async def htmx_list_prospect_contacts(
    request: Request,
    vendor_id: int = Query(0),
    company_id: int = Query(0),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List AI-discovered prospect contacts as an HTML table."""
    query = db.query(ProspectContact)
    if vendor_id:
        query = query.filter(ProspectContact.vendor_card_id == vendor_id)
    elif company_id:
        query = query.filter(ProspectContact.customer_site_id == company_id)

    contacts = (
        query.order_by(ProspectContact.created_at.desc()).limit(limit).all()
    )

    ctx = _base_ctx(request, user)
    ctx.update({
        "contacts": contacts,
        "total": len(contacts),
        "vendor_id": vendor_id,
        "company_id": company_id,
    })
    return templates.TemplateResponse(
        "htmx/partials/ai/prospect_contacts_table.html", ctx
    )


# ── 3. Promote prospect contact to real contact ─────────────────────


@router.post(
    "/v2/partials/ai/prospect-contacts/{contact_id}/promote",
    response_class=HTMLResponse,
)
async def htmx_promote_prospect_contact(
    contact_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a prospect contact to VendorContact or SiteContact."""
    from ...services.ai_offer_service import (
        promote_prospect_contact as _promote,
    )

    try:
        result = _promote(db, contact_id, user.id)
        db.commit()
    except ValueError as exc:
        logger.warning("Promote prospect {} failed: {}", contact_id, exc)
        return _error_html(str(exc))
    except Exception as exc:
        logger.error("Promote prospect {} error: {}", contact_id, exc)
        return _error_html("Failed to promote contact. Please try again.")

    promoted_type = result.get("promoted_to_type", "contact")
    html = (
        '<div class="ai-success" '
        'style="padding:0.75rem;border:1px solid #27ae60;border-radius:6px;'
        'background:#eafaf1;color:#1e8449;margin:0.5rem 0;">'
        f"Contact promoted to <strong>{promoted_type}</strong> successfully."
        "</div>"
    )
    return HTMLResponse(
        content=html,
        status_code=200,
        headers={"HX-Trigger": "contactPromoted"},
    )


# ── 4. Parse vendor email reply into offers ──────────────────────────


@router.post(
    "/v2/partials/ai/parse-email",
    response_class=HTMLResponse,
)
async def htmx_parse_email(
    request: Request,
    email_body: str = Form(...),
    email_subject: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
):
    """Parse a vendor email reply into structured offer data via AI."""
    if not _ai_enabled(user):
        return _error_html("AI features are not enabled for your account.")

    if not email_body.strip():
        return _error_html("Email body is required.")

    try:
        from ...services.ai_email_parser import parse_email

        result = await parse_email(
            email_body=email_body,
            email_subject=email_subject,
            vendor_name=vendor_name,
        )
    except Exception as exc:
        logger.error("AI email parse failed: {}", exc)
        return _error_html("Email parsing failed. Please try again later.")

    if not result:
        return _error_html("Could not extract any offer data from the email.")

    quotes = result.get("quotes", [])
    confidence = result.get("overall_confidence", 0)
    email_type = result.get("email_type", "unclear")
    vendor_notes = result.get("vendor_notes")

    ctx = _base_ctx(request, user)
    ctx.update({
        "quotes": quotes,
        "confidence": confidence,
        "email_type": email_type,
        "vendor_notes": vendor_notes,
        "total_quotes": len(quotes),
    })
    return templates.TemplateResponse(
        "htmx/partials/ai/parsed_email.html", ctx
    )


# ── 5. Company intelligence brief ────────────────────────────────────


@router.get(
    "/v2/partials/companies/{company_id}/intel",
    response_class=HTMLResponse,
)
async def htmx_company_intel(
    company_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate and display an AI company intelligence brief."""
    if not _ai_enabled(user):
        return _error_html("AI features are not enabled for your account.")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(404, "Company not found")

    try:
        from ...services.ai_service import company_intel

        intel = await company_intel(
            company_name=company.name,
            domain=company.domain,
        )
    except Exception as exc:
        logger.error("AI company intel failed for {}: {}", company_id, exc)
        return _error_html(
            "Could not generate company intelligence. Please try again."
        )

    if not intel:
        return _error_html(
            "No intelligence data available for this company."
        )

    ctx = _base_ctx(request, user)
    ctx.update({"company": company, "intel": intel})
    return templates.TemplateResponse(
        "htmx/partials/ai/company_intel.html", ctx
    )


# ── 6. AI-drafted RFQ email ─────────────────────────────────────────


@router.post(
    "/v2/partials/requisitions/{req_id}/ai-draft-rfq",
    response_class=HTMLResponse,
)
async def htmx_ai_draft_rfq(
    req_id: int,
    request: Request,
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate an AI-drafted RFQ email for a requisition."""
    if not _ai_enabled(user):
        return _error_html("AI features are not enabled for your account.")

    requisition = (
        db.query(Requisition).filter(Requisition.id == req_id).first()
    )
    if not requisition:
        raise HTTPException(404, "Requisition not found")

    requirements = (
        db.query(Requirement)
        .filter(Requirement.requisition_id == req_id)
        .all()
    )
    parts = [
        {
            "mpn": r.primary_mpn or "Unknown",
            "qty": r.target_qty or 1,
            "target_price": float(r.target_price) if r.target_price else None,
        }
        for r in requirements
    ]

    if not parts:
        return _error_html("Requisition has no line items to quote.")

    try:
        from ...services.ai_service import draft_rfq

        body = await draft_rfq(
            vendor_name=vendor_name or "Vendor",
            parts=parts,
            user_name=user.name or "",
        )
    except Exception as exc:
        logger.error("AI RFQ draft failed for req {}: {}", req_id, exc)
        return _error_html("RFQ draft generation failed. Please try again.")

    if not body:
        return _error_html("AI could not generate an RFQ draft.")

    ctx = _base_ctx(request, user)
    ctx.update({
        "draft_body": body,
        "vendor_name": vendor_name,
        "requisition": requisition,
        "parts": parts,
    })
    return templates.TemplateResponse(
        "htmx/partials/ai/rfq_draft.html", ctx
    )
