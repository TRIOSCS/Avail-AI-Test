"""Email service — batch RFQ sending, inbox monitoring, AI parsing."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .services.rfq_attachments import RfqAttachment

from loguru import logger
from sqlalchemy import func as sqla_func
from sqlalchemy.orm import Session

from .constants import (
    ActivityType,
    ContactStatus,
    OfferStatus,
    PendingBatchStatus,
    VendorResponseStatus,
)
from .models import (
    ActivityLog,
    Contact,
    ExcessOutreach,
    Offer,
    PendingBatch,
    ProcessedMessage,
    Requirement,
    Requisition,
    SiteContact,
    VendorCard,
    VendorResponse,
)
from .services.activity_service import log_activity, log_email_activity
from .services.credential_service import get_credential_cached
from .shared_constants import JUNK_DOMAINS as NOISE_DOMAINS
from .shared_constants import JUNK_EMAIL_PREFIXES as NOISE_PREFIXES
from .shared_constants import RFQ_SUBJECT_TAG_RE
from .utils.async_helpers import hold_bg_task
from .vendor_utils import normalize_vendor_name


def _build_html_body(plain_text: str) -> str:
    """Convert plain text to minimal HTML that looks like a normal email."""
    html_body = plain_text.replace("\n", "<br>\n")
    return f"""<html><body style="font-family: Calibri, Arial, sans-serif; font-size: 14px; color: #333;">
{html_body}
</body></html>"""


def _create_contact(
    db: Session,
    requisition_id: int,
    user_id: int,
    vendor_name: str,
    vendor_email: str,
    parts: list,
    subject: str,
    body: str,
    status: str,
    error_message: str | None = None,
    sent_at: datetime | None = None,
) -> Contact:
    """Create and flush a Contact record with common fields.

    Called by: send_batch_rfq (for success, exception-error, and API-error cases).
    Depends on: normalize_vendor_name.
    sent_at: set only for status="sent" — records the true send moment so the
    outbound clock advances immediately without waiting for scan_sent_folder.
    """
    now = datetime.now(UTC)
    contact = Contact(
        requisition_id=requisition_id,
        user_id=user_id,
        contact_type="email",
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name or ""),
        vendor_contact=vendor_email,
        parts_included=parts,
        subject=subject,
        details=body,
        status=status,
        error_message=error_message,
        status_updated_at=now,
        created_at=now,
        sent_at=sent_at,
    )
    db.add(contact)
    db.flush()
    return contact


async def send_batch_rfq(
    token: str,
    db: Session,
    user_id: int,
    requisition_id: int | None = None,
    vendor_groups: list[dict] | None = None,
    requisition_parts_map: dict[int, list] | None = None,
    attachments: "list[RfqAttachment] | None" = None,
) -> list[dict]:
    """Send one RFQ email per vendor group.

    Each group: {vendor_name, vendor_email, parts, subject, body}.
    Returns one result dict per requested vendor, each tagged ``status``:
    ``"sent"``, ``"failed"`` (a send was attempted but errored), or ``"skipped"``
    (no contact email on file — no email attempted, no Contact created).

    Cross-requisition tracking: when ``requisition_parts_map`` ({requisition_id:
    parts}) is provided, ONE email still goes out per vendor, but one Contact row
    is written per (requisition, vendor) pair — each scoped to that requisition's
    parts, all sharing the email's graph_message_id/graph_conversation_id — and
    the subject carries one ``[ref:{id}]`` token per requisition (ascending id
    order, so preview and send stay deterministic and identical).

    Legacy shim: without the map, the scalar ``requisition_id`` behaves exactly
    as before (one Contact per vendor, parts taken from each vendor group) —
    callers like htmx_views.rfq_send need no change.

    ``requisition_id`` and ``requisition_parts_map`` are MUTUALLY EXCLUSIVE modes
    (single- vs cross-requisition). Passing both raises ``ValueError`` — the scalar
    would otherwise be silently ignored.

    ``attachments`` is an optional list of :class:`app.services.rfq_attachments.RfqAttachment`
    (or any object with ``name``, ``content_type``, ``content_bytes_b64`` attributes).
    When provided and non-empty, the SAME list is attached to EVERY vendor's email
    (datasheets are part-scoped, not vendor-scoped). Omitting it or passing None/[]
    keeps the payload byte-identical to the pre-attachment behaviour (no
    ``message.attachments`` key injected).
    """
    from app.utils.graph_client import GraphClient

    # The two requisition inputs are a sum type, not independent kwargs: EITHER
    # single-requisition mode (scalar requisition_id, parts from each vendor group) OR
    # cross-requisition mode (requisition_parts_map, parts from the map). Passing both is
    # meaningless — the scalar would be silently ignored (req_ids = sorted(parts_map)).
    # Fail loudly on the illegal combination instead of silently picking a winner.
    if requisition_parts_map is not None and requisition_id is not None:
        raise ValueError(
            "send_batch_rfq: pass requisition_id (single-req) OR requisition_parts_map "
            "(cross-req), never both — they are mutually exclusive modes"
        )

    vendor_groups = vendor_groups or []
    # Legacy scalar shim: a missing/empty map means single-requisition mode where
    # each Contact's parts come from its vendor group (byte-identical behavior).
    parts_map: dict[int, list] = requisition_parts_map or {}
    legacy_mode = not parts_map
    req_ids: list[int | None] = [requisition_id] if legacy_mode else sorted(parts_map)

    def _per_req_parts(group: dict) -> list[tuple[int, list]]:
        """(requisition_id, parts) pairs to write Contacts for, per vendor.

        Contact.requisition_id is NOT NULL, so a degenerate legacy call with no
        requisition at all yields no pairs (email sent, nothing tracked) instead of a
        guaranteed flush crash.
        """
        if legacy_mode:
            return [] if requisition_id is None else [(requisition_id, group.get("parts", []))]
        return [(rid, parts_map[rid]) for rid in req_ids if rid is not None]

    gc = GraphClient(token)
    results = []

    # Build payloads and send all emails in parallel. One token per involved
    # requisition, ascending id — identical to the sightings preview. A None
    # requisition (degenerate legacy call) yields no token at all, matching the
    # preview's untagged subject.
    avail_token = " ".join(f"[ref:{rid}]" for rid in req_ids if rid is not None)
    send_tasks = []
    send_groups = []  # Track which groups we're sending for

    for group in vendor_groups:
        email = group.get("vendor_email")
        if not email:
            # Don't silently drop: a selected vendor with no contact email must be
            # visible (logged + a "skipped" result record) so the caller can report it
            # distinctly rather than miscounting it as a delivery failure.
            logger.warning(
                "RFQ skipped — no contact email on file for vendor '{}'",
                group.get("vendor_name"),
            )
            results.append(
                {
                    "vendor_name": group.get("vendor_name"),
                    "vendor_email": "",
                    "status": "skipped",
                    "error": "no contact email on file",
                }
            )
            continue

        # DNC check — skip any email address that belongs to a do-not-contact
        # SiteContact. Case-insensitive comparison (func.lower on both sides)
        # matches the advisory check in _dnc_emails_for_cards so advisory ⊆
        # send-time. Compliance: the address must never appear in sendMail.
        dnc_match = (
            db.query(SiteContact)
            .filter(
                sqla_func.lower(SiteContact.email) == email.lower(),
                SiteContact.do_not_contact.is_(True),
            )
            .first()
        )
        if dnc_match:
            logger.warning(
                "RFQ skipped — do-not-contact flag set for vendor '{}' ({})",
                group.get("vendor_name"),
                email,
            )
            results.append(
                {
                    "vendor_name": group.get("vendor_name"),
                    "vendor_email": email,
                    "status": "skipped",
                    "error": "do-not-contact",
                }
            )
            continue

        html_body = _build_html_body(group["body"])
        raw_subject = group["subject"]
        tagged_subject = f"{raw_subject} {avail_token}" if avail_token not in raw_subject else raw_subject
        group["_tagged_subject"] = tagged_subject

        message: dict = {
            "subject": tagged_subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": email}}],
            "isReadReceiptRequested": False,
            "isDeliveryReceiptRequested": False,
        }
        # Attach datasheets when provided — same list for every vendor (part-scoped, not
        # vendor-scoped). Only inject the key when there are actual attachments; omitting it
        # keeps the payload byte-identical to pre-attachment behaviour (regression-safe).
        if attachments:
            message["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": att.name,
                    "contentType": att.content_type,
                    "contentBytes": att.content_bytes_b64,
                }
                for att in attachments
            ]
        payload = {"message": message, "saveToSentItems": "true"}
        send_tasks.append(gc.post_json("/me/sendMail", payload))
        send_groups.append(group)

    # Fire all sends with bounded parallelism (max 5 concurrent)
    sem = asyncio.Semaphore(5)

    async def _throttled_send(coro):
        async with sem:
            return await coro

    send_results = await asyncio.gather(*[_throttled_send(task) for task in send_tasks], return_exceptions=True)

    # Process results: create Contact records (one per involved requisition per
    # vendor), then batch-lookup sent message IDs. Each vendor's contact block
    # runs inside its own SAVEPOINT so one flush failure can't poison the other
    # vendors' rows or leave the session unusable for the route's commit (the
    # emails are already out at this point — tracking must be best-effort
    # per-vendor, and a tracking failure must be VISIBLE in the results).
    contacts_to_lookup = []  # (contacts_for_one_vendor, tagged_subject, vendor_email)
    for group, send_result in zip(send_groups, send_results):
        email = group["vendor_email"]
        tagged_subject = group.pop("_tagged_subject")

        if isinstance(send_result, Exception) or (isinstance(send_result, dict) and "error" in send_result):
            if isinstance(send_result, Exception):
                err_detail = str(send_result)
                logger.error(f"Send error to {email}: {send_result}")
            else:
                err_detail = str(send_result.get("detail", ""))
                logger.error(f"Send failed to {email}: {send_result}")
            per_req_parts = _per_req_parts(group)
            failed_contacts: list[Contact] = []
            try:
                with db.begin_nested():
                    failed_contacts = [
                        _create_contact(
                            db,
                            rid,
                            user_id,
                            group["vendor_name"],
                            email,
                            parts,
                            tagged_subject,
                            group["body"],
                            ContactStatus.FAILED,
                            err_detail[:500],
                        )
                        for rid, parts in per_req_parts
                    ]
            except Exception:
                logger.error(
                    "Contact tracking failed for vendor '{}' (requisitions {}) on the failed-send path",
                    group["vendor_name"],
                    [rid for rid, _ in per_req_parts],
                    exc_info=True,
                )
            results.append(
                {
                    "id": failed_contacts[0].id if failed_contacts else None,
                    "vendor_name": group["vendor_name"],
                    "vendor_email": email,
                    "status": "failed",
                    "error": err_detail[:200],
                }
            )
            continue

        per_req_parts = _per_req_parts(group)
        send_time = datetime.now(UTC)
        try:
            with db.begin_nested():
                contacts = [
                    _create_contact(
                        db,
                        rid,
                        user_id,
                        group["vendor_name"],
                        email,
                        parts,
                        tagged_subject,
                        group["body"],
                        ContactStatus.SENT,
                        sent_at=send_time,
                    )
                    for rid, parts in per_req_parts
                ]
                # Write the outbound ActivityLog IMMEDIATELY at send time (one row per
                # involved requisition) so the outbound clock advances now rather than
                # waiting up to 30 min for scan_sent_folder.  graph_message_id /
                # graph_conversation_id are not available yet — scan_sent_folder will
                # reconcile by setting ActivityLog.external_id on the EXISTING row
                # instead of creating a second one.
                for contact in contacts:
                    log_email_activity(
                        user_id=user_id,
                        direction="sent",
                        email_addr=email,
                        subject=tagged_subject,
                        external_id=None,  # filled in later by scan_sent_folder
                        contact_name=group["vendor_name"],
                        db=db,
                        requisition_id=contact.requisition_id,
                        occurred_at=send_time,  # ensures reconcile filter matches (no dup)
                    )
        except Exception:
            # The email WAS delivered — report a tracking error for this vendor
            # only; the rest of the batch keeps its rows.
            logger.error(
                "Contact tracking failed for vendor '{}' (requisitions {}) — RFQ email was already sent",
                group["vendor_name"],
                [rid for rid, _ in per_req_parts],
                exc_info=True,
            )
            results.append(
                {
                    "id": None,
                    "vendor_name": group["vendor_name"],
                    "vendor_email": email,
                    "status": "failed",
                    "error": "tracking_error: email sent but Contact rows could not be recorded",
                }
            )
            continue

        if contacts:
            contacts_to_lookup.append((contacts, tagged_subject, email))
        results.append(
            {
                "id": contacts[0].id if contacts else None,
                "vendor_name": group["vendor_name"],
                "vendor_email": email,
                # Per-vendor total across all involved requisitions (legacy
                # single-requisition: len of the group's parts, unchanged).
                "parts_count": sum(len(c.parts_included or []) for c in contacts),
                "status": "sent",
            }
        )

    # Batch-lookup sent message IDs in parallel for reply matching. One email per
    # vendor → all of that vendor's per-requisition Contacts share its graph ids.
    # Every vendor in a batch shares the SAME tagged subject, so the lookup is
    # vendor-discriminating (recipient email), never subject-only.
    if contacts_to_lookup:
        lookup_results = await asyncio.gather(
            *[_find_sent_message(gc, subj, vendor_email) for _, subj, vendor_email in contacts_to_lookup],
            return_exceptions=True,
        )
        for (contacts, subj, vendor_email), sent_msg in zip(contacts_to_lookup, lookup_results):
            if isinstance(sent_msg, dict) and sent_msg:
                for contact in contacts:
                    contact.graph_message_id = sent_msg.get("id")
                    contact.graph_conversation_id = sent_msg.get("conversationId")
            else:
                # The sent message could not be located (None after retries, or the
                # lookup task raised and was captured by return_exceptions). These
                # Contacts keep NULL graph ids, which silently downgrades all future
                # reply matching for this vendor from Tier-1 conversationId (exact
                # thread) to the weaker Tier-2/3/4 heuristics — make the dropped
                # association OBSERVABLE rather than silent. Common on large batches:
                # the vendor's own message can sit below the $top window in Sent Items.
                detail = f": {sent_msg}" if isinstance(sent_msg, Exception) else ""
                logger.warning(
                    "Sent-message lookup found no match for vendor '{}' <{}> "
                    "(subject '{}', requisitions {}) — Contact graph ids left NULL; "
                    "reply matching degrades to subject/email heuristics{}",
                    contacts[0].vendor_name,
                    vendor_email,
                    subj,
                    [c.requisition_id for c in contacts],
                    detail,
                )

    db.commit()

    # Tag propagation: propagate tags for ALL involved requisitions' RFQ'd parts
    # to vendor entities (one pass per unique vendor)
    try:
        from .services.tagging import propagate_tags_to_entity

        valid_req_ids = [rid for rid in req_ids if rid is not None]
        if valid_req_ids:
            reqs = db.query(Requirement).filter(Requirement.requisition_id.in_(valid_req_ids)).all()
            card_ids = [r.material_card_id for r in reqs if r.material_card_id]
            if card_ids:  # pragma: no cover
                seen_vendor_norms = set()
                for contacts, _, _ in contacts_to_lookup:
                    for contact_obj in contacts:
                        norm = contact_obj.vendor_name_normalized
                        if not norm or norm in seen_vendor_norms:
                            continue
                        seen_vendor_norms.add(norm)
                        vc = db.query(VendorCard).filter_by(normalized_name=norm).first()
                        if vc:
                            for cid in card_ids:
                                propagate_tags_to_entity("vendor_card", vc.id, cid, 0.5, db)
                db.commit()
    except Exception:  # pragma: no cover
        logger.warning("Tag propagation failed for RFQ batch", exc_info=True)

    return results


class SentMessageLookupError(RuntimeError):
    """Sent-Items lookup FAILED (Graph API errors) — delivery state is UNKNOWN.

    Raised by :func:`_find_sent_message` when the Sent-folder query itself errored (429 /
    5xx / expired token) on an attempt and no match was found, so "the message is not
    there" was never positively established. Callers implementing a double-send guard
    MUST treat this as indeterminate and never resend on it; ``None`` remains the
    positive "scanned the window cleanly, no match" answer that authorizes a resend.
    """


async def _find_sent_message(gc, subject: str, vendor_email: str) -> dict | None:
    """Find the just-sent message in Sent Items to get its ID and conversationId.

    Vendor-discriminating (F1): every vendor group in a batch shares an IDENTICAL tagged
    subject, so a subject-only match would return the newest vendor's message for every
    vendor — giving all batch Contacts one shared (wrong) conversation id and
    misattributing replies. The match therefore requires the vendor's email among
    toRecipients, and $top covers the whole batch fan-out (the vendor's own message can
    sit well below the top 5 right after a batch).

    Retries with exponential backoff (1s, 2s, 4s) to handle Graph API propagation
    delays.

    Three-state contract (deep-review #2, finding 2):
      - **found** → the message dict (``id`` + ``conversationId``);
      - **positively not found** → ``None`` — every attempt scanned the Sent window
        cleanly and the recipient's message is not there (a resend-safe answer);
      - **lookup failed** → raises :class:`SentMessageLookupError` — at least one attempt
        hit a Graph API error and no match was found, so delivery state is UNKNOWN.
        Double-send guards (e.g. ``retry_outreach_send``) must map this to their
        no-resend branch, never to "not delivered".
    """
    wanted = (vendor_email or "").strip().lower()
    delays = [1, 2, 4]
    api_error = False
    scanned = 0
    for delay in delays:
        try:
            await asyncio.sleep(delay)
            data = await gc.get_json(
                "/me/mailFolders/sentItems/messages",
                params={
                    "$top": "50",
                    "$orderby": "sentDateTime desc",
                    "$select": "id,conversationId,subject,toRecipients",
                },
            )
            msgs: list[dict] = data.get("value", []) if data else []  # Graph JSON boundary
            scanned = len(msgs)
            for m in msgs:
                if m.get("subject", "").strip() != subject.strip():
                    continue
                recipients = {
                    (r.get("emailAddress", {}).get("address") or "").lower() for r in m.get("toRecipients", [])
                }
                if wanted in recipients:
                    return m
        except Exception as e:
            api_error = True
            logger.warning(f"Sent message lookup attempt failed: {e}")
    # Distinguish a transient API failure from a genuine no-match: an API error means the
    # lookup FAILED and delivery state is unknown → raise (three-state contract), so a
    # double-send guard never mistakes an outage for "positively not delivered". A clean
    # no-match means the recipient never appeared in the $top window (likely pushed below
    # it by a large batch fan-out) → return None; the caller leaves graph ids NULL.
    if api_error:
        logger.warning(
            "Sent-message lookup FAILED for <{}> (subject '{}') — Graph API errors during retries; "
            "delivery state unknown (raising, not a positive no-match)",
            wanted,
            subject,
        )
        raise SentMessageLookupError(
            f"Sent-Items lookup for <{wanted}> (subject '{subject}') hit Graph API errors — delivery state unknown"
        )
    logger.warning(
        "Sent-message lookup found no match for <{}> (subject '{}') within the top {} Sent items "
        "— recipient may have been pushed below the window by a large batch",
        wanted,
        subject,
        scanned,
    )
    return None


def _scope_thread_contacts_to_sender(thread_contacts: list[Contact], sender_email: str) -> list[Contact]:
    """Vendor-scope a conversation-id (Tier-1) match to the replying vendor.

    A conversation id can map to contacts of DIFFERENT vendors (legacy rows
    written by the old subject-only sent-lookup), so a blind fan-out would
    progress every batch vendor when ONE replies. Scope: contacts whose vendor
    email equals the sender, falling back to same-domain contacts (a colleague
    replying from another mailbox), then to the full thread ONLY when it is
    single-vendor (every contact shares one vendor email). A multi-vendor thread
    with an unrecognized sender matches nothing — Tiers 2-4 take over.

    Fan out across requisitions for the SAME vendor, never across vendors.

    Called by: poll_inbox (Tier 1), _repair_contact_status_for_ooo_bounce.
    """
    sender = (sender_email or "").lower()
    exact = [c for c in thread_contacts if (c.vendor_contact or "").lower() == sender]
    if exact:
        return exact
    sender_domain = sender.split("@", 1)[1] if "@" in sender else ""
    if sender_domain and sender_domain not in NOISE_DOMAINS:
        domain_matches = [
            c
            for c in thread_contacts
            if c.vendor_contact
            and "@" in c.vendor_contact
            and c.vendor_contact.lower().split("@", 1)[1] == sender_domain
        ]
        if domain_matches:
            return domain_matches
    distinct_vendor_emails = {(c.vendor_contact or "").lower() for c in thread_contacts}
    if len(distinct_vendor_emails) == 1:
        return list(thread_contacts)
    logger.warning(
        "Tier-1 thread match dropped: sender {} matches none of the {} vendors on the conversation",
        sender,
        len(distinct_vendor_emails),
    )
    return []


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
        vendor_name_normalized=normalize_vendor_name(vendor_name or ""),
        vendor_contact=vendor_phone,
        parts_included=parts,
        subject=f"Call to {vendor_name}",
        created_at=datetime.now(UTC),
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
    token: str, db: Session, requisition_id: int | None = None, scanned_by_user_id: int | None = None
) -> list[dict]:
    """Check inbox for vendor replies. Smart-matches to outbound RFQs.

    Matching priority:
    1. conversationId — same email thread as a sent RFQ (global, exact; matches
       ALL Contacts sharing the thread — cross-requisition fan-out)
    2. Subject [ref:{req_id}] / [AVAIL-{req_id}] token(s) — explicit RFQ tags
       (every token in a multi-requisition subject is resolved)
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
    from app.config import settings
    from app.models import SyncState
    from app.utils.graph_client import GraphAPIError, GraphClient, GraphSyncStateExpired

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
                max_page_size=50,
                # Bound the initial full-sync round (and thus the whole resumable
                # drain) to the standard first-time backfill window instead of the
                # entire mailbox history.
                initial_lookback_days=settings.inbox_backfill_days,
            )
            messages = items
            used_delta = True

            # Persist new delta token
            if new_delta:
                if sync:
                    sync.delta_token = new_delta
                    sync.last_sync_at = datetime.now(UTC)
                else:
                    sync = SyncState(
                        user_id=scanned_by_user_id,
                        folder="inbox",
                        delta_token=new_delta,
                        last_sync_at=datetime.now(UTC),
                    )
                    db.add(sync)
                db.flush()
        except GraphSyncStateExpired as e:
            # 410 — Graph itself says the stored token is invalid. This is the
            # ONLY condition under which clearing it is correct; the fallback
            # full scan covers this poll and the next delta round starts fresh.
            logger.warning(f"Inbox delta token expired (410) — clearing and falling back: {e}")
            if sync and sync.delta_token:
                sync.delta_token = None
                db.flush()
            messages = []
            used_delta = False
        except GraphAPIError as e:
            # Typed error page mid-round — delta_query did NOT return a token, so
            # the stored one still points at the unfetched data and the next poll
            # resumes incrementally. Do NOT clear it; auth failures bubble up so
            # the caller marks the poll failed (no silent success).
            if e.status in (401, 403):
                logger.error(f"Inbox auth failure (not falling back): {e}")
                raise
            logger.warning(f"Inbox delta hit Graph error page (token kept), falling back to full scan: {e}")
            messages = []
            used_delta = False
        except Exception as e:
            # Transient/network error (e.g. httpx.ReadTimeout re-raised by the
            # retry layer). The stored token is still valid — clearing it would
            # force a full re-enumeration of the backfill window — so keep it and
            # surface a failed poll (the same outage would break the fallback
            # scan too).
            logger.error(f"Inbox delta failed (token kept): {e}")
            raise

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
            if data and "error" in data:
                # The retry layer signals exhausted retries / non-retryable 4xx
                # as an {"error": ...} dict — a hard Graph outage must surface
                # as a failed poll, not a successful empty one.
                raise GraphAPIError(data["error"], str(data.get("detail", "")))
            messages = data.get("value", []) if data else []
        except Exception as e:
            logger.error(f"Inbox poll failed: {e}")
            raise  # Let caller handle — router returns proper error, job skips watermark

    # ── H2: Dedup via processed_messages table (belt-and-suspenders) ──
    incoming_ids = [m.get("id", "") for m in messages if m.get("id")]
    already_processed = set()
    if incoming_ids:
        # Check both VendorResponse and ProcessedMessage tables
        vr_rows = db.query(VendorResponse.message_id).filter(VendorResponse.message_id.in_(incoming_ids)).all()
        pm_rows = (
            db.query(ProcessedMessage.message_id)
            .filter(
                ProcessedMessage.message_id.in_(incoming_ids),
                ProcessedMessage.processing_type == "inbox_poll",
            )
            .all()
        )
        already_processed = {r[0] for r in vr_rows} | {r[0] for r in pm_rows}

    # Pre-load outbound email contacts for matching (last 6 months). Ordered by
    # id (= creation order) so the fan-out lists below are deterministic and the
    # last-writer-wins maps genuinely keep the MOST RECENT contact.
    cutoff = datetime.now(UTC) - timedelta(days=180)
    all_contacts = (
        db.query(Contact)
        .filter(
            Contact.contact_type == "email",
            Contact.created_at > cutoff,
        )
        .order_by(Contact.id)
        .all()
    )

    # ConversationId map is GLOBAL — thread-specific, unambiguous. Cross-requisition
    # sends write one Contact per (requisition, vendor) sharing a single
    # graph_conversation_id, so each conversation maps to a LIST of contacts; a
    # reply on the thread is attributed to ALL of them.
    conv_id_map: dict[str, list[Contact]] = {}
    # Email and domain maps are USER-SCOPED to prevent cross-user data leaks
    email_map = {}  # vendor_email -> Contact (only this user's contacts)
    domain_map = {}  # vendor_domain -> Contact (only this user's contacts)
    # O(1) lookup for Tier 2 subject-token matching: (req_id, email) -> Contact.
    # The setdefault is correct under the per-requisition fan-out: send_batch_rfq
    # writes at most one Contact per (requisition, vendor email) pair per send, so
    # keys are effectively unique; for historical duplicates (e.g. a re-send) it
    # keeps the earliest contact, which is the long-standing behavior.
    req_email_map: dict[tuple[int, str], Contact] = {}
    for c in all_contacts:
        if c.graph_conversation_id:
            conv_id_map.setdefault(c.graph_conversation_id, []).append(c)  # type: ignore[call-overload]  # Column[str] key is str at instance level
        # Build req+email map for Tier 2 (all contacts, not just this user's)
        if c.requisition_id and c.vendor_contact:
            req_email_map.setdefault((c.requisition_id, c.vendor_contact.lower()), c)
        # Only build email/domain maps for the scanning user
        if scanned_by_user_id and c.user_id == scanned_by_user_id:
            if c.vendor_contact:
                email_lower = c.vendor_contact.lower()
                # Last-writer-wins = most-recent contact BY DESIGN: Tier 3/4 are
                # user-scoped fallback heuristics for untokenized replies, so they
                # deliberately stay single-contact (no fan-out).
                email_map[email_lower] = c
                # Extract domain for domain-level matching
                if "@" in email_lower:
                    domain = email_lower.split("@", 1)[1]
                    if domain not in NOISE_DOMAINS:
                        domain_map[domain] = c

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
        # A cross-requisition RFQ writes one Contact per (requisition, vendor), so
        # Tiers 1-2 can match SEVERAL contacts for one message. One VendorResponse
        # is created per message (contact_id = first matched contact); every
        # matched contact's status progresses below.
        matched_contacts: list[Contact] = []
        matched_req_id = requisition_id
        # Resell outreach rows this reply matches (buyer replying to an offer-out). Stays
        # empty unless the resell tier below fires; only ever set when no Contact matched.
        matched_resell_rows: list[ExcessOutreach] = []

        # Tier 1: ConversationId (exact thread, global) — all of the REPLYING
        # vendor's contacts on the thread (vendor-scoped: a reply from vendor A
        # must never progress vendor B's contacts on a collided conversation).
        if conv_id and conv_id in conv_id_map:
            matched_contacts = _scope_thread_contacts_to_sender(conv_id_map[conv_id], email_addr)
            if matched_contacts:
                matched_req_id = matched_contacts[0].requisition_id

        # Tier 2: Subject [ref:]/[AVAIL-] tokens — iterate ALL tokens (cross-
        # requisition subjects carry one token per involved requisition). Tokens
        # can outlive their requisition (deleted reqs, forwarded subjects), so
        # they are existence-filtered in ONE query before any use — a stale id
        # must never reach VendorResponse.requisition_id (FK violation on PG).
        if not matched_contacts:
            token_req_ids = list(dict.fromkeys(int(t) for t in RFQ_SUBJECT_TAG_RE.findall(subj)))
            if token_req_ids:
                live_ids = {row[0] for row in db.query(Requisition.id).filter(Requisition.id.in_(token_req_ids)).all()}
                if dropped := [t for t in token_req_ids if t not in live_ids]:
                    logger.warning("Dropping stale [ref:] token(s) {} from '{}'", dropped, subj[:120])
                token_req_ids = [t for t in token_req_ids if t in live_ids]
            if token_req_ids:
                # O(1) dict lookups instead of O(n) list comprehensions
                matched_contacts = [
                    req_contact
                    for token_req_id in token_req_ids
                    if (req_contact := req_email_map.get((token_req_id, email_addr)))
                ]
                if matched_contacts:
                    matched_req_id = matched_contacts[0].requisition_id
                else:
                    # Token(s) found but no exact email match — still assign to
                    # the first token's requisition
                    matched_req_id = token_req_ids[0]

        # Tier 2.5: Resell outreach (exact conversation/message id). A buyer replying to
        # an offered-OUT excess list — the trader→buyer inverse of the RFQ path. Runs only
        # when no Contact matched (an RFQ reply always wins), and BEFORE the fuzzy
        # email/domain fallbacks, mirroring the exact-before-fuzzy tier order. Reuses the
        # already-built + unit-tested resell matcher; a hit routes to record_response below.
        if not matched_contacts:
            from .services.resell_outreach_service import _match_outreach

            matched_resell_rows = _match_outreach(db, conversation_id=conv_id, message_id=msg_id)

        # Tier 3: Exact email match (USER-SCOPED, most-recent contact by design). Yields
        # to the exact resell match above — an exact conv/msg hit must win over this fuzzy
        # sender-email fallback (a buyer whose address also appears in email_map).
        if not matched_contacts and not matched_resell_rows and email_addr in email_map:
            matched_contacts = [email_map[email_addr]]
            matched_req_id = matched_contacts[0].requisition_id

        # Tier 4: Domain match (USER-SCOPED, most-recent contact by design)
        if not matched_contacts and not matched_resell_rows and "@" in email_addr:
            sender_domain = email_addr.split("@", 1)[1]
            if sender_domain in domain_map:
                matched_contacts = [domain_map[sender_domain]]
                matched_req_id = matched_contacts[0].requisition_id

        try:
            # Use savepoint so a single message failure doesn't poison the session
            nested = db.begin_nested()
            # ONE VendorResponse per message (it is per-message, not per-
            # requisition); contact_id anchors to the first matched contact.
            body_content = msg.get("body", {}).get("content", msg.get("bodyPreview", ""))
            vr = VendorResponse(
                requisition_id=matched_req_id,
                contact_id=matched_contacts[0].id if matched_contacts else None,
                vendor_name=sender.get("name", email_addr),
                vendor_email=email_addr,
                subject=subj,
                body=body_content,
                message_id=msg_id,
                graph_conversation_id=conv_id,
                scanned_by_user_id=scanned_by_user_id,
                received_at=msg.get("receivedDateTime"),
                status="matched" if (matched_contacts or matched_resell_rows) else VendorResponseStatus.NEW,
                created_at=datetime.now(UTC),
            )
            db.add(vr)
            db.flush()

            # Activity timeline: log the inbound vendor reply so it appears on the
            # requisition Activity tab. Dedups on external_id=msg_id. Auto-replies (OOO/
            # vacation/bounce/NDR) are not genuine correspondence — skip logging them so
            # they never leak onto a customer's Activity tab (ISS-030). The VendorResponse
            # row above still records the raw message for the RFQ classifier either way.
            if not _is_auto_reply(subj, body_content):
                log_email_activity(
                    user_id=scanned_by_user_id,
                    direction="received",
                    email_addr=email_addr,
                    subject=subj,
                    external_id=msg_id,
                    contact_name=sender.get("name"),
                    db=db,
                    requisition_id=matched_req_id,
                )

            # H2: Record in processed_messages for cross-type dedup
            db.add(
                ProcessedMessage(
                    message_id=msg_id,
                    processing_type="inbox_poll",
                    processed_at=datetime.now(UTC),
                )
            )

            # ── Contact status progression ──
            # Every matched contact progresses: a cross-requisition reply must
            # advance the (requisition, vendor) row on EVERY involved requisition.
            for mc in matched_contacts:
                _progress_contact_status(mc, vr, db)

            # ── Resell outreach progression (Tier 2.5 hit) ──
            # A buyer replying to an offered-out list: advance the ExcessOutreach
            # row(s) sent→responded and log the inbound reply on the resell timeline.
            # commit=False keeps it inside THIS message's savepoint (one bad resell
            # reply rolls back with the rest, never poisons the whole scan). Offer
            # extraction stays MANUAL (the "Convert to offer" quick-add), so has_offer
            # is False here — status advances to responded, not bid.
            # An OOO / vacation / bounce auto-reply is NOT genuine engagement: advancing the
            # outreach to "responded" and logging a "meaningful" inbound reply would stop the
            # follow-up clock on a buyer who never actually replied. The RFQ path repairs this
            # after AI parsing, but a purely-resell reply skips AI parsing — so gate it here.
            # The VendorResponse row above still records the raw inbound message either way.
            if matched_resell_rows and not _is_auto_reply(subj, vr.body):
                from .services.resell_outreach_service import _log_inbound_reply_activity, record_response

                updated = record_response(
                    db,
                    conversation_id=conv_id,
                    message_id=msg_id,
                    has_offer=False,  # offer extraction stays manual (Convert-to-offer)
                    commit=False,
                )
                for outreach_row in updated:
                    _log_inbound_reply_activity(db, outreach=outreach_row, vr=vr)

            nested.commit()

            # ── Reply classification + AI parsing (batch or inline) ──
            # Only enqueue for AI parsing AFTER the savepoint commits. If the
            # savepoint above had failed, the except rolls back vr; enqueuing
            # earlier would let a vanished vendor_response_id reach AI parsing
            # (billed) and _auto_create_offers_from_parse, poisoning the final
            # db.commit() and rolling back the entire scan.
            # A purely-resell reply (matched the outreach tier, no Contact) carries no RFQ
            # to parse and no contact status to repair, so it never enters AI parsing —
            # don't bill Claude for it. A reply that also matched a Contact still parses.
            purely_resell = bool(matched_resell_rows) and not matched_contacts
            if get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY") and not purely_resell:
                pending_parse.append(vr)

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
                    "matched_contact_id": matched_contacts[0].id if matched_contacts else None,
                    "matched_requisition_id": matched_req_id,
                }
            )
        except Exception as e:
            logger.error(f"Failed to save inbox message {msg_id[:20]}: {e}")
            nested.rollback()
            continue

    # ── Submit AI batch or fall back to sequential parsing ──
    # Gate on the daily email-mining budget cap BEFORE either path spends Claude credits.
    # Trimming here (rather than inside _submit_parse_batch) keeps the batch and its
    # sequential fallback under the same ceiling — the fallback can't bypass the cap.
    if pending_parse:
        to_parse = _enforce_email_mining_cap(pending_parse)
        if to_parse:
            try:
                await _submit_parse_batch(to_parse, db)
            except Exception as e:
                logger.warning(f"Batch submit failed, falling back to sequential: {e}")
                await _parse_sequential_fallback(to_parse, db)
            # Bill the day's counter for the calls just dispatched (batch or fallback),
            # so successive scans within the same UTC day respect the cap.
            _record_email_mining_calls(len(to_parse))

    # ── Post-parse: update contact status for OOO/bounce classifications ──
    # Only the sequential fallback sets vr.classification synchronously; batch
    # results arrive later and are repaired in process_batch_results.
    for vr in pending_parse:
        _repair_contact_status_for_ooo_bounce(vr, db)

    # Single commit for all new responses
    try:
        db.commit()
    except Exception as e:
        logger.error(f"Batch commit failed during inbox poll: {e}")
        db.rollback()
        raise  # Let caller handle retry / watermark advancement

    return results


