"""services/ai_intake_parser.py — AI parser for pasted customer/vendor text.

Called by: app/routers/ai.py (/api/ai/intake-draft)
Depends on: app/utils/llm_router.py and normalization helpers
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from app.utils.llm_router import routed_structured
from app.utils.normalization import (
    detect_currency,
    normalize_condition,
    normalize_date_code,
    normalize_moq,
    normalize_mpn,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)

INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {
            "type": "string",
            "enum": ["rfq", "offer", "unclear"],
            "description": "Customer request for parts vs vendor quote/offer text.",
        },
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "requisition_name": {"type": "string"},
        "customer_name": {"type": "string"},
        "vendor_name": {"type": "string"},
        "notes": {"type": "string"},
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "manufacturer": {"type": "string"},
                    "target_price": {"type": "number"},
                    "condition": {"type": "string"},
                    "date_codes": {"type": "string"},
                    "packaging": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["mpn"],
            },
        },
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "vendor_name": {"type": "string"},
                    "mpn": {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "qty_available": {"type": "integer"},
                    "unit_price": {"type": "number"},
                    "currency": {"type": "string"},
                    "lead_time": {"type": "string"},
                    "date_code": {"type": "string"},
                    "condition": {"type": "string"},
                    "packaging": {"type": "string"},
                    "moq": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["mpn"],
            },
        },
    },
    "required": ["document_type", "confidence", "requirements", "offers"],
}

SYSTEM_PROMPT = """You extract structured data from pasted electronic component text.

The input may be:
- a customer RFQ / request for quote asking for parts
- a vendor offer / quote with pricing and availability
- messy copied email text, chat text, notes, or a table pasted as plain text

Rules:
- Decide whether the text is best treated as an RFQ, an offer, or unclear
- RFQ = customer is requesting parts, quantities, targets, conditions, or dates
- Offer = vendor is providing stock, pricing, MOQ, lead time, date code, condition
- Extract every explicit part line you can find
- Never invent part numbers, quantities, prices, names, or companies
- Leave unknown fields empty instead of guessing
- Use vendor_name/customer_name only if the text states them clearly
- requisition_name should be a short human-friendly draft title
- confidence is 0.0 to 1.0 based on extraction clarity
- Return only valid JSON matching the schema"""


async def parse_freeform_intake(
    text: str,
    requisition_context: list[dict[str, Any]] | None = None,
    mode: str = "auto",
) -> dict[str, Any] | None:
    """Classify pasted text and extract an RFQ or offer draft.

    Args:
        text: Raw pasted text from user.
        requisition_context: Existing parts for context matching.
        mode: "auto" lets AI decide, "rfq" forces all rows to requirements,
              "offer" forces all rows to offers.
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return None

    context_lines = []
    for item in (requisition_context or [])[:20]:
        mpn = (item.get("mpn") or "").strip()
        qty = item.get("qty")
        if mpn:
            context_lines.append(f"- {mpn} x{qty or '?'}")
    context_block = ""
    if context_lines:
        context_block = "\nCurrent requisition context:\n" + "\n".join(context_lines)

    prompt = f"Pasted text:\n{cleaned}{context_block}"

    try:
        result = await routed_structured(
            prompt=prompt,
            schema=INTAKE_SCHEMA,
            system=SYSTEM_PROMPT,
            model_tier="fast",
            max_tokens=1800,
        )
    except Exception:
        logger.warning("AI intake LLM call failed, falling back to heuristic parser")
        result = None

    if not result or not isinstance(result, dict):
        logger.info("Using heuristic fallback parser")
        result = _heuristic_parse(cleaned)
        if not result:
            return None

    _normalize_requirements(result)
    _normalize_offers(result)
    _normalize_top_level(result)
    _coerce_mode(result, mode)
    _backfill_document_type(result)
    _backfill_requisition_name(result)

    return result


