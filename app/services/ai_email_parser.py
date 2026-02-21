"""AI Email Parser — Extract structured quote data from vendor email replies.

Purpose:
  Automatically parse vendor RFQ reply emails into structured quote data using
  the Gradient inference API. Handles multi-part quotes, multi-currency, and
  common email formats (tabular, inline, partial quotes, "no stock" responses).

Business Rules:
  - Confidence ≥ 0.8 → auto-apply (create draft offers without review)
  - Confidence 0.5–0.8 → flag for human review
  - Confidence < 0.5 → store raw, don't auto-apply
  - Currency defaults to USD unless explicitly stated
  - Handle "no stock", "price on request", and partial quotes gracefully
  - Never auto-send anything — parsed data is always a draft

Called by: routers/ai.py (POST /api/ai/parse-email)
Depends on: services/gradient_service.py, utils/normalization.py
"""

import re

from loguru import logger

from app.services.gradient_service import gradient_json
from app.utils.normalization import (
    detect_currency,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_moq,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)

CONFIDENCE_AUTO = 0.8
CONFIDENCE_REVIEW = 0.5

SYSTEM_PROMPT = """\
You are a precise data extractor for electronic component vendor email replies.

Context: An electronic component broker sent RFQ (Request for Quote) emails to \
vendors asking for pricing and availability. A vendor has replied. Extract \
structured quote data from their reply.

Industry knowledge:
- MPN = Manufacturer Part Number (e.g., STM32F407VGT6, LM358DR, 0402-100nF)
- MOQ = Minimum Order Quantity
- Lead time = delivery time (days or weeks)
- Date code = manufacturing date (e.g., "2024+", "2339", "DC2024")
- Condition: "new", "refurbished", "used", "pull" (from desoldered boards)
- Packaging: "tape & reel" (T&R), "tray", "tube", "bulk", "cut tape"
- Common currencies: USD ($), EUR (€), GBP (£), CNY/RMB (¥)

Rules:
- Extract ALL quoted parts, even if some are unavailable
- Each part gets its own entry in the "quotes" array
- Only include fields you can confidently extract — omit uncertain fields
- For "no stock" or "not available" responses, set qty_available to 0 and \
  unit_price to null
- For "price on request" or "call for pricing", set unit_price to null and \
  add a note
- If the email contains a table or list of parts, extract each row separately
- Confidence reflects how clearly pricing/availability was stated (0.0–1.0)
- Set confidence < 0.5 if the email is ambiguous, a bounce, or out-of-office

Return ONLY valid JSON matching this exact structure:
{
  "quotes": [
    {
      "part_number": "string or null",
      "manufacturer": "string or null",
      "quantity_available": integer or null,
      "unit_price": number or null,
      "currency": "USD",
      "lead_time_days": integer or null,
      "lead_time_text": "string or null",
      "moq": integer or null,
      "date_code": "string or null",
      "condition": "string or null",
      "packaging": "string or null",
      "notes": "string or null",
      "confidence": 0.0-1.0
    }
  ],
  "overall_confidence": 0.0-1.0,
  "email_type": "quote"|"no_stock"|"partial"|"price_on_request"|"ooo_bounce"|"unclear",
  "vendor_notes": "string or null"
}"""


async def parse_email(
    email_body: str,
    email_subject: str = "",
    vendor_name: str = "",
) -> dict | None:
    """Parse a vendor email reply into structured quote data.

    Args:
        email_body: Raw email body text (plain text or HTML).
        email_subject: Email subject line for context.
        vendor_name: Sending vendor name for context.

    Returns:
        Parsed result dict with quotes list and confidence, or None on failure.
    """
    body = _clean_email_body(email_body)
    if not body:
        logger.warning("Empty email body — nothing to parse")
        return None

    # Truncate to avoid token waste on long emails
    body_truncated = body[:5000]

    prompt = f"Vendor: {vendor_name}\n" if vendor_name else ""
    prompt += f"Subject: {email_subject}\n\n" if email_subject else ""
    prompt += f"Email body:\n{body_truncated}"

    result = await gradient_json(
        prompt,
        system=SYSTEM_PROMPT,
        model_tier="strong",
        max_tokens=2048,
        temperature=0.1,
        timeout=45,
    )

    if not result or not isinstance(result, dict):
        logger.warning("Email parser returned no result or invalid format")
        return None

    # Normalize the extracted quotes
    _normalize_quotes(result)

    return result


def _normalize_quotes(result: dict) -> None:
    """Apply deterministic normalization to AI-extracted quote values."""
    quotes = result.get("quotes", [])
    if isinstance(quotes, str):
        import json
        try:
            quotes = json.loads(quotes)
            result["quotes"] = quotes
        except (ValueError, TypeError):
            result["quotes"] = []
            return

    for q in quotes:
        if not isinstance(q, dict):
            continue

        if q.get("unit_price") is not None:
            q["unit_price"] = normalize_price(q["unit_price"])

        if q.get("quantity_available") is not None:
            q["quantity_available"] = normalize_quantity(q["quantity_available"])

        if q.get("lead_time_text"):
            q["lead_time_days"] = normalize_lead_time(q["lead_time_text"])
        elif q.get("lead_time_days") is not None:
            # Already an integer, just validate
            try:
                q["lead_time_days"] = int(q["lead_time_days"])
            except (ValueError, TypeError):
                q["lead_time_days"] = None

        if q.get("condition"):
            q["condition"] = normalize_condition(q["condition"]) or q["condition"]

        if q.get("date_code"):
            q["date_code"] = normalize_date_code(q["date_code"])

        if q.get("moq") is not None:
            q["moq"] = normalize_moq(q["moq"])

        if q.get("packaging"):
            q["packaging"] = normalize_packaging(q["packaging"]) or q["packaging"]

        if q.get("currency"):
            q["currency"] = detect_currency(q["currency"])
        else:
            q["currency"] = "USD"

        # Ensure confidence is a float 0-1
        conf = q.get("confidence")
        if conf is not None:
            try:
                q["confidence"] = max(0.0, min(1.0, float(conf)))
            except (ValueError, TypeError):
                q["confidence"] = 0.5


def should_auto_apply(result: dict) -> bool:
    """Check if parsed result is confident enough to auto-create offers."""
    return (result.get("overall_confidence") or 0) >= CONFIDENCE_AUTO


def should_flag_review(result: dict) -> bool:
    """Check if parsed result needs human review."""
    conf = result.get("overall_confidence") or 0
    return CONFIDENCE_REVIEW <= conf < CONFIDENCE_AUTO


def _clean_email_body(body: str) -> str:
    """Strip HTML, excessive whitespace, and email disclaimers.

    Preserves newlines so tabular data and list formatting survive intact —
    the LLM needs structure to parse tables accurately.
    """
    if not body:
        return ""
    # Replace <br>, <p>, <tr>, <li> with newlines before stripping tags
    text = re.sub(r"<br\s*/?>|</p>|</tr>|</li>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse horizontal whitespace per line (preserve newlines)
    text = re.sub(r"[^\S\n]+", " ", text)
    # Collapse 3+ consecutive newlines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    disclaimer_patterns = [
        r"(?i)this email and any attachments.*?(?=\n\n|\Z)",
        r"(?i)confidentiality notice.*?(?=\n\n|\Z)",
        r"(?i)DISCLAIMER.*?(?=\n\n|\Z)",
    ]
    for pat in disclaimer_patterns:
        text = re.sub(pat, "", text, flags=re.DOTALL)
    return text.strip()
