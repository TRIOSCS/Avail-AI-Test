"""utils/vendor_helpers.py — Shared helpers for vendor-related routes.

Contains VendorCard creation/lookup, serialization, contact-cleaning utilities,
website scraping, and merge logic used across vendor CRUD, contacts, materials,
and analytics routers.

Called by: app.routers.vendors_crud, app.routers.vendor_contacts,
           app.routers.materials, app.routers.vendor_analytics, app.routers.vendors
Depends on: models, vendor_utils, cache, enrichment_service, config
"""

import asyncio
import ipaddress
import os
import re
import socket

from loguru import logger
from sqlalchemy import text as sqltext
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from ..http_client import http_redirect
from ..models import VendorCard, VendorReview
from ..services.credential_service import get_credential_cached
from ..services.specialty_detector import commodity_slug_to_display
from ..services.vendor_analysis_service import _analyze_vendor_materials
from ..shared_constants import JUNK_DOMAINS as _JUNK_DOMAINS
from ..shared_constants import JUNK_EMAIL_PREFIXES as _JUNK_EMAILS
from ..vendor_utils import fuzzy_score_vendor, normalize_vendor_name

# ── Constants ────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


# ── Helper Functions ─────────────────────────────────────────────────────


def _extract_domain_from_name(name: str) -> str | None:
    """Try to extract a likely domain from a vendor name (e.g. 'Digi-Key' ->
    'digikey')."""
    cleaned = re.sub(r"[^a-z0-9]", "", name.lower())
    return cleaned if len(cleaned) >= 3 else None


def get_or_create_card(vendor_name: str, db: Session, domain: str | None = None) -> VendorCard:
    """Find existing VendorCard by normalized name, domain, or fuzzy match, or create
    new.

    1. Exact normalized match (fastest path)
    2. Domain match — if a domain is provided, merge into existing card with same domain
    3. Fuzzy match with threshold >= 82 -- auto-merge to avoid duplicates
    4. No match -- create new card
    """
    norm = normalize_vendor_name(vendor_name)
    card = db.query(VendorCard).filter_by(normalized_name=norm).first()
    if card:
        return card

    # Domain-based dedup: if the same domain already exists, merge into that card
    if domain:
        from sqlalchemy import func as sqlfunc

        domain_lower = domain.strip().lower()
        card = db.query(VendorCard).filter(sqlfunc.lower(VendorCard.domain) == domain_lower).first()
        if card:
            alts = list(card.alternate_names or [])
            if vendor_name not in alts and vendor_name != card.display_name:
                alts.append(vendor_name)
                card.alternate_names = alts
                db.commit()
            logger.info(
                "Domain-matched vendor '%s' to '%s' (domain=%s)",
                vendor_name,
                card.display_name,
                domain_lower,
            )
            return card

    # Fuzzy match: use pg_trgm on PostgreSQL, fall back to rapidfuzz
    if not os.environ.get("TESTING"):  # pragma: no cover
        try:
            trgm_rows = db.execute(
                sqltext(
                    "SELECT id, normalized_name, similarity(normalized_name, :q) AS sim "
                    "FROM vendor_cards WHERE normalized_name % :q "
                    "ORDER BY sim DESC LIMIT 5"
                ),
                {"q": norm},
            ).fetchall()
            if trgm_rows and trgm_rows[0].sim >= 0.6:
                card = db.get(VendorCard, trgm_rows[0].id)
                if card:
                    alts = list(card.alternate_names or [])
                    if vendor_name not in alts and vendor_name != card.display_name:
                        alts.append(vendor_name)
                        card.alternate_names = alts
                        db.commit()
                    logger.info(
                        "pg_trgm matched vendor '%s' to '%s' (sim=%.2f)",
                        vendor_name,
                        card.display_name,
                        trgm_rows[0].sim,
                    )
                    return card
        except (ProgrammingError, OperationalError):
            pass  # pg_trgm not available -- fall through to rapidfuzz

    try:
        existing = db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.display_name).limit(500).all()
        best_score, best_card_id = 0, None
        for row in existing:
            score = fuzzy_score_vendor(norm, row.normalized_name)
            if score > best_score:
                best_score = score
                best_card_id = row.id
        if best_score >= 82 and best_card_id:
            card = db.get(VendorCard, best_card_id)
            if card:
                alts = list(card.alternate_names or [])
                if vendor_name not in alts and vendor_name != card.display_name:
                    alts.append(vendor_name)
                    card.alternate_names = alts
                    db.commit()
                logger.info(
                    "Fuzzy-matched vendor '%s' to '%s' (score=%d)",
                    vendor_name,
                    card.display_name,
                    best_score,
                )
                return card
    except ImportError:
        pass  # rapidfuzz not installed -- skip fuzzy matching

    card = VendorCard(normalized_name=norm, display_name=vendor_name, emails=[], phones=[])
    db.add(card)
    db.commit()
    return card


async def _background_enrich_vendor(card_id: int, domain: str, vendor_name: str):
    """Fire-and-forget enrichment for a vendor card.

    Runs in background.
    """
    from ..database import SessionLocal
    from ..enrichment_service import apply_enrichment_to_vendor, enrich_entity

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
                logger.info(
                    "Background enrichment completed for vendor %s (card %d): %s",
                    vendor_name,
                    card_id,
                    enrichment.get("source", "unknown"),
                )
        finally:
            db.close()
    except Exception:
        logger.exception("Background enrichment failed for vendor card %d", card_id)

    # Also run AI material analysis if vendor has sighting data
    if get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        try:
            await _analyze_vendor_materials(card_id)
        except Exception:
            logger.exception("Background material analysis failed for vendor card %d", card_id)


