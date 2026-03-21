"""Freeform text parser — AI extracts RFQ or Offer templates from pasted customer/vendor
text.

Purpose:
  Sales and buyers paste free-form text from customers (RFQ) or vendors (offers).
  AI cleans and structures it into editable templates. User reviews, edits, and saves.

Called by: routers/ai.py (parse-freeform-rfq, parse-freeform-offer)
Depends on: utils/claude_client, utils/llm_router
"""

from app.utils.llm_router import routed_structured
from app.utils.normalization import (
    detect_currency,
    normalize_condition,
    normalize_date_code,
    normalize_moq,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)

# ── RFQ schema (customer request) ────────────────────────────────────────

RFQ_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Requisition/project name"},
        "customer_name": {"type": "string", "description": "Customer or company name"},
        "deadline": {"type": "string", "description": "Delivery deadline (YYYY-MM-DD or ASAP)"},
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "primary_mpn": {"type": "string", "description": "Part number"},
                    "target_qty": {"type": "integer", "description": "Quantity needed"},
                    "target_price": {"type": "number", "description": "Target or max price"},
                    "substitutes": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                    "brand": {"type": "string", "description": "Manufacturer/brand name"},
                    "condition": {"type": "string", "description": "Part condition: new, refurbished, used"},
                    "customer_pn": {"type": "string", "description": "Customer's internal part number"},
                    "date_codes": {"type": "string", "description": "Date code requirements (e.g. 2024+, 2339)"},
                    "packaging": {"type": "string", "description": "Packaging: tape & reel, tray, tube, bulk"},
                    "need_by_date": {"type": "string", "description": "Line-item need-by date (YYYY-MM-DD)"},
                },
                "required": ["primary_mpn"],
            },
        },
    },
    "required": ["name", "requirements"],
}

RFQ_SYSTEM = """You extract structured RFQ (Request for Quote) data from free-form customer text.

Context: A salesperson or buyer pasted text from a customer email, chat, or document.
Extract: requisition name, customer name, deadline, and line items (part numbers, quantities, target prices).

Rules:
- primary_mpn: manufacturer part number (e.g. STM32F407VGT6, LM358DR). Required per line.
- target_qty: quantity needed. Default 1 if not stated.
- target_price: max/target price if mentioned. Omit if unknown.
- substitutes: alternate part numbers if listed.
- brand: manufacturer name if stated (e.g. Texas Instruments, STMicroelectronics). Omit if unknown.
- condition: new, refurbished, used, pull. Default "new" if not stated.
- customer_pn: customer's internal part number if listed. Omit if not stated.
- date_codes: date code requirements (e.g. "2024+", "2339"). Omit if not stated.
- packaging: tape & reel, tray, tube, bulk. Omit if not stated.
- need_by_date: per-line need-by date in YYYY-MM-DD. Omit if not stated.
- Infer customer_name from sender, signature, or context.
- deadline: YYYY-MM-DD if date given, or "ASAP" if urgent/ASAP.
- name: project name, PO reference, or "CustomerName - Parts" if unclear."""


# ── Offer schema (vendor quote) ───────────────────────────────────────────

OFFER_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": "string", "description": "Vendor/supplier name"},
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
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
    "required": ["vendor_name", "offers"],
}

OFFER_SYSTEM = """You extract structured offer/quote data from free-form vendor text.

Context: A buyer pasted text from a vendor email, quote, or stock list.
Extract: vendor name and line items (part numbers, prices, quantities, lead times).

Rules:
- mpn: part number. Required per line.
- qty_available, unit_price: extract numbers. Omit if not stated.
- currency: USD unless EUR/GBP/CNY stated.
- lead_time: e.g. "2 weeks", "4-6 weeks", "stock".
- condition: new, refurbished, used, pull.
- packaging: tape & reel, tray, tube, bulk.
- moq: minimum order quantity.
- Include all parts mentioned, even if some are "no stock" (qty 0)."""


async def parse_freeform_rfq(raw_text: str) -> dict | None:
    """Parse free-form customer text into RFQ template (requisition + requirements)."""
    text = (raw_text or "").strip()[:6000]
    if not text:
        return None
    result = await routed_structured(
        prompt=f"Customer/vendor text:\n\n{text}",
        schema=RFQ_PARSE_SCHEMA,
        system=RFQ_SYSTEM,
        model_tier="fast",
        max_tokens=2048,
    )
    if not result:
        return None
    # Normalize requirements
    for r in result.get("requirements", []):
        if r.get("target_qty") is None:
            r["target_qty"] = 1
        if r.get("target_price") is not None:
            r["target_price"] = normalize_price(r["target_price"])
        if r.get("substitutes") is None:
            r["substitutes"] = []
        if r.get("condition"):
            r["condition"] = normalize_condition(r["condition"]) or r["condition"]
        if not r.get("condition"):
            r["condition"] = "new"
        if r.get("packaging"):
            r["packaging"] = normalize_packaging(r["packaging"]) or r["packaging"]
        if r.get("date_codes"):
            r["date_codes"] = normalize_date_code(r["date_codes"]) or r["date_codes"]
    return result


async def parse_freeform_offer(raw_text: str, rfq_context: list | None = None) -> dict | None:
    """Parse free-form vendor text into offer template(s)."""
    text = (raw_text or "").strip()[:6000]
    if not text:
        return None
    ctx_str = ""
    if rfq_context:
        parts = ", ".join(f"{p.get('mpn', '?')} x{p.get('qty', 1)}" for p in rfq_context[:10])
        ctx_str = f"\nParts we asked about: {parts}\n"
    result = await routed_structured(
        prompt=f"{ctx_str}Vendor text:\n\n{text}",
        schema=OFFER_PARSE_SCHEMA,
        system=OFFER_SYSTEM,
        model_tier="fast",
        max_tokens=2048,
    )
    if not result:
        return None
    # Normalize offers
    for o in result.get("offers", []):
        if o.get("unit_price") is not None:
            o["unit_price"] = normalize_price(o["unit_price"])
        if o.get("qty_available") is not None:
            o["qty_available"] = normalize_quantity(o["qty_available"])
        if o.get("condition"):
            o["condition"] = normalize_condition(o["condition"]) or o["condition"]
        if o.get("date_code"):
            o["date_code"] = normalize_date_code(o["date_code"]) or o["date_code"]
        if o.get("moq") is not None:
            o["moq"] = normalize_moq(o["moq"])
        if o.get("packaging"):
            o["packaging"] = normalize_packaging(o["packaging"]) or o["packaging"]
        o["currency"] = detect_currency(o.get("currency")) or "USD"
    return result
