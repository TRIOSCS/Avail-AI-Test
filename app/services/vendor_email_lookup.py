"""
services/vendor_email_lookup.py — Find vendor emails for specific part numbers.

Queries all internal data sources (sightings, vendor cards, contacts, email
intelligence, material vendor history) then enriches via external providers
(Apollo, Hunter, RocketReach, AI) for vendors missing emails.

Called by: routers/vendor_inquiry.py, routers/rfq.py
Depends on: models, enrichment_service, vendor_utils
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models import (
    Contact,
    EmailIntelligence,
    MaterialCard,
    MaterialVendorHistory,
    Sighting,
    VendorCard,
    VendorContact,
)
from ..vendor_utils import normalize_vendor_name


async def find_vendors_for_parts(
    mpns: list[str],
    db: Session,
    enrich_missing: bool = True,
    enrich_timeout: float = 15.0,
) -> dict[str, list[dict]]:
    """Find all vendor emails for a list of part numbers.

    Returns {mpn: [vendor_info_dict, ...]} where vendor_info_dict contains:
        vendor_name, emails, phones, domain, source, card_id, qty, price, last_seen
    """
    results: dict[str, list[dict]] = {mpn: [] for mpn in mpns}
    # Track all vendors found across all parts for batch enrichment
    all_vendors: dict[str, dict] = {}  # norm_name -> {display, card_id, domain, ...}

    for mpn in mpns:
        mpn_upper = mpn.upper().strip()
        if not mpn_upper:
            continue

        vendor_data = _query_db_for_part(mpn_upper, db)
        results[mpn] = vendor_data

        for v in vendor_data:
            norm = normalize_vendor_name(v["vendor_name"])
            if norm and norm not in all_vendors:
                all_vendors[norm] = v

    # Enrich vendors that have no emails
    if enrich_missing:
        needs_enrichment = [
            v for v in all_vendors.values() if not v.get("emails")
        ]
        if needs_enrichment:
            logger.info(
                "Enriching %d vendors missing emails out of %d total",
                len(needs_enrichment),
                len(all_vendors),
            )
            await _enrich_vendors_batch(
                needs_enrichment, db, timeout=enrich_timeout
            )
            # Re-query to get updated emails
            for mpn in mpns:
                results[mpn] = _query_db_for_part(mpn.upper().strip(), db)

    return results


def _query_db_for_part(mpn_upper: str, db: Session) -> list[dict]:
    """Query all internal sources for vendors of a specific MPN."""
    like_pattern = f"%{mpn_upper}%"
    vendors: dict[str, dict] = {}  # normalized_name -> vendor info

    # 1. Sightings — vendors who listed this part on APIs
    sightings = (
        db.query(Sighting)
        .filter(
            sqlfunc.upper(Sighting.normalized_mpn).like(like_pattern)
            | sqlfunc.upper(sqlfunc.coalesce(Sighting.mpn_matched, "")).like(
                like_pattern
            )
        )
        .order_by(Sighting.created_at.desc())
        .limit(200)
        .all()
    )
    for s in sightings:
        vn = (s.vendor_name or "").strip()
        if not vn:
            continue
        norm = normalize_vendor_name(vn)
        if not norm:
            continue

        if norm not in vendors:
            vendors[norm] = {
                "vendor_name": vn,
                "emails": [],
                "phones": [],
                "domain": None,
                "card_id": None,
                "sources": set(),
                "qty_available": None,
                "unit_price": None,
                "currency": None,
                "last_seen": None,
                "sighting_count": 0,
            }
        entry = vendors[norm]
        entry["sighting_count"] += 1
        if s.vendor_email and s.vendor_email not in entry["emails"]:
            entry["emails"].append(s.vendor_email.strip().lower())
        if s.vendor_phone and s.vendor_phone not in entry["phones"]:
            entry["phones"].append(s.vendor_phone.strip())
        entry["sources"].add(s.source_type or "api")
        # Keep best price/qty
        if s.qty_available and (
            not entry["qty_available"]
            or s.qty_available > entry["qty_available"]
        ):
            entry["qty_available"] = s.qty_available
        if s.unit_price and (
            not entry["unit_price"] or s.unit_price < entry["unit_price"]
        ):
            entry["unit_price"] = s.unit_price
            entry["currency"] = s.currency
        if s.created_at:
            ts = s.created_at.isoformat() if hasattr(s.created_at, "isoformat") else str(s.created_at)
            if not entry["last_seen"] or ts > entry["last_seen"]:
                entry["last_seen"] = ts

    # 2. Material vendor history — vendors seen on past searches
    try:
        history_rows = (
            db.query(MaterialVendorHistory)
            .join(MaterialCard)
            .filter(sqlfunc.upper(MaterialCard.mpn).like(like_pattern))
            .order_by(MaterialVendorHistory.times_seen.desc())
            .limit(50)
            .all()
        )
        for h in history_rows:
            vn = (h.vendor_name or "").strip()
            if not vn:
                continue
            norm = normalize_vendor_name(vn)
            if not norm or norm in vendors:
                continue
            vendors[norm] = {
                "vendor_name": vn,
                "emails": [],
                "phones": [],
                "domain": None,
                "card_id": None,
                "sources": {"material_history"},
                "qty_available": h.last_seen_qty,
                "unit_price": h.last_seen_price,
                "currency": None,
                "last_seen": None,
                "sighting_count": h.times_seen or 1,
            }
    except Exception as e:
        logger.debug("Material history query failed: %s", e)

    # 3. Email intelligence — vendor emails that mentioned this part
    try:
        ei_rows = (
            db.query(EmailIntelligence)
            .filter(
                sqlfunc.cast(EmailIntelligence.parts_detected, db.bind.dialect.name == "postgresql" and "TEXT" or "VARCHAR").ilike(f"%{mpn_upper}%")
            )
            .order_by(EmailIntelligence.received_at.desc())
            .limit(30)
            .all()
        )
    except Exception:
        # Fallback: simpler query if cast fails
        try:
            from sqlalchemy import cast, String
            ei_rows = (
                db.query(EmailIntelligence)
                .filter(cast(EmailIntelligence.parts_detected, String).ilike(f"%{mpn_upper}%"))
                .order_by(EmailIntelligence.received_at.desc())
                .limit(30)
                .all()
            )
        except Exception as e:
            logger.debug("Email intelligence query failed: %s", e)
            ei_rows = []

    for ei in ei_rows:
        if not ei.sender_email:
            continue
        domain = ei.sender_domain or ei.sender_email.split("@")[-1]
        norm = normalize_vendor_name(domain.split(".")[0])
        if not norm:
            continue
        # Try to match to existing vendor
        matched = False
        for vn, entry in vendors.items():
            if entry.get("domain") == domain or norm in vn:
                if ei.sender_email not in entry["emails"]:
                    entry["emails"].append(ei.sender_email)
                entry["sources"].add("email_intelligence")
                matched = True
                break
        if not matched:
            vendors[norm] = {
                "vendor_name": domain,
                "emails": [ei.sender_email],
                "phones": [],
                "domain": domain,
                "card_id": None,
                "sources": {"email_intelligence"},
                "qty_available": None,
                "unit_price": None,
                "currency": None,
                "last_seen": (
                    ei.received_at.isoformat()
                    if ei.received_at
                    else None
                ),
                "sighting_count": 1,
            }

    # 4. Enrich with VendorCard data (emails, phones, domain, contacts)
    if vendors:
        norm_names = list(vendors.keys())
        cards = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name.in_(norm_names))
            .all()
        )
        card_by_norm: dict[str, VendorCard] = {
            c.normalized_name: c for c in cards
        }

        # Get all vendor contacts for these cards
        card_ids = [c.id for c in cards]
        vcontacts = []
        if card_ids:
            vcontacts = (
                db.query(VendorContact)
                .filter(VendorContact.vendor_card_id.in_(card_ids))
                .order_by(VendorContact.confidence.desc())
                .all()
            )

        contacts_by_card: dict[int, list[VendorContact]] = {}
        for vc in vcontacts:
            contacts_by_card.setdefault(vc.vendor_card_id, []).append(vc)

        for norm, entry in vendors.items():
            card = card_by_norm.get(norm)
            if not card:
                continue
            entry["card_id"] = card.id
            entry["domain"] = card.domain
            entry["vendor_name"] = card.display_name or entry["vendor_name"]
            # Merge card emails
            for email in card.emails or []:
                if email and email not in entry["emails"]:
                    entry["emails"].append(email)
            # Merge card phones
            for phone in card.phones or []:
                if phone and phone not in entry["phones"]:
                    entry["phones"].append(phone)
            # Add individual contacts
            card_contacts = contacts_by_card.get(card.id, [])
            for vc in card_contacts:
                if vc.email and vc.email not in entry["emails"]:
                    entry["emails"].append(vc.email)
                if vc.phone and vc.phone not in entry["phones"]:
                    entry["phones"].append(vc.phone)

    # 5. Check past RFQ contacts for these vendors
    if vendors:
        norm_names = list(vendors.keys())
        past_contacts = (
            db.query(Contact)
            .filter(
                Contact.vendor_name_normalized.in_(norm_names),
                Contact.contact_type == "email",
                Contact.vendor_contact.isnot(None),
            )
            .order_by(Contact.created_at.desc())
            .limit(200)
            .all()
        )
        for pc in past_contacts:
            pnorm = pc.vendor_name_normalized
            if pnorm in vendors and pc.vendor_contact:
                if pc.vendor_contact not in vendors[pnorm]["emails"]:
                    vendors[pnorm]["emails"].append(pc.vendor_contact)
                    vendors[pnorm]["sources"].add("past_rfq")

    # 6. Broadcast vendors — always included regardless of MPN match
    broadcast_cards = (
        db.query(VendorCard)
        .filter(
            VendorCard.is_broadcast == True,  # noqa: E712
            VendorCard.is_blacklisted == False,  # noqa: E712
        )
        .all()
    )
    for card in broadcast_cards:
        norm = card.normalized_name
        if norm in vendors:
            # Already found via sightings — just tag as broadcast too
            vendors[norm]["sources"].add("broadcast")
            continue
        emails = []
        for email in card.emails or []:
            if email and email not in emails:
                emails.append(email)
        # Also pull VendorContact emails for this card
        bcontacts = (
            db.query(VendorContact)
            .filter_by(vendor_card_id=card.id)
            .order_by(VendorContact.confidence.desc())
            .all()
        )
        for vc in bcontacts:
            if vc.email and vc.email not in emails:
                emails.append(vc.email)
        phones = list(card.phones or [])
        vendors[norm] = {
            "vendor_name": card.display_name or norm,
            "emails": emails,
            "phones": phones,
            "domain": card.domain,
            "card_id": card.id,
            "sources": {"broadcast"},
            "qty_available": None,
            "unit_price": None,
            "currency": None,
            "last_seen": None,
            "sighting_count": 0,
        }

    # Convert to list, sorted by sighting count (most seen first)
    vendor_list = sorted(
        vendors.values(),
        key=lambda v: (len(v["emails"]) > 0, v["sighting_count"]),
        reverse=True,
    )
    # Convert sets to lists for JSON serialization
    for v in vendor_list:
        v["sources"] = sorted(v["sources"])
    return vendor_list


async def _enrich_vendors_batch(
    vendors: list[dict],
    db: Session,
    timeout: float = 15.0,
) -> None:
    """Enrich vendors missing emails via external providers."""
    from ..enrichment_service import find_suggested_contacts
    from ..vendor_utils import merge_emails_into_card, merge_phones_into_card

    sem = asyncio.Semaphore(5)

    async def _enrich_one(vendor: dict) -> None:
        async with sem:
            card_id = vendor.get("card_id")
            domain = vendor.get("domain") or ""
            name = vendor.get("vendor_name", "")
            if not domain and not name:
                return
            try:
                contacts = await asyncio.wait_for(
                    find_suggested_contacts(
                        domain=domain, name=name, title_filter="sales"
                    ),
                    timeout=5,
                )
                emails = list(
                    dict.fromkeys(
                        c["email"] for c in contacts if c.get("email")
                    )
                )
                phones = list(
                    dict.fromkeys(
                        c["phone"] for c in contacts if c.get("phone")
                    )
                )
                if emails and card_id:
                    card = db.query(VendorCard).get(card_id)
                    if card:
                        merge_emails_into_card(card, emails)
                        if phones:
                            merge_phones_into_card(card, phones)
                vendor["emails"] = emails
                vendor["phones"] = phones
            except asyncio.TimeoutError:
                logger.debug("Enrichment timed out for %s", name)
            except Exception as e:
                logger.debug("Enrichment failed for %s: %s", name, e)

    try:
        await asyncio.wait_for(
            asyncio.gather(
                *[_enrich_one(v) for v in vendors[:20]],
                return_exceptions=True,
            ),
            timeout=timeout,
        )
        db.commit()
    except asyncio.TimeoutError:
        logger.info("Batch vendor enrichment hit %ss cap", timeout)
        try:
            db.commit()
        except Exception:
            db.rollback()


def build_inquiry_groups(
    vendor_results: dict[str, list[dict]],
    parts_with_qty: list[dict],
    company_name: str = "TRIO Supply Chain Solutions",
    sender_name: str = "Purchasing Team",
) -> list[dict]:
    """Build RFQ vendor groups from lookup results.

    Args:
        vendor_results: {mpn: [vendor_info_dict, ...]}
        parts_with_qty: [{"mpn": "...", "qty": 50}, ...]
        company_name: Sender company name
        sender_name: Sender name for email

    Returns:
        List of dicts ready for send_batch_rfq:
        [{vendor_name, vendor_email, parts, subject, body}, ...]
    """
    # Build part → qty map
    qty_map = {p["mpn"].upper(): p.get("qty", 0) for p in parts_with_qty}
    all_mpns = [p["mpn"] for p in parts_with_qty]

    # Collect vendors across all parts, tracking which parts each has
    vendor_parts: dict[str, dict] = {}  # email -> {vendor_name, parts, ...}

    for mpn, vendors in vendor_results.items():
        for v in vendors:
            for email in v.get("emails", []):
                email_lower = email.lower().strip()
                if email_lower not in vendor_parts:
                    vendor_parts[email_lower] = {
                        "vendor_name": v["vendor_name"],
                        "vendor_email": email_lower,
                        "parts": [],
                        "domain": v.get("domain"),
                    }
                if mpn not in vendor_parts[email_lower]["parts"]:
                    vendor_parts[email_lower]["parts"].append(mpn)

    # Build email groups
    groups = []
    for email, info in vendor_parts.items():
        parts = info["parts"]
        parts_lines = []
        for mpn in parts:
            qty = qty_map.get(mpn.upper(), 0)
            qty_str = f" — {qty} pcs" if qty else ""
            parts_lines.append(f"  • {mpn}{qty_str}")
        parts_text = "\n".join(parts_lines)

        subject = f"Stock Inquiry — {', '.join(parts[:3])}"
        if len(parts) > 3:
            subject += f" + {len(parts) - 3} more"

        body = (
            f"Hi,\n\n"
            f"We have an order in hand and are looking for immediate availability "
            f"on the following part(s):\n\n"
            f"{parts_text}\n\n"
            f"Could you please confirm:\n"
            f"1. Stock availability and quantity\n"
            f"2. Unit pricing\n"
            f"3. Lead time / date code\n"
            f"4. Condition (new, refurbished, etc.)\n\n"
            f"This is an active order — quick response appreciated.\n\n"
            f"Thank you,\n"
            f"{sender_name}\n"
            f"{company_name}"
        )

        groups.append({
            "vendor_name": info["vendor_name"],
            "vendor_email": email,
            "parts": all_mpns,
            "subject": subject,
            "body": body,
        })

    return groups