def _normalize_top_level(result: dict[str, Any]) -> None:
    """Normalize high-level document metadata."""
    doc_type = (result.get("document_type") or "unclear").strip().lower()
    if doc_type not in {"rfq", "offer", "unclear"}:
        doc_type = "unclear"
    result["document_type"] = doc_type

    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        result["confidence"] = 0.0

    for key in ("summary", "requisition_name", "customer_name", "vendor_name", "notes"):
        value = result.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        value = " ".join(value.split()).strip()
        result[key] = value or None

    if not result.get("summary"):
        req_count = len(result.get("requirements") or [])
        offer_count = len(result.get("offers") or [])
        if result["document_type"] == "offer":
            result["summary"] = f"Drafted {offer_count} offer line(s) from pasted vendor text."
        elif result["document_type"] == "rfq":
            result["summary"] = f"Drafted {req_count} RFQ line(s) from pasted customer text."
        else:
            result["summary"] = "AI reviewed the pasted text and built a draft to verify."


def _normalize_requirements(result: dict[str, Any]) -> None:
    """Normalize extracted RFQ requirement rows."""
    raw_rows = _coerce_json_list(result.get("requirements"))
    normalized: list[dict[str, Any]] = []

    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        mpn = normalize_mpn((row.get("mpn") or "").strip()) or (row.get("mpn") or "").strip().upper()
        if not mpn:
            continue

        qty = normalize_quantity(row.get("quantity"))
        if qty is None or qty < 1:
            qty = 1

        target_price = normalize_price(row.get("target_price")) if row.get("target_price") is not None else None
        condition = normalize_condition(row.get("condition")) or _clean_scalar(row.get("condition"))
        packaging = normalize_packaging(row.get("packaging")) or _clean_scalar(row.get("packaging"))

        normalized.append(
            {
                "mpn": mpn,
                "quantity": qty,
                "manufacturer": _clean_scalar(row.get("manufacturer")),
                "target_price": target_price,
                "condition": condition,
                "date_codes": _clean_scalar(row.get("date_codes")),
                "packaging": packaging,
                "notes": _clean_scalar(row.get("notes")),
            }
        )

    result["requirements"] = normalized


def _normalize_offers(result: dict[str, Any]) -> None:
    """Normalize extracted vendor offer rows."""
    raw_rows = _coerce_json_list(result.get("offers"))
    default_vendor = _clean_scalar(result.get("vendor_name"))
    normalized: list[dict[str, Any]] = []

    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        mpn = normalize_mpn((row.get("mpn") or "").strip()) or (row.get("mpn") or "").strip().upper()
        if not mpn:
            continue

        qty_available = normalize_quantity(row.get("qty_available")) if row.get("qty_available") is not None else None
        unit_price = normalize_price(row.get("unit_price")) if row.get("unit_price") is not None else None
        vendor_name = _clean_scalar(row.get("vendor_name")) or default_vendor or ""

        normalized.append(
            {
                "vendor_name": vendor_name,
                "mpn": mpn,
                "manufacturer": _clean_scalar(row.get("manufacturer")),
                "qty_available": qty_available,
                "unit_price": unit_price,
                "currency": detect_currency(row.get("currency")) if row.get("currency") else "USD",
                "lead_time": _clean_scalar(row.get("lead_time")),
                "date_code": normalize_date_code(row.get("date_code")) if row.get("date_code") else None,
                "condition": normalize_condition(row.get("condition")) or _clean_scalar(row.get("condition")),
                "packaging": normalize_packaging(row.get("packaging")) or _clean_scalar(row.get("packaging")),
                "moq": normalize_moq(row.get("moq")) if row.get("moq") is not None else None,
                "notes": _clean_scalar(row.get("notes")),
            }
        )

    result["offers"] = normalized


def _backfill_document_type(result: dict[str, Any]) -> None:
    """Infer a document type from extracted rows when the model is unsure."""
    if result.get("document_type") != "unclear":
        return

    req_count = len(result.get("requirements") or [])
    offer_count = len(result.get("offers") or [])
    if offer_count and offer_count >= req_count:
        result["document_type"] = "offer"
    elif req_count:
        result["document_type"] = "rfq"


def _backfill_requisition_name(result: dict[str, Any]) -> None:
    """Provide a safe default draft title."""
    if result.get("requisition_name"):
        return

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    if result.get("document_type") == "offer":
        vendor_name = result.get("vendor_name") or "Vendor"
        result["requisition_name"] = f"{vendor_name} offer intake {stamp}"
    elif result.get("document_type") == "rfq":
        customer_name = result.get("customer_name") or "Customer"
        result["requisition_name"] = f"{customer_name} RFQ intake {stamp}"
    else:
        result["requisition_name"] = f"AI intake draft {stamp}"


