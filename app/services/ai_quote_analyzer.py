"""AI Quote Analyzer — Compare vendor quotes and recommend best option.

Purpose:
  Given multiple quotes for the same part, generate a comparison summary
  with recommendation, risk factors, and best-in-category picks.

Design rules:
  - Returns structured comparison dict, or None on failure
  - Factors: price, lead time, vendor reliability, date code freshness,
    MOQ vs required qty, condition
  - Flags anomalies: prices far from median, suspicious date codes
  - Temperature 0.2 for consistent analytical output

Called by: routers/ai.py (POST /api/ai/compare-quotes)
Depends on: services/gradient_service.py
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.services.gradient_service import gradient_json

SYSTEM_PROMPT = """\
You are an expert electronic component procurement analyst for an independent \
distributor (broker). You compare vendor quotes and provide actionable recommendations.

Analysis factors (in priority order):
1. Unit price — lower is better, but suspiciously low prices are a red flag
2. Vendor reliability — higher vendor_score (0-100) means more trustworthy
3. Lead time — shorter is better; factor in buyer's urgency
4. Date code — newer is better; flag anything older than 3 years
5. MOQ vs required quantity — penalize if MOQ far exceeds need
6. Condition — "New" preferred; "Refurbished" or "Used" needs flagging
7. Quantity available — must meet or exceed required quantity

Risk flags to check:
- Price >40% below median = possible counterfeit or gray market
- Price >60% above median = overpriced
- Date code older than 3 years = aging stock risk
- Condition not "New" = reliability concern
- MOQ > 2x required quantity = excess inventory risk
- Very short lead time + low price = suspicious

Return ONLY valid JSON:
{
  "summary": "2-3 sentence overview of the quotes landscape",
  "recommendation": "Which vendor to go with and why (1-2 sentences)",
  "risk_factors": ["list of risk flags found, if any"],
  "best_price": {"vendor": "name", "unit_price": 0.00, "reason": "..."},
  "fastest_delivery": {"vendor": "name", "lead_time_days": 0, "reason": "..."},
  "best_overall": {"vendor": "name", "reason": "why this is the best choice"},
  "anomalies": ["list of pricing or quality anomalies detected"]
}"""


async def compare_quotes(
    part_number: str,
    quotes: list[dict[str, Any]],
    required_qty: int | None = None,
) -> dict | None:
    """Compare multiple vendor quotes and generate recommendation.

    Args:
        part_number: The part number being quoted.
        quotes: List of quote dicts, each with:
            vendor_name, vendor_score, unit_price, currency,
            quantity_available, lead_time_days, date_code,
            condition, moq.
        required_qty: Quantity the buyer actually needs (for MOQ analysis).

    Returns:
        Comparison dict with summary, recommendation, risk_factors,
        best_price, fastest_delivery, best_overall, anomalies.
        None on failure.
    """
    if len(quotes) < 2:
        logger.info("compare_quotes called with <2 quotes, skipping")
        return None

    quotes_section = _format_quotes(quotes)

    prompt = f"Part number: {part_number}\n"
    if required_qty:
        prompt += f"Required quantity: {required_qty}\n"
    prompt += f"Number of quotes: {len(quotes)}\n\n"
    prompt += f"Quotes:\n{quotes_section}\n\n"
    prompt += "Analyze these quotes and provide your recommendation."

    result = await gradient_json(
        prompt,
        system=SYSTEM_PROMPT,
        model_tier="default",
        max_tokens=600,
        temperature=0.2,
        timeout=30,
    )

    if not result or not isinstance(result, dict):
        logger.warning("Quote analyzer returned no result or invalid format")
        return None

    return _validate_result(result, quotes)


def _format_quotes(quotes: list[dict[str, Any]]) -> str:
    """Format quotes into a readable list for the LLM prompt."""
    lines = []
    for i, q in enumerate(quotes[:15], 1):  # Cap at 15 quotes
        vendor = q.get("vendor_name", "Unknown")
        price = q.get("unit_price")
        currency = q.get("currency", "USD")
        qty = q.get("quantity_available")
        lead = q.get("lead_time_days")
        dc = q.get("date_code")
        cond = q.get("condition")
        moq = q.get("moq")
        score = q.get("vendor_score")

        line = f"{i}. {vendor}"
        if score is not None:
            line += f" (reliability: {score}/100)"
        if price is not None:
            line += f" — {currency} {price:.4f}/unit"
        if qty is not None:
            line += f", {qty} available"
        if lead is not None:
            line += f", {lead} day lead"
        if dc:
            line += f", DC: {dc}"
        if cond:
            line += f", {cond}"
        if moq is not None:
            line += f", MOQ: {moq}"

        lines.append(line)

    return "\n".join(lines)


def _validate_result(result: dict, quotes: list[dict]) -> dict:
    """Validate and clean up LLM comparison result."""
    clean = {
        "summary": str(result.get("summary", "")),
        "recommendation": str(result.get("recommendation", "")),
        "risk_factors": result.get("risk_factors", []),
        "best_price": result.get("best_price"),
        "fastest_delivery": result.get("fastest_delivery"),
        "best_overall": result.get("best_overall"),
        "anomalies": result.get("anomalies", []),
        "quote_count": len(quotes),
    }

    # Ensure list fields are lists
    if not isinstance(clean["risk_factors"], list):
        clean["risk_factors"] = []
    if not isinstance(clean["anomalies"], list):
        clean["anomalies"] = []

    # Ensure dict fields are dicts or None
    for key in ("best_price", "fastest_delivery", "best_overall"):
        if clean[key] is not None and not isinstance(clean[key], dict):
            clean[key] = None

    return clean
