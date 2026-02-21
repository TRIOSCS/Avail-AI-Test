"""Email Intelligence Mining Service v2.

Scans the user's Microsoft 365 inbox/sent via Graph API for:
1. Vendor offer emails — extract vendor name, email, parts, prices
2. Stock list attachments — parse Excel/CSV for part numbers and vendor info
3. Email signatures — extract phone, title, address for vendor cards
4. Outbound RFQ tracking — detect AVAIL RFQs in SentItems (Upgrade 3)

Hardening:
  H1: Immutable IDs (via GraphClient)
  H2: ProcessedMessage dedup — skip messages already processed
  H6: Retry with exponential backoff (via GraphClient)
  H8: Delta Query — incremental sync instead of full inbox scan

Enriches VendorCards with verified contact info from real correspondence.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from app.utils.graph_client import GraphSyncStateExpired

log = logging.getLogger(__name__)

# Common stock list file extensions
STOCK_LIST_EXTENSIONS = {".xlsx", ".xls", ".csv", ".tsv"}

# Patterns that suggest an email contains component offers
OFFER_PATTERNS = [
    r"(?i)quot(?:e|ation)",
    r"(?i)in\s*stock",
    r"(?i)avail(?:able|ability)",
    r"(?i)lead\s*time",
    r"(?i)unit\s*price",
    r"(?i)rfq\s*(?:response|reply)",
    r"(?i)(?:we|i)\s*(?:have|can\s*offer|can\s*supply)",
    r"(?i)stock\s*list",
    r"(?i)line\s*card",
    r"(?i)price\s*list",
    r"(?i)inventory\s*list",
    r"(?i)excess\s*list",
]

# Part number pattern — uppercase alphanumeric with dashes/slashes, 4+ chars
MPN_PATTERN = re.compile(r"\b([A-Z0-9][A-Z0-9\-/\.#]{3,30}[A-Z0-9])\b")

# Email signature extraction patterns
PHONE_PATTERN = re.compile(
    r"(?:(?:phone|tel|cell|mobile|fax|ph|direct|office)[\s:.\-]*)?"
    r"(\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})",
    re.IGNORECASE,
)

WEBSITE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?([a-zA-Z0-9\-]+\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})?)",
    re.IGNORECASE,
)

# Subject token pattern: [AVAIL-{req_id}]
AVAIL_TOKEN_RE = re.compile(r"\[AVAIL-(\d+)\]")

# Fields requested from Graph API for messages
MSG_SELECT = "id,subject,from,receivedDateTime,body,hasAttachments,conversationId"
SENT_SELECT = "id,subject,toRecipients,sentDateTime,conversationId,hasAttachments"


class EmailMiner:
    """Mines Microsoft 365 inbox for vendor intelligence.

    Hardened with ProcessedMessage dedup (H2) and Delta Query (H8).
    """

    def __init__(self, access_token: str, db=None, user_id: int | None = None):
        from app.utils.graph_client import GraphClient

        self.gc = GraphClient(access_token)
        self.db = db
        self.user_id = user_id

    # ── H2: Dedup helpers ────────────────────────────────────────────

    def _already_processed(
        self, message_ids: list[str], processing_type: str
    ) -> set[str]:
        """Check which message IDs have already been processed (H2)."""
        if not self.db or not message_ids:
            return set()

        from app.models import ProcessedMessage

        rows = (
            self.db.query(ProcessedMessage.message_id)
            .filter(
                ProcessedMessage.message_id.in_(message_ids),
                ProcessedMessage.processing_type == processing_type,
            )
            .all()
        )
        return {r[0] for r in rows}

    def _mark_processed(self, message_id: str, processing_type: str):
        """Record a message as processed to prevent reprocessing (H2)."""
        if not self.db:
            return
        from app.models import ProcessedMessage

        try:
            self.db.add(
                ProcessedMessage(
                    message_id=message_id,
                    processing_type=processing_type,
                    processed_at=datetime.now(timezone.utc),
                )
            )
            self.db.flush()
        except Exception:
            # Duplicate key — already processed (race condition safety)
            self.db.rollback()

    # ── H8: Delta Query helpers ──────────────────────────────────────

    def _get_delta_token(self, folder: str) -> str | None:
        """Retrieve stored delta token for incremental sync (H8)."""
        if not self.db or not self.user_id:
            return None
        from app.models import SyncState

        sync = (
            self.db.query(SyncState)
            .filter(
                SyncState.user_id == self.user_id,
                SyncState.folder == folder,
            )
            .first()
        )
        return sync.delta_token if sync else None

    def _save_delta_token(self, folder: str, token: str):
        """Persist new delta token after sync (H8)."""
        if not self.db or not self.user_id:
            return
        from app.models import SyncState

        sync = (
            self.db.query(SyncState)
            .filter(
                SyncState.user_id == self.user_id,
                SyncState.folder == folder,
            )
            .first()
        )
        if sync:
            sync.delta_token = token
            sync.last_sync_at = datetime.now(timezone.utc)
        else:
            self.db.add(
                SyncState(
                    user_id=self.user_id,
                    folder=folder,
                    delta_token=token,
                    last_sync_at=datetime.now(timezone.utc),
                )
            )
        self.db.flush()

    def _clear_delta_token(self, folder: str):
        """Discard a stale delta token so the next scan does a full re-sync."""
        if not self.db or not self.user_id:
            return
        from app.models import SyncState

        sync = (
            self.db.query(SyncState)
            .filter(
                SyncState.user_id == self.user_id,
                SyncState.folder == folder,
            )
            .first()
        )
        if sync:
            sync.delta_token = None
            self.db.flush()
            log.info(f"Cleared stale delta token for {folder} (user {self.user_id})")

    # ══════════════════════════════════════════════════════════════════
    #  Inbound: Vendor Contact Mining
    # ══════════════════════════════════════════════════════════════════

    async def scan_inbox(
        self, lookback_days: int = 180, max_messages: int = 500, use_delta: bool = True
    ) -> dict:
        """Full inbox scan — returns enrichment data for vendor cards.

        H2: Skips messages already in processed_messages (processing_type='mining')
        H8: Uses Delta Query when available (incremental scan)

        Returns:
            {
                "vendors_found": int,
                "contacts_enriched": [...],
                "offers_parsed": [...],
                "stock_lists_found": int,
                "messages_scanned": int,
                "used_delta": bool,
            }
        """
        messages = []
        used_delta = False

        # ── H8: Try Delta Query for incremental scan ──
        if use_delta and self.user_id:
            delta_token = self._get_delta_token("inbox_mining")
            try:
                items, new_token = await self.gc.delta_query(
                    "/me/mailFolders/inbox/messages/delta",
                    delta_token=delta_token,
                    params={"$select": MSG_SELECT, "$top": "50"},
                    max_items=max_messages,
                )
                messages = items
                used_delta = True
                if new_token:
                    self._save_delta_token("inbox_mining", new_token)
                log.info(f"Delta scan (mining): {len(messages)} changes")
            except GraphSyncStateExpired:
                log.warning("Delta token expired for inbox mining — clearing and falling back")
                self._clear_delta_token("inbox_mining")
                messages = []
                used_delta = False
            except Exception as e:
                log.warning(
                    f"Delta query failed for mining, falling back to search: {e}"
                )
                messages = []
                used_delta = False

        # ── Fallback: Keyword search scan ──
        if not messages and not used_delta:
            since = (
                datetime.now(timezone.utc) - timedelta(days=lookback_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            queries = [
                f"received>={since} AND (subject:RFQ OR subject:quote OR subject:stock OR subject:inventory OR subject:availability)",
                f"received>={since} AND (body:in stock OR body:lead time OR body:unit price)",
            ]
            seen_ids = set()
            for query in queries:
                results = await self._search_messages(
                    query, max_messages // len(queries)
                )
                for msg in results:
                    msg_id = msg.get("id", "")
                    if msg_id and msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        messages.append(msg)

        # ── H2: Filter already-processed messages ──
        all_ids = [m.get("id", "") for m in messages if m.get("id")]
        already_done = self._already_processed(all_ids, "mining")
        messages = [m for m in messages if m.get("id") and m["id"] not in already_done]

        # Process messages
        contacts = {}
        offers = []

        for msg in messages:
            sender = msg.get("from", {}).get("emailAddress", {})
            sender_email = (sender.get("address") or "").lower()
            sender_name = sender.get("name", "")

            if not sender_email:
                continue

            body = msg.get("body", {}).get("content", "")
            subject = msg.get("subject", "")

            # Extract vendor intelligence from this email
            vendor_info = self._extract_vendor_info(
                sender_name, sender_email, body, subject
            )

            # Track unique vendor contacts
            vendor_key = self._normalize_vendor_from_email(sender_email)
            if vendor_key not in contacts:
                contacts[vendor_key] = {
                    "vendor_name": vendor_info["vendor_name"],
                    "emails": set(),
                    "phones": set(),
                    "websites": set(),
                    "parts_mentioned": set(),
                    "message_count": 0,
                    "last_contact": None,
                }

            c = contacts[vendor_key]
            c["emails"].add(sender_email)
            c["phones"].update(vendor_info.get("phones", []))
            c["websites"].update(vendor_info.get("websites", []))
            c["message_count"] += 1

            received = msg.get("receivedDateTime")
            if received:
                try:
                    dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
                    if not c["last_contact"] or dt > c["last_contact"]:
                        c["last_contact"] = dt
                except Exception:
                    pass

            # Check if this is an offer email
            if self._is_offer_email(subject, body):
                parts = self._extract_part_numbers(subject + " " + body)
                c["parts_mentioned"].update(parts)
                if parts:
                    offers.append(
                        {
                            "from_email": sender_email,
                            "vendor_name": vendor_info["vendor_name"],
                            "subject": subject,
                            "parts": list(parts)[:50],
                            "received": received,
                        }
                    )

            # H2: Mark as processed
            self._mark_processed(msg["id"], "mining")

        # Convert to serializable format
        enriched = []
        for key, c in contacts.items():
            enriched.append(
                {
                    "vendor_key": key,
                    "vendor_name": c["vendor_name"],
                    "emails": sorted(c["emails"]),
                    "phones": sorted(c["phones"]),
                    "websites": sorted(c["websites"]),
                    "parts_mentioned": sorted(c["parts_mentioned"])[:100],
                    "message_count": c["message_count"],
                    "last_contact": c["last_contact"].isoformat()
                    if c["last_contact"]
                    else None,
                }
            )

        return {
            "vendors_found": len(contacts),
            "contacts_enriched": enriched,
            "offers_parsed": offers[:200],
            "stock_lists_found": 0,
            "messages_scanned": len(messages),
            "used_delta": used_delta,
        }

    # ══════════════════════════════════════════════════════════════════
    #  Stock List Attachment Discovery
    # ══════════════════════════════════════════════════════════════════

    async def scan_for_stock_lists(self, lookback_days: int = 90) -> list[dict]:
        """Find emails with stock list attachments.

        H2: Skips attachment messages already in processed_messages (type='attachment').
        """
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        query = f"received>={since} AND hasAttachments:true AND (subject:stock list OR subject:inventory OR subject:excess OR subject:line card)"

        results = []
        messages = await self._search_messages(query, 100)

        # H2: Filter already-processed
        msg_ids = [m.get("id", "") for m in messages if m.get("id")]
        already_done = self._already_processed(msg_ids, "attachment")

        for msg in messages:
            msg_id = msg.get("id", "")
            if not msg_id or msg_id in already_done:
                continue

            sender = msg.get("from", {}).get("emailAddress", {})
            attachments = msg.get("attachments", [])

            stock_files = []
            for att in attachments:
                name = (att.get("name") or "").lower()
                if any(name.endswith(ext) for ext in STOCK_LIST_EXTENSIONS):
                    stock_files.append(
                        {
                            "filename": att.get("name"),
                            "size": att.get("size"),
                            "attachment_id": att.get("id"),
                            "message_id": msg_id,
                        }
                    )

            if stock_files:
                results.append(
                    {
                        "from_email": (sender.get("address") or "").lower(),
                        "vendor_name": sender.get("name", ""),
                        "subject": msg.get("subject", ""),
                        "received": msg.get("receivedDateTime"),
                        "stock_files": stock_files,
                    }
                )
                # H2: Mark message as processed for attachment scanning
                self._mark_processed(msg_id, "attachment")

        return results

    # ══════════════════════════════════════════════════════════════════
    #  Upgrade 3: Outbound Mining — Scan SentItems for AVAIL RFQs
    # ══════════════════════════════════════════════════════════════════

    async def scan_sent_items(
        self, lookback_days: int = 90, max_messages: int = 500
    ) -> dict:
        """Scan Sent Items for outbound AVAIL RFQs.

        Detects emails with [AVAIL-{req_id}] subject tokens.
        Tracks per-vendor outreach counts for engagement scoring.

        H2: Dedup via processed_messages (type='sent_scan')
        H8: Delta Query on SentItems folder

        Returns:
            {
                "messages_scanned": int,
                "rfqs_detected": int,
                "vendors_contacted": {vendor_domain: count},
                "used_delta": bool,
            }
        """
        messages = []
        used_delta = False

        # ── H8: Try Delta Query on SentItems ──
        if self.user_id:
            delta_token = self._get_delta_token("sent_items")
            try:
                items, new_token = await self.gc.delta_query(
                    "/me/mailFolders/sentItems/messages/delta",
                    delta_token=delta_token,
                    params={"$select": SENT_SELECT, "$top": "50"},
                    max_items=max_messages,
                )
                messages = items
                used_delta = True
                if new_token:
                    self._save_delta_token("sent_items", new_token)
                log.info(f"Delta scan (sent): {len(messages)} changes")
            except GraphSyncStateExpired:
                log.warning("Delta token expired for sent items — clearing and falling back")
                self._clear_delta_token("sent_items")
                messages = []
                used_delta = False
            except Exception as e:
                log.warning(f"Delta query failed for SentItems, falling back: {e}")
                messages = []
                used_delta = False

        # ── Fallback: Search SentItems ──
        if not messages and not used_delta:
            since = (
                datetime.now(timezone.utc) - timedelta(days=lookback_days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                results = await self.gc.get_all_pages(
                    "/me/mailFolders/sentItems/messages",
                    params={
                        "$filter": f"sentDateTime ge {since}",
                        "$select": SENT_SELECT,
                        "$top": "50",
                        "$orderby": "sentDateTime desc",
                    },
                    max_items=max_messages,
                )
                messages = results
            except Exception as e:
                log.warning(f"SentItems search failed: {e}")
                return {
                    "messages_scanned": 0,
                    "rfqs_detected": 0,
                    "vendors_contacted": {},
                    "used_delta": False,
                }

        # ── H2: Filter already-processed ──
        msg_ids = [m.get("id", "") for m in messages if m.get("id")]
        already_done = self._already_processed(msg_ids, "sent_scan")
        messages = [m for m in messages if m.get("id") and m["id"] not in already_done]

        # ── Detect AVAIL RFQs and track vendor outreach ──
        rfqs_detected = 0
        vendors_contacted: dict[str, int] = {}  # domain → count

        for msg in messages:
            msg_id = msg.get("id", "")
            subject = msg.get("subject", "")

            # Only care about AVAIL-tagged RFQs
            token_match = AVAIL_TOKEN_RE.search(subject)
            if not token_match:
                # Still mark as processed to avoid re-scanning
                self._mark_processed(msg_id, "sent_scan")
                continue

            rfqs_detected += 1

            # Extract recipient domains
            recipients = msg.get("toRecipients", [])
            for recip in recipients:
                addr = (recip.get("emailAddress", {}).get("address") or "").lower()
                if addr and "@" in addr:
                    domain = addr.split("@", 1)[1]
                    vendors_contacted[domain] = vendors_contacted.get(domain, 0) + 1

            self._mark_processed(msg_id, "sent_scan")

        # Flush dedup records
        if self.db:
            try:
                self.db.flush()
            except Exception:
                self.db.rollback()

        return {
            "messages_scanned": len(messages),
            "rfqs_detected": rfqs_detected,
            "vendors_contacted": vendors_contacted,
            "used_delta": used_delta,
        }

    # ── Deep Mining (scans ALL emails, not just offer-tagged) ──────────

    async def deep_scan_inbox(
        self, lookback_days: int = 365, max_messages: int = 2000
    ) -> dict:
        """Deep scan ALL emails for contacts, signatures, and vendor intelligence.

        Unlike scan_inbox(), this does NOT filter by OFFER_PATTERNS — it processes
        every email to extract signatures, brand mentions, and contact data.

        Uses separate processing_type='deep_mining' for dedup (H2).

        Returns: {
            messages_scanned: int,
            signatures_extracted: int,
            brands_detected: int,
            contacts_found: int,
            per_domain: {domain: {emails: [...], phones: [...], brands: [...], commodities: [...]}}
        }
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch all messages (no keyword filter)
        params = {
            "$filter": f"receivedDateTime ge {cutoff_str}",
            "$orderby": "receivedDateTime desc",
            "$top": "50",
            "$select": MSG_SELECT,
        }

        try:
            messages = await self.gc.get_all_pages(
                "/me/messages", params=params, max_items=max_messages
            )
        except Exception as e:
            log.warning("Deep scan inbox error: %s", e)
            return {
                "messages_scanned": 0,
                "signatures_extracted": 0,
                "brands_detected": 0,
                "contacts_found": 0,
                "per_domain": {},
            }

        # Filter already-processed
        msg_ids = [m.get("id") for m in messages if m.get("id")]
        if self.db and msg_ids:
            processed = self._already_processed(msg_ids, "deep_mining")
        else:
            processed = set()

        # Skip common internal/system domains
        skip_domains = {
            "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
            "microsoft.com", "google.com", "noreply", "no-reply",
            "notifications", "mailer-daemon",
        }

        per_domain = {}
        signatures_extracted = 0
        brands_detected = 0
        contacts_found = 0

        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in processed:
                continue

            sender = msg.get("sender", {}).get("emailAddress", {})
            sender_email = (sender.get("address") or "").strip().lower()
            sender_name = (sender.get("name") or "").strip()

            if not sender_email or "@" not in sender_email:
                continue

            domain = sender_email.split("@")[-1].lower()
            if any(skip in domain for skip in skip_domains):
                continue

            body = (msg.get("body", {}).get("content") or "")[:5000]
            subject = msg.get("subject") or ""

            # Extract vendor info (phones, websites)
            vendor_info = self._extract_vendor_info(sender_name, sender_email, body, subject)

            # Accumulate per domain
            if domain not in per_domain:
                per_domain[domain] = {
                    "vendor_name": vendor_info["vendor_name"],
                    "emails": set(),
                    "phones": set(),
                    "websites": set(),
                    "brands": set(),
                    "commodities": set(),
                    "sender_names": set(),
                    "subjects": [],
                }

            entry = per_domain[domain]
            entry["emails"].add(sender_email)
            entry["phones"].update(vendor_info.get("phones", []))
            entry["websites"].update(vendor_info.get("websites", []))
            if sender_name:
                entry["sender_names"].add(sender_name)
            if subject:
                entry["subjects"].append(subject[:200])

            # Detect brands and commodities from subject + body snippet
            try:
                from app.services.specialty_detector import (
                    detect_brands_from_text,
                    detect_commodities_from_text,
                )
                text_for_analysis = f"{subject} {body[:2000]}"
                brands = detect_brands_from_text(text_for_analysis)
                commodities = detect_commodities_from_text(text_for_analysis)
                entry["brands"].update(brands)
                entry["commodities"].update(commodities)
                if brands:
                    brands_detected += 1
            except Exception:
                pass

            contacts_found += 1

            # Mark as processed
            if self.db:
                self._mark_processed(msg_id, "deep_mining")

        signatures_extracted = len(per_domain)

        # Convert sets to lists for JSON serialization
        for domain, entry in per_domain.items():
            entry["emails"] = list(entry["emails"])
            entry["phones"] = list(entry["phones"])
            entry["websites"] = list(entry["websites"])
            entry["brands"] = list(entry["brands"])
            entry["commodities"] = list(entry["commodities"])
            entry["sender_names"] = list(entry["sender_names"])

        if self.db:
            try:
                self.db.commit()
            except Exception as e:
                log.warning("Deep scan commit error: %s", e)
                self.db.rollback()

        return {
            "messages_scanned": len(messages) - len(processed),
            "signatures_extracted": signatures_extracted,
            "brands_detected": brands_detected,
            "contacts_found": contacts_found,
            "per_domain": per_domain,
        }

    # ── Private helpers ──────────────────────────────────────────────────

    async def _search_messages(self, query: str, limit: int = 250) -> list[dict]:
        """Search messages via Graph API with pagination using GraphClient."""
        params = {
            "$search": f'"{query}"',
            "$top": str(min(50, limit)),
            "$select": MSG_SELECT,
        }
        try:
            return await self.gc.get_all_pages(
                "/me/messages", params=params, max_items=limit
            )
        except Exception as e:
            log.warning(f"Email search error: {e}")
            return []

    def _extract_vendor_info(
        self, sender_name: str, sender_email: str, body: str, subject: str
    ) -> dict:
        """Extract vendor name, phones, websites from email content."""
        vendor_name = sender_name.strip() if sender_name else ""
        if not vendor_name or vendor_name == sender_email:
            domain = sender_email.split("@")[-1] if "@" in sender_email else ""
            vendor_name = domain.split(".")[0].title() if domain else "Unknown"

        # Extract from signature block (usually last ~30 lines)
        lines = body.split("\n") if body else []
        signature_block = "\n".join(lines[-30:]) if len(lines) > 30 else body

        phones = set()
        for match in PHONE_PATTERN.finditer(signature_block):
            phone = re.sub(r"[^\d+]", "", match.group(1))
            if len(phone) >= 10:
                phones.add(phone)

        websites = set()
        for match in WEBSITE_PATTERN.finditer(signature_block):
            domain = match.group(1).lower()
            skip = {
                "gmail.com",
                "yahoo.com",
                "outlook.com",
                "hotmail.com",
                "microsoft.com",
                "google.com",
                "facebook.com",
                "linkedin.com",
                "twitter.com",
                "instagram.com",
                "youtube.com",
            }
            if domain not in skip:
                websites.add(domain)

        return {
            "vendor_name": vendor_name,
            "email": sender_email,
            "phones": list(phones),
            "websites": list(websites),
        }

    def _is_offer_email(self, subject: str, body: str) -> bool:
        """Check if email looks like a component offer/quote."""
        text = f"{subject} {body[:2000]}"
        matches = sum(1 for p in OFFER_PATTERNS if re.search(p, text))
        return matches >= 2

    def _extract_part_numbers(self, text: str) -> set:
        """Extract likely electronic component part numbers from text."""
        candidates = MPN_PATTERN.findall(text.upper())

        false_positives = {
            "HTTP",
            "HTTPS",
            "HTML",
            "HREF",
            "FONT",
            "SIZE",
            "COLOR",
            "TABLE",
            "STYLE",
            "CLASS",
            "WIDTH",
            "HEIGHT",
            "ALIGN",
            "BORDER",
            "CELLPADDING",
            "CELLSPACING",
            "COLSPAN",
            "ROWSPAN",
            "VALIGN",
            "BGCOLOR",
            "IMAGE",
            "ARIAL",
            "VERDANA",
            "HELVETICA",
            "SERIF",
            "SANS",
            "SPAN",
            "MAILTO",
            "SUBJECT",
            "FROM",
            "BEST",
            "REGARDS",
            "THANK",
            "THANKS",
            "PLEASE",
            "HELLO",
            "DEAR",
            "SINCERELY",
            "KIND",
            "REGARDS",
            "OFFER",
            "QUOTE",
            "PRICE",
            "STOCK",
            "AVAILABLE",
            "QUANTITY",
        }

        valid = set()
        for c in candidates:
            if c in false_positives:
                continue
            if len(c) < 4:
                continue
            if not any(ch.isdigit() for ch in c):
                continue
            if not any(ch.isalpha() for ch in c):
                continue
            valid.add(c)

        return valid

    def _normalize_vendor_from_email(self, email: str) -> str:
        """Derive a vendor key from email domain."""
        if "@" not in email:
            return email.lower()
        domain = email.split("@")[-1].lower()
        parts = domain.replace("www.", "").split(".")
        if len(parts) >= 2:
            return parts[0]
        return domain
