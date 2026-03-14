"""Email Intelligence Service — AI-powered email classification and pricing extraction.

Replaces regex-only offer detection with a two-stage pipeline:
  1. Regex pre-filter → only call AI for ambiguous emails (0-1 regex matches)
  2. AI classification via Gradient Sonnet for classification, Opus for pricing

Confidence routing:
  - >= 0.8: auto-create draft Offers linked to material cards
  - 0.5-0.8: flag for human review
  - < 0.5: store classification only

Called by: connectors/email_mining.py (scan_inbox), scheduler.py
Depends on: gradient_service, ai_email_parser, models/email_intelligence
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..models.intelligence import EmailIntelligence

CLASSIFICATION_SYSTEM = """\
You are an email classifier for an electronic component brokerage.

Classify this email into exactly ONE category:
- offer: vendor is providing pricing, availability, or a quote for electronic parts
- stock_list: vendor is sharing their inventory/stock list
- quote_reply: vendor is replying to an RFQ (Request for Quote) we sent
- general: regular business correspondence (not about parts)
- ooo: out-of-office auto-reply
- spam: marketing, newsletter, or unsolicited promotion

Also extract:
- parts_mentioned: list of manufacturer part numbers (MPNs) found
- has_pricing: true if the email contains specific pricing information
- brands_detected: manufacturer/brand names mentioned (e.g., Texas Instruments, STMicro)
- commodities_detected: component categories (e.g., capacitors, resistors, ICs, connectors)

