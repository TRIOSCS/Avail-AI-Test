"""Apollo sync service -- discovery, enrichment, sync, and enrollment.

Orchestrates Apollo API calls for the /api/apollo/* endpoints.
Masks emails during discovery (revealed only after enrichment).

Called by: app/routers/apollo_sync.py
Depends on: app/http_client.py, app/config.py, app/services/prospect_contacts.py
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.http_client import http
from app.models import VendorCard, VendorContact
from app.services.prospect_contacts import classify_contact_seniority, mask_email

APOLLO_BASE = "https://api.apollo.io/api/v1"


def _get_api_key() -> str:
    return getattr(settings, "apollo_api_key", "")


def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": _get_api_key(),
    }


async def discover_contacts(
    domain: str,
    title_keywords: list[str] | None = None,
    max_results: int = 10,
) -> dict:
    """Search Apollo for contacts at a domain. Returns masked preview (no raw emails).

    Returns: {domain, contacts: [{apollo_id, full_name, title, seniority, email_masked, ...}], total_found}
    """
    api_key = _get_api_key()
    if not api_key:
        return {
            "domain": domain,
            "contacts": [],
            "total_found": 0,
            "note": "Apollo API key not configured",
        }

    titles = title_keywords or [
        "procurement",
        "purchasing",
        "buyer",
        "supply chain",
        "component engineer",
        "commodity manager",
        "sourcing",
    ]

    payload = {
        "q_organization_domains": domain,
        "person_titles": titles,
        "per_page": min(max_results, 25),
        "page": 1,
    }

    try:
        resp = await http.post(
            f"{APOLLO_BASE}/mixed_people/api_search",
            json=payload,
            headers=_headers(),
            timeout=30,
        )

        if resp.status_code != 200:
            logger.warning(
                "Apollo discover failed for {}: {} {}",
                domain,
                resp.status_code,
                resp.text[:200],
            )
            return {
                "domain": domain,
                "contacts": [],
                "total_found": 0,
                "note": f"API error: {resp.status_code}",
            }

        data = resp.json()
        people = data.get("people", [])
        total = data.get("pagination", {}).get("total_entries", len(people))

        contacts = []
        for p in people:
            first = (p.get("first_name") or "").strip()
            last = (p.get("last_name") or "").strip()
            full_name = (
                f"{first} {last}".strip()
                if first or last
                else p.get("name", "Unknown")
            )
            email = p.get("email") or ""
            title = p.get("title") or p.get("headline") or ""
            org = p.get("organization") or {}

            contacts.append(
                {
                    "apollo_id": p.get("id"),
                    "full_name": full_name,
                    "title": title,
                    "seniority": classify_contact_seniority(title),
                    "email_masked": mask_email(email) if email else None,
                    "linkedin_url": p.get("linkedin_url"),
                    "company_name": org.get("name"),
                }
            )

        return {"domain": domain, "contacts": contacts, "total_found": total}

    except Exception as e:
        logger.error("Apollo discover error for {}: {}", domain, e)
        return {
            "domain": domain,
            "contacts": [],
            "total_found": 0,
            "note": str(e),
        }


async def get_credits() -> dict:
    """Fetch current Apollo credit usage from profile endpoint."""
    api_key = _get_api_key()
    if not api_key:
        return {
            "lead_credits_remaining": 0,
            "lead_credits_used": 0,
            "direct_dial_remaining": 0,
            "direct_dial_used": 0,
            "ai_credits_remaining": 0,
            "ai_credits_used": 0,
            "note": "Apollo API key not configured",
        }

    try:
        resp = await http.get(
            f"{APOLLO_BASE}/users/api_profile",
            params={"include_credit_usage": "true"},
            headers=_headers(),
            timeout=15,
        )

        if resp.status_code != 200:
            logger.warning(
                "Apollo credits fetch failed: {} {}",
                resp.status_code,
                resp.text[:200],
            )
            note = f"API error: {resp.status_code}"
            if resp.status_code == 403:
                note = "API key needs master key permissions — regenerate in Apollo Settings > API Keys with 'Set as master key' enabled"
            return {
                "lead_credits_remaining": 0,
                "lead_credits_used": 0,
                "direct_dial_remaining": 0,
                "direct_dial_used": 0,
                "ai_credits_remaining": 0,
                "ai_credits_used": 0,
                "note": note,
            }

        data = resp.json()
        lead_total = data.get("effective_num_lead_credits", 0)
        lead_used = data.get("num_lead_credits_used", 0)
        dd_total = data.get("effective_num_direct_dial_credits", 0)
        dd_used = data.get("num_direct_dial_credits_used", 0)
        ai_total = data.get("effective_num_ai_credits", 0)
        ai_used = data.get("num_ai_credits_used", 0)

        return {
            "lead_credits_remaining": lead_total - lead_used,
            "lead_credits_used": lead_used,
            "direct_dial_remaining": dd_total - dd_used,
            "direct_dial_used": dd_used,
            "ai_credits_remaining": ai_total - ai_used,
            "ai_credits_used": ai_used,
        }

    except Exception as e:
        logger.error("Apollo credits error: {}", e)
        return {
            "lead_credits_remaining": 0,
            "lead_credits_used": 0,
            "direct_dial_remaining": 0,
            "direct_dial_used": 0,
            "ai_credits_remaining": 0,
            "ai_credits_used": 0,
            "note": str(e),
        }


# -- Enrichment + Sync --


async def enrich_selected_contacts(
    apollo_ids: list[str],
    vendor_card_id: int,
    db: Session,
) -> dict:
    """Enrich selected contacts via Apollo people/match. Costs 1 lead credit each.

    Creates VendorContact rows attached to the given vendor card.
    Returns: {enriched, verified, credits_used, credits_remaining, contacts: [...]}
    """
    api_key = _get_api_key()
    if not api_key:
        return {
            "enriched": 0,
            "verified": 0,
            "credits_used": 0,
            "credits_remaining": 0,
            "contacts": [],
        }

    vendor_card = db.get(VendorCard, vendor_card_id)
    if not vendor_card:
        return {
            "enriched": 0,
            "verified": 0,
            "credits_used": 0,
            "credits_remaining": 0,
            "contacts": [],
            "error": "Vendor card not found",
        }

    contacts = []
    verified_count = 0

    for apollo_id in apollo_ids:
        try:
            resp = await http.post(
                f"{APOLLO_BASE}/people/match",
                json={"id": apollo_id},
                headers=_headers(),
                timeout=30,
            )

            if resp.status_code != 200:
                logger.warning(
                    "Apollo enrich failed for {}: {}", apollo_id, resp.status_code
                )
                continue

            person = resp.json().get("person")
            if not person:
                continue

            first = (person.get("first_name") or "").strip()
            last = (person.get("last_name") or "").strip()
            full_name = f"{first} {last}".strip() or "Unknown"
            email = person.get("email")
            email_status = person.get("email_status", "unknown")
            is_verified = email_status == "verified"
            phone = _extract_phone(person)
            title = person.get("title") or ""

            if is_verified:
                verified_count += 1

            # Upsert VendorContact (dedup on vendor_card_id + email)
            existing = None
            if email:
                existing = (
                    db.query(VendorContact)
                    .filter_by(vendor_card_id=vendor_card_id, email=email)
                    .first()
                )

            if existing:
                existing.full_name = full_name
                existing.title = title
                existing.phone = phone or existing.phone
                existing.linkedin_url = (
                    person.get("linkedin_url") or existing.linkedin_url
                )
                existing.is_verified = is_verified
                existing.source = "apollo"
            else:
                new_contact = VendorContact(
                    vendor_card_id=vendor_card_id,
                    full_name=full_name,
                    first_name=first,
                    last_name=last,
                    title=title,
                    email=email,
                    phone=phone,
                    linkedin_url=person.get("linkedin_url"),
                    source="apollo",
                    is_verified=is_verified,
                    confidence=90 if is_verified else 60,
                    contact_type="person",
                )
                db.add(new_contact)

            contacts.append(
                {
                    "apollo_id": apollo_id,
                    "full_name": full_name,
                    "title": title,
                    "email": email,
                    "email_status": email_status,
                    "phone": phone,
                    "linkedin_url": person.get("linkedin_url"),
                    "seniority": classify_contact_seniority(title),
                    "is_verified": is_verified,
                }
            )

        except Exception as e:
            logger.error("Apollo enrich error for {}: {}", apollo_id, e)

    db.commit()
    credit_info = await get_credits()

    return {
        "enriched": len(contacts),
        "verified": verified_count,
        "credits_used": len(contacts),
        "credits_remaining": credit_info.get("lead_credits_remaining", 0),
        "contacts": contacts,
    }


async def sync_contacts_to_apollo(
    db: Session,
    label: str = "availai-import",
) -> dict:
    """Push AvailAI vendor contacts (with emails) to Apollo as contacts.

    Uses run_dedupe=true to avoid duplicates.
    Returns: {synced, skipped, errors}
    """
    api_key = _get_api_key()
    if not api_key:
        return {"synced": 0, "skipped": 0, "errors": 0, "note": "No API key"}

    contacts = (
        db.query(VendorContact)
        .filter(
            VendorContact.email.isnot(None),
            VendorContact.email != "",
        )
        .all()
    )

    synced = 0
    skipped = 0
    errors = 0

    for contact in contacts:
        payload = {
            "first_name": contact.first_name or "",
            "last_name": contact.last_name or "",
            "email": contact.email,
            "title": contact.title or "",
            "organization_name": (
                contact.vendor_card.display_name if contact.vendor_card else ""
            ),
            "label_names": [label],
            "run_dedupe": True,
        }

        try:
            resp = await http.post(
                f"{APOLLO_BASE}/contacts",
                json=payload,
                headers=_headers(),
                timeout=15,
            )

            if resp.status_code == 200:
                synced += 1
            elif resp.status_code == 422:
                skipped += 1
            else:
                errors += 1
                logger.warning(
                    "Apollo sync error for {}: {}", contact.email, resp.status_code
                )

        except Exception as e:
            errors += 1
            logger.error("Apollo sync exception for {}: {}", contact.email, e)

    return {"synced": synced, "skipped": skipped, "errors": errors}


def _extract_phone(person: dict) -> str | None:
    """Extract best phone from Apollo person record."""
    if person.get("phone_number"):
        return person["phone_number"]
    phones = person.get("phone_numbers", [])
    if phones:
        for ptype in ("direct_dial", "mobile", "work"):
            for p in phones:
                if p.get("type") == ptype and p.get("sanitized_number"):
                    return p["sanitized_number"]
        if phones[0].get("sanitized_number"):
            return phones[0]["sanitized_number"]
    return None
