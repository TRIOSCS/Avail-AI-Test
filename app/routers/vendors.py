"""
routers/vendors.py — Vendor Card & Material Card Routes

CRUD for VendorCards (master vendor profiles), VendorReviews, structured
VendorContacts, MaterialCards, and the 3-tier vendor contact lookup
waterfall (cache → website scrape → AI search).

Business Rules:
- VendorCards accumulate intelligence from multiple sources
- Vendor name normalization ensures single profile per vendor
- 3-tier contact lookup: cache (free) → scrape (free) → AI (expensive)
- Blacklisted vendors still appear in search ("leave no stone unturned")

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_utils, config
"""

import asyncio
from datetime import datetime, timezone
import ipaddress
import logging
import re
import socket

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func as sqlfunc, text as sqltext
from sqlalchemy.exc import IntegrityError

from ..schemas.vendors import (
    MaterialCardUpdate,
    VendorBlacklistToggle,
    VendorCardUpdate,
    VendorContactCreate,
    VendorContactUpdate,
    VendorContactLookup,
    VendorEmailAdd,
    VendorReviewCreate,
)
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import require_user, require_buyer
from ..models import (
    User,
    VendorCard,
    VendorContact,
    VendorReview,
    Contact,
    VendorResponse,
    MaterialCard,
    MaterialVendorHistory,
    Offer,
    Sighting,
    Company,
)
from ..vendor_utils import normalize_vendor_name
from ..search_service import normalize_mpn

log = logging.getLogger(__name__)

router = APIRouter(tags=["vendors"])

# Generic email domains — not useful for vendor enrichment
_GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "live.com",
        "msn.com",
        "protonmail.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "gmx.com",
        "fastmail.com",
    }
)


# ── Helpers ──────────────────────────────────────────────────────────────


def get_or_create_card(vendor_name: str, db: Session) -> VendorCard:
    """Find existing VendorCard by normalized name, or create a new one."""
    norm = normalize_vendor_name(vendor_name)
    card = db.query(VendorCard).filter_by(normalized_name=norm).first()
    if not card:
        card = VendorCard(
            normalized_name=norm, display_name=vendor_name, emails=[], phones=[]
        )
        db.add(card)
        db.commit()
    return card


async def _background_enrich_vendor(card_id: int, domain: str, vendor_name: str):
    """Fire-and-forget enrichment for a vendor card. Runs in background."""
    from ..enrichment_service import enrich_entity, apply_enrichment_to_vendor
    from ..database import SessionLocal

    try:
        enrichment = await enrich_entity(domain, vendor_name)
        if not enrichment:
            return
        db = SessionLocal()
        try:
            card = db.get(VendorCard, card_id)
            if card:
                apply_enrichment_to_vendor(card, enrichment)
                db.commit()
                log.info(
                    "Background enrichment completed for vendor %s (card %d): %s",
                    vendor_name,
                    card_id,
                    enrichment.get("source", "unknown"),
                )
        finally:
            db.close()
    except Exception:
        log.exception("Background enrichment failed for vendor card %d", card_id)

    # Also run AI material analysis if vendor has sighting data
    if settings.anthropic_api_key:
        try:
            await _analyze_vendor_materials(card_id)
        except Exception:
            log.exception(
                "Background material analysis failed for vendor card %d", card_id
            )


