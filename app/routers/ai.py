"""
ai.py — AI Intelligence Layer Router

AI-powered features: contact enrichment, vendor response parsing,
company intelligence cards, and smart RFQ drafts.

Business Rules:
- AI features gated by settings.ai_features_enabled (off/mike_only/all)
- Contact enrichment uses Apollo → Claude web search fallback
- Response parsing confidence thresholds: 80%+ auto, 50-80% review, <50% raw

Called by: main.py (router mount)
Depends on: services/ai_service.py, services/response_parser.py, connectors/apollo_client.py
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import is_admin as _is_admin
from ..dependencies import require_user
from ..models import (
    Contact,
    CustomerSite,
    Offer,
    ProspectContact,
    Requirement,
    User,
    VendorCard,
    VendorResponse,
)
from ..schemas.ai import (
    CompareQuotesRequest,
    NormalizePartsRequest,
    ParseEmailRequest,
    ProspectContactSave,
    ProspectFinderRequest,
    RfqDraftEmailRequest,
    RfqDraftRequest,
    SaveDraftOffersRequest,
)
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["ai"])


# ── Helpers ──────────────────────────────────────────────────────────────


def _ai_enabled(user: User) -> bool:
    """Check if AI features are enabled for this user."""
    flag = settings.ai_features_enabled
    if flag == "off":
        return False
    # "mike_only" and "all" both allow any authenticated user for now
    return True


def _build_vendor_history(vendor_name: str, db: Session) -> dict:
    """Gather vendor history from AVAIL for smart RFQ context."""
    norm = normalize_vendor_name(vendor_name)

    card = db.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
    if not card:
        return {}

    from sqlalchemy import func

    # Single combined query for rfq count, offer count, and last contact date
    total_rfqs = (
        db.query(func.count(Contact.id))
        .filter(
            Contact.vendor_name.ilike(f"%{vendor_name}%"),
            Contact.contact_type == "email",
        )
        .scalar()
    ) or 0

    # Combine offer count + last contact into parallel-style queries
    total_offers = (
        db.query(func.count(Offer.id))
        .filter(Offer.vendor_name.ilike(f"%{vendor_name}%"))
        .scalar()
    ) or 0

    last_contact_date = (
        db.query(func.max(Contact.created_at))
        .filter(Contact.vendor_name.ilike(f"%{vendor_name}%"))
        .scalar()
    )

    return {
        "total_rfqs": total_rfqs,
        "total_offers": total_offers,
        "last_contact_date": last_contact_date.strftime("%Y-%m-%d") if last_contact_date else None,
        "avg_response_hours": card.response_velocity_hours,
        "engagement_score": card.engagement_score,
    }


# ── Feature 1: Contact Enrichment ────────────────────────────────────────


from ..rate_limit import limiter


@router.post("/api/ai/find-contacts")
@limiter.limit("10/minute")
async def ai_find_contacts(
    payload: ProspectFinderRequest,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Find contacts at a company or vendor using Apollo + web search fallback."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    entity_type = payload.entity_type
    entity_id = payload.entity_id
    title_keywords = payload.title_keywords

    company_name = ""
    domain = None
    site_id = None
    vendor_card_id = None

    if entity_type in ("company", "site") and entity_id:
        site = db.query(CustomerSite).filter(CustomerSite.id == entity_id).first()
        if site:
            company_name = site.company.name if site.company else site.site_name
            domain = site.company.domain if site.company else None
            site_id = site.id
    elif entity_type == "vendor" and entity_id:
        card = db.query(VendorCard).filter(VendorCard.id == entity_id).first()
        if card:
            company_name = card.display_name
            domain = card.domain
            vendor_card_id = card.id

    if not company_name:
        # No company resolved from entity_type/entity_id — will fail below
        pass

    if not company_name:
        raise HTTPException(400, "company_name or entity_id required")

    from app.connectors.apollo_client import search_contacts as apollo_search

    apollo_results = await apollo_search(company_name, domain, title_keywords, limit=5)

    web_results = []
    if len(apollo_results) < 3:
        from app.services.ai_service import enrich_contacts_websearch

        web_results = await enrich_contacts_websearch(
            company_name, domain, title_keywords, limit=5
        )

    seen_emails: set[str] = set()
    merged = []
    for c in apollo_results + web_results:
        email = (c.get("email") or "").lower()
        key = email if email else c.get("full_name", "").lower()
        if key and key not in seen_emails:
            seen_emails.add(key)
            merged.append(c)

    saved_ids = []
    for c in merged:
        pc = ProspectContact(
            customer_site_id=site_id,
            vendor_card_id=vendor_card_id,
            full_name=c["full_name"],
            title=c.get("title"),
            email=c.get("email"),
            email_status=c.get("email_status"),
            phone=c.get("phone"),
            linkedin_url=c.get("linkedin_url"),
            source=c.get("source", "unknown"),
            confidence=c.get("confidence", "low"),
        )
        db.add(pc)
        db.flush()
        saved_ids.append(pc.id)
        c["id"] = pc.id

    db.commit()
    return {"contacts": merged, "total": len(merged), "saved_ids": saved_ids}


@router.get("/api/ai/prospect-contacts")
async def list_prospect_contacts(
    entity_type: str = "",
    entity_id: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List enriched contacts for a company site or vendor."""
    q = db.query(ProspectContact)
    if entity_type == "company" and entity_id:
        q = q.filter(ProspectContact.customer_site_id == entity_id)
    elif entity_type == "vendor" and entity_id:
        q = q.filter(ProspectContact.vendor_card_id == entity_id)
    else:
        raise HTTPException(400, "entity_type and entity_id required")

    contacts = q.order_by(ProspectContact.created_at.desc()).limit(50).all()
    return [
        {
            "id": c.id,
            "full_name": c.full_name,
            "title": c.title,
            "email": c.email,
            "email_status": c.email_status,
            "phone": c.phone,
            "linkedin_url": c.linkedin_url,
            "source": c.source,
            "confidence": c.confidence,
            "is_saved": c.is_saved,
            "found_at": c.found_at.isoformat() if c.found_at else None,
        }
        for c in contacts
    ]


