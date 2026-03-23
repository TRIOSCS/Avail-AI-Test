"""Global search service — fast SQL search + AI intent search.

Provides two search tiers:
  - fast_search(): pg_trgm fuzzy matching across 7 entity types (<100ms)
  - ai_search(): Claude Haiku intent parsing + targeted queries (<2s)

Called by: app/routers/htmx_views.py (global search endpoints)
Depends on: SQLAlchemy models, app/utils/sql_helpers.py, app/utils/claude_client.py
"""

import hashlib

from loguru import logger
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

from app.models.crm import Company, SiteContact
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard, VendorContact
from app.utils.claude_client import claude_structured
from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError
from app.utils.search_builder import SearchBuilder

RESULT_LIMIT = 5

# ── Cache helpers (used by ai_search in Task 4) ──────────────────────

AI_CACHE_TTL_SECONDS = 300  # 5 minutes


def _ai_cache_key(query: str) -> str:
    normalized = query.lower().strip()
    h = hashlib.md5(normalized.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"ai_search:{h}"


def _get_ai_cache(query: str) -> dict | None:
    """Check Redis for cached AI search result."""
    try:
        from app.cache.intel_cache import get_cached

        return get_cached(_ai_cache_key(query))
    except Exception:
        logger.debug("AI search cache read failed", exc_info=True)
        return None


def _set_ai_cache(query: str, result: dict) -> None:
    """Cache AI search result in Redis."""
    try:
        from app.cache.intel_cache import set_cached

        set_cached(_ai_cache_key(query), result, ttl_days=AI_CACHE_TTL_SECONDS / 86400)
    except Exception as e:
        logger.warning("AI search cache write failed: %s", e)


# ── Helpers ───────────────────────────────────────────────────────────


def _is_postgres(db: Session) -> bool:
    """Check if the DB backend is PostgreSQL (vs SQLite in tests)."""
    return db.bind.dialect.name == "postgresql"


def _to_dict(obj, fields: list[str], entity_type: str) -> dict:
    """Convert a SQLAlchemy model to a search result dict."""
    d = {"type": entity_type, "id": obj.id}
    for f in fields:
        val = getattr(obj, f, None)
        # Convert non-serializable types to string
        d[f] = val
    return d


# ── Empty result template ─────────────────────────────────────────────

EMPTY_GROUPS = {
    "requisitions": [],
    "companies": [],
    "vendors": [],
    "vendor_contacts": [],
    "site_contacts": [],
    "parts": [],
    "offers": [],
}


def _empty_result() -> dict:
    return {"best_match": None, "groups": {k: [] for k in EMPTY_GROUPS}, "total_count": 0}


# ── Fast search (Tier 1) ─────────────────────────────────────────────


def fast_search(query: str, db: Session) -> dict:
    """Search all entities with ILIKE + pg_trgm fuzzy matching.

    Sync function — FastAPI runs it in a thread pool from async handlers. Falls back to
    plain ILIKE on SQLite (test mode).
    """
    if not query or len(query.strip()) < 2:
        return _empty_result()

    sb = SearchBuilder(query.strip())
    pattern = f"%{sb.safe}%"
    use_pg = _is_postgres(db)

    groups = {}
    all_results = []

    # --- Requisitions ---
    q = db.query(Requisition).filter(Requisition.name.ilike(pattern) | Requisition.customer_name.ilike(pattern))
    if use_pg:
        q = q.order_by(
            func.greatest(
                func.similarity(Requisition.name, query),
                func.similarity(Requisition.customer_name, query),
            ).desc()
        )
    rows = q.limit(RESULT_LIMIT).all()
    groups["requisitions"] = [_to_dict(r, ["name", "customer_name", "status"], "requisition") for r in rows]
    all_results.extend(groups["requisitions"])

    # --- Companies ---
    q = db.query(Company).filter(Company.name.ilike(pattern) | Company.domain.ilike(pattern))
    if use_pg:
        q = q.order_by(func.similarity(Company.name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["companies"] = [_to_dict(r, ["name", "domain", "account_type"], "company") for r in rows]
    all_results.extend(groups["companies"])

    # --- Vendors (includes JSON emails/phones cast to string) ---
    q = db.query(VendorCard).filter(
        VendorCard.display_name.ilike(pattern)
        | VendorCard.normalized_name.ilike(pattern)
        | VendorCard.domain.ilike(pattern)
        | cast(VendorCard.emails, String).ilike(pattern)
        | cast(VendorCard.phones, String).ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(VendorCard.display_name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["vendors"] = [_to_dict(r, ["display_name", "domain"], "vendor") for r in rows]
    all_results.extend(groups["vendors"])

    # --- Vendor Contacts ---
    q = db.query(VendorContact).filter(
        VendorContact.full_name.ilike(pattern) | VendorContact.email.ilike(pattern) | VendorContact.phone.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(VendorContact.full_name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["vendor_contacts"] = [_to_dict(r, ["full_name", "email", "phone", "title"], "vendor_contact") for r in rows]
    all_results.extend(groups["vendor_contacts"])

    # --- Site Contacts ---
    q = db.query(SiteContact).filter(
        SiteContact.full_name.ilike(pattern) | SiteContact.email.ilike(pattern) | SiteContact.phone.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(SiteContact.full_name, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["site_contacts"] = [_to_dict(r, ["full_name", "email", "phone", "title"], "site_contact") for r in rows]
    all_results.extend(groups["site_contacts"])

    # --- Parts (Requirements) — dedup by normalized_mpn so same part across reqs shows once ---
    q = db.query(Requirement).filter(
        Requirement.primary_mpn.ilike(pattern)
        | Requirement.normalized_mpn.ilike(pattern)
        | Requirement.brand.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(Requirement.primary_mpn, query).desc())
    rows = q.limit(RESULT_LIMIT * 3).all()  # fetch extra to dedup from
    seen_mpns: set[str] = set()
    deduped_parts = []
    for r in rows:
        mpn_key = (r.normalized_mpn or r.primary_mpn or "").lower()
        if mpn_key not in seen_mpns:
            seen_mpns.add(mpn_key)
            deduped_parts.append(_to_dict(r, ["primary_mpn", "normalized_mpn", "brand", "requisition_id"], "part"))
            if len(deduped_parts) >= RESULT_LIMIT:
                break
    groups["parts"] = deduped_parts
    all_results.extend(groups["parts"])

    # --- Offers — dedup by (mpn, vendor_name) so same offer combo shows once ---
    q = db.query(Offer).filter(Offer.vendor_name.ilike(pattern) | Offer.mpn.ilike(pattern))
    if use_pg:
        q = q.order_by(func.similarity(Offer.mpn, query).desc())
    rows = q.limit(RESULT_LIMIT * 3).all()
    seen_offers: set[tuple[str, str]] = set()
    deduped_offers = []
    for r in rows:
        offer_key = ((r.mpn or "").lower(), (r.vendor_name or "").lower())
        if offer_key not in seen_offers:
            seen_offers.add(offer_key)
            deduped_offers.append(
                _to_dict(r, ["vendor_name", "mpn", "unit_price", "qty_available", "requisition_id"], "offer")
            )
            if len(deduped_offers) >= RESULT_LIMIT:
                break
    groups["offers"] = deduped_offers
    all_results.extend(groups["offers"])

    # --- Best match: first result from first non-empty group ---
    best = all_results[0] if all_results else None

    return {
        "best_match": best,
        "groups": groups,
        "total_count": len(all_results),
    }


# ── AI Search (Tier 2) ──────────────────────────────────────────────

SEARCH_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "searches": {
            "type": "array",
            "description": "One or more search operations to perform",
            "items": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": [
                            "requisition",
                            "company",
                            "vendor",
                            "vendor_contact",
                            "site_contact",
                            "part",
                            "offer",
                        ],
                    },
                    "text_query": {
                        "type": "string",
                        "description": "Free-text to search/match against",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Structured filters to apply",
                        "properties": {
                            "status": {"type": "string"},
                            "customer_name": {"type": "string"},
                            "vendor_name": {"type": "string"},
                            "brand": {"type": "string"},
                            "email_domain": {"type": "string"},
                            "is_blacklisted": {"type": "boolean"},
                        },
                    },
                },
                "required": ["entity_type", "text_query"],
            },
        },
    },
    "required": ["searches"],
}

SEARCH_SYSTEM_PROMPT = """\
You are a search intent parser for an electronic component sourcing platform.
Given a user's search query, determine which entities they want to find and what
filters to apply.

Available entities:
- requisition: Purchase requests. Fields: name, customer_name, status (active/closed/cancelled)
- company: Customer/prospect companies. Fields: name, domain, account_type (Customer/Prospect/Partner/Competitor)
- vendor: Component suppliers. Fields: display_name, domain, is_blacklisted
- vendor_contact: People at vendor companies. Fields: full_name, email, phone, title
- site_contact: People at customer companies. Fields: full_name, email, phone, title, contact_role (buyer/technical/decision_maker)
- part: Component requirements. Fields: primary_mpn, normalized_mpn, brand, sourcing_status (open/sourcing/offered/quoted/won/lost)
- offer: Vendor price quotes. Fields: mpn, vendor_name, status (active/sold)

Rules:
- If the query looks like an email address, search vendor_contacts and site_contacts by email
- If the query looks like a phone number, search vendor_contacts and site_contacts by phone
- If the query looks like a part number (alphanumeric with dashes), search parts and offers by MPN
- If the query mentions a company by name, search companies and vendors
- If the query is ambiguous, return multiple searches to cover likely intents
- Always set text_query to the relevant search term extracted from the natural language

Examples:
- "LM358" -> search parts (text_query="LM358") + offers (text_query="LM358")
- "john@acme.com" -> search vendor_contacts (text_query="john@acme.com") + site_contacts (text_query="john@acme.com")
- "open reqs for Raytheon" -> search requisitions (text_query="Raytheon", filters={status:"active", customer_name:"Raytheon"})
- "who sells LM317?" -> search parts (text_query="LM317") + offers (text_query="LM317") + vendors (text_query="LM317")\
"""

# Map entity_type -> (Model, search_fields, display_fields, group_key)
_ENTITY_CONFIG = {
    "requisition": (Requisition, ["name", "customer_name"], ["name", "customer_name", "status"], "requisitions"),
    "company": (Company, ["name", "domain"], ["name", "domain", "account_type"], "companies"),
    "vendor": (VendorCard, ["display_name", "normalized_name", "domain"], ["display_name", "domain"], "vendors"),
    "vendor_contact": (
        VendorContact,
        ["full_name", "email", "phone"],
        ["full_name", "email", "phone", "title"],
        "vendor_contacts",
    ),
    "site_contact": (
        SiteContact,
        ["full_name", "email", "phone"],
        ["full_name", "email", "phone", "title"],
        "site_contacts",
    ),
    "part": (
        Requirement,
        ["primary_mpn", "normalized_mpn", "brand"],
        ["primary_mpn", "normalized_mpn", "brand", "requisition_id"],
        "parts",
    ),
    "offer": (
        Offer,
        ["vendor_name", "mpn"],
        ["vendor_name", "mpn", "unit_price", "qty_available", "requisition_id"],
        "offers",
    ),
}

# Map filter names to (entity_type -> model attribute)
_FILTER_MAP = {
    "status": {"requisition": "status", "offer": "status", "part": "sourcing_status"},
    "customer_name": {"requisition": "customer_name"},
    "vendor_name": {"offer": "vendor_name"},
    "brand": {"part": "brand"},
    "email_domain": {"vendor_contact": "email", "site_contact": "email"},
    "is_blacklisted": {"vendor": "is_blacklisted"},
}


def _run_intent_query(search_op: dict, db: Session) -> tuple[str, list[dict]]:
    """Execute a single search intent operation and return (group_key, results)."""
    entity_type = search_op.get("entity_type", "")
    text_query = search_op.get("text_query", "")
    filters = search_op.get("filters", {})

    config = _ENTITY_CONFIG.get(entity_type)
    if not config:
        return ("", [])

    model, search_fields, display_fields, group_key = config
    sb_intent = SearchBuilder(text_query.strip()) if text_query.strip() else None
    pattern = f"%{sb_intent.safe}%" if sb_intent and sb_intent.safe else None

    # Build ILIKE conditions on search fields
    conditions = []
    if pattern:
        for field_name in search_fields:
            col = getattr(model, field_name, None)
            if col is not None:
                conditions.append(col.ilike(pattern))

    if not conditions:
        return (group_key, [])

    q = db.query(model).filter(or_(*conditions))

    # Apply structured filters
    for filter_name, filter_value in (filters or {}).items():
        entity_attrs = _FILTER_MAP.get(filter_name, {})
        attr_name = entity_attrs.get(entity_type)
        if not attr_name:
            continue

        col = getattr(model, attr_name, None)
        if col is None:
            continue

        if filter_name == "email_domain":
            # Domain filter: ILIKE %@domain
            sb_filter = SearchBuilder(str(filter_value))
            q = q.filter(col.ilike(f"%@{sb_filter.safe}%"))
        elif filter_name == "is_blacklisted":
            q = q.filter(col == filter_value)
        else:
            # Exact-ish match via ILIKE for text filters
            sb_filter = SearchBuilder(str(filter_value))
            q = q.filter(col.ilike(f"%{sb_filter.safe}%"))

    # Dedup parts by normalized_mpn, offers by (mpn, vendor_name)
    if entity_type == "part":
        rows = q.limit(RESULT_LIMIT * 3).all()
        seen: set[str] = set()
        deduped = []
        for r in rows:
            mpn_key = (getattr(r, "normalized_mpn", None) or getattr(r, "primary_mpn", None) or "").lower()
            if mpn_key not in seen:
                seen.add(mpn_key)
                deduped.append(_to_dict(r, display_fields, entity_type))
                if len(deduped) >= RESULT_LIMIT:
                    break
        return (group_key, deduped)

    if entity_type == "offer":
        rows = q.limit(RESULT_LIMIT * 3).all()
        seen_offers: set[tuple[str, str]] = set()
        deduped = []
        for r in rows:
            key = ((getattr(r, "mpn", None) or "").lower(), (getattr(r, "vendor_name", None) or "").lower())
            if key not in seen_offers:
                seen_offers.add(key)
                deduped.append(_to_dict(r, display_fields, entity_type))
                if len(deduped) >= RESULT_LIMIT:
                    break
        return (group_key, deduped)

    rows = q.limit(RESULT_LIMIT).all()
    return (group_key, [_to_dict(r, display_fields, entity_type) for r in rows])


async def ai_search(query: str, db: Session) -> dict:
    """AI-powered intent search using Claude Haiku.

    This function IS async because it awaits claude_structured(). The sync DB queries
    within it are fine — FastAPI handles the mix.

    Returns same structure as fast_search() for template compatibility. Falls back to
    fast_search() on Claude failure.
    """
    # Check cache first
    cached = _get_ai_cache(query)
    if cached is not None:
        return cached

    # Call Claude for intent parsing
    try:
        intent = await claude_structured(
            query,
            SEARCH_INTENT_SCHEMA,
            system=SEARCH_SYSTEM_PROMPT,
            model_tier="fast",
            timeout=10,
        )
    except ClaudeUnavailableError:
        logger.info("Claude not configured — falling back to fast_search")
        return fast_search(query, db)
    except ClaudeError as e:
        logger.warning("Claude AI failed for search intent: %s", e)
        return fast_search(query, db)

    if not intent or "searches" not in intent:
        logger.debug("AI search: Claude returned None, falling back to fast_search")
        return fast_search(query, db)

    # Execute targeted queries per intent, dedup by (type, id)
    groups = {k: [] for k in EMPTY_GROUPS}
    seen: set[tuple[str, int]] = set()

    for search_op in intent["searches"]:
        group_key, results = _run_intent_query(search_op, db)
        if group_key and results:
            for r in results:
                key = (r["type"], r["id"])
                if key not in seen:
                    seen.add(key)
                    groups[group_key].append(r)

    all_results = [r for g in groups.values() for r in g]
    best = all_results[0] if all_results else None
    result = {
        "best_match": best,
        "groups": groups,
        "total_count": len(all_results),
    }

    # Cache successful result
    _set_ai_cache(query, result)

    return result