Return ONLY valid JSON:
{
  "classification": "offer|stock_list|quote_reply|general|ooo|spam",
  "confidence": 0.0-1.0,
  "parts_mentioned": ["MPN1", "MPN2"],
  "has_pricing": true|false,
  "brands_detected": ["Brand1"],
  "commodities_detected": ["Category1"]
}"""


async def classify_email_ai(
    subject: str,
    body: str,
    sender_email: str = "",
) -> dict | None:
    """Classify an email using Gradient AI (Sonnet tier).

    Returns classification dict or None on failure.
    """
    from app.services.gradient_service import gradient_json

    prompt = f"From: {sender_email}\nSubject: {subject}\n\nBody:\n{body[:3000]}"

    result = await gradient_json(
        prompt,
        system=CLASSIFICATION_SYSTEM,
        model_tier="default",
        max_tokens=512,
        temperature=0.1,
        timeout=20,
    )

    if not result or not isinstance(result, dict):
        return None

    # Validate classification
    valid_classes = {"offer", "stock_list", "quote_reply", "general", "ooo", "spam"}
    classification = result.get("classification", "general")
    if classification not in valid_classes:
        classification = "general"
    result["classification"] = classification

    # Clamp confidence
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
    except (ValueError, TypeError):
        result["confidence"] = 0.5

    return result


async def extract_pricing_intelligence(
    subject: str,
    body: str,
    sender_email: str = "",
    vendor_name: str = "",
) -> dict | None:
    """Extract structured pricing data from offer emails using Gradient Opus.

    Only called for emails classified as offers with has_pricing=True.
    Returns parsed quote data or None on failure.
    """
    from app.services.ai_email_parser import parse_email

    result = await parse_email(
        email_body=body,
        email_subject=subject,
        vendor_name=vendor_name,
    )
    return result


def store_email_intelligence(
    db: Session,
    *,
    message_id: str,
    user_id: int,
    sender_email: str,
    subject: str,
    received_at: datetime | None,
    conversation_id: str | None,
    classification: dict,
    parsed_quotes: dict | None = None,
) -> "EmailIntelligence":
    """Persist AI classification result to email_intelligence table.

    Args:
        db: Database session.
        message_id: Graph API message ID.
        user_id: User who owns the mailbox.
        sender_email: Sender email address.
        subject: Email subject line.
        received_at: When the email was received.
        conversation_id: Graph conversation ID for thread grouping.
        classification: AI classification result dict.
        parsed_quotes: Optional pricing extraction result.

    Returns:
        Created EmailIntelligence record.
    """
    from app.models import EmailIntelligence

    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""

    conf = classification.get("confidence", 0.0)
    has_pricing = classification.get("has_pricing", False)

    # Determine review/auto-apply status
    auto_applied = False
    needs_review = False
    cls_type = classification.get("classification", "general")
    no_review = {"spam", "ooo", "general"}

    if cls_type not in no_review:
        if conf >= 0.8:
            if parsed_quotes:
                auto_applied = True
        elif conf >= 0.5:
            needs_review = True

    record = EmailIntelligence(
        message_id=message_id,
        user_id=user_id,
        sender_email=sender_email.lower(),
        sender_domain=domain,
        classification=classification.get("classification", "general"),
        confidence=conf,
        has_pricing=has_pricing,
        parts_detected=classification.get("parts_mentioned", []),
        brands_detected=classification.get("brands_detected", []),
        commodities_detected=classification.get("commodities_detected", []),
        parsed_quotes=parsed_quotes,
        subject=subject[:500] if subject else None,
        received_at=received_at,
        conversation_id=conversation_id,
        auto_applied=auto_applied,
        needs_review=needs_review,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.flush()
    return record


async def process_email_intelligence(
    db: Session,
    *,
    message_id: str,
    user_id: int,
    sender_email: str,
    sender_name: str,
    subject: str,
    body: str,
    received_at: datetime | None,
    conversation_id: str | None,
    regex_offer_matches: int,
) -> dict | None:
    """Full AI intelligence pipeline for a single email.

    Strategy: Regex pre-filter → only call AI for ambiguous cases (0-1 matches).
    For clear offers (2+ regex matches), skip AI classification and go straight
    to pricing extraction.

    Returns: classification dict with all intelligence, or None if skipped.
    """
    classification = None

    if regex_offer_matches >= 2:
        # Clear offer — skip AI classification, use regex result
        classification = {
            "classification": "offer",
            "confidence": 0.7 + min(0.3, regex_offer_matches * 0.05),
            "parts_mentioned": [],
            "has_pricing": True,
            "brands_detected": [],
            "commodities_detected": [],
        }
    else:
        # Ambiguous — use AI classification
        classification = await classify_email_ai(subject, body, sender_email)
        if not classification:
            return None

    # Extract pricing for offer emails with pricing
    parsed_quotes = None
    if classification.get("classification") in ("offer", "quote_reply") and classification.get("has_pricing"):
        parsed_quotes = await extract_pricing_intelligence(subject, body, sender_email, sender_name)

    # Extract durable facts (non-fatal)
    try:
        await extract_durable_facts(
            db,
            body=body,
            sender_email=sender_email,
            sender_name=sender_name,
            classification=classification.get("classification", ""),
            parsed_quotes=parsed_quotes,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Fact extraction failed (non-fatal): %s", e)

    # Store result
    try:
        record = store_email_intelligence(
            db,
            message_id=message_id,
            user_id=user_id,
            sender_email=sender_email,
            subject=subject,
            received_at=received_at,
            conversation_id=conversation_id,
            classification=classification,
            parsed_quotes=parsed_quotes,
        )
        return {
            "id": record.id,
            "classification": classification.get("classification"),
            "confidence": classification.get("confidence"),
            "has_pricing": classification.get("has_pricing"),
            "auto_applied": record.auto_applied,
            "needs_review": record.needs_review,
        }
    except Exception as e:
        logger.warning("Failed to store email intelligence: %s", e)
        db.rollback()
        return None


def get_recent_intelligence(
    db: Session, user_id: int, limit: int = 50, classification: str | None = None
) -> list[dict]:
    """Fetch recent email intelligence records for dashboard display."""
    from app.models import EmailIntelligence

    query = db.query(EmailIntelligence).filter(EmailIntelligence.user_id == user_id)
    if classification:
        query = query.filter(EmailIntelligence.classification == classification)

    records = query.order_by(EmailIntelligence.created_at.desc()).limit(limit).all()

    return [
        {
            "id": r.id,
            "message_id": r.message_id,
            "sender_email": r.sender_email,
            "sender_domain": r.sender_domain,
            "classification": r.classification,
            "confidence": r.confidence,
            "has_pricing": r.has_pricing,
            "parts_detected": r.parts_detected or [],
            "brands_detected": r.brands_detected or [],
            "subject": r.subject,
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "auto_applied": r.auto_applied,
            "needs_review": r.needs_review,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


# ═══════════════════════════════════════════════════════════════════════
#  Phase 4: AI Brand/Commodity Detection + Thread Summarization
# ═══════════════════════════════════════════════════════════════════════

SPECIALTY_SYSTEM = """\
You are an expert at identifying electronic component brands and commodity \
categories from email text.

Extract:
- brands: manufacturer/brand names (e.g., Texas Instruments, STMicro, Murata, \
  Samsung, Analog Devices, NXP, Infineon, Microchip, ON Semi, Vishay)
- commodities: component categories (e.g., capacitors, resistors, ICs, \
  microcontrollers, power management, connectors, memory, sensors, LEDs)
- sender_type: "distributor", "manufacturer_rep", "broker", or "unknown"

