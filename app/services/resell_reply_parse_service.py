"""resell_reply_parse_service.py — AI draft of offer lines from a buyer's resell reply.

Purpose:
  The on-demand "Draft offer from reply" button on the resell reply viewer. The
  buyer's inbound bid email (already matched to the outreach by the inbox poll's
  Tier 2.5 pass, which DELIBERATELY leaves it unparsed) goes through one
  claude_structured call scoped to the list's line MPNs. The result renders as
  tappable draft rows that pre-fill the existing convert-to-offer form — the
  trader reviews and submits each line themselves. This module never writes to
  the database, and the inbox-mining >=0.8 auto-create path stays untouched:
  manual extraction there is a recorded design decision.

Called by: routers/resell.py (POST /v2/partials/resell/{list}/outreach/{o}/parse-reply)
Depends on: utils/claude_client, utils/claude_errors, utils/normalization
"""

import re

from loguru import logger

from app.utils import claude_client
from app.utils.claude_errors import ClaudeError
from app.utils.normalization import normalize_mpn_key

_MAX_TEXT_CHARS = 20_000
_MAX_LINES = 100

REPLY_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "take_all": {
            "type": ["boolean", "null"],
            "description": "True only if the buyer clearly bids on the WHOLE list, not specific lines",
        },
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": ["string", "null"], "description": "Part number the bid line refers to, verbatim"},
                    "quantity": {"type": ["integer", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "lead_time_days": {"type": ["integer", "null"]},
                    "terms": {"type": ["string", "null"], "description": "Payment/packaging/date-code terms stated"},
                    "notes": {"type": ["string", "null"], "description": "Other context from the reply for this line"},
                },
            },
        },
    },
    "required": ["lines"],
}

REPLY_PARSE_SYSTEM = """You extract a buyer's bid from their reply to an excess-inventory offer list.

Context: We offered a list of excess parts; the buyer replied with a bid — often terse, phone-typed, one or two lines.
The user message starts with the list's part numbers, then the reply text.

Rules:
- One line per part the buyer bids on. mpn verbatim as the buyer wrote it (match to the list's part numbers when clearly intended).
- quantity / unit_price / lead_time_days: only when stated. NEVER invent or infer a price.
- take_all: true ONLY when the buyer clearly wants the entire list ("take them all", "whole lot").
- terms: payment/packaging/date-code conditions stated. notes: any other per-line context.
- Empty lines array when the reply contains no actual bid."""


def _clean_str(value: object, max_len: int = 255) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:max_len] or None


def _pos_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


async def parse_buyer_reply(reply_text: str, *, line_mpns: list[str]) -> dict | None:
    """Parse a buyer's reply into sanitized draft offer lines scoped to the list.

    Returns None when the AI call fails, else {"take_all": bool, "lines": [...]}
    where each line carries on_list (normalized-key membership in *line_mpns*) so
    the UI can flag bids that don't match any posted line. Never writes.
    """
    # Strip only tag-SHAPED spans (start with a letter or /letter, or a comment) so
    # plain-text angle-bracket content a buyer typed — "<$2.00 each>", "qty <500>" —
    # survives to the model. Pre-slice at 10× the cap bounds the regex scan on a
    # flooded thread; the real char budget is applied to the stripped text.
    text = re.sub(r"</?[A-Za-z][^>]*>|<!--.*?-->", " ", (reply_text or "")[: _MAX_TEXT_CHARS * 10])
    text = text.strip()[:_MAX_TEXT_CHARS]
    if not text:
        return None

    prompt = "List part numbers: " + ", ".join(line_mpns[:200]) + "\n\nBuyer reply:\n" + text
    try:
        # Interactive HTMX caller: single attempt, tightened timeout (P2.8 pattern).
        ai = await claude_client.claude_structured(
            prompt,
            REPLY_PARSE_SCHEMA,
            system=REPLY_PARSE_SYSTEM,
            model_tier="fast",
            max_tokens=4096,
            timeout=45,
            max_attempts=1,
            cost_bucket="resell_reply_parse",
        )
    except ClaudeError as e:
        logger.warning("resell reply parse failed: {}", e)
        return None
    if ai is None:
        return None

    list_keys = {normalize_mpn_key(m) for m in line_mpns if m}
    lines: list[dict] = []
    for raw in (ai.get("lines") or [])[:_MAX_LINES]:
        if not isinstance(raw, dict):
            continue
        mpn = _clean_str(raw.get("mpn"))
        if not mpn:
            continue
        price = raw.get("unit_price")
        lines.append(
            {
                "mpn": mpn,
                "quantity": _pos_int(raw.get("quantity")),
                "unit_price": float(price) if isinstance(price, (int, float)) and price >= 0 else None,
                "lead_time_days": _pos_int(raw.get("lead_time_days")),
                "terms": _clean_str(raw.get("terms")),
                "notes": _clean_str(raw.get("notes"), 1000),
                "on_list": normalize_mpn_key(mpn) in list_keys,
            }
        )
    return {"take_all": bool(ai.get("take_all")), "lines": lines}
