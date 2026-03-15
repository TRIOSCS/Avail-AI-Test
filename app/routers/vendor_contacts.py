"""routers/vendor_contacts.py — Vendor contact lookup, CRUD, and email metrics.

Handles the 3-tier vendor contact waterfall (cache -> scrape -> AI),
structured VendorContact CRUD, email metrics, and the quick add-email
endpoint.

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_helpers, cache, credential_service
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..database import get_db
from ..dependencies import require_buyer, require_user
from ..models import Contact, User, VendorCard, VendorContact, VendorResponse
from ..schemas.responses import VendorEmailMetricsResponse
from ..schemas.vendors import VendorContactCreate, VendorContactLookup, VendorContactUpdate, VendorEmailAdd
from ..services.credential_service import get_credential_cached
from ..utils.phone_utils import format_phone_e164
from ..utils.vendor_helpers import (
    _background_enrich_vendor,
    clean_emails,
    clean_phones,
    get_or_create_card,
    merge_contact_into_card,
    scrape_website_contacts,
)
from ..vendor_utils import GENERIC_EMAIL_DOMAINS as _GENERIC_EMAIL_DOMAINS
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["vendors"])


# -- 3-Tier Vendor Contact Lookup ---------------------------------------------


@router.post("/api/vendor-contact")
async def lookup_vendor_contact(
    payload: VendorContactLookup,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """3-tier waterfall: cache -> website scrape -> AI web search."""
    vendor_name = payload.vendor_name

    norm = normalize_vendor_name(vendor_name)
    card = db.query(VendorCard).filter_by(normalized_name=norm).first()
    if not card:
        card = VendorCard(normalized_name=norm, display_name=vendor_name, emails=[], phones=[])
        db.add(card)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            card = db.query(VendorCard).filter_by(normalized_name=norm).first()

    # TIER 1: Cache check (free, instant)
    if card.emails:
        return {
            "vendor_name": card.display_name,
            "emails": card.emails or [],
            "phones": card.phones or [],
            "website": card.website,
            "card_id": card.id,
            "source": "cached",
            "tier": 1,
        }

    # TIER 2: Website scrape (free, ~1-2 sec)
    if card.website:
        logger.info(f"Tier 2: Scraping {card.website} for {vendor_name}")
        try:
            scraped = await scrape_website_contacts(card.website)
            if scraped["emails"] or scraped["phones"]:
                merge_contact_into_card(card, scraped["emails"], scraped["phones"], source="website_scrape")
                db.commit()
                if card.emails:
                    return {
                        "vendor_name": card.display_name,
                        "emails": card.emails or [],
                        "phones": card.phones or [],
                        "website": card.website,
                        "card_id": card.id,
                        "source": "website_scrape",
                        "tier": 2,
                    }
        except Exception as e:
            logger.warning(f"Tier 2 scrape failed for {vendor_name}: {e}")

    # TIER 3: AI lookup (expensive, last resort)
    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        return {
            "vendor_name": vendor_name,
            "emails": card.emails or [],
            "phones": card.phones or [],
            "website": card.website,
            "card_id": card.id,
            "source": None,
            "tier": 0,
            "error": "No API key configured",
        }

    logger.info(f"Tier 3: AI lookup for {vendor_name}")
    try:
        website_hint = f" Their website may be {card.website}." if card.website else ""

        from ..utils.claude_client import claude_json

        info = await claude_json(
            prompt=(
                f"Find ALL contact information for '{vendor_name}', an electronic "
                f"component distributor/broker.{website_hint}\n\n"
                f"Search these sources:\n"
                f"1. Their company website -- look for contact, about, sales pages\n"
                f"2. LinkedIn company page -- phone numbers, website\n"
                f"3. Industry directories (FindChips, IC Source, TrustedParts)\n"
                f"4. Google Maps / business listings\n\n"
                f"I need EVERY email you can find:\n"
                f"- General: info@, contact@, support@\n"
                f"- Sales: sales@, rfq@, quotes@, purchasing@\n"
                f"- Individual salespeople: firstname@, firstname.lastname@\n\n"
                f"And ALL phone numbers -- main line, sales direct, fax.\n\n"
                f"Return ONLY a JSON object:\n"
                f'{{"emails": [...], "phones": [...], "website": "..."}}\n'
                f"No explanation, no markdown, just the JSON."
            ),
            model_tier="fast",
            max_tokens=1024,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            timeout=60,
        )

        if not info or not isinstance(info, dict):
            info = {}

        ai_emails = info.get("emails") or []
        if isinstance(ai_emails, str):
            ai_emails = [ai_emails]
        single_email = info.get("email")
        if single_email and single_email not in ai_emails:
            ai_emails.insert(0, single_email)
        ai_emails = clean_emails(ai_emails)

        ai_phones = info.get("phones") or []
        if isinstance(ai_phones, str):
            ai_phones = [ai_phones]
        single_phone = info.get("phone")
        if single_phone and single_phone not in ai_phones:
            ai_phones.insert(0, single_phone)
        ai_phones = clean_phones(ai_phones)

        website = info.get("website")

        merge_contact_into_card(card, ai_emails, ai_phones, website, source="ai_lookup")
        db.commit()

        return {
            "vendor_name": card.display_name,
            "emails": card.emails or [],
            "phones": card.phones or [],
            "website": card.website,
            "card_id": card.id,
            "source": "ai_lookup",
            "tier": 3,
        }

    except Exception as e:
        logger.warning(f"Tier 3 AI lookup failed for {vendor_name}: {e}")
        return {
            "vendor_name": vendor_name,
            "emails": card.emails or [],
            "phones": card.phones or [],
            "website": card.website,
            "card_id": card.id,
            "source": None,
            "tier": 0,
            "error": str(e)[:200],
        }


# -- Structured Vendor Contact CRUD -------------------------------------------


@router.get("/api/vendor-contacts/bulk")
async def bulk_vendor_contacts(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """All vendor contacts in a single query -- replaces N+1 per-vendor fetches."""
    from sqlalchemy.orm import joinedload as jl

    query = (
        db.query(VendorContact)
        .join(VendorCard)
        .filter(VendorCard.is_blacklisted == False)  # noqa: E712
        .options(jl(VendorContact.vendor_card))
        .order_by(VendorContact.id)
    )
    total = query.count()
    contacts = query.offset(offset).limit(limit).all()
    items = [
        {
            "id": c.id,
            "vendor_id": c.vendor_card_id,
            "vendor_name": c.vendor_card.display_name if c.vendor_card else "Unknown",
            "contact_type": c.contact_type,
            "full_name": c.full_name,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "title": c.title,
            "label": c.label,
            "email": c.email,
            "phone": c.phone,
            "phone_mobile": c.phone_mobile,
            "source": c.source,
            "is_verified": c.is_verified,
            "confidence": c.confidence,
            "interaction_count": c.interaction_count,
            "relationship_score": c.relationship_score,
            "activity_trend": c.activity_trend,
            "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
        }
        for c in contacts
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/api/vendors/{card_id}/contacts")
async def list_vendor_contacts(card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """List all structured contacts for a vendor card."""
    contacts = (
        db.query(VendorContact)
        .filter_by(vendor_card_id=card_id)
        .order_by(VendorContact.confidence.desc(), VendorContact.last_seen_at.desc())
        .all()
    )
    return [
        {
            "id": c.id,
            "contact_type": c.contact_type,
            "full_name": c.full_name,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "title": c.title,
            "label": c.label,
            "email": c.email,
            "phone": c.phone,
            "phone_mobile": c.phone_mobile,
            "phone_type": c.phone_type,
            "source": c.source,
            "is_verified": c.is_verified,
            "confidence": c.confidence,
            "interaction_count": c.interaction_count,
            "relationship_score": c.relationship_score,
            "activity_trend": c.activity_trend,
            "score_computed_at": c.score_computed_at.isoformat() if c.score_computed_at else None,
            "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
        }
        for c in contacts
    ]


@router.get("/api/vendors/{card_id}/contacts/{contact_id}/timeline")
async def get_contact_timeline(
    card_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Activity timeline for a specific vendor contact."""
    from ..models import ActivityLog

    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.vendor_contact_id == contact_id)
        .order_by(ActivityLog.occurred_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": a.id,
            "activity_type": a.activity_type,
            "channel": a.channel,
            "subject": a.subject,
            "notes": a.notes,
            "duration_seconds": a.duration_seconds,
            "auto_logged": a.auto_logged,
            "occurred_at": a.occurred_at.isoformat()
            if a.occurred_at
            else (a.created_at.isoformat() if a.created_at else None),
            "requisition_id": a.requisition_id,
            "quote_id": a.quote_id,
        }
        for a in activities
    ]


