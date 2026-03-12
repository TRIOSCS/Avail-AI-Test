"""Free-text RFQ/Offer parser — AI-powered extraction from pasted text.

Users paste free-form text (customer emails, chat messages, vendor quotes)
and the AI extracts structured part data, determining whether it's an RFQ
(customer requesting parts) or an Offer (vendor offering parts).

Called by: routers/ai.py (parse-free-text endpoint)
Depends on: utils/claude_client.py
"""

from loguru import logger

from app.utils.claude_client import claude_structured

FREE_TEXT_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {
            "type": "string",
            "enum": ["rfq", "offer"],
            "description": (
                "rfq = someone is REQUESTING quotes / wants to BUY parts. "
                "offer = someone is OFFERING / quoting parts for sale."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 confidence in extraction accuracy",
        },
        "company_name": {
            "type": "string",
            "description": "Customer or vendor company name if mentioned",
        },
        "contact_name": {
            "type": "string",
            "description": "Contact person name if mentioned",
        },
        "contact_email": {
            "type": "string",
            "description": "Contact email if mentioned",
        },
        "notes": {
            "type": "string",
            "description": "General notes, delivery requirements, or context not captured in line items",
        },
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {
                        "type": "string",
                        "description": "Manufacturer part number",
                    },
                    "manufacturer": {
                        "type": "string",
                        "description": "Part manufacturer if mentioned",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Quantity requested or available",
                    },
                    "target_price": {
                        "type": "number",
                        "description": "Target or quoted unit price",
                    },
                    "currency": {
                        "type": "string",
                        "description": "Currency code (USD, EUR, etc.)",
                    },
                    "condition": {
                        "type": "string",
                        "description": "new, refurbished, used, etc.",
                    },
                    "date_code": {
                        "type": "string",
                        "description": "Date code requirement or offered",
                    },
                    "lead_time": {
                        "type": "string",
                        "description": "Lead time or delivery requirement",
                    },
                    "packaging": {
                        "type": "string",
                        "description": "Packaging type (tape & reel, tray, tube, etc.)",
                    },
                    "moq": {
                        "type": "integer",
                        "description": "Minimum order quantity",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Per-line notes or special requirements",
                    },
                },
                "required": ["mpn"],
            },
            "description": "Extracted line items (parts)",
        },
    },
    "required": ["document_type", "confidence", "line_items"],
}

SYSTEM_PROMPT = """You are a precise data extractor for the electronic components industry.

You receive free-form text that a sales or purchasing person has pasted. It could be:
- A customer email requesting quotes (RFQ)
- A vendor email offering parts / providing quotes (Offer)
- A pasted list of parts from a chat, spreadsheet, or other source

Your job:
1. Determine if this is an RFQ (someone wants to BUY) or an Offer (someone is SELLING/quoting).
   - If the text asks "can you quote", "need pricing", "looking for", "please quote" → rfq
   - If the text says "we have in stock", "offering at", "quote attached", unit prices are given → offer
   - If it's just a list of parts with quantities but no clear direction, default to rfq
2. Extract every part number mentioned with as much detail as possible.
3. Clean up part numbers: remove leading/trailing whitespace, standardize formatting.
4. Extract quantities, prices, conditions, date codes, lead times where available.
5. Extract company name and contact info if present in signatures or headers.

Rules:
- Always extract ALL parts mentioned, even if some have incomplete data.
- If a price is mentioned, include it. If not, omit the field.
- Currency defaults to USD unless explicitly stated otherwise.
- Condition defaults to "new" if not stated.
- Be generous with extraction — include parts even if you're unsure about some fields.
- Set confidence based on how clearly the text communicates the parts and intent."""


async def parse_free_text(text: str) -> dict | None:
    """Parse free-form text into structured RFQ or Offer data.

    Args:
        text: Raw pasted text from user

    Returns:
        Parsed result dict with document_type, line_items, etc. or None on failure
    """
    if not text or not text.strip():
        return None

    truncated = text[:8000]

    prompt = f"Parse the following text and extract all electronic component part data:\n\n{truncated}"

    result = await claude_structured(
        prompt=prompt,
        schema=FREE_TEXT_PARSE_SCHEMA,
        system=SYSTEM_PROMPT,
        model_tier="smart",
        max_tokens=2048,
        timeout=45,
    )

    if not result:
        logger.warning("Free-text parse returned no result")
        return None

    _normalize_line_items(result)

    logger.info(
        "Free-text parsed: type={}, items={}, confidence={:.2f}",
        result.get("document_type"),
        len(result.get("line_items", [])),
        result.get("confidence", 0),
    )

    return result


def _normalize_line_items(result: dict) -> None:
    """Apply basic normalization to extracted line items."""
    from app.utils.normalization import (
        normalize_condition,
        normalize_packaging,
    )

    for item in result.get("line_items", []):
        if not isinstance(item, dict):
            continue

        if item.get("mpn"):
            item["mpn"] = item["mpn"].strip()

        if item.get("condition"):
            item["condition"] = normalize_condition(item["condition"]) or item["condition"]

        if item.get("packaging"):
            item["packaging"] = normalize_packaging(item["packaging"]) or item["packaging"]

        if not item.get("currency"):
            item["currency"] = "USD"

        if item.get("quantity") is not None:
            try:
                item["quantity"] = max(1, int(item["quantity"]))
            except (ValueError, TypeError):
                item["quantity"] = 1