# Out-of-office / vacation / bounce / delivery-failure phrases that mark a message as an
# automated reply rather than a genuine human response.
_AUTO_REPLY_SIGNALS = (
    "out of office",
    "automatic reply",
    "autoreply",
    "i am currently out",
    "on vacation",
    "will return",
    "away from",
    "undeliverable",
    "delivery failure",
)


def _is_auto_reply(subject: str, body: str) -> bool:
    """True if the message looks like an OOO / vacation / bounce auto-reply.

    Shared by the RFQ classifier and the resell reply path so neither treats an
    automated reply as genuine engagement (advancing status / stopping the follow-up
    clock).
    """
    body_lower = (body or "").lower()[:2000]
    subject_lower = (subject or "").lower()
    return any(s in body_lower or s in subject_lower for s in _AUTO_REPLY_SIGNALS)


def _classify_response(parsed: dict, body: str, subject: str) -> dict:
    """Classify a vendor response into actionable categories."""
    body_lower = (body or "").lower()[:2000]

    # OOO / bounce detection
    if _is_auto_reply(subject, body):
        return {
            "type": "ooo_bounce",
            "needs_action": False,
            "action_hint": "Auto-reply detected \u2014 will need follow-up later",
        }

    parts = parsed.get("parts", [])
    sentiment = parsed.get("sentiment", "neutral")

    # Quote provided — has actual pricing
    if parts and any(p.get("unit_price") for p in parts):
        return {
            "type": "quote_provided",
            "needs_action": True,
            "action_hint": f"Quote received for {len(parts)} part(s) \u2014 review pricing",
        }

    # Partial availability — some parts but not all, or limited qty
    if parts and sentiment == "positive":
        if any(p.get("qty_available") for p in parts):
            return {
                "type": "partial_availability",
                "needs_action": True,
                "action_hint": "Partial stock available \u2014 review quantities",
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
            "action_hint": "Vendor proposed an alternative \u2014 review offer",
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
            "action_hint": "Vendor asked a question \u2014 reply needed",
        }

    # Default: follow_up_pending (got a reply but unclear what it means)
    return {
        "type": "follow_up_pending",
        "needs_action": True,
        "action_hint": "Response received \u2014 review and determine next steps",
    }


