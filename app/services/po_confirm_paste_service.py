"""po_confirm_paste_service.py — AI parse of a pasted PO confirmation into form prefill.

Purpose:
  The inbound twin of the Copy-for-ERP chip. A buyer pastes the PO confirmation
  text they copied out of the ERP screen (or an emailed confirmation) into the
  Approvals PO pane; this extracts po_number / est. ship date / payment method /
  serial numbers for the confirm-PO form and cross-checks vendor, part number,
  quantity, and unit cost against the plan line, returning ADVISORY warnings.
  The human transports the text and reviews every field — nothing here reads,
  writes, or syncs the ERP, and this module never writes to the database.

Called by: routers/htmx/approvals_hub.py (POST /v2/partials/approvals/po/{id}/parse-confirmation)
Depends on: utils/claude_client, utils/claude_errors, constants (PO_LINE_PAYMENT_METHODS)
"""

import re
from datetime import date

from loguru import logger

from app.constants import PO_LINE_PAYMENT_METHODS
from app.utils import claude_client
from app.utils.claude_errors import ClaudeError

_MAX_TEXT_CHARS = 20_000
_MAX_SERIALS = 500

# Regex-first PO number: a labeled token ("PO 77812", "PO Number: 77812", "P.O.# ABC-123").
# Deliberately ERP-neutral — a labeled token, not any screen layout. The token must
# contain a digit (so "po box"/"po token" never misfire) and must not be a bare
# YYYY-MM-DD date (so "PO: 2026-09-04" falls through to the AI).
_PO_LABEL_RE = re.compile(
    r"\bP\.?\s?O\.?\s*(?:number|no\.?)?\s*[#:\-]{0,2}\s*"
    r"(?!\d{4}-\d{2}-\d{2}\b)(?=[A-Za-z-]*\d)([A-Za-z0-9][A-Za-z0-9-]{2,30})\b",
    re.IGNORECASE,
)

_ALLOWED_PAYMENT_METHODS = {m.value for m in PO_LINE_PAYMENT_METHODS}

PO_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "po_number": {"type": ["string", "null"], "description": "Purchase order number, verbatim"},
        "estimated_ship_date": {"type": ["string", "null"], "description": "Estimated ship date, YYYY-MM-DD"},
        "payment_method": {
            "type": ["string", "null"],
            "enum": ["cc", "paypal", "wire", "ach", "cod", None],
            "description": "Payment terms mapped to one of: cc, paypal, wire, ach, cod",
        },
        "serial_numbers": {"type": "array", "items": {"type": ["string", "null"]}},
        "vendor_name": {"type": ["string", "null"], "description": "Vendor/supplier the PO was cut to"},
        "mpn": {"type": ["string", "null"], "description": "Part number on the PO"},
        "quantity": {"type": ["integer", "null"]},
        "unit_cost": {"type": ["number", "null"]},
    },
    "required": ["po_number"],
}

PO_CONFIRM_SYSTEM = """You extract structured fields from purchase-order confirmation text a buyer pasted from their ERP screen or a confirmation email.

Rules:
- Copy values VERBATIM — never invent, complete, or reformat identifiers.
- po_number: the purchase order number. estimated_ship_date: YYYY-MM-DD only (null if no full date).
- payment_method: map stated terms to exactly one of cc, paypal, wire, ach, cod (e.g. "credit card"→cc, "wire transfer"→wire, "collect on delivery"→cod). Null if not stated.
- serial_numbers: every unit serial number listed, verbatim, in order.
- vendor_name / mpn / quantity / unit_cost: as stated on the confirmation — used only to cross-check against the plan line.
- Use null for anything the text does not state."""


def _clean(value: object) -> str | None:
    """Strip/truncate a scalar to a form-safe string, None when empty/absent."""
    if value is None:
        return None
    return str(value).strip()[:255] or None