def card_to_dict(card: VendorCard, db: Session) -> dict:
    """Serialize a VendorCard with reviews, brand profile, and engagement metrics."""
    reviews = db.query(VendorReview).filter_by(vendor_card_id=card.id).all()
    avg = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else None

    # Material profile: brands/manufacturers this vendor carries
    # Note: sightings.vendor_name is raw (needs LOWER/TRIM),
    # material_vendor_history.vendor_name is already normalized
    norm = card.normalized_name
    mfr_rows = db.execute(
        sqltext("""
        SELECT manufacturer, SUM(cnt) as total FROM (
            SELECT manufacturer, COUNT(*) as cnt FROM sightings
            WHERE LOWER(TRIM(vendor_name)) = :norm
              AND manufacturer IS NOT NULL AND manufacturer != ''
            GROUP BY manufacturer
            UNION ALL
            SELECT last_manufacturer as manufacturer, COUNT(*) as cnt FROM material_vendor_history
            WHERE vendor_name = :norm
              AND last_manufacturer IS NOT NULL AND last_manufacturer != ''
            GROUP BY last_manufacturer
        ) combined
        GROUP BY manufacturer ORDER BY total DESC LIMIT 15
    """),
        {"norm": norm},
    ).fetchall()
    brands = [{"name": r[0], "count": r[1]} for r in mfr_rows]

    mpn_count = (
        db.execute(
            sqltext("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT mpn_matched as mpn FROM sightings
            WHERE LOWER(TRIM(vendor_name)) = :norm AND mpn_matched IS NOT NULL
            UNION
            SELECT DISTINCT mc.normalized_mpn as mpn FROM material_vendor_history mvh
            JOIN material_cards mc ON mc.id = mvh.material_card_id
            WHERE mvh.vendor_name = :norm
        ) all_mpns
    """),
            {"norm": norm},
        ).scalar()
        or 0
    )

    return {
        "id": card.id,
        "normalized_name": card.normalized_name,
        "display_name": card.display_name,
        "domain": card.domain,
        "website": card.website,
        "emails": card.emails or [],
        "phones": card.phones or [],
        "sighting_count": card.sighting_count or 0,
        "is_blacklisted": card.is_blacklisted or False,
        "linkedin_url": card.linkedin_url,
        "legal_name": card.legal_name,
        "industry": card.industry,
        "employee_size": card.employee_size,
        "hq_city": card.hq_city,
        "hq_state": card.hq_state,
        "hq_country": card.hq_country,
        "last_enriched_at": card.last_enriched_at.isoformat()
        if card.last_enriched_at
        else None,
        "enrichment_source": card.enrichment_source,
        "avg_rating": avg,
        "review_count": len(reviews),
        "reviews": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_name": r.user.name if r.user else "",
                "rating": r.rating,
                "comment": r.comment,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reviews
        ],
        "brands": brands,
        "unique_parts": mpn_count,
        "engagement_score": card.engagement_score,
        "total_outreach": card.total_outreach,
        "total_responses": card.total_responses,
        "ghost_rate": card.ghost_rate,
        "response_velocity_hours": card.response_velocity_hours,
        "last_contact_at": card.last_contact_at.isoformat()
        if card.last_contact_at
        else None,
        "brand_tags": card.brand_tags or [],
        "commodity_tags": card.commodity_tags or [],
        "material_tags_updated_at": card.material_tags_updated_at.isoformat()
        if card.material_tags_updated_at
        else None,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


# ── Vendor Cards CRUD ────────────────────────────────────────────────────


@router.get("/api/vendors")
async def list_vendors(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    q = request.query_params.get("q", "").strip().lower()
    limit = min(int(request.query_params.get("limit", "200")), 1000)
    offset = max(int(request.query_params.get("offset", "0")), 0)
    query = db.query(VendorCard).order_by(VendorCard.display_name)
    if q:
        safe_q = q.replace("%", r"\%").replace("_", r"\_")
        query = query.filter(VendorCard.normalized_name.ilike(f"%{safe_q}%"))
    total = query.count()
    cards = query.limit(limit).offset(offset).all()
    if not cards:
        return []
    # Batch fetch review stats — single query instead of N+1
    card_ids = [c.id for c in cards]
    review_stats = {}
    if card_ids:
        for cid, avg, cnt in (
            db.query(
                VendorReview.vendor_card_id,
                sqlfunc.avg(VendorReview.rating),
                sqlfunc.count(VendorReview.id),
            )
            .filter(VendorReview.vendor_card_id.in_(card_ids))
            .group_by(VendorReview.vendor_card_id)
            .all()
        ):
            review_stats[cid] = (avg, cnt)
    results = []
    for c in cards:
        stat = review_stats.get(c.id)
        avg_rating = round(float(stat[0]), 1) if stat else None
        review_count = int(stat[1]) if stat else 0
        results.append(
            {
                "id": c.id,
                "display_name": c.display_name,
                "emails": c.emails or [],
                "phones": c.phones or [],
                "sighting_count": c.sighting_count or 0,
                "engagement_score": c.engagement_score,
                "is_blacklisted": c.is_blacklisted or False,
                "avg_rating": avg_rating,
                "review_count": review_count,
            }
        )
    return {"vendors": results, "total": total, "limit": limit, "offset": offset}


@router.get("/api/autocomplete/names")
async def autocomplete_names(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Lightweight name autocomplete across VendorCards and Companies."""
    q = request.query_params.get("q", "").strip().lower()
    if len(q) < 2:
        return []
    limit = min(int(request.query_params.get("limit", "8")), 20)
    safe_q = q.replace("%", r"\%").replace("_", r"\_")

    vendors = (
        db.query(VendorCard.id, VendorCard.display_name)
        .filter(VendorCard.normalized_name.ilike(f"%{safe_q}%"))
        .order_by(VendorCard.display_name)
        .limit(limit)
        .all()
    )
    companies = (
        db.query(Company.id, Company.name)
        .filter(Company.is_active, Company.name.ilike(f"%{safe_q}%"))
        .order_by(Company.name)
        .limit(limit)
        .all()
    )

    results = []
    for v in vendors:
        results.append({"id": v.id, "name": v.display_name, "type": "vendor"})
    for c in companies:
        results.append({"id": c.id, "name": c.name, "type": "customer"})
    results.sort(key=lambda r: r["name"].lower())
    return results[:limit]


@router.get("/api/vendors/{card_id}")
async def get_vendor(
    card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    return card_to_dict(card, db)


@router.put("/api/vendors/{card_id}")
async def update_vendor(
    card_id: int,
    data: VendorCardUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    if data.emails is not None:
        card.emails = data.emails
    if data.phones is not None:
        card.phones = data.phones
    if data.website is not None:
        card.website = data.website
    if data.display_name is not None and data.display_name.strip():
        card.display_name = data.display_name.strip()
    if data.is_blacklisted is not None:
        card.is_blacklisted = data.is_blacklisted
    db.commit()
    return card_to_dict(card, db)


@router.post("/api/vendors/{card_id}/blacklist")
async def toggle_blacklist(
    card_id: int,
    data: VendorBlacklistToggle,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle vendor blacklist status."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    card.is_blacklisted = (
        data.blacklisted if data.blacklisted is not None else (not card.is_blacklisted)
    )
    db.commit()
    return card_to_dict(card, db)


@router.delete("/api/vendors/{card_id}")
async def delete_vendor(
    card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    db.delete(card)
    db.commit()
    return {"ok": True}


# ── Vendor Reviews ───────────────────────────────────────────────────────


@router.post("/api/vendors/{card_id}/reviews")
async def add_review(
    card_id: int,
    payload: VendorReviewCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    review = VendorReview(
        vendor_card_id=card.id,
        user_id=user.id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(review)
    db.commit()
    return card_to_dict(card, db)


@router.delete("/api/vendors/{card_id}/reviews/{review_id}")
async def delete_review(
    card_id: int,
    review_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    review = (
        db.query(VendorReview)
        .filter_by(id=review_id, vendor_card_id=card_id, user_id=user.id)
        .first()
    )
    if not review:
        raise HTTPException(404, "Review not found or not yours")
    db.delete(review)
    db.commit()
    card = db.get(VendorCard, card_id)
    if not card:
        return {"ok": True}
    return card_to_dict(card, db)


# ── Contact Cleaning Utilities ───────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_JUNK_EMAILS = {
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "webmaster",
    "privacy",
    "abuse",
    "spam",
    "unsubscribe",
    "root",
    "hostmaster",
    "example",
    "test",
    "admin@example",
}
_JUNK_DOMAINS = {
    "example.com",
    "sentry.io",
    "googleapis.com",
    "google.com",
    "facebook.com",
    "twitter.com",
    "youtube.com",
    "linkedin.com",
    "schema.org",
    "w3.org",
    "cloudflare.com",
    "jquery.com",
    "bootstrapcdn.com",
    "gstatic.com",
    "gravatar.com",
    "wordpress.org",
}


def clean_emails(raw_emails: list[str]) -> list[str]:
    """Deduplicate, lowercase, filter junk emails."""
    seen: set[str] = set()
    clean: list[str] = []
    for e in raw_emails:
        e = e.strip().lower()
        if not e or "@" not in e or len(e) > 100:
            continue
        local, domain = e.rsplit("@", 1)
        if local in _JUNK_EMAILS or domain in _JUNK_DOMAINS:
            continue
        if e.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
            continue
        if e not in seen:
            seen.add(e)
            clean.append(e)
    return clean


def clean_phones(raw_phones: list[str]) -> list[str]:
    """Deduplicate, filter too-short/junk phone numbers."""
    seen: set[str] = set()
    clean: list[str] = []
    for p in raw_phones:
        p = p.strip()
        digits = re.sub(r"\D", "", p)
        if len(digits) < 7 or len(digits) > 15:
            continue
        if digits not in seen:
            seen.add(digits)
            clean.append(p)
    return clean


def is_private_url(url: str) -> bool:
    """SSRF protection — reject URLs pointing to private/internal networks."""
    try:
        from urllib.parse import urlparse

        hostname = urlparse(url).hostname or ""
        if not hostname:
            return True
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except (ValueError, socket.gaierror):
        return True  # Can't resolve = block it


async def scrape_website_contacts(url: str) -> dict:
    """Fetch vendor website homepage + /contact page, extract emails and phones."""
    emails: set[str] = set()
    phones: set[str] = set()

    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/")

    if is_private_url(url):
        log.warning(f"SSRF blocked: {url}")
        return {"emails": [], "phones": []}

    pages_to_try = [url + "/contact", url + "/contact-us", url]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for page_url in pages_to_try:
            try:
                resp = await client.get(page_url, headers=headers)
                if resp.status_code != 200:
                    continue
                html = resp.text[:200_000]  # Cap at 200KB

                for mailto in re.findall(r'mailto:([^"\'?\s]+)', html, re.IGNORECASE):
                    email = mailto.split("?")[0].strip().lower()
                    if "@" in email:
                        emails.add(email)

                for match in _EMAIL_RE.findall(html):
                    emails.add(match.lower())

                for tel in re.findall(r'tel:([^"\'<\s]+)', html, re.IGNORECASE):
                    phones.add(tel.strip())

                # Short-circuit: found emails, skip remaining pages
                if clean_emails(list(emails)):
                    break

            except Exception as e:
                log.debug(f"Scrape failed for {page_url}: {e}")
                continue

    return {"emails": clean_emails(list(emails)), "phones": clean_phones(list(phones))}


def merge_contact_into_card(
    card: VendorCard,
    emails: list,
    phones: list,
    website: str = None,
    source: str = None,
) -> bool:
    """Merge new contact data into vendor card. Returns True if anything changed."""
    from ..vendor_utils import merge_emails_into_card, merge_phones_into_card

    changed = False
    if merge_emails_into_card(card, emails) > 0:
        changed = True
    if merge_phones_into_card(card, phones) > 0:
        changed = True
    if website and not card.website:
        card.website = website
        changed = True
    if source and changed:
        card.source = source
    return changed


# ── 3-Tier Vendor Contact Lookup ─────────────────────────────────────────


@router.post("/api/vendor-contact")
async def lookup_vendor_contact(
    payload: VendorContactLookup,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """3-tier waterfall: cache → website scrape → AI web search."""
    vendor_name = payload.vendor_name

    norm = normalize_vendor_name(vendor_name)
    card = db.query(VendorCard).filter_by(normalized_name=norm).first()
    if not card:
        card = VendorCard(
            normalized_name=norm, display_name=vendor_name, emails=[], phones=[]
        )
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
        log.info(f"Tier 2: Scraping {card.website} for {vendor_name}")
        try:
            scraped = await scrape_website_contacts(card.website)
            if scraped["emails"] or scraped["phones"]:
                merge_contact_into_card(
                    card, scraped["emails"], scraped["phones"], source="website_scrape"
                )
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
            log.warning(f"Tier 2 scrape failed for {vendor_name}: {e}")

    # TIER 3: AI lookup (expensive, last resort)
    if not settings.anthropic_api_key:
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

    log.info(f"Tier 3: AI lookup for {vendor_name}")
    try:
        website_hint = f" Their website may be {card.website}." if card.website else ""

        from ..utils.claude_client import claude_json

        info = await claude_json(
            prompt=(
                f"Find ALL contact information for '{vendor_name}', an electronic "
                f"component distributor/broker.{website_hint}\n\n"
                f"Search these sources:\n"
                f"1. Their company website — look for contact, about, sales pages\n"
                f"2. LinkedIn company page — phone numbers, website\n"
                f"3. Industry directories (FindChips, IC Source, TrustedParts)\n"
                f"4. Google Maps / business listings\n\n"
                f"I need EVERY email you can find:\n"
                f"- General: info@, contact@, support@\n"
                f"- Sales: sales@, rfq@, quotes@, purchasing@\n"
                f"- Individual salespeople: firstname@, firstname.lastname@\n\n"
                f"And ALL phone numbers — main line, sales direct, fax.\n\n"
                f"Return ONLY a JSON object:\n"
                f'{{"emails": [...], "phones": [...], "website": "..."}}\n'
                f"No explanation, no markdown, just the JSON."
            ),
            model_tier="fast",
            max_tokens=1024,
            tools=[
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
            ],
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
        log.warning(f"Tier 3 AI lookup failed for {vendor_name}: {e}")
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


# ── Structured Vendor Contact CRUD ──────────────────────────────────────


@router.get("/api/vendors/{card_id}/contacts")
async def list_vendor_contacts(
    card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
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
            "title": c.title,
            "label": c.label,
            "email": c.email,
            "phone": c.phone,
            "phone_type": c.phone_type,
            "source": c.source,
            "is_verified": c.is_verified,
            "confidence": c.confidence,
            "interaction_count": c.interaction_count,
            "last_interaction_at": c.last_interaction_at.isoformat()
            if c.last_interaction_at
            else None,
            "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
        }
        for c in contacts
    ]


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
    existing = (
        db.query(VendorContact).filter_by(vendor_card_id=card_id, email=email).first()
    )
    if existing:
        return {
            "id": existing.id,
            "message": "Contact already exists",
            "duplicate": True,
        }

    vc = VendorContact(
        vendor_card_id=card_id,
        email=email,
        full_name=payload.full_name,
        title=payload.title,
        label=payload.label,
        phone=payload.phone,
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
    vc = (
        db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    )
    if not vc:
        raise HTTPException(404, "Contact not found")

    old_email = vc.email

    if payload.full_name is not None:
        vc.full_name = payload.full_name
        vc.contact_type = "individual" if payload.full_name else "company"
    if payload.title is not None:
        vc.title = payload.title
    if payload.email is not None and payload.email != old_email:
        existing = (
            db.query(VendorContact)
            .filter_by(vendor_card_id=card_id, email=payload.email)
            .first()
        )
        if existing and existing.id != contact_id:
            raise HTTPException(409, "Another contact already has this email")
        vc.email = payload.email
    if payload.label is not None:
        vc.label = payload.label
    if payload.phone is not None:
        vc.phone = payload.phone

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
    vc = (
        db.query(VendorContact).filter_by(id=contact_id, vendor_card_id=card_id).first()
    )
    if not vc:
        raise HTTPException(404, "Contact not found")
    # Remove from legacy emails[] too
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if card and vc.email and card.emails and vc.email in card.emails:
        card.emails = [e for e in card.emails if e != vc.email]
    db.delete(vc)
    db.commit()
    return {"ok": True}


# ── Vendor Email Metrics ────────────────────────────────────────────────


@router.get("/api/vendors/{card_id}/email-metrics")
async def vendor_email_metrics(
    card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Compute vendor email performance metrics from contact/response data."""
    card = db.query(VendorCard).filter_by(id=card_id).first()
    if not card:
        raise HTTPException(404, "Vendor card not found")

    contacts = (
        db.query(Contact)
        .filter(
            Contact.vendor_name == card.display_name,
            Contact.contact_type == "email",
        )
        .all()
    )

    responses = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.vendor_name == card.display_name,
        )
        .all()
    )

    total_sent = len(contacts)
    total_replied = len(
        [c for c in contacts if c.status in ("responded", "quoted", "declined")]
    )
    total_quoted = len([c for c in contacts if c.status == "quoted"])

    # Response time calculation
    response_hours: list[float] = []
    for vr in responses:
        if vr.contact_id and vr.received_at:
            matching_contact = next(
                (c for c in contacts if c.id == vr.contact_id), None
            )
            if matching_contact and matching_contact.created_at:
                delta = vr.received_at - matching_contact.created_at
                response_hours.append(delta.total_seconds() / 3600)

    avg_response_hours = (
        round(sum(response_hours) / len(response_hours), 1) if response_hours else None
    )
    last_contacted = max((c.created_at for c in contacts), default=None)
    last_reply = max(
        (vr.received_at for vr in responses if vr.received_at), default=None
    )

    return {
        "vendor_name": card.display_name,
        "total_rfqs_sent": total_sent,
        "total_replies": total_replied,
        "total_quotes": total_quoted,
        "response_rate": round(total_replied / total_sent * 100)
        if total_sent
        else None,
        "quote_rate": round(total_quoted / total_sent * 100) if total_sent else None,
        "avg_response_hours": avg_response_hours,
        "last_contacted": last_contacted.isoformat() if last_contacted else None,
        "last_reply": last_reply.isoformat() if last_reply else None,
        "active_rfqs": len([c for c in contacts if c.status in ("sent", "opened")]),
    }


# ── Add Email to Vendor Card ───────────────────────────────────────────


@router.post("/api/vendor-card/add-email")
async def add_email_to_card(
    payload: VendorEmailAdd,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Quick-add an email to a vendor card.

    Also creates a VendorContact record, extracts domain for the card,
    and triggers background enrichment if a business domain is found.
    """
    card = get_or_create_card(payload.vendor_name, db)

    # 1. Add to legacy emails[] JSON array (existing behavior)
    emails = [
        e
        for e in (card.emails or [])
        if isinstance(e, str) and e.lower() != payload.email
    ]
    emails.insert(0, payload.email)  # Manual entries go to the top
    card.emails = emails

    # 2. Create VendorContact if not already present
    contact_created = False
    existing_contact = (
        db.query(VendorContact)
        .filter_by(vendor_card_id=card.id, email=payload.email)
        .first()
    )
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
        if (
            settings.clay_api_key
            or settings.explorium_api_key
            or settings.anthropic_api_key
        ):
            asyncio.create_task(
                _background_enrich_vendor(card.id, domain_extracted, card.display_name)
            )
            enrich_triggered = True

    return {
        "ok": True,
        "card_id": card.id,
        "emails": card.emails,
        "contact_created": contact_created,
        "domain": card.domain,
        "enrich_triggered": enrich_triggered,
    }


# ── Material Cards ──────────────────────────────────────────────────────


def material_card_to_dict(card: MaterialCard, db: Session) -> dict:
    """Serialize a material card with vendor history."""
    history = (
        db.query(MaterialVendorHistory)
        .filter_by(material_card_id=card.id)
        .order_by(MaterialVendorHistory.last_seen.desc())
        .all()
    )
    return {
        "id": card.id,
        "normalized_mpn": card.normalized_mpn,
        "display_mpn": card.display_mpn,
        "manufacturer": card.manufacturer,
        "description": card.description,
        "search_count": card.search_count or 0,
        "last_searched_at": card.last_searched_at.isoformat()
        if card.last_searched_at
        else None,
        "vendor_count": len(history),
        "vendor_history": [
            {
                "id": vh.id,
                "vendor_name": vh.vendor_name,
                "source_type": vh.source_type,
                "is_authorized": vh.is_authorized,
                "first_seen": vh.first_seen.isoformat() if vh.first_seen else None,
                "last_seen": vh.last_seen.isoformat() if vh.last_seen else None,
                "times_seen": vh.times_seen or 1,
                "last_qty": vh.last_qty,
                "last_price": vh.last_price,
                "last_currency": vh.last_currency,
                "last_manufacturer": vh.last_manufacturer,
                "vendor_sku": vh.vendor_sku,
            }
            for vh in history
        ],
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


@router.get("/api/materials")
async def list_materials(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    q = request.query_params.get("q", "").strip().lower()
    limit = min(int(request.query_params.get("limit", "200")), 1000)
    offset = max(int(request.query_params.get("offset", "0")), 0)
    query = db.query(MaterialCard).order_by(MaterialCard.last_searched_at.desc())
    if q:
        safe_q = q.replace("%", r"\%").replace("_", r"\_")
        query = query.filter(MaterialCard.normalized_mpn.ilike(f"{safe_q}%"))
    total = query.count()
    cards = query.limit(limit).offset(offset).all()
    if not cards:
        return {"materials": [], "total": total, "limit": limit, "offset": offset}
    # Batch fetch vendor counts — single query instead of N+1
    card_ids = [c.id for c in cards]
    counts = (
        dict(
            db.query(
                MaterialVendorHistory.material_card_id,
                sqlfunc.count(MaterialVendorHistory.id),
            )
            .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
            .group_by(MaterialVendorHistory.material_card_id)
            .all()
        )
        if card_ids
        else {}
    )
    return {
        "materials": [
            {
                "id": c.id,
                "display_mpn": c.display_mpn,
                "manufacturer": c.manufacturer,
                "search_count": c.search_count or 0,
                "vendor_count": counts.get(c.id, 0),
                "last_searched_at": c.last_searched_at.isoformat()
                if c.last_searched_at
                else None,
            }
            for c in cards
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/api/materials/{card_id}")
async def get_material(
    card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    return material_card_to_dict(card, db)


@router.get("/api/materials/by-mpn/{mpn}")
async def get_material_by_mpn(
    mpn: str, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Look up a material card by MPN."""
    norm = normalize_mpn(mpn)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
    if not card:
        raise HTTPException(404, "No material card found for this MPN")
    return material_card_to_dict(card, db)


@router.put("/api/materials/{card_id}")
async def update_material(
    card_id: int,
    data: MaterialCardUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    if data.manufacturer is not None:
        card.manufacturer = data.manufacturer
    if data.description is not None:
        card.description = data.description
    if data.display_mpn is not None and data.display_mpn.strip():
        card.display_mpn = data.display_mpn.strip()
    db.commit()
    return material_card_to_dict(card, db)


@router.delete("/api/materials/{card_id}")
async def delete_material(
    card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    db.delete(card)
    db.commit()
    return {"ok": True}


# ── Standalone Stock Import ────────────────────────────────────────────


@router.post("/api/materials/import-stock")
async def import_stock_list_standalone(
    request: Request, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    """Import a vendor stock list — stores ALL rows as MaterialCard + MaterialVendorHistory."""
    form = await request.form()
    file = form.get("file")
    vendor_name = (form.get("vendor_name") or "").strip()
    if not file:
        raise HTTPException(400, "No file uploaded")
    if not vendor_name:
        raise HTTPException(400, "Vendor name is required")

    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large — 10MB maximum")

    from ..file_utils import parse_tabular_file, normalize_stock_row

    rows = parse_tabular_file(content, file.filename or "upload.csv")

    # Upsert VendorCard
    vendor_website = (form.get("vendor_website") or "").strip()
    norm_vendor = normalize_vendor_name(vendor_name)
    vendor_card = db.query(VendorCard).filter_by(normalized_name=norm_vendor).first()
    new_vendor = False
    if not vendor_card:
        domain = ""
        if vendor_website:
            domain = (
                vendor_website.replace("https://", "")
                .replace("http://", "")
                .replace("www.", "")
                .split("/")[0]
                .lower()
            )
        vendor_card = VendorCard(
            normalized_name=norm_vendor,
            display_name=vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(vendor_card)
        try:
            db.flush()
            new_vendor = True
        except IntegrityError:
            db.rollback()
            vendor_card = (
                db.query(VendorCard).filter_by(normalized_name=norm_vendor).first()
            )

    imported = 0
    skipped = 0

    for raw_row in rows:
        parsed = normalize_stock_row(raw_row)
        if not parsed:
            skipped += 1
            continue

        norm = normalize_mpn(parsed["mpn"])
        if not norm:
            skipped += 1
            continue

        # Upsert MaterialCard
        card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
        if not card:
            card = MaterialCard(
                normalized_mpn=norm,
                display_mpn=parsed["mpn"].strip(),
                manufacturer=parsed.get("manufacturer") or "",
            )
            db.add(card)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()

        # Upsert MaterialVendorHistory
        mvh = (
            db.query(MaterialVendorHistory)
            .filter_by(material_card_id=card.id, vendor_name=norm_vendor)
            .first()
        )
        if mvh:
            mvh.last_seen = datetime.now(timezone.utc)
            mvh.times_seen = (mvh.times_seen or 0) + 1
            if parsed.get("qty") is not None:
                mvh.last_qty = parsed["qty"]
            if parsed.get("price") is not None:
                mvh.last_price = parsed["price"]
            if parsed.get("manufacturer"):
                mvh.last_manufacturer = parsed["manufacturer"]
            mvh.source_type = "stock_list"
        else:
            mvh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=norm_vendor,
                source_type="stock_list",
                source="stock_list",
                last_qty=parsed.get("qty"),
                last_price=parsed.get("price"),
                last_manufacturer=parsed.get("manufacturer") or "",
            )
            db.add(mvh)

        imported += 1

    vendor_card.sighting_count = (vendor_card.sighting_count or 0) + imported
    db.commit()

    # Trigger enrichment for new vendor with domain
    enrich_triggered = False
    if new_vendor and vendor_card.domain and not vendor_card.last_enriched_at:
        if (
            settings.clay_api_key
            or settings.explorium_api_key
            or settings.anthropic_api_key
        ):
            asyncio.create_task(
                _background_enrich_vendor(
                    vendor_card.id, vendor_card.domain, vendor_card.display_name
                )
            )
            enrich_triggered = True

    return {
        "imported_rows": imported,
        "skipped_rows": skipped,
        "total_rows": len(rows),
        "vendor_name": vendor_name,
        "enrich_triggered": enrich_triggered,
    }


# ── Vendor Offer History ──────────────────────────────────────────────


@router.get("/api/vendors/{card_id}/offer-history")
async def get_vendor_offer_history(
    card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """All parts this vendor has ever offered, from MaterialVendorHistory."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    q = request.query_params.get("q", "").strip().lower()
    limit = min(int(request.query_params.get("limit", "100")), 500)
    offset = max(int(request.query_params.get("offset", "0")), 0)

    query = (
        db.query(MaterialVendorHistory, MaterialCard)
        .join(MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id)
        .filter(MaterialVendorHistory.vendor_name == card.normalized_name)
    )
    if q:
        safe_q = q.replace("%", r"\%").replace("_", r"\_")
        query = query.filter(MaterialCard.normalized_mpn.ilike(f"%{safe_q}%"))

    total = query.count()
    results = (
        query.order_by(MaterialVendorHistory.last_seen.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return {
        "vendor_name": card.display_name,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "mpn": mc.display_mpn,
                "manufacturer": mvh.last_manufacturer or mc.manufacturer or "",
                "qty": mvh.last_qty,
                "price": mvh.last_price,
                "currency": mvh.last_currency or "USD",
                "source_type": mvh.source_type,
                "times_seen": mvh.times_seen or 1,
                "first_seen": mvh.first_seen.isoformat() if mvh.first_seen else None,
                "last_seen": mvh.last_seen.isoformat() if mvh.last_seen else None,
                "material_card_id": mc.id,
            }
            for mvh, mc in results
        ],
    }


# ── Confirmed Offers (Buyer-entered quotes) ─────────────────────────────


@router.get("/api/vendors/{card_id}/confirmed-offers")
async def get_vendor_confirmed_offers(
    card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Confirmed quotes manually entered by buyers for this vendor."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    q = request.query_params.get("q", "").strip().lower()
    limit = min(int(request.query_params.get("limit", "50")), 200)
    offset = max(int(request.query_params.get("offset", "0")), 0)

    query = db.query(Offer).filter(Offer.vendor_card_id == card_id)
    if q:
        safe_q = q.replace("%", r"\%").replace("_", r"\_")
        query = query.filter(Offer.mpn.ilike(f"%{safe_q}%"))

    total = query.count()
    rows = query.order_by(Offer.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "vendor_name": card.display_name,
        "total": total,
        "items": [
            {
                "id": o.id,
                "mpn": o.mpn,
                "manufacturer": o.manufacturer or "",
                "qty_available": o.qty_available,
                "unit_price": float(o.unit_price) if o.unit_price is not None else None,
                "currency": o.currency or "USD",
                "lead_time": o.lead_time or "",
                "condition": o.condition or "",
                "status": o.status or "active",
                "notes": o.notes or "",
                "entered_by": o.entered_by.name if o.entered_by else "",
                "requisition_id": o.requisition_id,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in rows
        ],
    }


# ── Parts Sightings Summary ─────────────────────────────────────────────


@router.get("/api/vendors/{card_id}/parts-summary")
async def get_vendor_parts_summary(
    card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parts this vendor has been seen with, grouped by MPN with counts and date ranges."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    norm = card.normalized_name
    q = request.query_params.get("q", "").strip().lower()
    limit = min(int(request.query_params.get("limit", "100")), 500)
    offset = max(int(request.query_params.get("offset", "0")), 0)

    # Combine sightings and material_vendor_history into a unified parts summary
    q_filter = ""
    if q:
        safe_q = q.replace("'", "''").replace("%", r"\%").replace("_", r"\_")
        q_filter = f"AND LOWER(mpn) LIKE '%{safe_q}%'"

    rows = db.execute(
        sqltext(f"""
        SELECT mpn, manufacturer, sighting_count, first_seen, last_seen, last_price, last_qty
        FROM (
            SELECT
                COALESCE(mpn_matched, '') as mpn,
                MAX(manufacturer) as manufacturer,
                COUNT(*) as sighting_count,
                MIN(created_at) as first_seen,
                MAX(created_at) as last_seen,
                (array_agg(unit_price ORDER BY created_at DESC))[1] as last_price,
                (array_agg(qty_available ORDER BY created_at DESC))[1] as last_qty
            FROM sightings
            WHERE LOWER(TRIM(vendor_name)) = :norm
              AND mpn_matched IS NOT NULL AND mpn_matched != ''
            GROUP BY COALESCE(mpn_matched, '')
            UNION ALL
            SELECT
                mc.display_mpn as mpn,
                COALESCE(mvh.last_manufacturer, mc.manufacturer, '') as manufacturer,
                mvh.times_seen as sighting_count,
                mvh.first_seen,
                mvh.last_seen,
                mvh.last_price,
                mvh.last_qty
            FROM material_vendor_history mvh
            JOIN material_cards mc ON mc.id = mvh.material_card_id
            WHERE mvh.vendor_name = :norm
        ) combined
        WHERE mpn != '' {q_filter}
        ORDER BY last_seen DESC NULLS LAST
        OFFSET :off LIMIT :lim
    """),
        {"norm": norm, "off": offset, "lim": limit},
    ).fetchall()

    # Get total count
    total = (
        db.execute(
            sqltext(f"""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT COALESCE(mpn_matched, '') as mpn FROM sightings
            WHERE LOWER(TRIM(vendor_name)) = :norm AND mpn_matched IS NOT NULL AND mpn_matched != ''
            UNION
            SELECT DISTINCT mc.display_mpn as mpn FROM material_vendor_history mvh
            JOIN material_cards mc ON mc.id = mvh.material_card_id
            WHERE mvh.vendor_name = :norm
        ) all_mpns
        WHERE mpn != '' {q_filter}
    """),
            {"norm": norm},
        ).scalar()
        or 0
    )

    return {
        "vendor_name": card.display_name,
        "total": total,
        "items": [
            {
                "mpn": r[0],
                "manufacturer": r[1] or "",
                "sighting_count": r[2] or 1,
                "first_seen": r[3].isoformat() if r[3] else None,
                "last_seen": r[4].isoformat() if r[4] else None,
                "last_price": r[5],
                "last_qty": r[6],
            }
            for r in rows
        ],
    }


# ── AI Material Analysis ────────────────────────────────────────────────


async def _analyze_vendor_materials(card_id: int, db_session=None):
    """Analyze a vendor's MaterialVendorHistory to generate brand and commodity tags.

    If db_session is None, creates its own session (for background use).
    """
    from ..utils.claude_client import claude_json
    from ..database import SessionLocal

    own_session = db_session is None
    db = db_session or SessionLocal()
    try:
        card = db.get(VendorCard, card_id)
        if not card:
            return

        # Fetch parts from both MaterialVendorHistory and Sightings
        parts_list = []
        seen_mpns = set()

        # 1. MaterialVendorHistory (long-term tracked)
        mvh_rows = (
            db.query(MaterialVendorHistory, MaterialCard)
            .join(
                MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id
            )
            .filter(MaterialVendorHistory.vendor_name == card.normalized_name)
            .order_by(MaterialVendorHistory.times_seen.desc())
            .limit(150)
            .all()
        )
        for mvh, mc in mvh_rows:
            key = (mc.display_mpn or "").lower()
            if key and key not in seen_mpns:
                seen_mpns.add(key)
                parts_list.append(
                    f"{mc.display_mpn} — {mvh.last_manufacturer or mc.manufacturer or 'unknown'}"
                )

        # 2. Sightings (search results) — fill remaining slots
        sighting_rows = (
            db.query(Sighting.mpn_matched, Sighting.manufacturer)
            .filter(
                sqlfunc.lower(sqlfunc.trim(Sighting.vendor_name))
                == card.normalized_name
            )
            .filter(Sighting.mpn_matched.isnot(None), Sighting.mpn_matched != "")
            .order_by(Sighting.created_at.desc())
            .limit(200)
            .all()
        )
        for mpn, mfr in sighting_rows:
            key = (mpn or "").lower()
            if key and key not in seen_mpns:
                seen_mpns.add(key)
                parts_list.append(f"{mpn} — {mfr or 'unknown'}")
            if len(parts_list) >= 200:
                break

        if not parts_list:
            return

        prompt = (
            f"Analyze this vendor's part inventory and classify them.\n\n"
            f"Vendor: {card.display_name}\n"
            f"Parts they carry ({len(parts_list)} samples):\n"
            + "\n".join(parts_list[:200])
            + "\n\n"
            "Return JSON with two arrays:\n"
            '- "brands": major brands/manufacturers this vendor carries (e.g., "IBM", "Dell", "HP", "Intel", "Samsung"). '
            "Only include brands with clear evidence from the parts list. Max 15.\n"
            '- "commodities": commodity categories of parts they carry (e.g., "CPU", "HDD", "DDR", "LCD", '
            '"SSD", "Power Supply", "Network Card", "Motherboard", "Memory", "GPU", "Cable", "Connector"). '
            "Use short, standard industry terms. Max 15.\n\n"
            "Return ONLY the JSON object, no explanation."
        )

        result = await claude_json(
            prompt,
            system="You are a parts classification expert for the electronic components and IT hardware industry. "
            "Analyze part numbers and manufacturers to identify brands and commodity categories.",
            model_tier="fast",
            max_tokens=512,
        )

        if not result or not isinstance(result, dict):
            return

        brands = result.get("brands", [])
        commodities = result.get("commodities", [])

        # Validate: must be lists of strings
        if isinstance(brands, list):
            card.brand_tags = [str(b).strip() for b in brands if b][:15]
        if isinstance(commodities, list):
            card.commodity_tags = [str(c).strip() for c in commodities if c][:15]
        card.material_tags_updated_at = datetime.now(timezone.utc)
        db.commit()

        log.info(
            "Material tags updated for vendor %s (card %d): %d brands, %d commodities",
            card.display_name,
            card_id,
            len(card.brand_tags),
            len(card.commodity_tags),
        )
    except Exception:
        log.exception("Material analysis failed for vendor card %d", card_id)
        if own_session:
            db.rollback()
    finally:
        if own_session:
            db.close()


@router.post("/api/vendors/{card_id}/analyze-materials")
async def analyze_vendor_materials(
    card_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    """On-demand AI analysis of vendor's material inventory to generate brand/commodity tags."""
    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    if not settings.anthropic_api_key:
        raise HTTPException(503, "AI not configured — set ANTHROPIC_API_KEY in .env")

    await _analyze_vendor_materials(card_id, db_session=db)

    # Refresh after update
    db.refresh(card)
    return {
        "ok": True,
        "brand_tags": card.brand_tags or [],
        "commodity_tags": card.commodity_tags or [],
    }
