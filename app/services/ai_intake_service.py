"""
ai_intake_service.py — Free-form intake parser for RFQ/Offer templates.

Converts pasted customer/vendor free text into structured draft rows that users
can review and edit before saving to requirements/offers.

Called by: app/routers/ai.py (/api/ai/intake-parse)
Depends on: app.utils.llm_router, app.utils.normalization, loguru
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from app.utils.llm_router import routed_structured
from app.utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)

INTAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "detected_type": {"type": "string", "enum": ["rfq", "offer", "mixed", "unknown"]},
        "context": {
            "type": "object",
            "properties": {
                "requisition_name": {"type": "string"},
                "customer_name": {"type": "string"},
                "vendor_name": {"type": "string"},
            },
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "row_type": {"type": "string", "enum": ["requirement", "offer"]},
                    "mpn": {"type": "string"},
                    "qty": {"type": "integer"},
                    "unit_price": {"type": "number"},
                    "currency": {"type": "string"},
                    "vendor_name": {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "lead_time": {"type": "string"},
                    "condition": {"type": "string"},
                    "packaging": {"type": "string"},
                    "notes": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["row_type", "mpn"],
            },
        },
    },
    "required": ["detected_type", "rows"],
}

SYSTEM_PROMPT = """You extract procurement data from messy pasted text for an electronic component sourcing CRM.

Task:
- Parse free-form customer/vendor text into clean line-item rows.
- Output requirements (RFQ input) and/or offers (vendor quote input).
- If unsure, keep confidence low and still return best-effort extraction.