@router.post("/api/ai/prospect-contacts/{contact_id}/save")
async def save_prospect_contact(
    contact_id: int,
    payload: ProspectContactSave | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save a prospect contact (mark as kept by user)."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == contact_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    pc.is_saved = True
    pc.saved_by_id = user.id
    if payload and payload.notes:
        pc.notes = payload.notes
    db.commit()
    return {
        "ok": True,
        "id": pc.id,
        "contact": {
            "full_name": pc.full_name,
            "title": pc.title,
            "email": pc.email,
            "phone": pc.phone,
            "linkedin_url": pc.linkedin_url,
            "source": pc.source,
        },
    }


@router.delete("/api/ai/prospect-contacts/{contact_id}")
async def delete_prospect_contact(
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a prospect contact."""
    pc = db.query(ProspectContact).filter(ProspectContact.id == contact_id).first()
    if not pc:
        raise HTTPException(404, "Prospect contact not found")
    db.delete(pc)
    db.commit()
    return {"ok": True}


# ── Feature 2a: Parse RFQ Email (Gradient) ────────────────────────────────


@router.post("/api/ai/parse-email")
@limiter.limit("10/minute")
async def ai_parse_email(
    payload: ParseEmailRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Parse a vendor email reply into structured quotes using Gradient AI."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    from app.services.ai_email_parser import parse_email, should_auto_apply, should_flag_review

    result = await parse_email(
        email_body=payload.email_body,
        email_subject=payload.email_subject,
        vendor_name=payload.vendor_name,
    )

    if not result:
        return {"parsed": False, "quotes": [], "reason": "Parser returned no result"}

    return {
        "parsed": True,
        "quotes": result.get("quotes", []),
        "overall_confidence": result.get("overall_confidence", 0),
        "email_type": result.get("email_type", "unclear"),
        "vendor_notes": result.get("vendor_notes"),
        "auto_apply": should_auto_apply(result),
        "needs_review": should_flag_review(result),
    }


# ── Feature 2c: Part Number Normalization ──────────────────────────────


@router.post("/api/ai/normalize-parts")
@limiter.limit("10/minute")
async def ai_normalize_parts(
    payload: NormalizePartsRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Normalize part numbers using AI — infer manufacturer, package, base part."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    from app.services.ai_part_normalizer import normalize_parts

    results = await normalize_parts(payload.parts)
    return {"parts": results, "count": len(results)}


# ── Feature 2b: Parse Vendor Reply → Structured Offer (Anthropic) ────────


@router.post("/api/ai/parse-response/{response_id}")
@limiter.limit("10/minute")
async def ai_parse_response(
    response_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-parse a vendor response with the upgraded parser. Returns draft offers."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    vr = db.query(VendorResponse).filter(VendorResponse.id == response_id).first()
    if not vr:
        raise HTTPException(404, "Vendor response not found")

    rfq_context = None
    if vr.requisition_id:
        reqs = (
            db.query(Requirement)
            .filter(Requirement.requisition_id == vr.requisition_id)
            .all()
        )
        rfq_context = [
            {
                "mpn": r.primary_mpn,
                "qty": r.target_qty,
                "target_price": float(r.target_price) if r.target_price else None,
            }
            for r in reqs
            if r.primary_mpn
        ]

    from app.services.response_parser import (
        extract_draft_offers,
        parse_vendor_response,
        should_auto_apply,
        should_flag_review,
    )

    result = await parse_vendor_response(
        email_body=vr.body or "",
        email_subject=vr.subject or "",
        vendor_name=vr.vendor_name or "",
        rfq_context=rfq_context,
    )

    if not result:
        return {"parsed": False, "reason": "Parser returned no result"}

    vr.parsed_data = result
    vr.confidence = result.get("confidence", 0)
    vr.classification = result.get("overall_classification")
    vr.needs_action = result.get("overall_classification") in (
        "quote_provided",
        "counter_offer",
        "clarification_needed",
        "partial_availability",
    )

    draft_offers = extract_draft_offers(result, vr.vendor_name or "")
    db.commit()

    return {
        "parsed": True,
        "classification": result.get("overall_classification"),
        "confidence": result.get("confidence"),
        "auto_apply": should_auto_apply(result),
        "needs_review": should_flag_review(result),
        "parts": result.get("parts", []),
        "draft_offers": draft_offers,
        "vendor_notes": result.get("vendor_notes"),
    }


@router.post("/api/ai/save-parsed-offers")
async def save_parsed_offers(
    payload: SaveDraftOffersRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save AI-parsed draft offers to the Offers table."""
    response_id = payload.response_id
    requisition_id = payload.requisition_id

    created = []
    for o in payload.offers:
        req_id = None
        if o.mpn:
            from app.utils.normalization import fuzzy_mpn_match

            reqs = (
                db.query(Requirement)
                .filter(Requirement.requisition_id == requisition_id)
                .all()
            )
            for r in reqs:
                if fuzzy_mpn_match(o.mpn, r.primary_mpn):
                    req_id = r.id
                    break

        offer = Offer(
            requisition_id=requisition_id,
            requirement_id=req_id,
            vendor_name=o.vendor_name,
            mpn=o.mpn,
            manufacturer=o.manufacturer,
            qty_available=o.qty_available,
            unit_price=o.unit_price,
            currency=o.currency,
            lead_time=o.lead_time,
            date_code=o.date_code,
            condition=o.condition,
            packaging=o.packaging,
            moq=o.moq,
            source="ai_parsed",
            vendor_response_id=response_id,
            entered_by_id=user.id,
            notes=o.notes,
            status="pending_review",
        )
        db.add(offer)
        db.flush()
        created.append(offer.id)

    db.commit()
    return {"created": len(created), "offer_ids": created}


# ── Feature 3: Company Intelligence Cards ─────────────────────────────────


@router.get("/api/ai/company-intel")
@limiter.limit("10/minute")
async def get_company_intel(
    request: Request,
    company_name: str = "",
    domain: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get intelligence brief for a company. Cached 7 days."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")
    if not company_name:
        raise HTTPException(400, "company_name required")

    from app.services.ai_service import company_intel

    intel = await company_intel(company_name, domain or None)

    if not intel:
        return {"available": False, "reason": "Intel not available"}
    return {"available": True, "intel": intel}


# ── Feature 4: Smart RFQ Drafts ──────────────────────────────────────────


@router.post("/api/ai/draft-rfq")
@limiter.limit("10/minute")
async def ai_draft_rfq(
    payload: RfqDraftRequest,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate a personalized RFQ email body for a vendor."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    vendor_name = payload.vendor_name
    parts = payload.parts

    vendor_history = _build_vendor_history(vendor_name, db)

    from app.services.ai_service import draft_rfq

    draft = await draft_rfq(
        vendor_name=vendor_name,
        parts=parts,
        vendor_history=vendor_history,
        user_name=user.name or "",
        user_signature=user.email_signature or "",
    )

    if not draft:
        return {"available": False, "reason": "Draft generation failed"}
    return {"available": True, "body": draft}


@router.post("/api/ai/draft-rfq-email")
@limiter.limit("10/minute")
async def ai_draft_rfq_email(
    payload: RfqDraftEmailRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Generate a detailed RFQ email with subject and body using Gradient AI."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    from app.services.ai_email_drafter import draft_rfq_email

    parts = [p.model_dump() for p in payload.parts]

    result = await draft_rfq_email(
        vendor_name=payload.vendor_name,
        parts=parts,
        buyer_name=payload.buyer_name,
        vendor_contact_name=payload.vendor_contact_name,
    )

    if not result:
        return {"available": False, "reason": "Draft generation failed"}
    return {"available": True, "subject": result["subject"], "body": result["body"]}


# ── Feature 5: Quote Comparison ─────────────────────────────────────────


@router.post("/api/ai/compare-quotes")
@limiter.limit("10/minute")
async def ai_compare_quotes(
    payload: CompareQuotesRequest,
    request: Request,
    user: User = Depends(require_user),
):
    """Compare multiple vendor quotes and recommend the best option."""
    if not _ai_enabled(user):
        raise HTTPException(403, "AI features not enabled")

    from app.services.ai_quote_analyzer import compare_quotes

    quotes = [q.model_dump() for q in payload.quotes]

    result = await compare_quotes(
        part_number=payload.part_number,
        quotes=quotes,
        required_qty=payload.required_qty,
    )

    if not result:
        return {"available": False, "reason": "Comparison not available"}
    return {"available": True, **result}
