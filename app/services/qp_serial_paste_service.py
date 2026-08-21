"""qp_serial_paste_service.py — AI parse of pasted packing-list text into QP serial
rows.

Purpose:
  A buyer pastes the vendor packing list / test report text into the Quality Plan's
  Serial section instead of hand-keying one QpSerialEntry row per submit. One
  claude_structured call maps the text to candidate rows (PO, part #, serial #,
  Seagate SN, TSO, customer PO). The caller renders them as a preview the human
  confirms — this module never writes to the database.

Called by: routers/quality_plans.py (POST /v2/qp/{id}/serial/parse)
Depends on: utils/claude_client, utils/claude_errors
"""

from loguru import logger

from app.utils import claude_client
from app.utils.claude_errors import ClaudeError

# Verbatim transcription fields only — mirrors QpSerialEntry's String(255) columns.
_ROW_FIELDS = ("purchase_order", "part_number", "serial_number", "seagate_sn", "tso", "customer_po")

# Both caps are generous for a real packing list; they bound one interactive call.
_MAX_TEXT_CHARS = 20_000
_MAX_ROWS = 300

SERIAL_PASTE_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "purchase_order": {"type": ["string", "null"], "description": "Our purchase order number"},
                    "part_number": {"type": ["string", "null"], "description": "Part number"},
                    "serial_number": {"type": ["string", "null"], "description": "Serial number, verbatim"},
                    "seagate_sn": {"type": ["string", "null"], "description": "Seagate SN if listed"},
                    "tso": {"type": ["string", "null"], "description": "TSO reference"},
                    "customer_po": {"type": ["string", "null"], "description": "Customer PO number"},
                },
            },
        },
    },
    "required": ["rows"],
}

SERIAL_PASTE_SYSTEM = """You extract serial-number tracking rows from packing list, test report, or shipment text.

Context: A buyer pasted text listing serialized units (drives, cards, FRUs). Each unit becomes one row.

Rules:
- Copy every value VERBATIM — exact characters, exact case. Never invent, complete, or "fix" a value.
- One row per serial number. If one PO/part covers many serials, repeat it on each serial's row.
- serial_number: the unit's serial. seagate_sn: only a value explicitly labeled Seagate SN.
- purchase_order: our PO number. customer_po: the customer's PO. tso: a TSO reference.
- Use null for anything the text does not state. Skip header/summary lines that describe no unit."""


async def parse_serial_paste(raw_text: str) -> list[dict] | None:
    """Parse pasted packing-list text into sanitized QP serial-row dicts.

    Returns [] for an empty paste (no AI call), None when the AI call fails, else a list
    of dicts keyed by _ROW_FIELDS (values str|None, stripped, ≤255 chars; rows with no
    values at all are dropped).
    """
    text = (raw_text or "").strip()[:_MAX_TEXT_CHARS]
    if not text:
        return []

    try:
        # Interactive HTMX caller: tightened single attempt so the request can't hang
        # for the timeout × retries worst case (claude_client P2.8 pattern).
        result = await claude_client.claude_structured(
            text,
            SERIAL_PASTE_SCHEMA,
            system=SERIAL_PASTE_SYSTEM,
            model_tier="fast",
            max_tokens=8192,
            timeout=60,
            max_attempts=1,
            cost_bucket="qp_serial_paste",
        )
    except ClaudeError as e:
        logger.warning("QP serial paste parse failed: {}", e)
        return None
    if result is None:
        return None

    rows: list[dict] = []
    for raw_row in (result.get("rows") or [])[:_MAX_ROWS]:
        if not isinstance(raw_row, dict):
            continue
        row: dict = {}
        for field in _ROW_FIELDS:
            value = raw_row.get(field)
            if value is not None:
                value = str(value).strip()[:255] or None
            row[field] = value
        if any(row.values()):
            rows.append(row)
    return rows
