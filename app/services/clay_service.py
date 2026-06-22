"""Clay enrichment — MCP-driven callback helpers.

Clay is now called via the MCP connector (Clay MCP tool) routed through the
enrichment router. The old outbound-webhook + callback round-trip has been
removed.

This module retains only the inbound-callback helpers so that any Clay data
delivered via MCP responses can be applied to VendorCard / Company records
using the same field-mapping and contact-persistence logic.

Gated behind ``settings.clay_enrichment_enabled`` and
``settings.clay_cooldown_minutes`` (the circuit breaker used by the MCP path).

Called by: (future MCP apply path, or directly in tests)
Depends on: app/cache/intel_cache, app/services/enrichment_credit_guard,
            app/enrichment_service (apply_*)
"""

from loguru import logger

from app.cache.intel_cache import get_cached, set_cached

_CORR_PREFIX = "clay:corr:"
_CORR_TTL_DAYS = 7
_CONSUMED_TTL_DAYS = 1  # keep a tombstone briefly to block replays

MAX_CALLBACK_BYTES = 256 * 1024
MAX_CALLBACK_CONTACTS = 100

_COMPANY_FIELDS = (
    "legal_name",
    "industry",
    "employee_size",
    "hq_city",
    "hq_state",
    "hq_country",
    "linkedin_url",
    "website",
)


def _corr_key(token: str) -> str:
    return f"{_CORR_PREFIX}{token}"


# ── Callback: apply an enriched row ──────────────────────────────────


def _confidence_from_marker(marker) -> int:
    if marker is None:
        return 70
    if isinstance(marker, (int, float)):
        return int(min(100, marker)) if marker > 1 else int(marker * 100)
    m = str(marker).strip().lower()
    return {
        "a": 90,
        "high": 90,
        "verified": 90,
        "valid": 90,
        "b": 70,
        "medium": 70,
        "c": 40,
        "low": 40,
        "invalid": 40,
    }.get(m, 70)


def handle_callback(payload: dict, db) -> dict:
    """Apply an enriched row from Clay (via MCP or any caller).

    Returns a summary dict.
    """
    from app.enrichment_service import (
        apply_enrichment_to_company,
        apply_enrichment_to_vendor,
    )

    token = payload.get("correlation_token") or payload.get("token")
    if not token:
        return {"status": "rejected", "reason": "missing correlation_token"}

    corr = get_cached(_corr_key(token))
    if not corr:
        return {"status": "rejected", "reason": "unknown_or_expired_token"}
    if corr.get("consumed"):
        return {"status": "rejected", "reason": "token_already_used"}

    entity_type = corr["entity_type"]
    entity_id = corr["entity_id"]

    company_in = payload.get("company") if isinstance(payload.get("company"), dict) else payload
    firmographics = {"source": "clay"}
    for f in _COMPANY_FIELDS:
        if company_in.get(f):
            firmographics[f] = company_in[f]

    applied: list[str] = []
    contacts_added = 0
    raw_contacts = payload.get("contacts") or []
    contacts = raw_contacts[:MAX_CALLBACK_CONTACTS] if isinstance(raw_contacts, list) else []

    if entity_type == "vendor_card":
        from app.models import VendorCard, VendorContact

        card = db.get(VendorCard, entity_id)
        if not card:
            return {"status": "rejected", "reason": "vendor_not_found"}
        applied = apply_enrichment_to_vendor(card, firmographics)
        contacts_added = _add_vendor_contacts(db, VendorContact, card.id, contacts)
    else:
        from app.models import Company, CustomerSite, SiteContact

        company = db.get(Company, entity_id)
        if not company:
            return {"status": "rejected", "reason": "company_not_found"}
        applied = apply_enrichment_to_company(company, firmographics)
        site = db.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
        if site:
            contacts_added = _add_site_contacts(db, SiteContact, site.id, contacts)

    # One-time token: overwrite with a short-lived tombstone to block replays.
    set_cached(_corr_key(token), {"consumed": True}, ttl_days=_CONSUMED_TTL_DAYS)

    try:
        db.commit()
    except Exception as e:
        logger.error("Clay callback commit failed: {}", e)
        db.rollback()
        return {"status": "error", "reason": "commit_failed"}

    logger.info(
        "Clay callback applied for {} #{}: {} field(s), {} contact(s)",
        entity_type,
        entity_id,
        len(applied),
        contacts_added,
    )
    return {
        "status": "applied",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "company_fields": applied,
        "contacts": contacts_added,
    }


def _add_vendor_contacts(db, VendorContact, vendor_card_id: int, contacts: list) -> int:
    added = 0
    for c in contacts:
        if not isinstance(c, dict):
            continue
        email = (c.get("email") or "").strip().lower()
        name = c.get("full_name") or c.get("name")
        if not email and not name:
            continue
        if email and db.query(VendorContact).filter_by(vendor_card_id=vendor_card_id, email=email).first():
            continue
        db.add(
            VendorContact(
                vendor_card_id=vendor_card_id,
                full_name=name,
                title=c.get("title"),
                email=email or None,
                phone=c.get("phone"),
                linkedin_url=c.get("linkedin_url"),
                source="clay",
                confidence=_confidence_from_marker(c.get("email_confidence") or c.get("confidence")),
                contact_type="individual",
            )
        )
        added += 1
    return added


def _add_site_contacts(db, SiteContact, customer_site_id: int, contacts: list) -> int:
    added = 0
    for c in contacts:
        if not isinstance(c, dict):
            continue
        name = c.get("full_name") or c.get("name")
        if not name:
            continue  # SiteContact.full_name is NOT NULL
        email = (c.get("email") or "").strip().lower()
        if email and db.query(SiteContact).filter_by(customer_site_id=customer_site_id, email=email).first():
            continue
        db.add(
            SiteContact(
                customer_site_id=customer_site_id,
                full_name=name,
                title=c.get("title"),
                email=email or None,
                phone=c.get("phone"),
                enrichment_source="clay",
            )
        )
        added += 1
    return added
