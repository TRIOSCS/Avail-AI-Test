"""Email service — batch RFQ sending, inbox monitoring, AI parsing."""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session

from .config import settings
from .services.credential_service import get_credential_cached
from .models import Contact, VendorResponse, ProcessedMessage, PendingBatch

log = logging.getLogger(__name__)


def _build_html_body(plain_text: str) -> str:
    """Convert plain text to minimal HTML that looks like a normal email."""
    html_body = plain_text.replace("\n", "<br>\n")
    return f"""<html><body style="font-family: Calibri, Arial, sans-serif; font-size: 14px; color: #333;">
{html_body}
</body></html>"""


async def send_batch_rfq(
    token: str,
    db: Session,
    user_id: int,
    requisition_id: int,
    vendor_groups: list[dict],
) -> list[dict]:
    """Send one RFQ email per vendor group. Each group: {vendor_name, vendor_email, parts, subject, body}.
    Returns list of created Contact records as dicts."""
    from app.utils.graph_client import GraphClient

    gc = GraphClient(token)
    results = []

    # AI-rephrase each body so emails don't look like cookie-cutter spam
    if get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        try:
            from app.services.ai_service import rephrase_rfq

            rephrase_tasks = [rephrase_rfq(g["body"]) for g in vendor_groups if g.get("body")]
            rephrased = await asyncio.gather(*rephrase_tasks, return_exceptions=True)
            idx = 0
            for g in vendor_groups:
                if g.get("body"):
                    result = rephrased[idx]
                    if isinstance(result, str) and result:
                        g["body"] = result
                    idx += 1
        except Exception as e:
            log.warning(f"AI rephrase failed, using original bodies: {e}")

    # Build payloads and send all emails in parallel
    avail_token = f"[AVAIL-{requisition_id}]"
    send_tasks = []
    send_groups = []  # Track which groups we're sending for

    for group in vendor_groups:
        email = group.get("vendor_email")
        if not email:
            continue

        html_body = _build_html_body(group["body"])
        raw_subject = group["subject"]
        tagged_subject = f"{avail_token} {raw_subject}" if avail_token not in raw_subject else raw_subject
        group["_tagged_subject"] = tagged_subject

        payload = {
            "message": {
                "subject": tagged_subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": email}}],
                "isReadReceiptRequested": False,
                "isDeliveryReceiptRequested": False,
            },
            "saveToSentItems": "true",
        }
        send_tasks.append(gc.post_json("/me/sendMail", payload))
        send_groups.append(group)

    # Fire all sends in parallel
    send_results = await asyncio.gather(*send_tasks, return_exceptions=True)

    # Process results: create Contact records, then batch-lookup sent message IDs
    contacts_to_lookup = []  # (contact, tagged_subject) pairs
    for group, send_result in zip(send_groups, send_results):
        email = group["vendor_email"]
        tagged_subject = group.pop("_tagged_subject")

        if isinstance(send_result, Exception):
            log.error(f"Send error to {email}: {send_result}")
            results.append({"vendor_name": group["vendor_name"], "vendor_email": email,
                            "status": "error", "error": str(send_result)[:200]})
            continue

        if "error" in send_result:
            log.error(f"Send failed to {email}: {send_result}")
            results.append({"vendor_name": group["vendor_name"], "vendor_email": email,
                            "status": "failed", "error": str(send_result.get("detail", ""))[:200]})
            continue

        contact = Contact(
            requisition_id=requisition_id, user_id=user_id, contact_type="email",
            vendor_name=group["vendor_name"], vendor_contact=email,
            parts_included=group.get("parts", []), subject=tagged_subject,
            details=group["body"], status="sent",
            status_updated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(contact)
        db.flush()
        contacts_to_lookup.append((contact, tagged_subject))
        results.append({"id": contact.id, "vendor_name": contact.vendor_name,
                        "vendor_email": email, "parts_count": len(contact.parts_included),
                        "status": "sent"})

    # Batch-lookup sent message IDs in parallel for reply matching
    if contacts_to_lookup:
        lookup_results = await asyncio.gather(
            *[_find_sent_message(gc, subj) for _, subj in contacts_to_lookup],
            return_exceptions=True,
        )
        for (contact, _), sent_msg in zip(contacts_to_lookup, lookup_results):
            if isinstance(sent_msg, dict) and sent_msg:
                contact.graph_message_id = sent_msg.get("id")
                contact.graph_conversation_id = sent_msg.get("conversationId")

    db.commit()
    return results


async def _find_sent_message(gc, subject: str) -> dict | None:
    """Find the just-sent message in Sent Items to get its ID and conversationId."""
    try:
        await asyncio.sleep(1)  # Brief delay for Graph to process
        data = await gc.get_json(
            "/me/mailFolders/sentItems/messages",
            params={
                "$top": "5",
                "$orderby": "sentDateTime desc",
                "$select": "id,conversationId,subject",
            },
        )
        msgs = data.get("value", []) if data else []
        for m in msgs:
            if m.get("subject", "").strip() == subject.strip():
                return m
    except Exception as e:
        log.debug(f"Sent message lookup failed: {e}")
    return None


def log_phone_contact(
    db: Session,
    user_id: int,
    requisition_id: int,
    vendor_name: str,
    vendor_phone: str,
    parts: list[str],
) -> dict:
    """Log a verified phone contact (click-to-call initiated)."""
    contact = Contact(
        requisition_id=requisition_id,
        user_id=user_id,
        contact_type="phone",
        vendor_name=vendor_name,
        vendor_contact=vendor_phone,
        parts_included=parts,
        subject=f"Call to {vendor_name}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(contact)
    db.commit()
    return {
        "id": contact.id,
        "vendor_name": vendor_name,
        "vendor_phone": vendor_phone,
        "contact_type": "phone",
        "created_at": contact.created_at.isoformat(),
    }


async def poll_inbox(
    token: str, db: Session, requisition_id: int = None, scanned_by_user_id: int = None
) -> list[dict]:
    """Check inbox for vendor replies. Smart-matches to outbound RFQs.

    Matching priority:
    1. conversationId — same email thread as a sent RFQ (global, exact)
    2. Subject [AVAIL-{req_id}] token — explicit RFQ tag
    3. Vendor email — sender matches a vendor we contacted (USER-SCOPED to avoid data leaks)
    4. Sender domain — vendor domain match (USER-SCOPED)
    5. Unmatched — saved for manual review

    Hardening:
    - H1: Immutable IDs via GraphClient headers
    - H2: ProcessedMessage dedup (belt-and-suspenders with VendorResponse check)
    - H6: Retry with exponential backoff via GraphClient
    - H8: Delta Query when sync_state available (incremental, not full scan)

    Returns list of new responses found (already saved to DB).
    """
    import re
    from app.utils.graph_client import GraphClient
    from app.models import SyncState

    gc = GraphClient(token)

    # ── H8: Try Delta Query for incremental sync ──
    messages = []
    used_delta = False
    if scanned_by_user_id:
        sync = (
            db.query(SyncState)
            .filter(
                SyncState.user_id == scanned_by_user_id,
                SyncState.folder == "inbox",
            )
            .first()
        )
        delta_token = sync.delta_token if sync else None

        try:
            items, new_delta = await gc.delta_query(
                "/me/mailFolders/inbox/messages/delta",
                delta_token=delta_token,
                params={
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId",
                    "$top": "50",
                },
                max_items=200,
            )
            messages = items
            used_delta = True

            # Persist new delta token
            if new_delta:
                if sync:
                    sync.delta_token = new_delta
                    sync.last_sync_at = datetime.now(timezone.utc)
                else:
                    sync = SyncState(
                        user_id=scanned_by_user_id,
                        folder="inbox",
                        delta_token=new_delta,
                        last_sync_at=datetime.now(timezone.utc),
                    )
                    db.add(sync)
                db.flush()
        except Exception as e:
            log.warning(f"Delta query failed, falling back to full scan: {e}")
            messages = []
            used_delta = False

    # ── Fallback: traditional top-50 fetch ──
    if not messages and not used_delta:
        try:
            data = await gc.get_json(
                "/me/mailFolders/inbox/messages",
                params={
                    "$top": "50",
                    "$orderby": "receivedDateTime desc",
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,body,conversationId",
                },
            )
            messages = data.get("value", []) if data else []
        except Exception as e:
            log.error(f"Inbox poll failed: {e}")
            return []

    # ── H2: Dedup via processed_messages table (belt-and-suspenders) ──
    incoming_ids = [m.get("id", "") for m in messages if m.get("id")]
    already_processed = set()
    if incoming_ids:
        # Check both VendorResponse and ProcessedMessage tables
        vr_rows = (
            db.query(VendorResponse.message_id)
            .filter(VendorResponse.message_id.in_(incoming_ids))
            .all()
        )
        pm_rows = (
            db.query(ProcessedMessage.message_id)
            .filter(
                ProcessedMessage.message_id.in_(incoming_ids),
                ProcessedMessage.processing_type == "inbox_poll",
            )
            .all()
        )
        already_processed = {r[0] for r in vr_rows} | {r[0] for r in pm_rows}

    # Pre-load outbound email contacts for matching (last 6 months)
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    all_contacts = (
        db.query(Contact)
        .filter(
            Contact.contact_type == "email",
            Contact.created_at > cutoff,
        )
        .all()
    )

    # ConversationId map is GLOBAL — thread-specific, unambiguous
    conv_id_map = {}
    # Email and domain maps are USER-SCOPED to prevent cross-user data leaks
    email_map = {}  # vendor_email -> Contact (only this user's contacts)
    domain_map = {}  # vendor_domain -> Contact (only this user's contacts)
    for c in all_contacts:
        if c.graph_conversation_id:
            conv_id_map[c.graph_conversation_id] = c
        # Only build email/domain maps for the scanning user
        if scanned_by_user_id and c.user_id == scanned_by_user_id:
            if c.vendor_contact:
                email_lower = c.vendor_contact.lower()
                email_map[email_lower] = c
                # Extract domain for domain-level matching
                if "@" in email_lower:
                    domain = email_lower.split("@", 1)[1]
                    if domain not in NOISE_DOMAINS:
                        domain_map[domain] = c

    # Subject token pattern: [AVAIL-{req_id}]
    avail_token_re = re.compile(r"\[AVAIL-(\d+)\]")

    results = []
    pending_parse = []  # VendorResponse objects awaiting AI parsing
    for msg in messages:
        msg_id = msg.get("id", "")
        if not msg_id or msg_id in already_processed:
            continue

        sender = msg.get("from", {}).get("emailAddress", {})
        email_addr = (sender.get("address") or "").lower()
        subj = msg.get("subject", "")
        conv_id = msg.get("conversationId", "")

        # Skip noise
        if _is_noise_email(email_addr):
            continue

        # ── 4-tier reply matching ──
        matched_contact = None
        matched_req_id = requisition_id
        match_method = "unmatched"

        # Tier 1: ConversationId (exact thread, global)
        if conv_id and conv_id in conv_id_map:
            matched_contact = conv_id_map[conv_id]
            matched_req_id = matched_contact.requisition_id
            match_method = "conversation_id"

        # Tier 2: Subject [AVAIL-{id}] token
        if not matched_contact:
            token_match = avail_token_re.search(subj)
            if token_match:
                avail_req_id = int(token_match.group(1))
                # Verify this req exists and find the contact
                req_contacts = [
                    c
                    for c in all_contacts
                    if c.requisition_id == avail_req_id
                    and c.vendor_contact
                    and c.vendor_contact.lower() == email_addr
                ]
                if req_contacts:
                    matched_contact = req_contacts[0]
                    matched_req_id = avail_req_id
                    match_method = "subject_token"
                else:
                    # Token found but no exact email match — still assign to req
                    matched_req_id = avail_req_id
                    match_method = "subject_token_req_only"

        # Tier 3: Exact email match (USER-SCOPED)
        if not matched_contact and email_addr in email_map:
            matched_contact = email_map[email_addr]
            matched_req_id = matched_contact.requisition_id
            match_method = "email_exact"

        # Tier 4: Domain match (USER-SCOPED)
        if not matched_contact and "@" in email_addr:
            sender_domain = email_addr.split("@", 1)[1]
            if sender_domain in domain_map:
                matched_contact = domain_map[sender_domain]
                matched_req_id = matched_contact.requisition_id
                match_method = "domain"

        try:
            vr = VendorResponse(
                requisition_id=matched_req_id,
                contact_id=matched_contact.id if matched_contact else None,
                vendor_name=sender.get("name", email_addr),
                vendor_email=email_addr,
                subject=subj,
                body=msg.get("body", {}).get("content", msg.get("bodyPreview", "")),
                message_id=msg_id,
                graph_conversation_id=conv_id,
                scanned_by_user_id=scanned_by_user_id,
                match_method=match_method,
                received_at=msg.get("receivedDateTime"),
                status="matched" if matched_contact else "new",
                created_at=datetime.now(timezone.utc),
            )
            db.add(vr)
            db.flush()

            # H2: Record in processed_messages for cross-type dedup
            db.add(
                ProcessedMessage(
                    message_id=msg_id,
                    processing_type="inbox_poll",
                    processed_at=datetime.now(timezone.utc),
                )
            )

            # ── Reply classification + AI parsing (batch or inline) ──
            if get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
                pending_parse.append(vr)

            # ── Contact status progression ──
            if matched_contact:
                _progress_contact_status(matched_contact, vr, db)

            results.append(
                {
                    "id": vr.id,
                    "vendor_name": vr.vendor_name,
                    "vendor_email": vr.vendor_email,
                    "subject": vr.subject,
                    "status": vr.status,
                    "classification": vr.classification,
                    "needs_action": vr.needs_action,
                    "action_hint": vr.action_hint,
                    "parsed_data": vr.parsed_data,
                    "confidence": vr.confidence,
                    "received_at": vr.received_at,
                    "message_id": msg_id,
                    "match_method": match_method,
                    "matched_contact_id": matched_contact.id
                    if matched_contact
                    else None,
                    "matched_requisition_id": matched_req_id,
                }
            )
        except Exception as e:
            log.error(f"Failed to save inbox message {msg_id[:20]}: {e}")
            db.rollback()
            continue

    # ── Submit AI batch or fall back to sequential parsing ──
    if pending_parse:
        try:
            await _submit_parse_batch(pending_parse, db)
        except Exception as e:
            log.warning(f"Batch submit failed, falling back to sequential: {e}")
            await _parse_sequential_fallback(pending_parse, db)

    # Single commit for all new responses
    try:
        db.commit()
    except Exception as e:
        log.error(f"Batch commit failed during inbox poll: {e}")
        db.rollback()
        return []

    return results


def _classify_response(parsed: dict, body: str, subject: str) -> dict:
    """Classify a vendor response into actionable categories."""
    body_lower = (body or "").lower()[:2000]
    subject_lower = (subject or "").lower()

    # OOO / bounce detection
    ooo_signals = [
        "out of office",
        "automatic reply",
        "autoreply",
        "i am currently out",
        "on vacation",
        "will return",
        "away from",
        "undeliverable",
        "delivery failure",
    ]
    if any(s in body_lower or s in subject_lower for s in ooo_signals):
        return {
            "type": "ooo_bounce",
            "needs_action": False,
            "action_hint": "Auto-reply detected — will need follow-up later",
        }

    parts = parsed.get("parts", [])
    sentiment = parsed.get("sentiment", "neutral")

    # Quote provided — has actual pricing
    if parts and any(p.get("unit_price") for p in parts):
        return {
            "type": "quote_provided",
            "needs_action": True,
            "action_hint": f"Quote received for {len(parts)} part(s) — review pricing",
        }

    # Partial availability — some parts but not all, or limited qty
    if parts and sentiment == "positive":
        if any(p.get("qty_available") for p in parts):
            return {
                "type": "partial_availability",
                "needs_action": True,
                "action_hint": "Partial stock available — review quantities",
            }

    # No stock
    no_stock_signals = [
        "not available",
        "out of stock",
        "no stock",
        "don't have",
        "do not have",
        "cannot supply",
        "unable to",
        "unfortunately",
        "regret to",
        "unable to offer",
        "not in stock",
    ]
    if any(s in body_lower for s in no_stock_signals) or sentiment == "negative":
        return {
            "type": "no_stock",
            "needs_action": False,
            "action_hint": "Vendor confirmed no availability",
        }

    # Counter offer — alternative parts or conditions
    counter_signals = [
        "alternative",
        "instead",
        "substitute",
        "we can offer",
        "how about",
        "suggest",
        "similar",
    ]
    if any(s in body_lower for s in counter_signals):
        return {
            "type": "counter_offer",
            "needs_action": True,
            "action_hint": "Vendor proposed an alternative — review offer",
        }

    # Clarification needed
    question_signals = [
        "could you",
        "can you",
        "please confirm",
        "need more",
        "what quantity",
        "which version",
        "please clarify",
        "?",
    ]
    if any(s in body_lower for s in question_signals) and body_lower.count("?") >= 1:
        return {
            "type": "clarification_needed",
            "needs_action": True,
            "action_hint": "Vendor asked a question — reply needed",
        }

    # Default: follow_up_pending (got a reply but unclear what it means)
    return {
        "type": "follow_up_pending",
        "needs_action": True,
        "action_hint": "Response received — review and determine next steps",
    }


def _progress_contact_status(contact: Contact, vr: VendorResponse, db: Session):
    """Update the outbound Contact status based on the vendor's reply."""
    now = datetime.now(timezone.utc)

    # Already in a terminal state? Don't regress
    if contact.status in ("quoted", "declined"):
        return

    classification = vr.classification or ""

    if classification == "quote_provided":
        contact.status = "quoted"
    elif classification == "no_stock":
        contact.status = "declined"
    elif classification in ("ooo_bounce",):
        contact.status = "pending"  # Will need re-follow-up
    elif classification in (
        "clarification_needed",
        "counter_offer",
        "partial_availability",
    ):
        contact.status = "responded"
    else:
        # Generic response — at least we know they replied
        if contact.status in ("sent", "opened"):
            contact.status = "responded"

    contact.status_updated_at = now


# Noise filter — common non-vendor senders
NOISE_DOMAINS = {
    "microsoft.com",
    "microsoftonline.com",
    "office365.com",
    "office.com",
    "google.com",
    "googleapis.com",
    "googlemail.com",
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "github.com",
    "slack.com",
    "zoom.us",
    "teams.microsoft.com",
    "mailchimp.com",
    "constantcontact.com",
    "sendgrid.net",
    "amazonses.com",
    "hubspot.com",
    "salesforce.com",
    "marketo.com",
    "fedex.com",
    "ups.com",
    "usps.com",
    "dhl.com",
    "intuit.com",
    "quickbooks.com",
    "paypal.com",
    "stripe.com",
    "docusign.com",
    "dropbox.com",
    "box.com",
}

NOISE_PREFIXES = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "notifications",
    "alerts",
    "newsletter",
    "marketing",
    "support",
    "info",
    "billing",
}


def _is_noise_email(email: str) -> bool:
    """Check if an email address is likely non-vendor noise."""
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    if domain.lower() in NOISE_DOMAINS:
        return True
    if local.lower() in NOISE_PREFIXES:
        return True
    return False


async def parse_response_ai(body: str, subject: str) -> dict | None:
    """Use Claude to parse vendor email response.

    Delegates to claude_client for API calls. Kept as thin wrapper
    for backward compatibility with poll_inbox().
    """
    from app.services.response_parser import parse_vendor_response

    result = await parse_vendor_response(
        email_body=body,
        email_subject=subject,
        vendor_name="",  # Unknown at this point
    )

    if not result:
        return None

    # Map to the legacy format expected by poll_inbox
    return {
        "sentiment": result.get("overall_sentiment", "neutral"),
        "parts": result.get("parts", []),
        "confidence": result.get("confidence", 0),
    }


# ── Batch AI Processing ─────────────────────────────────────────────────


async def _submit_parse_batch(
    pending: list[VendorResponse],
    db: Session,
) -> None:
    """Submit pending VendorResponses to Anthropic Batch API for parsing."""
    from app.utils.claude_client import claude_batch_submit
    from app.services.response_parser import RESPONSE_PARSE_SCHEMA, SYSTEM_PROMPT, _clean_email_body

    requests = []
    request_map = {}  # custom_id -> vendor_response_id

    for vr in pending:
        cid = f"vr-{vr.id}"
        body_truncated = _clean_email_body(vr.body or "")[:4000]
        prompt = (
            f"Vendor: {vr.vendor_name}\n"
            f"Subject: {vr.subject}\n\n"
            f"Vendor reply:\n{body_truncated}"
        )
        requests.append({
            "custom_id": cid,
            "prompt": prompt,
            "schema": RESPONSE_PARSE_SCHEMA,
            "system": SYSTEM_PROMPT,
            "model_tier": "fast",
            "max_tokens": 1024,
        })
        request_map[cid] = vr.id

    batch_id = await claude_batch_submit(requests)
    if not batch_id:
        raise RuntimeError("Batch API returned no batch_id")

    # Record the pending batch for later polling
    pb = PendingBatch(
        batch_id=batch_id,
        batch_type="inbox_parse",
        request_map=request_map,
        status="processing",
        submitted_at=datetime.now(timezone.utc),
    )
    db.add(pb)
    log.info(f"Submitted batch {batch_id} with {len(requests)} email parse requests")


async def _parse_sequential_fallback(
    pending: list[VendorResponse],
    db: Session,
) -> None:
    """Fallback: parse emails concurrently (with semaphore) when batch API fails."""
    sem = asyncio.Semaphore(5)

    async def _parse_one(vr):
        async with sem:
            try:
                parsed = await parse_response_ai(vr.body, vr.subject)
                if parsed:
                    _apply_parsed_result(vr, parsed)
            except Exception as e:
                log.warning(f"Sequential AI parse failed for VR {vr.id}: {e}")

    await asyncio.gather(*[_parse_one(vr) for vr in pending])


def _apply_parsed_result(vr: VendorResponse, parsed: dict) -> None:
    """Apply AI-parsed data to a VendorResponse record."""
    vr.parsed_data = parsed
    vr.confidence = parsed.get("confidence", 0)
    vr.status = "parsed"
    classification = _classify_response(parsed, vr.body, vr.subject)
    vr.classification = classification["type"]
    vr.needs_action = classification["needs_action"]
    vr.action_hint = classification["action_hint"]


async def process_batch_results(db: Session) -> int:
    """Poll for completed Anthropic batches and apply results.

    Called by scheduler every tick. Returns count of results applied.
    """
    from app.utils.claude_client import claude_batch_results
    from app.services.response_parser import _normalize_parsed_parts

    pending_batches = (
        db.query(PendingBatch)
        .filter(PendingBatch.status == "processing")
        .all()
    )

    if not pending_batches:
        return 0

    total_applied = 0

    for pb in pending_batches:
        try:
            results = await claude_batch_results(pb.batch_id)
        except Exception as e:
            log.warning(f"Batch results check failed for {pb.batch_id}: {e}")
            # Mark as failed if submitted >24h ago
            if pb.submitted_at and datetime.now(timezone.utc) - pb.submitted_at > timedelta(hours=24):
                pb.status = "failed"
                pb.error_message = f"Timed out after 24h: {e}"
                db.commit()
            continue

        if results is None:
            # Still processing — check for timeout
            if pb.submitted_at and datetime.now(timezone.utc) - pb.submitted_at > timedelta(hours=24):
                pb.status = "failed"
                pb.error_message = "Batch did not complete within 24h"
                db.commit()
            continue

        # Batch complete — apply results
        request_map = pb.request_map or {}
        applied = 0

        for custom_id, parsed_data in results.items():
            vr_id = request_map.get(custom_id)
            if not vr_id:
                continue

            vr = db.get(VendorResponse, vr_id)
            if not vr or vr.status == "parsed":
                continue  # Already parsed (e.g., by sequential fallback)

            if parsed_data:
                # Claude may return tool input as a JSON string
                if isinstance(parsed_data, str):
                    try:
                        parsed_data = json.loads(parsed_data)
                    except (json.JSONDecodeError, TypeError):
                        log.warning(f"Batch item {custom_id}: unparseable string result")
                        continue
                if not isinstance(parsed_data, dict):
                    continue

                # Normalize extracted values
                _normalize_parsed_parts(parsed_data)

                # Map to legacy format
                legacy_parsed = {
                    "sentiment": parsed_data.get("overall_sentiment", "neutral"),
                    "parts": parsed_data.get("parts", []),
                    "confidence": parsed_data.get("confidence", 0),
                }
                _apply_parsed_result(vr, legacy_parsed)
                applied += 1

        pb.status = "completed"
        pb.completed_at = datetime.now(timezone.utc)
        pb.result_count = applied
        total_applied += applied

        try:
            db.commit()
            log.info(f"Batch {pb.batch_id} applied: {applied}/{len(request_map)} results")
        except Exception as e:
            log.error(f"Batch results commit failed for {pb.batch_id}: {e}")
            db.rollback()

    return total_applied