def _load_entity_tags(entity_type: str, entity_id: int, db: Session) -> list[dict]:
    """Load tags for any entity. Prefers visible tags, falls back to all tags.

    Shared by vendor + company detail.
    """
    from ..models.tags import EntityTag

    # Try visible tags first
    tags = (
        db.query(EntityTag)
        .filter(EntityTag.entity_type == entity_type, EntityTag.entity_id == entity_id, EntityTag.is_visible.is_(True))
        .order_by(EntityTag.interaction_count.desc())
        .all()
    )

    # Fall back to all tags if none are visible (strict two-gate threshold not yet met)
    if not tags:
        tags = (
            db.query(EntityTag)
            .filter(EntityTag.entity_type == entity_type, EntityTag.entity_id == entity_id)
            .order_by(EntityTag.interaction_count.desc())
            .limit(20)
            .all()
        )

    return [
        {
            "tag_name": et.tag.name,
            "tag_type": et.tag.tag_type,
            "count": et.interaction_count,
            "is_visible": et.is_visible,
        }
        for et in tags
    ]


def card_to_dict(card: VendorCard, db: Session) -> dict:
    """Serialize a VendorCard with reviews, brand profile, and engagement metrics.

    Uses Redis cache (6h TTL) for expensive brand/MPN aggregation queries.
    """
    from sqlalchemy.orm import joinedload

    reviews = db.query(VendorReview).options(joinedload(VendorReview.user)).filter_by(vendor_card_id=card.id).all()
    avg = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else None

    # Try Redis cache for expensive material profile queries
    import json as _json

    from ..cache.intel_cache import _get_redis

    cache_key = f"vprofile:{card.id}"
    brands = None
    mpn_count = None
    r = _get_redis()
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                _data = _json.loads(cached)
                brands = _data.get("brands")
                mpn_count = _data.get("mpn_count")
        except (OSError, ValueError):
            pass

    if brands is None:
        norm = card.normalized_name
        mfr_rows = db.execute(
            sqltext("""
            SELECT manufacturer, SUM(cnt) as total FROM (
                SELECT manufacturer, COUNT(*) as cnt FROM sightings
                WHERE vendor_name_normalized = :norm
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
                WHERE vendor_name_normalized = :norm AND mpn_matched IS NOT NULL
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

        # Cache for 6 hours
        if r:
            try:
                r.setex(cache_key, 21600, _json.dumps({"brands": brands, "mpn_count": mpn_count}))
            except (OSError, TypeError):
                pass

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
        "last_enriched_at": card.last_enriched_at.isoformat() if card.last_enriched_at else None,
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
        "vendor_score": card.vendor_score,
        "advancement_score": card.advancement_score,
        "is_new_vendor": card.is_new_vendor if card.is_new_vendor is not None else True,
        "engagement_score": card.vendor_score,
        "total_outreach": card.total_outreach,
        "total_responses": card.total_responses,
        "ghost_rate": card.ghost_rate,
        "response_velocity_hours": card.response_velocity_hours,
        "last_contact_at": card.last_contact_at.isoformat() if card.last_contact_at else None,
        "brand_tags": card.brand_tags or [],
        "commodity_tags": [commodity_slug_to_display(t) for t in (card.commodity_tags or [])],
        "material_tags_updated_at": card.material_tags_updated_at.isoformat()
        if card.material_tags_updated_at
        else None,
        "tags": _load_entity_tags("vendor_card", card.id, db),
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


# ── Contact Cleaning Utilities ───────────────────────────────────────────


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
    """SSRF protection -- reject URLs pointing to private/internal networks."""
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
    """Fetch vendor website homepage + /contact page, extract emails and phones.

    Results are cached in IntelCache with a 7-day TTL keyed by domain to avoid re-
    scraping the same vendor website on every page view.
    """
    from ..cache.intel_cache import get_cached, set_cached

    # Normalize URL for cache key
    raw_url = url
    if not url.startswith("http"):
        url = "https://" + url
    url = url.rstrip("/")

    # Extract domain for cache key
    try:
        domain = url.split("//", 1)[1].split("/")[0].lower().replace("www.", "")
    except IndexError:
        domain = raw_url.lower()
    cache_key = f"scrape:{domain}"

    # Check cache first
    cached = get_cached(cache_key)
    if cached is not None:
        return cached

    emails: set[str] = set()
    phones: set[str] = set()

    loop = asyncio.get_running_loop()
    if await loop.run_in_executor(None, is_private_url, url):
        logger.warning(f"SSRF blocked: {url}")
        return {"emails": [], "phones": []}

    pages_to_try = [url + "/contact", url + "/contact-us", url]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    }

    # Fetch all pages concurrently instead of sequentially
    tasks = [http_redirect.get(page_url, headers=headers, timeout=10) for page_url in pages_to_try]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for resp in results:
        if isinstance(resp, Exception):
            continue
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

    result = {"emails": clean_emails(list(emails)), "phones": clean_phones(list(phones))}

    # Cache result for 7 days
    set_cached(cache_key, result, ttl_days=7)

    return result


def merge_contact_into_card(
    card: VendorCard,
    emails: list,
    phones: list,
    website: str = None,
    source: str = None,
) -> bool:
    """Merge new contact data into vendor card.

    Returns True if anything changed.
    """
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
