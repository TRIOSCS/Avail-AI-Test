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

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

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
        parsed_quotes = await extract_pricing_intelligence(
            subject, body, sender_email, sender_name
        )

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

    query = db.query(EmailIntelligence).filter(
        EmailIntelligence.user_id == user_id
    )
    if classification:
        query = query.filter(EmailIntelligence.classification == classification)

    records = (
        query.order_by(EmailIntelligence.created_at.desc())
        .limit(limit)
        .all()
    )

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
        normalized.append({
            "brands": r.get("brands", []) if isinstance(r.get("brands"), list) else [],
            "commodities": r.get("commodities", []) if isinstance(r.get("commodities"), list) else [],
            "sender_type": r.get("sender_type", "unknown"),
        })

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


async def summarize_thread(
    token: str, conversation_id: str, db: Session, user_id: int
) -> dict | None:
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
