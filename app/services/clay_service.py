"""Clay enrichment service — asynchronous webhook + callback flow.

Clay has NO real-time REST API. Instead the integration is two-way:

  1. We POST a row (a domain) to a Clay table's *inbound webhook* URL
     (CLAY_WEBHOOK_URL) together with a shared secret header and a
     correlation token we generate. Clay enriches the row asynchronously.
  2. Clay's outbound "HTTP API" action POSTs the enriched row back to our
     callback endpoint (/api/webhooks/clay), echoing the correlation token
     and the shared secret. We verify both, look up what the token was for,
     and route the enriched fields into the EnrichmentQueue for review.

Called by:
  - app.services.deep_enrichment_service (to kick off async enrichment)
  - app.routers.v13_features.clay_webhook (the callback endpoint)
Depends on:
  - app.cache.intel_cache (correlation-token store, survives restarts)
  - app.services.credential_service (webhook URL + secret, DB→env fallback)
  - app.services.deep_enrichment_service.route_enrichment (confidence routing)
"""

import hmac
import json
import logging

from app.cache.intel_cache import get_cached, invalidate, set_cached
from app.config import settings
from app.http_client import http
from app.services.credential_service import get_credential_cached

log = logging.getLogger("avail.clay")

# Correlation tokens live in the intel cache for 7 days — long enough for
# Clay to finish even a slow waterfall, short enough to self-clean.
_CORR_PREFIX = "clay:corr:"
_CORR_TTL_DAYS = 7

# Company firmographic fields Clay may return that we know how to apply.
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


def _clay_webhook_url() -> str:
    """Clay table inbound webhook URL (DB credential → env fallback)."""
    return get_credential_cached("clay_enrichment", "CLAY_WEBHOOK_URL") or ""


def _clay_secret() -> str:
    """Shared secret echoed in the callback header (DB credential → env fallback)."""
    return get_credential_cached("clay_enrichment", "CLAY_CALLBACK_SECRET") or ""


def _corr_key(token: str) -> str:
    return f"{_CORR_PREFIX}{token}"


def _new_token() -> str:
    # secrets.token_urlsafe avoids Math.random-style determinism concerns and
    # is collision-safe for correlation purposes.
    import secrets

    return secrets.token_urlsafe(24)


async def request_clay_enrichment(
    domain: str, entity_type: str, entity_id: int
) -> dict:
    """Kick off an async Clay enrichment for a domain.

    Stores a correlation token mapping {entity_type, entity_id, domain} and
    POSTs the row to the Clay inbound webhook. Clay calls us back later.

    Returns a status dict; never raises (degrades gracefully when unconfigured).
    """
    webhook_url = _clay_webhook_url()
    if not webhook_url:
        log.debug("Clay webhook URL not configured — skipping Clay enrichment")
        return {"status": "skipped", "reason": "clay_not_configured"}

    if entity_type not in ("vendor_card", "company"):
        return {"status": "error", "reason": f"unsupported entity_type {entity_type}"}

    token = _new_token()
    set_cached(
        _corr_key(token),
        {"entity_type": entity_type, "entity_id": entity_id, "domain": domain},
        ttl_days=_CORR_TTL_DAYS,
    )

    callback_url = f"{settings.app_url.rstrip('/')}/api/webhooks/clay"
    headers = {"Content-Type": "application/json"}
    secret = _clay_secret()
    if secret:
        # Echoed back to us by Clay's outbound action so we can authenticate it.
        headers["x-clay-secret"] = secret

    body = {
        "domain": domain,
        "correlation_token": token,
        "callback_url": callback_url,
    }

    try:
        resp = await http.post(webhook_url, headers=headers, json=body, timeout=15)
    except Exception as e:
        log.warning("Clay webhook POST failed for %s: %s", domain, e)
        invalidate(_corr_key(token))
        return {"status": "error", "reason": str(e)}

    if resp.status_code not in (200, 201, 202):
        log.warning(
            "Clay webhook rejected row for %s: %s %s",
            domain, resp.status_code, resp.text[:200],
        )
        invalidate(_corr_key(token))
        return {"status": "error", "reason": f"webhook returned {resp.status_code}"}

    log.info("Clay enrichment requested for %s (token=%s…)", domain, token[:8])
    return {"status": "requested", "correlation_token": token}


