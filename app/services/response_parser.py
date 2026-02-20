"""Response parser — AI-powered vendor reply classification.

Email Mining v2 Upgrade 1: Structured outputs, confidence thresholds,
multi-part quote handling.

Confidence thresholds:
  ≥ 0.8  → auto-apply (create draft Offer)
  0.5-0.8 → flag for review (needs_review=True)
  < 0.5  → store raw, don't apply

Each part in a multi-part reply gets its own classification:
  quoted, no_stock, follow_up, counter_offer
"""

import json
import logging

from app.utils.claude_client import claude_structured
from app.utils.normalization import (
    detect_currency,
    fuzzy_mpn_match,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_moq,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)

log = logging.getLogger("avail.response_parser")

# ── Confidence thresholds ─────────────────────────────────────────────

CONFIDENCE_AUTO = 0.8  # Auto-apply parsed data
CONFIDENCE_REVIEW = 0.5  # Flag for human review
# Below 0.5: store raw only

# ── Structured Output schemas ─────────────────────────────────────────

RESPONSE_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral", "mixed"],
            "description": "Overall tone of the vendor's reply",
        },
        "overall_classification": {
            "type": "string",
            "enum": [
                "quote_provided",
                "no_stock",
                "counter_offer",
                "clarification_needed",
                "ooo_bounce",
                "follow_up",
            ],
            "description": "Primary response type",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence in the extraction accuracy",
        },
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string", "description": "Part number mentioned"},
                    "status": {
                        "type": "string",
                        "enum": ["quoted", "no_stock", "follow_up"],
                    },
                    "qty_available": {"type": "integer"},
                    "unit_price": {"type": "number"},
                    "currency": {"type": "string"},
                    "lead_time": {"type": "string"},
                    "condition": {"type": "string"},
                    "date_code": {"type": "string"},
                    "moq": {"type": "integer"},
                    "packaging": {"type": "string"},
                    "valid_days": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["mpn", "status"],
            },
            "description": "Per-part extraction from the vendor reply",
        },
        "vendor_notes": {
            "type": "string",
            "description": "Any important notes from the vendor not captured in parts",
        },
    },
    "required": ["overall_sentiment", "overall_classification", "confidence", "parts"],
}

SYSTEM_PROMPT = """You are a precise data extractor for electronic component vendor email replies.

Context: An electronic component broker sent an RFQ (request for quote) to a vendor.
The vendor has replied. Extract structured data from their reply.

Rules:
- Extract ALL parts mentioned, even if some are not available
- Set status per part: "quoted" (has price), "no_stock" (explicitly unavailable), "follow_up" (unclear)
- Only include fields you're confident about. Omit uncertain fields.
- For multi-part replies, extract each part separately
- Confidence should reflect how clearly the vendor stated pricing/availability
- Currency defaults to USD unless explicitly stated otherwise
- "out of office" or bounce messages → overall_classification: "ooo_bounce", empty parts list
- If the email is ambiguous or you can't extract reliably, set confidence < 0.5"""


async def parse_vendor_response(
    email_body: str,
    email_subject: str,
    vendor_name: str,
    rfq_context: dict | None = None,
) -> dict | None:
    """Parse a vendor email reply into structured data.

    Args:
        email_body: The email body text (HTML or plain)
        email_subject: Email subject line
        vendor_name: Sender/vendor name
        rfq_context: What we asked for — {mpn, qty, target_price} or list thereof

    Returns:
        Parsed result dict with confidence score, or None on failure
    """
    # Truncate body to avoid token waste
    body_truncated = _clean_email_body(email_body)[:4000]

    context_str = ""
    if rfq_context:
        if isinstance(rfq_context, list):
            parts_str = ", ".join(
                f"{p.get('mpn', '?')} x{p.get('qty', '?')}" for p in rfq_context[:10]
            )
            context_str = f"\nParts we asked about: {parts_str}"
        elif isinstance(rfq_context, dict):
            context_str = f"\nParts we asked about: {rfq_context.get('mpn', '?')} x{rfq_context.get('qty', '?')}"

    prompt = (
        f"Vendor: {vendor_name}\n"
        f"Subject: {email_subject}\n"
        f"{context_str}\n\n"
        f"Vendor reply:\n{body_truncated}"
    )

    result = await claude_structured(
        prompt=prompt,
        schema=RESPONSE_PARSE_SCHEMA,
        system=SYSTEM_PROMPT,
        model_tier="fast",
        max_tokens=1024,
    )

    if not result:
        return None

    # Extended thinking retry: if confidence is in the ambiguous review band,
    # retry with Sonnet + thinking to attempt higher-confidence extraction
    confidence = result.get("confidence", 0)
    if CONFIDENCE_REVIEW <= confidence < CONFIDENCE_AUTO:
        log.info(
            f"Ambiguous confidence {confidence:.2f} — retrying with extended thinking"
        )
        retry = await claude_structured(
            prompt=prompt,
            schema=RESPONSE_PARSE_SCHEMA,
            system=SYSTEM_PROMPT,
            model_tier="smart",
            max_tokens=1024,
            thinking_budget=2048,
            timeout=60,
        )
        if retry and retry.get("confidence", 0) > confidence:
            log.info(
                f"Extended thinking upgraded confidence: {confidence:.2f} → {retry['confidence']:.2f}"
            )
            result = retry

    # Post-process: normalize extracted values
    _normalize_parsed_parts(result)

    # Cross-validate against RFQ context
    if rfq_context:
        _cross_validate(result, rfq_context)

    return result