def _norm_mpn(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _cross_check(ai: dict, *, line_mpn, vendor_name, quantity, unit_cost) -> list[str]:
    """Advisory mismatch warnings — only when BOTH sides have a value.

    Never blocks.
    """
    warnings: list[str] = []
    ai_vendor = _clean(ai.get("vendor_name"))
    if ai_vendor and vendor_name:
        a, b = ai_vendor.casefold(), str(vendor_name).casefold()
        if a not in b and b not in a:
            warnings.append(
                f"Vendor on the confirmation ('{ai_vendor}') does not match this line's vendor ('{vendor_name}')."
            )
    ai_mpn = _clean(ai.get("mpn"))
    if ai_mpn and line_mpn and _norm_mpn(ai_mpn) != _norm_mpn(str(line_mpn)):
        warnings.append(f"Part number on the confirmation ('{ai_mpn}') does not match this line ('{line_mpn}').")
    ai_qty = ai.get("quantity")
    if isinstance(ai_qty, int) and quantity and ai_qty != quantity:
        warnings.append(f"Quantity on the confirmation ({ai_qty}) does not match this line ({quantity}).")
    ai_cost = ai.get("unit_cost")
    if isinstance(ai_cost, (int, float)) and unit_cost:
        try:
            if abs(float(ai_cost) - float(unit_cost)) > 0.005 * float(unit_cost):
                warnings.append(f"Unit cost on the confirmation ({ai_cost}) does not match this line ({unit_cost}).")
        except (TypeError, ValueError):
            pass
    return warnings


async def parse_po_confirmation(
    raw_text: str,
    *,
    line_mpn: str | None,
    vendor_name: str | None,
    quantity: int | None,
    unit_cost: float | None,
) -> dict | None:
    """Parse pasted PO-confirmation text into confirm-form prefill + advisory warnings.

    Returns None when the AI call fails, else a dict with po_number /
    estimated_ship_date / payment_method / serial_numbers (comma-joined str) / warnings.
    A labeled PO-number token found by regex WINS over the AI's value (the highest-
    stakes field stays deterministic whenever the text labels it).
    """
    text = (raw_text or "").strip()[:_MAX_TEXT_CHARS]
    if not text:
        return None

    try:
        # Interactive HTMX caller: single attempt, tightened timeout (P2.8 pattern).
        ai = await claude_client.claude_structured(
            text,
            PO_CONFIRM_SCHEMA,
            system=PO_CONFIRM_SYSTEM,
            model_tier="fast",
            max_tokens=4096,
            timeout=45,
            max_attempts=1,
            cost_bucket="po_confirm_paste",
        )
    except ClaudeError as e:
        logger.warning("PO confirmation paste parse failed: {}", e)
        return None
    if ai is None:
        return None

    # A UNIQUE labeled token is deterministic and wins over the AI. With several
    # labeled tokens (a customer-PO reference beside the real PO is common), only
    # the one the AI agrees with wins — never a blind first-match.
    tokens = list(dict.fromkeys(m.group(1) for m in _PO_LABEL_RE.finditer(text)))
    ai_po = _clean(ai.get("po_number"))
    if len(tokens) == 1:
        po_number = tokens[0]
    elif tokens:
        po_number = next((t for t in tokens if ai_po and t.casefold() in ai_po.casefold()), ai_po or tokens[0])
    else:
        po_number = ai_po

    ship_date = _clean(ai.get("estimated_ship_date"))
    if ship_date:
        try:
            # Normalize: Py3.12 fromisoformat also accepts "20260904"/"2026-W36-4",
            # which an <input type=date> would silently drop.
            ship_date = date.fromisoformat(ship_date).isoformat()
        except ValueError:
            ship_date = None

    payment_method = _clean(ai.get("payment_method"))
    if payment_method:
        payment_method = payment_method.lower()
        if payment_method not in _ALLOWED_PAYMENT_METHODS:
            payment_method = None

    serials = [s for s in (_clean(v) for v in (ai.get("serial_numbers") or [])[:_MAX_SERIALS]) if s]

    return {
        "po_number": po_number,
        "estimated_ship_date": ship_date,
        "payment_method": payment_method,
        "serial_numbers": ", ".join(serials) if serials else None,
        "warnings": _cross_check(
            ai, line_mpn=line_mpn, vendor_name=vendor_name, quantity=quantity, unit_cost=unit_cost
        ),
    }
