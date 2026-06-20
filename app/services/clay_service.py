"""Clay enrichment — asynchronous webhook + callback flow.

Clay has NO real-time REST API. The integration is two-way:

  1. We POST a row (a domain) to a Clay table's *inbound webhook*
     (CLAY_WEBHOOK_URL) with a shared secret header and a correlation token.
  2. Clay enriches asynchronously, then its outbound "HTTP API" action POSTs
     the enriched row back to /api/webhooks/clay, echoing the correlation
     token + the secret (and optionally an HMAC signature). We verify, look up
     what the token was for, and apply the firmographics/contacts.

Gated behind ``settings.clay_enrichment_enabled`` and a configured
CLAY_WEBHOOK_URL (resolved via the credential store, DB → env). Quota/rate-limit
responses trip the shared cooldown circuit (provider "clay").

Called by: app/routers/crm/enrichment.py (trigger), app/routers/v13_features/activity.py (callback).
Depends on: app/cache/intel_cache (correlation store), app/services/credential_service,
            app/services/enrichment_credit_guard (circuit), app/enrichment_service (apply_*).
"""

import hashlib
import hmac
import secrets

from loguru import logger

from app.cache.intel_cache import get_cached, set_cached
from app.config import settings
from app.http_client import http
from app.services.credential_service import get_credential_cached
from app.services.enrichment_credit_guard import circuit_open, trip_circuit

_CORR_PREFIX = "clay:corr:"
_CORR_TTL_DAYS = 7
_CONSUMED_TTL_DAYS = 1  # keep a tombstone briefly to block replays

MAX_CALLBACK_BYTES = 256 * 1024
MAX_CALLBACK_CONTACTS = 100
_QUOTA_STATUSES = (402, 429)

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


def _webhook_url() -> str:
    return get_credential_cached("clay_enrichment", "CLAY_WEBHOOK_URL") or ""


def _secret() -> str:
    return get_credential_cached("clay_enrichment", "CLAY_CALLBACK_SECRET") or ""


def _corr_key(token: str) -> str:
    return f"{_CORR_PREFIX}{token}"


def enabled_and_configured() -> bool:
    """True only when Clay is feature-on AND an inbound webhook URL is set."""
    return bool(settings.clay_enrichment_enabled and _webhook_url())


# ── Outbound: request an async enrichment ────────────────────────────


async def request_enrichment(domain: str, entity_type: str, entity_id: int) -> dict:
    """POST a domain to Clay's inbound webhook; Clay calls us back later.

    Never raises — returns a status dict and degrades gracefully.
    """
    if not enabled_and_configured():
        return {"status": "skipped", "reason": "clay_disabled_or_unconfigured"}
    if entity_type not in ("company", "vendor_card"):
        return {"status": "error", "reason": f"unsupported entity_type {entity_type}"}
    if circuit_open("clay"):
        return {"status": "skipped", "reason": "circuit_open"}

    token = secrets.token_urlsafe(24)
    set_cached(
        _corr_key(token),
        {"entity_type": entity_type, "entity_id": entity_id, "domain": domain},
        ttl_days=_CORR_TTL_DAYS,
    )

    headers = {"Content-Type": "application/json"}
    secret = _secret()
    if secret:
        headers["x-clay-secret"] = secret
    body = {
        "domain": domain,
        "correlation_token": token,
        "callback_url": f"{settings.app_url.rstrip('/')}/api/webhooks/clay",
    }

    try:
        resp = await http.post(_webhook_url(), headers=headers, json=body, timeout=15)
    except Exception as e:
        logger.warning("Clay webhook POST failed for {}: {}", domain, e)
        return {"status": "error", "reason": str(e)}

    if resp.status_code in _QUOTA_STATUSES:
        trip_circuit("clay", settings.clay_cooldown_minutes)
        logger.warning("Clay quota/rate-limit ({}) — tripping circuit", resp.status_code)
        return {"status": "error", "reason": f"quota {resp.status_code}"}
    if resp.status_code not in (200, 201, 202):
        logger.warning("Clay webhook rejected row for {}: {}", domain, resp.status_code)
        return {"status": "error", "reason": f"webhook returned {resp.status_code}"}

    logger.info("Clay enrichment requested for {} (token {}…)", domain, token[:8])
    return {"status": "requested", "correlation_token": token}


# ── Callback auth ────────────────────────────────────────────────────


def verify_secret(provided: str | None) -> bool:
    """Timing-safe shared-secret check.

    Rejects when no secret is configured.
    """
    expected = _secret()
    if not expected:
        logger.warning("Clay callback hit but CLAY_CALLBACK_SECRET not configured — rejecting")
        return False
    return hmac.compare_digest(expected, provided or "")


def verify_signature(raw_body: bytes, provided: str | None) -> bool:
    """Optional HMAC-SHA256 body signature (keyed by the secret).

    Accepts a
    ``sha256=`` prefix. False when no secret/signature present.
    """
    secret = _secret()
    if not secret or not provided:
        return False
    prov = provided.split("=", 1)[1] if provided.startswith("sha256=") else provided
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, prov)


# ── Callback: apply the enriched row ─────────────────────────────────


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
    """Apply an enriched row Clay POSTed back.

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
