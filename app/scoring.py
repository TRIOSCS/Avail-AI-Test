"""Lead Opportunity Score — ranks vendor sightings by weighted factors.

Scores 0-100 based on available data:
  Recency(30) + Quantity(20) + Source credibility(20) + Completeness(10) + Vendor reliability(10) + Price(10)
"""

import math
from datetime import datetime, timezone

SOURCE_SCORES = {
    "nexar": 85,
    "octopart": 85,
    "digikey": 90,
    "mouser": 90,
    "ebay": 60,
    "oemsecrets": 80,
    "sourcengine": 80,
    "brokerbin": 75,
    "upload": 70,
    "email": 95,
    "manual": 60,
}


def score_sighting(sighting, target_qty: int, weights: dict) -> float:
    """Score a Sighting object. Returns 0-100."""
    scores = {}

    # Recency — newer is better (exponential decay)
    if sighting.created_at:
        days = max(
            0,
            (datetime.now(timezone.utc) - sighting.created_at).total_seconds() / 86400,
        )
        scores["recency"] = max(5, 100 * math.exp(-0.012 * days))
    else:
        scores["recency"] = 50

    # Quantity — does it meet target?
    qty = sighting.qty_available or 0
    if qty <= 0:
        scores["quantity"] = 10
    elif target_qty and qty >= target_qty:
        scores["quantity"] = min(100, 70 + 30 * min(qty / target_qty, 3) / 3)
    elif qty >= 10000:
        scores["quantity"] = 85
    elif qty >= 1000:
        scores["quantity"] = 65
    elif qty >= 100:
        scores["quantity"] = 45
    else:
        scores["quantity"] = 25

    # Source credibility
    scores["source_credibility"] = SOURCE_SCORES.get(sighting.source_type or "", 50)

    # Data completeness — how much info do we have?
    fields = [
        sighting.unit_price,
        sighting.qty_available,
        sighting.manufacturer,
        sighting.vendor_email or sighting.vendor_phone,
        sighting.moq,
    ]
    scores["data_completeness"] = sum(20 for f in fields if f) or 10

    # Authorized distributor bonus
    scores["vendor_reliability"] = 90 if sighting.is_authorized else 50

    # Price — lower is better (needs context, default to neutral)
    scores["price"] = 60 if sighting.unit_price else 30

    # Weighted total
    total = (
        scores["recency"] * weights.get("recency", 30) / 100
        + scores["quantity"] * weights.get("quantity", 20) / 100
        + scores["source_credibility"] * weights.get("source_credibility", 20) / 100
        + scores["data_completeness"] * weights.get("data_completeness", 10) / 100
        + scores["vendor_reliability"] * weights.get("vendor_reliability", 10) / 100
        + scores["price"] * weights.get("price", 10) / 100
    )

    return max(0, min(100, round(total, 1)))
