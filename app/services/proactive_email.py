"""Proactive email drafter — AI-generated sales emails for proactive offers.

Uses Claude (via gradient_service) to draft personalized emails when
salespeople want to proactively offer parts to customers based on
purchase history matches.

Called by: routers/proactive.py (POST /api/proactive/draft)
Depends on: services/gradient_service.py
"""

from __future__ import annotations

import html as html_mod
from typing import Any

from loguru import logger

from app.services.gradient_service import gradient_json

SYSTEM_PROMPT = """\
You are a professional sales email writer for Trio Supply Chain Solutions, \
an independent electronic component distributor.

Context: A salesperson wants to proactively reach out to a customer who has \
purchased certain parts before. We now have those parts in stock and want \
to offer them.

Writing rules:
- Professional, warm, concise — 3-5 sentences of body text
- Reference the customer's history naturally: "I noticed you've sourced these \
  components before" or "Based on your previous orders"
- Create urgency without pressure: "wanted to give you first look" or \
  "limited availability"
- NEVER reveal our cost or margin
- NEVER mention specific competitor names
- Include the parts table (will be inserted separately — just reference it)
- No greeting line (will be added separately based on contact name)
- No signature (will be added separately)
- Keep it natural — don't sound like a template
- If notes are provided, weave them into the email naturally

Return ONLY valid JSON:
{
  "subject": "Brief, compelling subject line",
  "body": "The email body text (plain text, 3-5 sentences)"
}"""


async def draft_proactive_email(
    company_name: str,
    contact_name: str | None,
    parts: list[dict[str, Any]],
    salesperson_name: str = "",
    notes: str | None = None,
) -> dict | None:
    """Generate an AI-drafted proactive offer email.

    Args:
        company_name: Customer company name.
        parts: List of part dicts with: mpn, manufacturer, qty, sell_price,
               customer_purchase_count, customer_last_purchased_at.
        contact_name: Primary contact's first name (for greeting).
        salesperson_name: Name of the salesperson sending.
        notes: Optional notes to weave into the email.

    Returns:
        Dict with "subject", "body", "html" keys, or None on failure.
    """
    if not parts:
        return None

    parts_section = _format_parts(parts)

    prompt = f"Customer: {company_name}\n"
    if contact_name:
        prompt += f"Contact: {contact_name}\n"
    prompt += f"Salesperson: {salesperson_name or 'the salesperson'}\n\n"
    prompt += f"Parts to offer:\n{parts_section}\n\n"
    if notes:
        prompt += f"Additional notes from salesperson: {notes}\n\n"
    prompt += "Draft a proactive sales email offering these parts."

    result = await gradient_json(
        prompt,
        system=SYSTEM_PROMPT,
        model_tier="default",
        max_tokens=600,
        temperature=0.6,
        timeout=30,
    )

    if not result or not isinstance(result, dict):
        logger.warning("Proactive email drafter returned no result")
        return _fallback_draft(company_name, contact_name, parts, salesperson_name, notes)

    subject = result.get("subject", "").strip()
    body = result.get("body", "").strip()

    if not body:
        return _fallback_draft(company_name, contact_name, parts, salesperson_name, notes)

    if not subject:
        subject = f"Parts Available — {company_name}"

    html_body = _build_html(body, contact_name, parts, salesperson_name, notes)

    return {"subject": subject, "body": body, "html": html_body}


def _fallback_draft(
    company_name: str,
    contact_name: str | None,
    parts: list[dict],
    salesperson_name: str,
    notes: str | None,
) -> dict:
    """Template-based fallback when AI is unavailable."""
    mpns = ", ".join(p.get("mpn", "?") for p in parts[:3])
    if len(parts) > 3:
        mpns += f" +{len(parts) - 3} more"

    body = (
        "I wanted to reach out — we've just secured inventory on several "
        "parts you've sourced from us before. Given your history with these "
        "components, I wanted to give you first look before they're allocated "
        "elsewhere. Please see the details below and let me know if any "
        "quantities or pricing work for you."
    )

    subject = f"Parts Available — {company_name}"
    html_body = _build_html(body, contact_name, parts, salesperson_name, notes)

    return {"subject": subject, "body": body, "html": html_body}


def _build_html(
    body: str,
    contact_name: str | None,
    parts: list[dict],
    salesperson_name: str,
    notes: str | None,
) -> str:
    """Build the full HTML email from body text + parts table."""
    greeting = f"Hi {html_mod.escape(str(contact_name))}," if contact_name else "Hello,"

    rows_html = ""
    for item in parts:
        rows_html += f"""<tr>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(item.get("mpn", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(item.get("manufacturer", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right">{item.get("qty", 0):,}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb;text-align:right">${item.get("sell_price", 0):.4f}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(item.get("condition", "")))}</td>
            <td style="padding:6px 10px;border:1px solid #e5e7eb">{html_mod.escape(str(item.get("lead_time", "")))}</td>
        </tr>"""

    body_html = html_mod.escape(body).replace("\n", "<br>")
    notes_html = f'<p style="margin-top:12px">{html_mod.escape(str(notes))}</p>' if notes else ""
    safe_name = html_mod.escape(str(salesperson_name)) if salesperson_name else "Trio Supply Chain Solutions"

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:700px">
        <p>{greeting}</p>
        <p>{body_html}</p>
        <table style="border-collapse:collapse;width:100%;margin:16px 0">
            <thead><tr style="background:#f3f4f6">
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Part Number</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Manufacturer</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Qty Available</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:right">Unit Price</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Condition</th>
                <th style="padding:8px 10px;border:1px solid #e5e7eb;text-align:left">Lead Time</th>
            </tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        {notes_html}
        <p>Best regards,<br>{safe_name}<br>Trio Supply Chain Solutions</p>
    </div>
    """


def _format_parts(parts: list[dict[str, Any]]) -> str:
    """Format parts into a readable list for the LLM prompt."""
    lines = []
    for i, p in enumerate(parts[:15], 1):
        mpn = p.get("mpn", "Unknown")
        mfr = p.get("manufacturer")
        qty = p.get("qty", "?")
        sell = p.get("sell_price")

        line = f"{i}. {mpn}"
        if mfr:
            line += f" ({mfr})"
        line += f" — Qty: {qty}"
        if sell is not None:
            line += f", Price: ${sell:.4f}"

        count = p.get("customer_purchase_count")
        if count:
            line += f", Customer bought {count}x before"

        last = p.get("customer_last_purchased_at")
        if last:
            line += f", Last purchased: {last}"

        lines.append(line)

    return "\n".join(lines)