Return ONLY valid JSON:
{
  "brands": ["Brand1", "Brand2"],
  "commodities": ["Category1"],
  "sender_type": "distributor|manufacturer_rep|broker|unknown"
}"""


async def detect_specialties_ai(texts: list[str]) -> list[dict | None]:
    """Detect brands and commodities from email texts using batch AI.

    Only called as a fallback when keyword-based specialty_detector finds nothing.
    Uses Gradient batch processing for throughput.

    Args:
        texts: List of email subject+body snippets.

    Returns:
        List of specialty dicts (one per input), None on individual failures.
    """
    from app.services.gradient_service import gradient_batch_json

    results = await gradient_batch_json(
        texts,
        system=SPECIALTY_SYSTEM,
        model_tier="default",
        max_tokens=256,
        temperature=0.1,
    )

    # Validate/normalize results
    normalized = []
    for r in results:
        if not r or not isinstance(r, dict):
            normalized.append(None)
            continue
        normalized.append(
            {
                "brands": r.get("brands", []) if isinstance(r.get("brands"), list) else [],
                "commodities": r.get("commodities", []) if isinstance(r.get("commodities"), list) else [],
                "sender_type": r.get("sender_type", "unknown"),
            }
        )

    return normalized


THREAD_SUMMARY_SYSTEM = """\
You are summarizing an email thread between an electronic component broker \
and a vendor. Extract:

- key_points: 2-5 bullet points summarizing the thread
- latest_pricing: any pricing mentioned in the most recent messages
- action_items: outstanding actions/follow-ups
- thread_status: "active", "quoted", "negotiating", "closed", "stale"

Return ONLY valid JSON:
{
  "key_points": ["point1", "point2"],
  "latest_pricing": [{"mpn": "LM317T", "price": 0.50, "qty": 1000}],
  "action_items": ["action1"],
  "thread_status": "active|quoted|negotiating|closed|stale"
}"""


async def summarize_thread(token: str, conversation_id: str, db: Session, user_id: int) -> dict | None:
    """Summarize an email thread by conversation_id using AI.

    Fetches all messages via Graph API, then summarizes with Gradient Opus.
    Caches result in EmailIntelligence.thread_summary to avoid re-processing.

    Args:
        token: Graph API access token.
        conversation_id: Graph conversation ID.
        db: Database session.
        user_id: User ID for cache lookup.

    Returns:
        Summary dict or None on failure.
    """
    from app.models import EmailIntelligence
    from app.services.gradient_service import gradient_json
    from app.utils.graph_client import GraphClient

    # Check cache first
    cached = (
        db.query(EmailIntelligence)
        .filter(
            EmailIntelligence.conversation_id == conversation_id,
            EmailIntelligence.user_id == user_id,
            EmailIntelligence.thread_summary.isnot(None),
        )
        .order_by(EmailIntelligence.created_at.desc())
        .first()
    )
    if cached and cached.thread_summary:
        return cached.thread_summary

    # Fetch thread messages
    gc = GraphClient(token)
    try:
        messages = await gc.get_all_pages(
            "/me/messages",
            params={
                "$filter": f"conversationId eq '{conversation_id}'",
                "$select": "from,subject,body,receivedDateTime",
                "$orderby": "receivedDateTime asc",
                "$top": "25",
            },
            max_items=50,
        )
    except Exception as e:
        logger.warning("Thread fetch failed for %s: %s", conversation_id, e)
        return None

    if not messages:
        return None

    # Build thread text for summarization
    thread_text = ""
    for msg in messages:
        sender = msg.get("from", {}).get("emailAddress", {})
        sender_addr = sender.get("address", "unknown")
        body_text = (msg.get("body", {}).get("content") or "")[:1000]
        received = msg.get("receivedDateTime", "")
        thread_text += f"\n--- {sender_addr} ({received}) ---\n{body_text}\n"

    # Truncate for token limits
    thread_text = thread_text[:8000]

    result = await gradient_json(
        thread_text,
        system=THREAD_SUMMARY_SYSTEM,
        model_tier="strong",
        max_tokens=1024,
        temperature=0.2,
        timeout=45,
    )

    if not result or not isinstance(result, dict):
        return None

    # Cache the summary
    intel = (
        db.query(EmailIntelligence)
        .filter(
            EmailIntelligence.conversation_id == conversation_id,
            EmailIntelligence.user_id == user_id,
        )
        .order_by(EmailIntelligence.created_at.desc())
        .first()
    )
    if intel:
        intel.thread_summary = result
        db.flush()

    return result


# ═══════════════════════════════════════════════════════════════════════
#  Email Fact Extraction — durable facts from vendor emails
# ═══════════════════════════════════════════════════════════════════════

ALLOWED_CLASSIFICATIONS = {"offer", "quote_reply", "stock_list"}

# Default expiry in days per fact type (None = never expires)
FACT_EXPIRY_DEFAULTS: dict[str, int | None] = {
    "lead_time": 180,
    "moq": 90,
    "moq_flexibility": 90,
    "eol_notice": None,
    "availability": 30,
    "pricing_note": 90,
    "vendor_policy": 365,
    "warehouse_location": 365,
    "date_code": 180,
    "condition_note": 180,
}

FACT_EXTRACTION_PROMPT = """\
You are an expert at extracting durable facts from electronic component \
vendor emails. Extract concrete, reusable facts — NOT pricing data \
(that is handled separately).