# Bounce sub-signals within the merged "ooo_bounce" classification (the
# classifiers deliberately fold OOO and bounces into one bucket — see
# _classify_response's ooo_signals and RESPONSE_PARSE_SCHEMA).
_BOUNCE_SIGNALS = (
    "undeliverable",
    "delivery failure",
    "delivery has failed",
    "mailer-daemon",
    "address not found",
    "recipient not found",
)


def _repair_contact_status_for_ooo_bounce(vr: VendorResponse, db: Session) -> None:
    """Correct outbound Contact status when a reply classifies as OOO/bounce.

    The classification vocabulary is exactly ONE value for this family:
    "ooo_bounce" — emitted by _classify_response (sequential fallback path),
    RESPONSE_PARSE_SCHEMA's enum (batch path via _apply_parsed_result), and the
    ai.py reparse endpoint. Anything else here would be dead code (the old
    {"ooo","out_of_office","auto_reply"} / {"bounce",...} sets never fired).

    Applies to ALL contacts matched for the message — re-derived from the
    reply's graph_conversation_id with the same vendor-email scoping as Tier-1
    matching (never across vendors), falling back to the anchored vr.contact_id.
    Bounce-vs-OOO is sub-classified from the message text; terminal statuses
    (quoted/declined) are never regressed.

    Called by: poll_inbox (post-parse, sequential fallback), process_batch_results.
    """
    if (vr.classification or "").lower() != "ooo_bounce":
        return

    contacts: list[Contact] = []
    if vr.graph_conversation_id:
        thread = (
            db.query(Contact)
            .filter(
                Contact.graph_conversation_id == vr.graph_conversation_id,
                Contact.contact_type == "email",
            )
            .order_by(Contact.id)
            .all()
        )
        contacts = _scope_thread_contacts_to_sender(thread, (vr.vendor_email or "").lower())
    if not contacts and vr.contact_id:
        anchored = db.get(Contact, vr.contact_id)
        if anchored:
            contacts = [anchored]

    text = f"{vr.subject or ''} {vr.body or ''}".lower()
    new_status = ContactStatus.BOUNCED if any(s in text for s in _BOUNCE_SIGNALS) else ContactStatus.OOO
    now = datetime.now(UTC)
    for contact in contacts:
        if contact.status in (ContactStatus.QUOTED, ContactStatus.DECLINED):
            continue  # never regress a terminal state on a late auto-reply
        contact.status = new_status
        contact.status_updated_at = now