def _coerce_mode(result: dict[str, Any], mode: str) -> None:
    """Force all rows to one type when user explicitly declares mode.

    If mode is 'rfq', move any offer rows into requirements.
    If mode is 'offer', move any requirement rows into offers.
    """
    if mode not in ("rfq", "offer"):
        return

    if mode == "rfq":
        for offer in (result.get("offers") or []):
            result.setdefault("requirements", []).append({
                "mpn": offer.get("mpn", ""),
                "quantity": offer.get("qty_available") or 1,
                "manufacturer": offer.get("manufacturer"),
                "target_price": offer.get("unit_price"),
                "condition": offer.get("condition"),
                "date_codes": offer.get("date_code"),
                "packaging": offer.get("packaging"),
                "notes": offer.get("notes"),
            })
        result["offers"] = []
        result["document_type"] = "rfq"

    elif mode == "offer":
        default_vendor = _clean_scalar(result.get("vendor_name")) or ""
        for req in (result.get("requirements") or []):
            result.setdefault("offers", []).append({
                "vendor_name": default_vendor,
                "mpn": req.get("mpn", ""),
                "manufacturer": req.get("manufacturer"),
                "qty_available": req.get("quantity"),
                "unit_price": req.get("target_price"),
                "currency": "USD",
                "lead_time": None,
                "date_code": req.get("date_codes"),
                "condition": req.get("condition"),
                "packaging": req.get("packaging"),
                "moq": None,
                "notes": req.get("notes"),
            })
        result["requirements"] = []
        result["document_type"] = "offer"


def _heuristic_parse(text: str) -> dict[str, Any] | None:
    """Regex-based fallback for TSV/CSV/delimited text when LLM fails.

    Scans each line for part-number-like tokens followed by optional
    quantity and price columns. Returns a minimal result dict.
    """
    import re

    mpn_pattern = re.compile(
        r"^[\s\"']*([A-Z0-9][A-Z0-9\-\.\/\+]{2,30}[A-Z0-9])"
    )
    lines = text.strip().split("\n")
    rows: list[dict[str, Any]] = []

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        cells = re.split(r"[\t,|;]+", line)
        if not cells:
            continue

        match = mpn_pattern.match(cells[0].strip())
        if not match:
            continue

        mpn = match.group(1).strip()
        qty = None
        price = None
        manufacturer = None

        if len(cells) > 1:
            qty = normalize_quantity(cells[1].strip())
        if len(cells) > 2:
            price = normalize_price(cells[2].strip())
        if len(cells) > 3:
            manufacturer = _clean_scalar(cells[3].strip())

        rows.append({
            "mpn": normalize_mpn(mpn) or mpn.upper(),
            "quantity": qty or 1,
            "manufacturer": manufacturer,
            "target_price": price,
            "condition": None,
            "date_codes": None,
            "packaging": None,
            "notes": None,
        })

    if not rows:
        return None

    return {
        "document_type": "unclear",
        "confidence": 0.3,
        "summary": f"Heuristic parser extracted {len(rows)} row(s) from tabular text.",
        "requisition_name": None,
        "customer_name": None,
        "vendor_name": None,
        "notes": None,
        "requirements": rows,
        "offers": [],
    }


def _clean_text(text: str) -> str:
    """Trim pasted text while preserving line structure."""
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    lines = [line.rstrip() for line in cleaned.split("\n")]
    compacted: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip():
            blank_run = 0
            compacted.append(line)
            continue
        blank_run += 1
        if blank_run <= 2:
            compacted.append("")
    return "\n".join(compacted).strip()


def _coerce_json_list(value: Any) -> list[Any]:
    """Accept native arrays or JSON-encoded array strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (TypeError, ValueError):
            return []
    return []


def _clean_scalar(value: Any) -> str | None:
    """Collapse whitespace and blank values to None."""
    if value is None:
        return None
    value = " ".join(str(value).split()).strip()
    return value or None