def verify_clay_secret(provided: str | None) -> bool:
    """Timing-safe check of the callback's shared secret.

    Rejects when no secret is configured — the callback must not be open.
    """
    expected = _clay_secret()
    if not expected:
        log.warning("Clay callback hit but CLAY_CALLBACK_SECRET is not configured — rejecting")
        return False
    return hmac.compare_digest(expected, provided or "")


def _confidence_from_email_marker(marker) -> float:
    """Map Clay/Lusha-style email confidence markers to a 0..1 score."""
    if marker is None:
        return 0.7
    if isinstance(marker, (int, float)):
        # Treat as a 0..100 percentage if large, else 0..1.
        return min(1.0, marker / 100.0) if marker > 1 else float(marker)
    m = str(marker).strip().lower()
    if m in ("a", "a+", "a1", "high", "verified", "valid"):
        return 0.9
    if m in ("b", "medium", "probable", "catch-all", "catch_all"):
        return 0.7
    if m in ("c", "low", "invalid"):
        return 0.4
    return 0.7


def handle_clay_callback(payload: dict, db) -> dict:
    """Process an enriched row Clay POSTed back to our callback.

    Looks up the correlation token, maps company firmographics + contacts, and
    routes each into the EnrichmentQueue. Returns a summary dict.
    """
    from app.services.deep_enrichment_service import route_enrichment

    token = payload.get("correlation_token") or payload.get("token")
    if not token:
        return {"status": "rejected", "reason": "missing correlation_token"}

    corr = get_cached(_corr_key(token))
    if not corr:
        log.warning("Clay callback with unknown/expired token %s…", str(token)[:8])
        return {"status": "rejected", "reason": "unknown_or_expired_token"}

    entity_type = corr["entity_type"]
    entity_id = corr["entity_id"]

    # Company firmographics may be nested under "company" or flat at top level.
    company = payload.get("company") if isinstance(payload.get("company"), dict) else payload
    applied = []
    for field in _COMPANY_FIELDS:
        value = company.get(field)
        if value:
            route_enrichment(
                db, entity_type, entity_id, field,
                None, value,
                confidence=0.8,
                source="clay",
                enrichment_type="company_info",
            )
            applied.append(field)

    # Contacts → contact_info enrichment items keyed by email.
    contacts = payload.get("contacts") or []
    contacts_added = 0
    if isinstance(contacts, list):
        for c in contacts:
            if not isinstance(c, dict):
                continue
            email = (c.get("email") or "").strip().lower()
            if not email:
                continue
            confidence = _confidence_from_email_marker(
                c.get("email_confidence") or c.get("confidence")
            )
            contact_data = {
                "full_name": c.get("full_name") or c.get("name"),
                "title": c.get("title"),
                "email": email,
                "phone": c.get("phone"),
                "linkedin_url": c.get("linkedin_url"),
                "source": "clay",
            }
            route_enrichment(
                db, entity_type, entity_id,
                f"new_contact:{email}",
                None, json.dumps(contact_data),
                confidence=confidence,
                source="clay",
                enrichment_type="contact_info",
            )
            contacts_added += 1

    try:
        db.commit()
    except Exception as e:
        log.error("Clay callback commit failed: %s", e)
        db.rollback()
        return {"status": "error", "reason": "commit_failed"}

    # One-time token — drop it so a replay can't re-apply.
    invalidate(_corr_key(token))

    log.info(
        "Clay callback applied for %s #%s: %d company field(s), %d contact(s)",
        entity_type, entity_id, len(applied), contacts_added,
    )
    return {
        "status": "applied",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "company_fields": applied,
        "contacts": contacts_added,
    }