def _progress_contact_status(contact: Contact, vr: VendorResponse, db: Session):
    """Update the outbound Contact status based on the vendor's reply."""
    now = datetime.now(UTC)

    # Already in a terminal state? Don't regress
    if contact.status in (ContactStatus.QUOTED, ContactStatus.DECLINED):
        return

    classification = vr.classification or ""

    if classification == "quote_provided":
        contact.status = ContactStatus.QUOTED
    elif classification == "no_stock":
        contact.status = ContactStatus.DECLINED
    elif classification == "ooo_bounce":
        contact.status = ContactStatus.PENDING  # Will need re-follow-up
    elif classification in (
        "clarification_needed",
        "counter_offer",
        "partial_availability",
    ):
        contact.status = ContactStatus.RESPONDED
    else:
        # Generic response — at least we know they replied
        if contact.status in (ContactStatus.SENT, ContactStatus.OPENED):
            contact.status = ContactStatus.RESPONDED

    contact.status_updated_at = now


# NOISE_DOMAINS and NOISE_PREFIXES are imported from shared_constants
# (as JUNK_DOMAINS / JUNK_EMAIL_PREFIXES) at the top of this file.

# Exchange auto-generates one NDR/bounce mailbox per org with a random hex suffix
# appended to a fixed prefix (e.g. "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@
# own-domain.com") — JUNK_EMAIL_PREFIXES can't list it as an exact local-part match
# because the suffix varies per tenant. poll_inbox does not fetch the Graph
# internetMessageHeaders needed for a proper NDR/Content-Type: report header check, so
# this conservative startswith() on the fixed prefix is the local heuristic (ISS-030).
_EXCHANGE_NDR_LOCAL_PREFIX = "microsoftexchange"


