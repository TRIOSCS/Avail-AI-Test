"""
services/email_threads.py — Email thread fetching and attribution

Fetches vendor email threads from Microsoft Graph API and links them
to requirements via conversationId, subject tokens, part numbers,
or vendor domain. Provides in-memory caching with 5-minute TTL.

Business Rules:
- Never store full email bodies in PostgreSQL — Graph API is source of truth
- Filter out internal TRIOSCS-to-TRIOSCS messages
- Detect "needs response" when last message is from vendor with no reply in 24h
- Cache fetched threads for 5 minutes to avoid excessive Graph calls

Called by: routers/emails.py
Depends on: utils/graph_client.py, models.py, services/activity_service.py
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.models import Contact, Requirement, Sighting, VendorCard, VendorContact, VendorResponse
from app.utils.graph_client import GraphClient

log = logging.getLogger("avail.email_threads")

# ── In-memory cache ────────────────────────────────────────────────────
# key → (timestamp, data)
_thread_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 300  # 5 minutes

# TRIOSCS company domains — emails between these are internal
_TRIOSCS_DOMAINS = frozenset({"trioscs.com"})


def _cache_get(key: str) -> list | None:
    """Return cached data if still valid, else None."""
    entry = _thread_cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL:
        del _thread_cache[key]
        return None
    return data


def _cache_set(key: str, data: list) -> None:
    """Store data in cache with current timestamp."""
    _thread_cache[key] = (time.time(), data)


def clear_cache() -> None:
    """Clear all cached threads (useful for testing)."""
    _thread_cache.clear()


def _is_trioscs_domain(email: str) -> bool:
    """Check if an email address belongs to a TRIOSCS domain."""
    if not email or "@" not in email:
        return False
    domain = email.lower().split("@", 1)[1]
    return domain in _TRIOSCS_DOMAINS


def _is_internal_message(from_email: str, to_emails: list[str]) -> bool:
    """Check if a message is internal (TRIOSCS to TRIOSCS)."""
    if not _is_trioscs_domain(from_email):
        return False
    # All recipients must also be TRIOSCS for it to be internal
    if not to_emails:
        return False
    return all(_is_trioscs_domain(e) for e in to_emails)


def _extract_direction(from_email: str) -> str:
    """Determine if message was sent or received based on sender domain."""
    return "sent" if _is_trioscs_domain(from_email) else "received"


def _extract_participants(messages: list[dict]) -> list[str]:
    """Extract unique non-TRIOSCS participant emails from messages."""
    participants = set()
    for msg in messages:
        from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")
        if from_addr and not _is_trioscs_domain(from_addr):
            participants.add(from_addr.lower())
        for recip in msg.get("toRecipients", []):
            addr = recip.get("emailAddress", {}).get("address", "")
            if addr and not _is_trioscs_domain(addr):
                participants.add(addr.lower())
    return sorted(participants)


def _detect_needs_response(messages: list[dict]) -> bool:
    """Check if the last message is from a vendor with no reply in 24 hours.

    Returns True if:
    - Last message is from a non-TRIOSCS sender
    - That message was received more than 24 hours ago
    """
    if not messages:
        return False

    # Sort by received date descending
    sorted_msgs = sorted(
        messages,
        key=lambda m: m.get("receivedDateTime", ""),
        reverse=True,
    )
    last_msg = sorted_msgs[0]
    from_email = last_msg.get("from", {}).get("emailAddress", {}).get("address", "")

    if _is_trioscs_domain(from_email):
        return False  # We sent the last message — no response needed

    # Check if received > 24 hours ago
    received_str = last_msg.get("receivedDateTime", "")
    if not received_str:
        return True  # No date — assume needs response

    try:
        received_dt = datetime.fromisoformat(received_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - received_dt > timedelta(hours=24):
            return True
    except (ValueError, TypeError):
        return True

    return False


def _message_to_dict(msg: dict) -> dict:
    """Convert a Graph API message to our response format."""
    from_data = msg.get("from", {}).get("emailAddress", {})
    from_email = from_data.get("address", "")
    to_list = [
        r.get("emailAddress", {}).get("address", "")
        for r in msg.get("toRecipients", [])
    ]

    return {
        "id": msg.get("id", ""),
        "from_name": from_data.get("name", ""),
        "from_email": from_email,
        "to": [e for e in to_list if e],
        "subject": msg.get("subject", ""),
        "body_preview": msg.get("bodyPreview", ""),
        "received_date": msg.get("receivedDateTime"),
        "direction": _extract_direction(from_email),
    }


# ── Thread fetching for requirements ───────────────────────────────────


async def fetch_threads_for_requirement(
    requirement_id: int, user_token: str, db: Session, user_id: int | None = None
) -> list[dict]:
    """Fetch email threads linked to a requirement.

    Matching tiers:
    1. conversationId match — RFQ was sent, reply is in same thread
    2. [AVAIL-{req_id}] token in subject
    3. Part number match — requirement.part_number in email subject/body
    4. Vendor domain match — email from vendor with sightings for this requirement

    Args:
        requirement_id: The requirement to find threads for
        user_token: Valid M365 access token
        db: Database session
        user_id: Optional user ID for cache keying

    Returns:
        List of thread summary dicts
    """
    cache_key = f"{user_id}:{requirement_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        return []

    gc = GraphClient(user_token)
    threads: dict[str, dict] = {}  # conversation_id → thread summary

    # ── Tier 1: ConversationId match via Contact records ──
    contacts = (
        db.query(Contact)
        .filter(
            Contact.requisition_id == requirement.requisition_id,
            Contact.graph_conversation_id.isnot(None),
        )
        .all()
    )
    for contact in contacts:
        conv_id = contact.graph_conversation_id
        if conv_id and conv_id not in threads:
            try:
                messages = await gc.get_all_pages(
                    "/me/messages",
                    params={
                        "$filter": f"conversationId eq '{conv_id}'",
                        "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                        "$orderby": "receivedDateTime desc",
                        "$top": "50",
                    },
                    max_items=50,
                )
                if messages:
                    # Filter out internal messages
                    external_msgs = [
                        m for m in messages
                        if not _is_internal_message(
                            m.get("from", {}).get("emailAddress", {}).get("address", ""),
                            [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])],
                        )
                    ]
                    if external_msgs:
                        threads[conv_id] = _build_thread_summary(
                            conv_id, external_msgs, "conversation_id"
                        )
            except Exception as e:
                log.warning(f"Graph query failed for conversationId {conv_id[:20]}: {e}")

    # ── Tier 1b: ConversationId match via VendorResponse records ──
    vendor_responses = (
        db.query(VendorResponse)
        .filter(
            VendorResponse.requisition_id == requirement.requisition_id,
            VendorResponse.graph_conversation_id.isnot(None),
        )
        .all()
    )
    for vr in vendor_responses:
        conv_id = vr.graph_conversation_id
        if conv_id and conv_id not in threads:
            try:
                messages = await gc.get_all_pages(
                    "/me/messages",
                    params={
                        "$filter": f"conversationId eq '{conv_id}'",
                        "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                        "$orderby": "receivedDateTime desc",
                        "$top": "50",
                    },
                    max_items=50,
                )
                if messages:
                    external_msgs = [
                        m for m in messages
                        if not _is_internal_message(
                            m.get("from", {}).get("emailAddress", {}).get("address", ""),
                            [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])],
                        )
                    ]
                    if external_msgs:
                        threads[conv_id] = _build_thread_summary(
                            conv_id, external_msgs, "conversation_id"
                        )
            except Exception as e:
                log.warning(f"Graph query failed for VR conversationId: {e}")

    # ── Tier 2: Subject [AVAIL-{req_id}] token ──
    avail_token = f"[AVAIL-{requirement.requisition_id}]"
    try:
        subject_msgs = await gc.get_all_pages(
            "/me/messages",
            params={
                "$search": f'"subject:{avail_token}"',
                "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                "$top": "50",
            },
            max_items=50,
        )
        # Group by conversationId
        by_conv: dict[str, list[dict]] = {}
        for m in subject_msgs:
            cid = m.get("conversationId", "")
            if cid and cid not in threads:
                by_conv.setdefault(cid, []).append(m)

        for cid, msgs in by_conv.items():
            external_msgs = [
                m for m in msgs
                if not _is_internal_message(
                    m.get("from", {}).get("emailAddress", {}).get("address", ""),
                    [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])],
                )
            ]
            if external_msgs:
                threads[cid] = _build_thread_summary(cid, external_msgs, "subject_token")
    except Exception as e:
        log.warning(f"Graph subject search failed for {avail_token}: {e}")

    # ── Tier 3: Part number match ──
    part_number = requirement.primary_mpn
    if part_number and len(part_number) >= 3:
        try:
            pn_msgs = await gc.get_all_pages(
                "/me/messages",
                params={
                    "$search": f'"{part_number}"',
                    "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                    "$top": "25",
                },
                max_items=25,
            )
            by_conv: dict[str, list[dict]] = {}
            for m in pn_msgs:
                cid = m.get("conversationId", "")
                if cid and cid not in threads:
                    by_conv.setdefault(cid, []).append(m)

            for cid, msgs in by_conv.items():
                external_msgs = [
                    m for m in msgs
                    if not _is_internal_message(
                        m.get("from", {}).get("emailAddress", {}).get("address", ""),
                        [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])],
                    )
                ]
                if external_msgs:
                    threads[cid] = _build_thread_summary(
                        cid, external_msgs, "part_number"
                    )
        except Exception as e:
            log.warning(f"Graph part number search failed for {part_number}: {e}")

    # ── Tier 4: Vendor domain match ──
    # Find vendors with sightings for this requirement
    vendor_domains = set()
    sightings = (
        db.query(Sighting)
        .filter(Sighting.requirement_id == requirement_id)
        .all()
    )
    for s in sightings:
        if s.vendor_email and "@" in s.vendor_email:
            domain = s.vendor_email.lower().split("@", 1)[1]
            vendor_domains.add(domain)

    # Also check vendor cards linked through sightings
    vendor_names = {s.vendor_name for s in sightings if s.vendor_name}
    if vendor_names:
        from app.vendor_utils import normalize_vendor_name
        norm_names = [normalize_vendor_name(n) for n in vendor_names]
        cards = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name.in_(norm_names))
            .all()
        )
        for card in cards:
            if card.domain:
                vendor_domains.add(card.domain.lower())

    # Search by vendor domains in parallel (limit to avoid excessive API calls)
    import asyncio

    search_domains = [d for d in list(vendor_domains)[:5] if d not in _TRIOSCS_DOMAINS]

    async def _search_domain(domain):
        try:
            domain_msgs = await gc.get_all_pages(
                "/me/messages",
                params={
                    "$search": f'"from:{domain}"',
                    "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                    "$top": "15",
                },
                max_items=15,
            )
            domain_threads = {}
            by_conv: dict[str, list[dict]] = {}
            for m in domain_msgs:
                cid = m.get("conversationId", "")
                if cid and cid not in threads:
                    by_conv.setdefault(cid, []).append(m)

            for cid, msgs in by_conv.items():
                external_msgs = [
                    m for m in msgs
                    if not _is_internal_message(
                        m.get("from", {}).get("emailAddress", {}).get("address", ""),
                        [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])],
                    )
                ]
                if external_msgs:
                    domain_threads[cid] = _build_thread_summary(
                        cid, external_msgs, "vendor_domain"
                    )
            return domain_threads
        except Exception as e:
            log.warning(f"Graph domain search failed for {domain}: {e}")
            return {}

    if search_domains:
        domain_results = await asyncio.gather(*[_search_domain(d) for d in search_domains])
        for domain_threads_result in domain_results:
            threads.update(domain_threads_result)

    result = sorted(
        threads.values(),
        key=lambda t: t.get("last_message_date") or "",
        reverse=True,
    )

    _cache_set(cache_key, result)
    return result


def _build_thread_summary(
    conversation_id: str, messages: list[dict], matched_via: str
) -> dict:
    """Build a thread summary from a list of Graph API messages."""
    sorted_msgs = sorted(
        messages,
        key=lambda m: m.get("receivedDateTime", ""),
        reverse=True,
    )
    latest = sorted_msgs[0]
    subject = latest.get("subject", "(No Subject)")
    # Strip [AVAIL-xxx] prefix for cleaner display
    clean_subject = re.sub(r"\[AVAIL-\d+\]\s*", "", subject)

    return {
        "conversation_id": conversation_id,
        "subject": clean_subject or subject,
        "participants": _extract_participants(messages),
        "message_count": len(messages),
        "last_message_date": latest.get("receivedDateTime"),
        "snippet": latest.get("bodyPreview", "")[:200],
        "needs_response": _detect_needs_response(messages),
        "matched_via": matched_via,
    }


# ── Thread message fetching ────────────────────────────────────────────


async def fetch_thread_messages(
    conversation_id: str, user_token: str
) -> list[dict]:
    """Fetch all messages in a conversation thread.

    Args:
        conversation_id: The Graph API conversationId
        user_token: Valid M365 access token

    Returns:
        List of message dicts sorted by date ascending (oldest first)
    """
    gc = GraphClient(user_token)

    try:
        messages = await gc.get_all_pages(
            "/me/messages",
            params={
                "$filter": f"conversationId eq '{conversation_id}'",
                "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                "$orderby": "receivedDateTime asc",
                "$top": "50",
            },
            max_items=100,
        )
    except Exception as e:
        log.error(f"Failed to fetch thread messages for {conversation_id[:20]}: {e}")
        return []

    result = []
    for msg in messages:
        msg_dict = _message_to_dict(msg)
        # Skip internal messages
        if _is_internal_message(msg_dict["from_email"], msg_dict["to"]):
            continue
        result.append(msg_dict)

    return result


# ── Thread fetching for vendors ────────────────────────────────────────


async def fetch_threads_for_vendor(
    vendor_card_id: int, user_token: str, db: Session, user_id: int | None = None
) -> list[dict]:
    """Fetch email threads with a specific vendor.

    Finds VendorContacts for this vendor, gets their email domains,
    and queries Graph for recent messages from/to those domains.

    Args:
        vendor_card_id: The vendor card to find threads for
        user_token: Valid M365 access token
        db: Database session
        user_id: Optional user ID for cache keying

    Returns:
        List of thread summary dicts
    """
    cache_key = f"{user_id}:vendor:{vendor_card_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    vendor = db.get(VendorCard, vendor_card_id)
    if not vendor:
        return []

    # Collect vendor domains
    domains = set()
    if vendor.domain:
        domains.add(vendor.domain.lower())
    if vendor.domain_aliases:
        for alias in vendor.domain_aliases:
            if alias:
                domains.add(alias.lower())

    # Get domains from vendor contacts
    v_contacts = (
        db.query(VendorContact)
        .filter(
            VendorContact.vendor_card_id == vendor_card_id,
            VendorContact.email.isnot(None),
        )
        .all()
    )
    for vc in v_contacts:
        if vc.email and "@" in vc.email:
            domain = vc.email.lower().split("@", 1)[1]
            domains.add(domain)

    # Also check the vendor card's email list
    for email in (vendor.emails or []):
        if email and "@" in email:
            domain = email.lower().split("@", 1)[1]
            domains.add(domain)

    # Remove TRIOSCS and generic domains
    domains -= _TRIOSCS_DOMAINS
    domains -= {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com"}

    if not domains:
        _cache_set(cache_key, [])
        return []

    gc = GraphClient(user_token)
    threads: dict[str, dict] = {}

    # Search all domains in parallel
    async def _search_vendor_domain(domain):
        try:
            msgs = await gc.get_all_pages(
                "/me/messages",
                params={
                    "$search": f'"from:{domain}" OR "to:{domain}"',
                    "$select": "id,subject,from,toRecipients,bodyPreview,receivedDateTime,conversationId",
                    "$top": "25",
                },
                max_items=50,
            )

            domain_threads = {}
            by_conv: dict[str, list[dict]] = {}
            for m in msgs:
                cid = m.get("conversationId", "")
                if cid and cid not in threads:
                    by_conv.setdefault(cid, []).append(m)

            for cid, conv_msgs in by_conv.items():
                external_msgs = [
                    m for m in conv_msgs
                    if not _is_internal_message(
                        m.get("from", {}).get("emailAddress", {}).get("address", ""),
                        [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])],
                    )
                ]
                if external_msgs:
                    domain_threads[cid] = _build_thread_summary(
                        cid, external_msgs, "vendor_domain"
                    )
            return domain_threads
        except Exception as e:
            log.warning(f"Graph vendor search failed for domain {domain}: {e}")
            return {}

    search_domains = list(domains)[:5]
    if search_domains:
        domain_results = await asyncio.gather(*[_search_vendor_domain(d) for d in search_domains])
        for domain_threads_result in domain_results:
            threads.update(domain_threads_result)

    result = sorted(
        threads.values(),
        key=lambda t: t.get("last_message_date") or "",
        reverse=True,
    )

    _cache_set(cache_key, result)
    return result
