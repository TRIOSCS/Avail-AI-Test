"""AI Email Drafter — Auto-generate RFQ emails to send to vendors.

Purpose:
  Generate professional, concise RFQ emails based on requisition data.
  Includes part details, quantity, target price (tactfully), date code
  requirements, and condition constraints.

Design rules:
  - Returns subject + body dict, or None on failure
  - Tone: professional, concise — vendors get hundreds of RFQ emails
  - If target price provided, mention tactfully (not aggressive)
  - Never auto-send — always returns draft for human review
  - Temperature 0.6 for natural-sounding generation

Called by: routers/ai.py (POST /api/ai/draft-rfq)
Depends on: services/gradient_service.py
"""

from __future__ import annotations

from datetime import date
from typing import Any

from loguru import logger

from app.services.gradient_service import gradient_json

SYSTEM_PROMPT = """\
You are a professional RFQ email writer for an electronic component broker.

Context: The buyer works for Trio Supply Chain Solutions, an independent \
distributor specializing in hard-to-find electronic components.

Writing rules:
- Professional but concise — vendors receive hundreds of RFQs daily
- 3-5 sentences of body text plus a parts table/list
- Always request: pricing, availability, lead time, date code, condition
- If a target price is given, phrase it tactfully: "Our target is around $X" \
  or "We've seen pricing around $X" — never demand or pressure
- If date code or condition requirements exist, state them clearly but politely
- If a delivery deadline exists, mention it naturally
- No greeting line (will be added separately based on contact name)
- No signature (will be added separately)
- Keep it natural — don't sound like a template

Return ONLY valid JSON:
{
  "subject": "RFQ: [part numbers or brief description]",
  "body": "The email body text"
}"""


async def draft_rfq_email(
    vendor_name: str,
    parts: list[dict[str, Any]],
    buyer_name: str = "",
    vendor_contact_name: str | None = None,
) -> dict | None:
    """Generate an RFQ email draft for a vendor.

    Args:
        vendor_name: Target vendor company name.
        parts: List of part detail dicts, each with:
            part_number, manufacturer, quantity, target_price,
            date_code_requirement, condition_requirement,
            delivery_deadline, additional_notes.
        buyer_name: Name of the buyer sending the email.
        vendor_contact_name: Vendor contact's first name (for greeting).

    Returns:
        Dict with "subject" and "body" keys, or None on failure.
    """
    if not parts:
        return None

    parts_section = _format_parts(parts)

    prompt = f"Vendor: {vendor_name}\n"
    if vendor_contact_name:
        prompt += f"Contact: {vendor_contact_name}\n"
    prompt += f"Buyer: {buyer_name or 'the buyer'}\n\n"
    prompt += f"Parts requested:\n{parts_section}\n\n"
    prompt += "Draft an RFQ email requesting pricing and availability."

    result = await gradient_json(
        prompt,
        system=SYSTEM_PROMPT,
        model_tier="default",
        max_tokens=800,
        temperature=0.6,
        timeout=30,
    )

    if not result or not isinstance(result, dict):
        logger.warning("Email drafter returned no result or invalid format")
        return None

    subject = result.get("subject", "").strip()
    body = result.get("body", "").strip()

    if not body:
        return None

    # Add greeting if contact name provided
    if vendor_contact_name:
        body = f"Hi {vendor_contact_name},\n\n{body}"
    else:
        body = f"Hello,\n\n{body}"

    # Add sign-off
    if buyer_name:
        body += f"\n\nBest regards,\n{buyer_name}"

    # Default subject if LLM didn't provide one
    if not subject:
        mpns = [p.get("part_number", "?") for p in parts[:3]]
        subject = f"RFQ: {', '.join(mpns)}"
        if len(parts) > 3:
            subject += f" +{len(parts) - 3} more"

    return {"subject": subject, "body": body}


def _format_parts(parts: list[dict[str, Any]]) -> str:
    """Format parts into a readable list for the LLM prompt."""
    lines = []
    for i, p in enumerate(parts[:20], 1):  # Cap at 20 parts per email
        mpn = p.get("part_number", "Unknown")
        mfr = p.get("manufacturer")
        qty = p.get("quantity", "?")

        line = f"{i}. {mpn}"
        if mfr:
            line += f" ({mfr})"
        line += f" — Qty: {qty}"

        if p.get("target_price") is not None:
            line += f", Target: ${p['target_price']:.2f}"
        if p.get("date_code_requirement"):
            line += f", DC req: {p['date_code_requirement']}"
        if p.get("condition_requirement"):
            line += f", Condition: {p['condition_requirement']}"
        if p.get("delivery_deadline"):
            dl = p["delivery_deadline"]
            if isinstance(dl, date):
                line += f", Need by: {dl.isoformat()}"
            else:
                line += f", Need by: {dl}"
        if p.get("additional_notes"):
            line += f" [{p['additional_notes']}]"

        lines.append(line)

    return "\n".join(lines)