def _is_noise_email(email: str) -> bool:
    """Check if an email address is likely non-vendor noise."""
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    if domain.lower() in NOISE_DOMAINS:
        return True
    local_lower = local.lower()
    if local_lower in NOISE_PREFIXES:
        return True
    if local_lower.startswith(_EXCHANGE_NDR_LOCAL_PREFIX):
        return True
    return False


async def parse_response_ai(body: str, subject: str) -> dict | None:
    """Use Claude to parse vendor email response.

    Delegates to claude_client for API calls. Kept as thin wrapper for backward
    compatibility with poll_inbox().
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


def _mining_submit_key() -> str:
    """Redis key for today's count of dispatched email-mining Claude requests (UTC
    date)."""
    return "email_mining:batch:submitted:" + datetime.now(UTC).strftime("%Y-%m-%d")


def _email_mining_calls_today() -> int:
    """Today's email-mining Claude request count for the daily budget gate.

    Returns ``max(metered, submitted)`` so the cap holds even before a batch's own usage
    is metered:
      * ``metered`` sums the ``claude_usage:email_mining:{tier}:calls:{date}`` counters
        that ``claude_client._meter_usage`` writes on batch-results poll (lags submit);
      * ``submitted`` is the ``email_mining:batch:submitted:{date}`` counter this path
        bumps at dispatch time (covers the metering lag).
    Mirrors the enrichment worker's ``max(intel_cache.get_count(...), in_process_floor)``
    reconciliation. Best-effort: any cache error degrades to 0 (no cap) — never raises.
    """
    from app.cache import intel_cache

    try:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        metered = sum(
            intel_cache.get_count(f"claude_usage:email_mining:{tier}:calls:{today}")
            for tier in ("fast", "smart", "opus")
        )
        submitted = intel_cache.get_count(_mining_submit_key())
        return max(metered, submitted)
    except Exception as e:  # cap accounting must never break the inbox poll
        logger.debug("email-mining spend read skipped: {}", e)
        return 0


def _enforce_email_mining_cap(pending: list[VendorResponse]) -> list[VendorResponse]:
    """Trim today's email-mining batch to the remaining daily Claude-call budget.

    Returns the slice of *pending* that may be AI-parsed today (the highest-volume order
    is preserved). A non-positive ``email_mining_batch_daily_cap`` disables the cap →
    pre-cap unbounded behavior. Logs (Loguru) when the cap trims or fully blocks the
    batch; trimmed-off responses stay raw (status 'new'/'matched') and remain re-parsable
    later (manual ai.py reparse), so no data is lost — only Claude spend is bounded.
    """
    from app.config import settings

    cap = settings.email_mining_batch_daily_cap
    if cap <= 0:
        return pending

    already = _email_mining_calls_today()
    remaining = cap - already
    if remaining <= 0:
        logger.warning(
            "Email-mining daily budget cap reached ({}/{}) — skipping AI parse of {} "
            "vendor response(s) this cycle (raw rows kept; re-parsable after UTC rollover)",
            already,
            cap,
            len(pending),
        )
        return []
    if len(pending) > remaining:
        logger.warning(
            "Email-mining daily budget cap: trimming batch {}->{} request(s) (today {}/{}); "
            "{} response(s) stay raw this cycle",
            len(pending),
            remaining,
            already,
            cap,
            len(pending) - remaining,
        )
        return pending[:remaining]
    return pending


def _record_email_mining_calls(count: int) -> None:
    """Bump today's email-mining submitted-call counter by *count* (best-effort)."""
    if count <= 0:
        return
    from app.cache import intel_cache

    try:
        intel_cache.incr_count(_mining_submit_key(), amount=count, ttl_days=1.0)
    except Exception as e:  # metering is best-effort; never break the inbox poll
        logger.debug("email-mining submit-count bump skipped: {}", e)