Fact types to extract:
- lead_time: delivery lead time (e.g., "12-14 weeks ARO")
- moq: minimum order quantity (e.g., "MOQ 1000 pcs")
- moq_flexibility: willingness to negotiate MOQ (e.g., "can split into 500 pc lots")
- eol_notice: end-of-life or last-time-buy notice
- availability: stock status or availability note (e.g., "in stock", "allocated")
- pricing_note: pricing conditions, NOT actual prices (e.g., "volume discount above 10K")
- vendor_policy: shipping, payment, or return policies
- warehouse_location: where parts ship from (e.g., "ships from Hong Kong warehouse")
- date_code: date code information (e.g., "DC 2024+")
- condition_note: part condition (e.g., "factory sealed", "refurbished")

For each fact found, provide:
- fact_type: one of the types above
- value: the extracted fact text (concise, 1-2 sentences max)
- mpn: the MPN it applies to (if specific to a part), or null
- confidence: 0.0-1.0

Only extract facts you are confident about. Skip vague or ambiguous statements."""

FACT_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_type": {"type": "string"},
                    "value": {"type": "string"},
                    "mpn": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["fact_type", "value", "confidence"],
            },
        },
    },
    "required": ["facts"],
}


async def extract_durable_facts(
    db: Session,
    *,
    body: str,
    sender_email: str,
    sender_name: str,
    classification: str,
    parsed_quotes: dict | None,
    user_id: int,
) -> list:
    """Extract durable facts from vendor emails using AI and store in knowledge ledger.

    Cost control: only runs on offer/quote_reply/stock_list emails with body >= 50 chars.
    Dedup guard: skips facts that already exist within the last 7 days.

    Args:
        db: Database session.
        body: Email body text.
        sender_email: Sender email address.
        sender_name: Sender display name.
        classification: Email classification string.
        parsed_quotes: Parsed pricing data (if any).
        user_id: User who owns the mailbox.

    Returns:
        List of created KnowledgeEntry records, empty on failure or skip.
    """
    try:
        # Cost control gates
        if classification not in ALLOWED_CLASSIFICATIONS:
            return []
        if len(body) < 50:
            return []

        from app.utils.claude_client import claude_structured

        prompt = f"From: {sender_name} <{sender_email}>\n\nBody:\n{body[:3000]}"

        result = await claude_structured(
            prompt,
            FACT_EXTRACTION_SCHEMA,
            system=FACT_EXTRACTION_PROMPT,
            model_tier="fast",
            max_tokens=1024,
            timeout=20,
        )

        if not result or not isinstance(result, dict) or "facts" not in result:
            return []

        # Resolve vendor_card_id from sender email domain
        vendor_card_id = None
        if "@" in sender_email:
            domain = sender_email.split("@")[-1].lower()
            from app.models.vendors import VendorCard

            vendor = db.query(VendorCard).filter(VendorCard.domain == domain).first()
            if vendor:
                vendor_card_id = vendor.id

        from app.models.knowledge import KnowledgeEntry
        from app.services.knowledge_service import create_entry

        created = []
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)

        for fact in result["facts"]:
            fact_type = fact.get("fact_type", "")
            if fact_type not in FACT_EXPIRY_DEFAULTS:
                continue

            value = fact.get("value", "")
            mpn = fact.get("mpn")
            confidence = max(0.0, min(1.0, float(fact.get("confidence", 0.7))))

            # Dedup guard: check for recent duplicate
            dedup_q = db.query(KnowledgeEntry).filter(
                KnowledgeEntry.entry_type == "fact",
                KnowledgeEntry.created_at >= seven_days_ago,
                KnowledgeEntry.content == f"[{fact_type}] {value}",
            )
            if mpn:
                dedup_q = dedup_q.filter(KnowledgeEntry.mpn == mpn)
            if vendor_card_id:
                dedup_q = dedup_q.filter(KnowledgeEntry.vendor_card_id == vendor_card_id)

            if dedup_q.count() > 0:
                continue

            # Calculate expiry
            expiry_days = FACT_EXPIRY_DEFAULTS[fact_type]
            expires_at = (now + timedelta(days=expiry_days)) if expiry_days else None

            entry = create_entry(
                db,
                user_id=user_id,
                entry_type="fact",
                content=f"[{fact_type}] {value}",
                source="email_parsed",
                confidence=confidence,
                expires_at=expires_at,
                mpn=mpn,
                vendor_card_id=vendor_card_id,
                commit=False,
            )
            created.append(entry)

        if created:
            db.commit()
        logger.info("Extracted {} durable facts from email by {}", len(created), sender_email)
        return created

    except Exception as e:
        logger.warning("Failed to extract durable facts: {}", e)
        return []
