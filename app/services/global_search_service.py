"""Global search service — fast SQL search + AI intent search.

Provides two search tiers:
  - fast_search(): pg_trgm fuzzy matching across 7 entity types (<100ms)
  - ai_search(): Claude Haiku intent parsing + targeted queries (<2s)

Called by: app/routers/htmx_views.py (global search endpoints)
Depends on: SQLAlchemy models, app/utils/sql_helpers.py, app/utils/claude_client.py
"""

import hashlib

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from app.models.crm import Company, SiteContact
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard, VendorContact
from app.utils.sql_helpers import escape_like

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
        return None


def _set_ai_cache(query: str, result: dict) -> None:
    """Cache AI search result in Redis."""
    try:
        from app.cache.intel_cache import set_cached

        set_cached(_ai_cache_key(query), result, ttl_days=AI_CACHE_TTL_SECONDS / 86400)
    except Exception:
        pass


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

    safe = escape_like(query.strip())
    pattern = f"%{safe}%"
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

    # --- Parts (Requirements) ---
    q = db.query(Requirement).filter(
        Requirement.primary_mpn.ilike(pattern)
        | Requirement.normalized_mpn.ilike(pattern)
        | Requirement.brand.ilike(pattern)
    )
    if use_pg:
        q = q.order_by(func.similarity(Requirement.primary_mpn, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["parts"] = [_to_dict(r, ["primary_mpn", "normalized_mpn", "brand", "requisition_id"], "part") for r in rows]
    all_results.extend(groups["parts"])

    # --- Offers ---
    q = db.query(Offer).filter(Offer.vendor_name.ilike(pattern) | Offer.mpn.ilike(pattern))
    if use_pg:
        q = q.order_by(func.similarity(Offer.mpn, query).desc())
    rows = q.limit(RESULT_LIMIT).all()
    groups["offers"] = [
        _to_dict(r, ["vendor_name", "mpn", "unit_price", "qty_available", "requisition_id"], "offer") for r in rows
    ]
    all_results.extend(groups["offers"])

    # --- Best match: first result from first non-empty group ---
    best = all_results[0] if all_results else None

    return {
        "best_match": best,
        "groups": groups,
        "total_count": len(all_results),
    }