async def _submit_parse_batch(
    pending: list[VendorResponse],
    db: Session,
) -> None:
    """Submit pending VendorResponses to Anthropic Batch API for parsing."""
    from app.services.response_parser import RESPONSE_PARSE_SCHEMA, SYSTEM_PROMPT
    from app.utils.claude_client import claude_batch_submit
    from app.utils.text_utils import clean_email_body

    requests = []
    request_map = {}  # custom_id -> vendor_response_id

    for vr in pending:
        cid = f"vr-{vr.id}"
        body_truncated = clean_email_body(vr.body or "")[:4000]
        prompt = f"Vendor: {vr.vendor_name}\nSubject: {vr.subject}\n\nVendor reply:\n{body_truncated}"
        requests.append(
            {
                "custom_id": cid,
                "prompt": prompt,
                "schema": RESPONSE_PARSE_SCHEMA,
                "system": SYSTEM_PROMPT,
                "model_tier": "fast",
                "max_tokens": 1024,
            }
        )
        request_map[cid] = vr.id

    batch_id = await claude_batch_submit(requests, cost_bucket="email_mining")
    if not batch_id:
        raise RuntimeError("Batch API returned no batch_id")

    # Record the pending batch for later polling
    pb = PendingBatch(
        batch_id=batch_id,
        batch_type="inbox_parse",
        request_map=request_map,
        status=PendingBatchStatus.PROCESSING,
        submitted_at=datetime.now(UTC),
    )
    db.add(pb)
    logger.info(f"Submitted batch {batch_id} with {len(requests)} email parse requests")