def _normalize_parsed_parts(result: dict) -> None:
    """Apply deterministic normalization to AI-extracted values."""
    parts = result.get("parts", [])
    # Claude batch API sometimes returns parts as a JSON string — parse it
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
            result["parts"] = parts
        except (json.JSONDecodeError, TypeError):
            result["parts"] = []
            return
    for part in parts:
        if not isinstance(part, dict):
            continue
        if "unit_price" in part and part["unit_price"] is not None:
            part["unit_price"] = normalize_price(part["unit_price"])
        if "qty_available" in part and part["qty_available"] is not None:
            part["qty_available"] = normalize_quantity(part["qty_available"])
        if "lead_time" in part and part["lead_time"] is not None:
            part["lead_time_days"] = normalize_lead_time(part["lead_time"])
        if "condition" in part:
            part["condition_normalized"] = normalize_condition(part.get("condition"))
        if "date_code" in part:
            part["date_code"] = normalize_date_code(part.get("date_code"))
        if "moq" in part and part["moq"] is not None:
            part["moq"] = normalize_moq(part["moq"])
        if "packaging" in part:
            part["packaging_normalized"] = normalize_packaging(part.get("packaging"))
        if "currency" in part:
            part["currency"] = detect_currency(part.get("currency"))
        else:
            part["currency"] = "USD"


def _cross_validate(result: dict, rfq_context: dict | list) -> None:
    """Cross-validate parsed parts against what we asked for."""
    if isinstance(rfq_context, dict):
        rfq_parts = [rfq_context]
    elif isinstance(rfq_context, list):
        rfq_parts = rfq_context
    else:
        return

    rfq_mpns = [p.get("mpn", "") for p in rfq_parts if p.get("mpn")]

    for part in result.get("parts", []):
        mpn = part.get("mpn", "")
        part["mpn_matches_rfq"] = any(
            fuzzy_mpn_match(mpn, rfq_mpn) for rfq_mpn in rfq_mpns
        )


def _clean_email_body(body: str) -> str:
    """Strip HTML tags, excessive whitespace, and email signatures."""
    import re

    if not body:
        return ""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", body)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Remove common email disclaimers (often very long)
    disclaimer_patterns = [
        r"(?i)this email and any attachments.*?(?=\n\n|\Z)",
        r"(?i)confidentiality notice.*?(?=\n\n|\Z)",
        r"(?i)DISCLAIMER.*?(?=\n\n|\Z)",
    ]
    for pat in disclaimer_patterns:
        text = re.sub(pat, "", text)
    return text.strip()


def should_auto_apply(result: dict) -> bool:
    """Check if parsed result is confident enough to auto-create draft offers."""
    conf = result.get("confidence", 0)
    return conf >= CONFIDENCE_AUTO


def should_flag_review(result: dict) -> bool:
    """Check if parsed result needs human review."""
    conf = result.get("confidence", 0)
    return CONFIDENCE_REVIEW <= conf < CONFIDENCE_AUTO


def extract_draft_offers(result: dict, vendor_name: str) -> list[dict]:
    """Convert parsed result into draft Offer dicts.

    Only includes parts with status="quoted" and a price.
    """
    offers = []
    for part in result.get("parts", []):
        if part.get("status") != "quoted":
            continue
        if not part.get("unit_price"):
            continue

        offers.append(
            {
                "vendor_name": vendor_name,
                "mpn": part.get("mpn", ""),
                "manufacturer": part.get("manufacturer"),
                "qty_available": part.get("qty_available"),
                "unit_price": part.get("unit_price"),
                "currency": part.get("currency", "USD"),
                "lead_time": part.get("lead_time"),
                "date_code": part.get("date_code"),
                "condition": part.get("condition_normalized") or part.get("condition"),
                "packaging": part.get("packaging_normalized") or part.get("packaging"),
                "moq": part.get("moq"),
                "valid_days": part.get("valid_days"),
                "notes": part.get("notes"),
                "source": "ai_parsed",
                "status": "pending_review",
            }
        )

    return offers