@router.get("/api/vendors/{card_id}/contact-nudges")
async def get_contact_nudges(
    card_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get nudge suggestions for dormant/cooling contacts."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    from ..services.contact_intelligence import generate_contact_nudges

    return generate_contact_nudges(db, card_id)


@router.get("/api/vendors/{card_id}/contacts/{contact_id}/summary")
async def get_contact_summary(
    card_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-generated relationship summary for a contact."""
    from ..services.contact_intelligence import generate_contact_summary

    summary = generate_contact_summary(db, card_id, contact_id)
    return {"summary": summary}


@router.post("/api/vendors/{card_id}/contacts/{contact_id}/log-call")
async def log_contact_call(
    card_id: int,
    contact_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a click-to-call event for a specific vendor contact."""
    from ..models import ActivityLog

    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    now = datetime.now(timezone.utc)
    activity = ActivityLog(
        user_id=user.id,
        activity_type="call_initiated",
        channel="phone",
        vendor_card_id=card_id,
        vendor_contact_id=contact_id,
        contact_phone=vc.phone or vc.phone_mobile,
        contact_name=vc.full_name,
        auto_logged=True,
        occurred_at=now,
        created_at=now,
    )
    db.add(activity)

    vc.interaction_count = (vc.interaction_count or 0) + 1
    vc.last_interaction_at = now
    vc.last_seen_at = now

    db.commit()
    return {"ok": True, "activity_id": activity.id}


@router.post("/api/vendors/{card_id}/contacts")
async def add_vendor_contact(
    card_id: int,
    payload: VendorContactCreate,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Manually add a structured contact to a vendor card."""
    email = payload.email

    card = db.query(VendorCard).filter_by(id=card_id).first()
    if not card:
        raise HTTPException(404, "Vendor card not found")

    # Check for duplicate
    existing = db.query(VendorContact).filter_by(vendor_card_id=card_id, email=email).first()
    if existing:
        return {
            "id": existing.id,
            "message": "Contact already exists",
            "duplicate": True,
        }

    phone = format_phone_e164(payload.phone) or payload.phone if payload.phone else None
    vc = VendorContact(
        vendor_card_id=card_id,
        email=email,
        full_name=payload.full_name,
        title=payload.title,
        label=payload.label,
        phone=phone,
        contact_type="individual" if payload.full_name else "company",
        source="manual",
        is_verified=True,
        confidence=100,
    )
    db.add(vc)

    # Also add to legacy emails[] for backward compat
    if email not in (card.emails or []):
        card.emails = (card.emails or []) + [email]

    db.commit()
    return {"id": vc.id, "message": "Contact added", "duplicate": False}


@router.put("/api/vendors/{card_id}/contacts/{contact_id}")
async def update_vendor_contact(
    card_id: int,
    contact_id: int,
    payload: VendorContactUpdate,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Update a structured vendor contact."""
    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")

    old_email = vc.email

    if payload.full_name is not None:
        vc.full_name = payload.full_name
        vc.contact_type = "individual" if payload.full_name else "company"
    if payload.title is not None:
        vc.title = payload.title
    if payload.email is not None and payload.email != old_email:
        existing = db.query(VendorContact).filter_by(vendor_card_id=card_id, email=payload.email).first()
        if existing and existing.id != contact_id:
            raise HTTPException(409, "Another contact already has this email")
        vc.email = payload.email
    if payload.label is not None:
        vc.label = payload.label
    if payload.phone is not None:
        vc.phone = format_phone_e164(payload.phone) or payload.phone

    vc.last_seen_at = datetime.now(timezone.utc)

    # Sync legacy emails[] array
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if card and old_email != vc.email:
        if old_email and card.emails and old_email in card.emails:
            card.emails = [e for e in card.emails if e != old_email]
        if vc.email and vc.email not in (card.emails or []):
            card.emails = (card.emails or []) + [vc.email]

    db.commit()
    return {"ok": True, "id": vc.id}


@router.delete("/api/vendors/{card_id}/contacts/{contact_id}")
async def delete_vendor_contact(
    card_id: int,
    contact_id: int,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Delete a structured vendor contact."""
    vc = db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    if not vc:
        raise HTTPException(404, "Contact not found")
    # Remove from legacy emails[] too
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if card and vc.email and card.emails and vc.email in card.emails:
        card.emails = [e for e in card.emails if e != vc.email]
    db.delete(vc)
    db.commit()
    return {"ok": True}


# -- Vendor Email Metrics -----------------------------------------------------


@router.get(
    "/api/vendors/{card_id}/email-metrics", response_model=VendorEmailMetricsResponse, response_model_exclude_none=True
)
async def vendor_email_metrics(card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Compute vendor email performance metrics from contact/response data."""
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if not card:
        raise HTTPException(404, "Vendor card not found")

    @cached_endpoint(prefix="vendor_email_metrics", ttl_hours=2, key_params=["card_id"])
    def _fetch(card_id, db, display_name):
        contacts = (
            db.query(Contact)
            .filter(
                Contact.vendor_name == display_name,
                Contact.contact_type == "email",
            )
            .all()
        )

        responses = (
            db.query(VendorResponse)
            .filter(
                VendorResponse.vendor_name == display_name,
            )
            .all()
        )

        total_sent = len(contacts)
        total_replied = len([c for c in contacts if c.status in ("responded", "quoted", "declined")])
        total_quoted = len([c for c in contacts if c.status == "quoted"])

        contact_by_id = {c.id: c for c in contacts}
        response_hours: list[float] = []
        for vr in responses:
            if vr.contact_id and vr.received_at:
                matching_contact = contact_by_id.get(vr.contact_id)
                if matching_contact and matching_contact.created_at:
                    delta = vr.received_at - matching_contact.created_at
                    response_hours.append(delta.total_seconds() / 3600)

        avg_response_hours = round(sum(response_hours) / len(response_hours), 1) if response_hours else None
        last_contacted = max((c.created_at for c in contacts), default=None)
        last_reply = max((vr.received_at for vr in responses if vr.received_at), default=None)

        return {
            "vendor_name": display_name,
            "total_rfqs_sent": total_sent,
            "total_replies": total_replied,
            "total_quotes": total_quoted,
            "response_rate": round(total_replied / total_sent * 100) if total_sent else None,
            "quote_rate": round(total_quoted / total_sent * 100) if total_sent else None,
            "avg_response_hours": avg_response_hours,
            "last_contacted": last_contacted.isoformat() if last_contacted else None,
            "last_reply": last_reply.isoformat() if last_reply else None,
            "active_rfqs": len([c for c in contacts if c.status in ("sent", "opened")]),
        }

    return _fetch(card_id=card_id, db=db, display_name=card.display_name)


# -- Add Email to Vendor Card -------------------------------------------------


@router.post("/api/vendor-card/add-email")
async def add_email_to_card(
    payload: VendorEmailAdd,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Quick-add an email to a vendor card.

    Also creates a VendorContact record, extracts domain for the card, and triggers
    background enrichment if a business domain is found.
    """
    card = get_or_create_card(payload.vendor_name, db)

    # 1. Add to legacy emails[] JSON array (existing behavior)
    emails = [e for e in (card.emails or []) if isinstance(e, str) and e.lower() != payload.email]
    emails.insert(0, payload.email)  # Manual entries go to the top
    card.emails = emails

    # 2. Create VendorContact if not already present
    contact_created = False
    existing_contact = db.query(VendorContact).filter_by(vendor_card_id=card.id, email=payload.email).first()
    if not existing_contact:
        vc = VendorContact(
            vendor_card_id=card.id,
            email=payload.email,
            contact_type="company",
            source="rfq_manual",
            confidence=80,
            is_verified=False,
        )
        db.add(vc)
        contact_created = True

    # 3. Extract domain and set on card (skip generic email providers)
    domain_extracted = None
    domain_part = payload.email.split("@")[1] if "@" in payload.email else None
    if domain_part and domain_part not in _GENERIC_EMAIL_DOMAINS:
        domain_extracted = domain_part
        if not card.domain:
            card.domain = domain_extracted

    db.commit()

    # 4. Fire background enrichment if we have a usable domain and card not yet enriched
    enrich_triggered = False
    if domain_extracted and not card.last_enriched_at:
        if get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or get_credential_cached(
            "anthropic_ai", "ANTHROPIC_API_KEY"
        ):
            asyncio.create_task(_background_enrich_vendor(card.id, domain_extracted, card.display_name))
            enrich_triggered = True

    return {
        "ok": True,
        "card_id": card.id,
        "emails": card.emails,
        "contact_created": contact_created,
        "domain": card.domain,
        "enrich_triggered": enrich_triggered,
    }