async def _parse_sequential_fallback(
    pending: list[VendorResponse],
    db: Session,
) -> None:
    """Fallback: parse emails sequentially when batch API fails.

    DB writes are serialized to avoid concurrent access to a single session.
    AI calls are parallelized, but results are applied one at a time.
    """
    sem = asyncio.Semaphore(5)

    async def _parse_one(vr):
        async with sem:
            try:
                return vr, await parse_response_ai(vr.body, vr.subject)
            except Exception as e:
                logger.warning(f"Sequential AI parse failed for VR {vr.id}: {e}")
                return vr, None

    results = await asyncio.gather(*[_parse_one(vr) for vr in pending])

    # Apply results serially to avoid concurrent session writes
    for vr, parsed in results:
        if parsed:
            try:
                _apply_parsed_result(vr, parsed, db)
            except Exception as e:
                logger.warning(f"Failed to apply parsed result for VR {vr.id}: {e}")
                db.rollback()


def _apply_parsed_result(vr: VendorResponse, parsed: dict, db: Session | None = None) -> None:
    """Apply AI-parsed data to a VendorResponse record.

    Pure field assignment \u2014 classifies the response and sets fields on vr.
    When db is provided, also runs _auto_create_offers_from_parse for
    backward compatibility with existing callers.

    Called by: _parse_sequential_fallback, process_batch_results, tests.
    Depends on: _classify_response, _auto_create_offers_from_parse.
    """
    vr.parsed_data = parsed
    vr.confidence = parsed.get("confidence", 0)
    vr.status = VendorResponseStatus.PARSED
    classification = _classify_response(parsed, vr.body, vr.subject)
    vr.classification = classification["type"]
    vr.needs_action = classification["needs_action"]
    vr.action_hint = classification["action_hint"]

    # Delegate business-logic side effects when db is provided
    if db is not None:
        try:
            _auto_create_offers_from_parse(vr, parsed, db)
        except Exception as e:
            logger.error("Auto-create offers failed for VR {}: {}", getattr(vr, "id", "?"), e, exc_info=True)


