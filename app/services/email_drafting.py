"""Unified AI email drafting service.

A single dispatcher, :func:`draft_email`, powers three UI surfaces — RFQ rephrase,
vendor reply, and follow-up. Every path degrades gracefully when Claude is
unavailable so the calling UI never breaks: the user always ends up with an
editable draft (or, for vendor replies, ``None`` → a blank box to write in).

Drafts are advisory only. Nothing here sends mail; the caller routes the result
into an editable field behind the existing human-clicked send button.
"""

from typing import Any

from loguru import logger

from app.services.ai_service import rephrase_rfq
from app.utils.claude_client import claude_json, claude_text
from app.utils.claude_errors import ClaudeError

FAST = "fast"
_COST_BUCKET = "email_drafting"

_FOLLOW_UP_SYSTEM = (
    "You write short, professional follow-up emails for an electronic component "
    "broker chasing a vendor on an open RFQ. Be courteous and concise. Reference the "
    "parts and how long it has been. Do not invent prices, quantities, or "
    "commitments. Return only the email body — no subject line, no placeholders, no "
    "commentary."
)

_VENDOR_REPLY_SYSTEM = (
    "You draft a reply from an electronic component buyer to a vendor who has "
    "responded to an RFQ. Use only the facts provided — never invent prices, "
    "quantities, lead times, or commitments. Keep it concise and professional. "
    "Return JSON with two string fields: 'subject' and 'body'."
)

_QUAL_REQUEST_SYSTEM = (
    "You are a concise procurement buyer replying to a vendor to qualify an offer. "
    "Write a short, professional reply asking ONLY for the specific details listed — "
    "nothing more. Do not restate what you already know. Return JSON with two string "
    "fields: 'subject' and 'body'. Body is plain text, 2-5 sentences."
)


async def draft_email(kind: str, context: dict[str, Any]) -> dict | None:
    """Draft an email of ``kind`` from ``context``.

    Returns a dict with a ``body`` (and, for ``vendor_reply``, a ``subject``), or
    ``None`` when a vendor reply could not be drafted. Raises ``ValueError`` for an
    unknown ``kind`` (a programming error).
    """
    if kind == "rfq_rephrase":
        return await _draft_rfq_rephrase(context)
    if kind == "follow_up":
        return await _draft_follow_up(context)
    if kind == "vendor_reply":
        return await _draft_vendor_reply(context)
    if kind == "qual_request":
        return await _draft_qual_request(context)
    raise ValueError(f"Unknown draft kind: {kind!r}")


async def _draft_rfq_rephrase(context: dict[str, Any]) -> dict:
    body = (context.get("body") or "").strip()
    try:
        rephrased = await rephrase_rfq(body)
    except ClaudeError as exc:
        logger.warning("rfq_rephrase draft failed, keeping original: {}", exc)
        rephrased = None
    # Never lose the user's text — fall back to what they already wrote.
    return {"body": rephrased or body}


async def _draft_follow_up(context: dict[str, Any]) -> dict:
    vendor_name = context.get("vendor_name") or "there"
    parts_text = _format_parts(context.get("parts"))
    days_waiting = context.get("days_waiting")
    prompt = (
        f"Vendor: {vendor_name}\n"
        f"Parts we inquired about: {parts_text}\n"
        f"Days since our last message: "
        f"{days_waiting if days_waiting is not None else 'a few'}\n\n"
        "Draft a brief, polite follow-up asking whether they have availability and "
        "pricing."
    )
    try:
        body = await claude_text(
            prompt,
            system=_FOLLOW_UP_SYSTEM,
            model_tier=FAST,
            max_tokens=400,
            cost_bucket=_COST_BUCKET,
        )
    except ClaudeError as exc:
        logger.info("follow_up draft falling back to template: {}", exc)
        body = None
    if not body or not body.strip():
        body = _follow_up_fallback(vendor_name)
    return {"body": body.strip()}


async def _draft_vendor_reply(context: dict[str, Any]) -> dict | None:
    vendor_name = context.get("vendor_name") or "there"
    classification = context.get("classification") or "quote_provided"
    facts = _format_offer_facts(context)
    subject = context.get("subject") or "RFQ"
    prompt = (
        f"Vendor: {vendor_name}\n"
        f"Their reply was classified as: {classification}\n"
        f"What they told us: {facts}\n"
        f"Original subject: {subject}\n\n"
        "Draft our reply to the vendor appropriate to that classification "
        "(accept, clarify, or counter as fitting). Return JSON {subject, body}."
    )
    try:
        result = await claude_json(
            prompt,
            system=_VENDOR_REPLY_SYSTEM,
            model_tier=FAST,
            max_tokens=600,
            cost_bucket=_COST_BUCKET,
        )
    except ClaudeError as exc:
        logger.warning("vendor_reply draft failed: {}", exc)
        return None
    if not isinstance(result, dict) or not result.get("body"):
        return None
    reply_subject = str(result.get("subject") or _reply_subject(subject)).strip()
    return {"subject": reply_subject, "body": str(result["body"]).strip()}


async def _draft_qual_request(context: dict[str, Any]) -> dict | None:
    vendor_name = context.get("vendor_name") or "there"
    mpn = context.get("mpn") or ""
    subject = _reply_subject(context.get("subject") or "RFQ")
    items = [str(i).strip() for i in (context.get("items_requested") or []) if str(i).strip()]
    items_str = "\n".join(f"- {i}" for i in items) if items else "- (no items specified)"
    prompt = (
        f"Vendor: {vendor_name}\n"
        f"Part: {mpn}\n"
        f"Details we still need to qualify this offer:\n{items_str}\n\n"
        "Write the reply asking only for these items."
    )
    try:
        result = await claude_json(
            prompt,
            system=_QUAL_REQUEST_SYSTEM,
            model_tier=FAST,
            max_tokens=400,
            cost_bucket=_COST_BUCKET,
        )
    except ClaudeError as exc:
        logger.warning("qual_request draft failed: {}", exc)
        return None
    if not isinstance(result, dict) or not result.get("body"):
        return None
    return {"subject": subject, "body": str(result["body"]).strip()}


def _format_parts(parts: Any) -> str:
    if not parts:
        return "the requested parts"
    if isinstance(parts, str):
        return parts
    if isinstance(parts, (list, tuple)):
        names = []
        for p in parts:
            if isinstance(p, dict):
                names.append(str(p.get("mpn") or p.get("name") or "").strip())
            else:
                names.append(str(p).strip())
        names = [n for n in names if n]
        return ", ".join(names) if names else "the requested parts"
    return str(parts)


def _format_offer_facts(context: dict[str, Any]) -> str:
    bits = []
    for key, label in (
        ("mpn", "MPN"),
        ("qty", "qty"),
        ("price", "price"),
        ("lead_time", "lead time"),
    ):
        val = context.get(key)
        if val not in (None, ""):
            bits.append(f"{label}={val}")
    return "; ".join(bits) if bits else "no structured details parsed"


def _reply_subject(subject: str) -> str:
    s = (subject or "RFQ").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _follow_up_fallback(vendor_name: str) -> str:
    return (
        f"Dear {vendor_name},\n\n"
        "I'm following up on our previous inquiry. Please let us know if you have "
        "availability.\n\nThank you."
    )