Rules:
- Never invent part numbers.
- "row_type=requirement" for customer demand (requested qty, target price).
- "row_type=offer" for vendor supply/quote (available qty, quoted price).
- For offer rows, include vendor_name when visible in the text.
- Confidence is 0.0-1.0.
- Return strict JSON only.
"""


def _to_conf(value: Any, default: float = 0.55) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def _to_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return normalize_price(value)


def _to_qty(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return normalize_quantity(value)


def _clean_mode(mode: str) -> str:
    return mode if mode in {"auto", "rfq", "offer"} else "auto"


def _normalize_rows(rows: list[dict], mode: str, default_vendor: str = "") -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_type = (row.get("row_type") or "").strip().lower()
        if row_type not in {"requirement", "offer"}:
            row_type = "offer" if mode == "offer" else "requirement"

        mpn_raw = (row.get("mpn") or "").strip()
        mpn = normalize_mpn(mpn_raw) or mpn_raw
        if not mpn:
            continue

        qty = _to_qty(row.get("qty"))
        price = _to_price(row.get("unit_price"))
        vendor_name = (row.get("vendor_name") or "").strip()
        if row_type == "offer" and not vendor_name:
            vendor_name = default_vendor
        condition = normalize_condition((row.get("condition") or "").strip()) if row.get("condition") else None
        packaging = normalize_packaging((row.get("packaging") or "").strip()) if row.get("packaging") else None

        out.append(
            {
                "row_type": row_type,
                "mpn": mpn,
                "qty": qty,
                "unit_price": price,
                "currency": (row.get("currency") or "USD").strip().upper()[:8] or "USD",
                "vendor_name": vendor_name or None,
                "manufacturer": (row.get("manufacturer") or "").strip() or None,
                "lead_time": (row.get("lead_time") or "").strip() or None,
                "condition": condition or None,
                "packaging": packaging or None,
                "notes": (row.get("notes") or "").strip() or None,
                "confidence": _to_conf(row.get("confidence")),
            }
        )
    return _coerce_mode(out, mode, default_vendor)


def _coerce_mode(rows: list[dict], mode: str, default_vendor: str) -> list[dict]:
    if mode == "auto":
        return rows
    coerced: list[dict] = []
    for row in rows:
        new_row = dict(row)
        if mode == "rfq":
            new_row["row_type"] = "requirement"
            new_row["vendor_name"] = None
        else:
            new_row["row_type"] = "offer"
            if not new_row.get("vendor_name"):
                new_row["vendor_name"] = default_vendor or "Unknown Vendor"
        coerced.append(new_row)
    return coerced


def _infer_detected_type(rows: list[dict]) -> str:
    if not rows:
        return "unknown"
    req_count = sum(1 for r in rows if r.get("row_type") == "requirement")
    offer_count = sum(1 for r in rows if r.get("row_type") == "offer")
    if req_count and offer_count:
        return "mixed"
    return "offer" if offer_count else "rfq"


def _heuristic_parse(text: str, mode: str) -> list[dict]:
    rows: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return rows

    for line in lines[:250]:
        cols = [c.strip() for c in re.split(r"\t|,{2,}|;|\|", line) if c.strip()]
        src = cols if len(cols) >= 2 else [line]
        mpn_guess = ""
        qty_guess = None
        price_guess = None

        # MPN: first reasonably long token with letters/numbers.
        for token in src:
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{2,}$", token):
                mpn_guess = normalize_mpn(token) or token
                break
        if not mpn_guess:
            continue

        qty_match = re.search(r"\b(?:qty|quantity|q'ty)\s*[:=]?\s*([\d,]+)\b", line, flags=re.I)
        if qty_match:
            qty_guess = _to_qty(qty_match.group(1))
        elif len(cols) >= 2:
            qty_guess = _to_qty(cols[1])

        price_match = re.search(r"(?:\$|usd\s*)\s*([0-9]+(?:\.[0-9]+)?)", line, flags=re.I)
        if price_match:
            price_guess = _to_price(price_match.group(1))
        elif len(cols) >= 3:
            price_guess = _to_price(cols[2])

        row_type = "offer" if mode == "offer" else "requirement"
        if mode == "auto" and price_guess is not None:
            row_type = "offer"

        rows.append(
            {
                "row_type": row_type,
                "mpn": mpn_guess,
                "qty": qty_guess,
                "unit_price": price_guess,
                "currency": "USD",
                "vendor_name": "Unknown Vendor" if row_type == "offer" else None,
                "manufacturer": None,
                "lead_time": None,
                "condition": None,
                "packaging": None,
                "notes": None,
                "confidence": 0.4,
            }
        )
    return _coerce_mode(rows, mode, default_vendor="Unknown Vendor")


async def parse_freeform_intake(text: str, mode: str = "auto") -> dict:
    """Parse pasted free-form text into editable requirement/offer draft rows."""
    cleaned = (text or "").strip()
    mode = _clean_mode(mode)
    if not cleaned:
        return {"detected_type": "unknown", "context": {}, "rows": [], "summary": {"rows": 0, "requirements": 0, "offers": 0}}

    prompt = (
        f"Parsing mode: {mode}\n"
        "Extract line items from this pasted text. Keep unknown fields null.\n\n"
        f"Pasted text:\n{cleaned[:10000]}"
    )

    parsed: dict | None = None
    try:
        parsed = await routed_structured(
            prompt=prompt,
            schema=INTAKE_SCHEMA,
            system=SYSTEM_PROMPT,
            model_tier="fast",
            max_tokens=1800,
            timeout=45,
        )
    except Exception:
        logger.debug("Free-form intake AI parse failed", exc_info=True)

    context_raw = (parsed or {}).get("context", {}) if isinstance(parsed, dict) else {}
    context = {
        "requisition_name": (context_raw.get("requisition_name") or "").strip() or None,
        "customer_name": (context_raw.get("customer_name") or "").strip() or None,
        "vendor_name": (context_raw.get("vendor_name") or "").strip() or None,
    }
    rows = _normalize_rows((parsed or {}).get("rows", []) if isinstance(parsed, dict) else [], mode, context["vendor_name"] or "")

    if not rows:
        rows = _heuristic_parse(cleaned, mode)

    detected = (parsed or {}).get("detected_type") if isinstance(parsed, dict) else None
    if mode == "rfq":
        detected = "rfq"
    elif mode == "offer":
        detected = "offer"
    if detected not in {"rfq", "offer", "mixed", "unknown"}:
        detected = _infer_detected_type(rows)

    req_count = sum(1 for r in rows if r.get("row_type") == "requirement")
    offer_count = sum(1 for r in rows if r.get("row_type") == "offer")

    return {
        "detected_type": detected,
        "context": context,
        "rows": rows,
        "summary": {
            "rows": len(rows),
            "requirements": req_count,
            "offers": offer_count,
        },
    }