def _auto_create_offers_from_parse(vr: VendorResponse, parsed: dict, db: Session) -> None:
    """Auto-create Offer records and side effects from parsed email data.

    Business logic extracted from _apply_parsed_result:
    - Creates Offer records (high confidence >= 0.8 active, 0.5-0.8 pending_review)
    - Generates tasks for email-parsed offers
    - Captures offer facts into Knowledge Ledger
    - Propagates tags from material cards to vendor cards
    - Resets strategic vendor 39-day clock
    - Creates/updates deduplicated ActivityLog notifications

    Called by: _apply_parsed_result (when db provided).
    Depends on: extract_draft_offers, resolve_material_card, normalize_mpn_key,
                tier_for_parsed_offer, task_service, knowledge_service, tagging.
    """
    if not (vr.confidence and vr.confidence >= 0.5):
        return

    try:
        from .services.response_parser import extract_draft_offers

        draft_offers = extract_draft_offers(parsed, vr.vendor_name or "Unknown")
    except Exception as e:
        logger.warning("Failed to extract draft offers: {}", e)
        return

    req = db.get(Requisition, vr.requisition_id) if vr.requisition_id else None
    owner_id = vr.scanned_by_user_id
    if req and req.created_by:
        owner_id = req.created_by

    # Build maps for linking: MPN -> requirement_id, MPN -> material_card_id
    from .search_service import resolve_material_card
    from .utils.normalization import normalize_mpn_key

    mpn_to_req_id: dict[str, int] = {}
    mpn_to_card_id: dict[str, int] = {}
    if req:
        for r in db.query(Requirement).filter(Requirement.requisition_id == vr.requisition_id).all():
            if r.primary_mpn:
                key = normalize_mpn_key(r.primary_mpn) or r.primary_mpn.upper().strip()
                mpn_to_req_id[key] = r.id
                if r.material_card_id:
                    mpn_to_card_id[key] = r.material_card_id

    for draft in draft_offers:
        try:
            raw_mpn = draft.get("mpn") or ""
            mpn_key = normalize_mpn_key(raw_mpn) or raw_mpn.upper().strip()
            # Dedup: check if offer already exists from this vendor response
            existing = (
                db.query(Offer.id)
                .filter(
                    Offer.vendor_response_id == vr.id,
                    Offer.mpn == draft.get("mpn", ""),
                )
                .first()
            )
            if existing:
                continue

            # Resolve material card -- use requirement's card or find/create
            card_id = mpn_to_card_id.get(mpn_key)
            if not card_id and raw_mpn.strip():
                card = resolve_material_card(raw_mpn, db)
                if card:
                    card_id = card.id

            from .evidence_tiers import tier_for_parsed_offer

            offer = Offer(
                requisition_id=vr.requisition_id,
                requirement_id=mpn_to_req_id.get(mpn_key),
                material_card_id=card_id,
                vendor_name=draft.get("vendor_name", ""),
                vendor_name_normalized=normalize_vendor_name(draft.get("vendor_name", "")),
                mpn=draft.get("mpn", ""),
                manufacturer=draft.get("manufacturer"),
                qty_available=draft.get("qty_available"),
                unit_price=draft.get("unit_price"),
                currency=draft.get("currency", "USD"),
                lead_time=draft.get("lead_time"),
                date_code=draft.get("date_code"),
                condition=draft.get("condition"),
                packaging=draft.get("packaging"),
                moq=draft.get("moq"),
                notes=draft.get("notes"),
                source="email_parse",
                status=OfferStatus.ACTIVE if vr.confidence >= 0.8 else OfferStatus.PENDING_REVIEW,
                vendor_response_id=vr.id,
                evidence_tier=tier_for_parsed_offer(vr.confidence),
                parse_confidence=vr.confidence,
            )
            db.add(offer)
            db.flush()

            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=offer.requisition_id,
                requirement_id=offer.requirement_id,
                user_id=None,
                vendor_card_id=offer.vendor_card_id,
                description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
                details={"offer_id": offer.id, "source": offer.source},
            )

            # Auto-generate task for email-parsed offer
            try:
                from app.services.task_service import on_email_offer_parsed

                on_email_offer_parsed(
                    db,
                    offer.requisition_id,
                    offer.vendor_name or "Unknown",
                    offer.mpn or "?",
                    offer.id,
                )
            except Exception:
                logger.warning("Task auto-gen for email offer failed", exc_info=True)

            # Auto-capture offer facts into Knowledge Ledger
            try:
                from app.services.knowledge_service import capture_offer_fact

                capture_offer_fact(db, offer=offer)
            except Exception as e:
                logger.warning("Knowledge auto-capture (email offer) failed: {}", e)

            # Tag propagation: propagate material card tags to vendor
            try:
                if offer.material_card_id and offer.vendor_name_normalized:
                    from .services.tagging import propagate_tags_to_entity

                    vc = db.query(VendorCard).filter_by(normalized_name=offer.vendor_name_normalized).first()
                    if vc:  # pragma: no cover
                        propagate_tags_to_entity("vendor_card", vc.id, offer.material_card_id, 1.0, db)
            except Exception:
                logger.warning("Tag propagation failed for offer {}", offer.id, exc_info=True)

            # Reset strategic vendor 39-day clock
            if offer.vendor_card_id:
                try:
                    from app.services.strategic_vendor_service import record_offer as sv_record

                    sv_record(db, offer.vendor_card_id)
                except Exception:
                    logger.warning("Strategic vendor clock reset failed for offer {}", offer.id, exc_info=True)

            # Deduplicated notification -- update existing if unread, else create new
            if owner_id:
                notif_q = db.query(ActivityLog).filter(
                    ActivityLog.user_id == owner_id,
                    ActivityLog.activity_type == "offer_pending_review",
                    ActivityLog.requisition_id == vr.requisition_id,
                    ActivityLog.dismissed_at.is_(None),
                )
                # When requisition_id is None the filter above would match ALL
                # null-req notifications from any vendor — scope to this vendor.
                if vr.requisition_id is None:
                    notif_q = notif_q.filter(ActivityLog.contact_name == vr.vendor_name)
                existing_notif = notif_q.first()
                if existing_notif:
                    existing_notif.subject = (
                        f"New vendor offer needs review: {vr.vendor_name or 'Unknown'} \u2014 {draft.get('mpn', '?')}"
                    )
                    existing_notif.created_at = datetime.now(UTC)
                else:
                    db.add(
                        ActivityLog(
                            user_id=owner_id,
                            activity_type="offer_pending_review",
                            channel="system",
                            requisition_id=vr.requisition_id,
                            contact_name=vr.vendor_name,
                            subject=f"New vendor offer needs review: {vr.vendor_name or 'Unknown'} \u2014 {draft.get('mpn', '?')}",
                        )
                    )
        except Exception as e:
            logger.error("Failed to create offer for {}: {}", draft.get("mpn", "?"), e, exc_info=True)

    # Publish SSE event so sightings page refreshes for affected requirements
    if owner_id:
        try:
            from .services.sse_broker import broker

            affected_req_ids = set(mpn_to_req_id.values())
            loop = asyncio.get_event_loop()
            for rid in affected_req_ids:
                task = loop.create_task(
                    broker.publish(
                        f"user:{owner_id}",
                        "sighting-updated",
                        json.dumps({"requirement_id": rid}),
                    )
                )
                hold_bg_task(task)
        except Exception:
            logger.debug("SSE publish from auto-create offers skipped (no event loop)")


async def process_batch_results(db: Session) -> int:
    """Poll for completed Anthropic batches and apply results.

    Called by scheduler every tick. Returns count of results applied.
    """
    from app.services.response_parser import _normalize_parsed_parts
    from app.utils.claude_client import claude_batch_results

    pending_batches = db.query(PendingBatch).filter(PendingBatch.status == PendingBatchStatus.PROCESSING).all()

    if not pending_batches:
        return 0

    total_applied = 0

    for pb in pending_batches:
        try:
            results = await claude_batch_results(pb.batch_id, cost_bucket="email_mining")
        except Exception as e:
            logger.warning(f"Batch results check failed for {pb.batch_id}: {e}")
            # Mark as failed if submitted >24h ago
            if pb.submitted_at:
                sa = pb.submitted_at if pb.submitted_at.tzinfo else pb.submitted_at.replace(tzinfo=UTC)
                if datetime.now(UTC) - sa > timedelta(hours=24):
                    pb.status = PendingBatchStatus.FAILED
                    pb.error_message = f"Timed out after 24h: {e}"
                    db.commit()
            continue

        if results is None:
            # Still processing -- check for timeout
            if pb.submitted_at:
                sa = pb.submitted_at if pb.submitted_at.tzinfo else pb.submitted_at.replace(tzinfo=UTC)
                if datetime.now(UTC) - sa > timedelta(hours=24):
                    pb.status = PendingBatchStatus.FAILED
                    pb.error_message = "Batch did not complete within 24h"
                    db.commit()
            continue

        # Batch complete -- apply results
        request_map: dict = pb.request_map or {}
        applied = 0

        for custom_id, parsed_data in results.items():
            vr_id = request_map.get(custom_id)
            if not vr_id:
                continue

            vr = db.get(VendorResponse, vr_id)
            if not vr or vr.status == VendorResponseStatus.PARSED:
                continue  # Already parsed (e.g., by sequential fallback)

            if parsed_data:
                # Claude may return tool input as a JSON string
                if isinstance(parsed_data, str):
                    try:
                        parsed_data = json.loads(parsed_data)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"Batch item {custom_id}: unparseable string result")
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
                _apply_parsed_result(vr, legacy_parsed, db)
                # Batch path of the OOO/bounce contact-status repair: the
                # classification only exists NOW, so the poll-time block never
                # sees it — repair here or it never happens.
                _repair_contact_status_for_ooo_bounce(vr, db)
                applied += 1

        pb.status = PendingBatchStatus.COMPLETED
        pb.completed_at = datetime.now(UTC)
        pb.result_count = applied
        total_applied += applied

        try:
            db.commit()
            logger.info(f"Batch {pb.batch_id} applied: {applied}/{len(request_map)} results")
        except Exception as e:
            logger.error(f"Batch results commit failed for {pb.batch_id}: {e}")
            db.rollback()

    return total_applied
