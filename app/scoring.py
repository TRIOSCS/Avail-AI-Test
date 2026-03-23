"""Sighting Score — buyer-usefulness-oriented multi-factor scoring.

Optimized for lead usefulness: a smaller number of strong, explainable
leads beats a large number of weak ones.  Missing data is penalized (not
neutral) because a buyer can't act on a lead that has no price or qty.

score_sighting_v2() weights trust, price, quantity, freshness, and
completeness.  classify_lead() and explain_lead() add human-readable
quality labels and plain-English reasons a buyer should care.

score_unified() provides a single scoring entry point for all search
result types (live API, historical, vendor affinity, AI research),
returning a normalised confidence percentage, color, and source badge.

Called by: search_service._save_sightings(), sighting_to_dict(),
           search_service.search_requirement()
Depends on: nothing (pure logic)
"""

from loguru import logger

NEW_VENDOR_BASELINE = 35.0

MISSING_DATA_SCORE = 25.0

WEAK_LEAD_THRESHOLD = 30.0


def score_sighting(vendor_score: float | None, is_authorized: bool) -> float:
    """Score a sighting based on the vendor's unified score.

    Returns 0-100.
    """
    if is_authorized:
        return 100.0
    if vendor_score is None:
        return NEW_VENDOR_BASELINE
    return round(vendor_score, 1)


def score_sighting_v2(
    vendor_score: float | None,
    is_authorized: bool,
    unit_price: float | None = None,
    median_price: float | None = None,
    qty_available: int | None = None,
    target_qty: int | None = None,
    age_hours: float | None = None,
    has_price: bool = False,
    has_qty: bool = False,
    has_lead_time: bool = False,
    has_condition: bool = False,
) -> tuple[float, dict]:
    """Multi-factor sighting score with explainable components.

    Missing data is penalized (25/100) rather than treated as neutral (50), because a
    buyer can't act on a lead without price or quantity info.

    Returns (total_score, {"trust": .., "price": .., "qty": .., "freshness": ..,
    "completeness": ..}).
    """
    if is_authorized:
        trust = 95.0
    elif vendor_score is not None:
        trust = vendor_score
    else:
        trust = NEW_VENDOR_BASELINE

    if unit_price and median_price and median_price > 0 and unit_price > 0:
        ratio = median_price / unit_price
        price_f = min(100.0, max(0.0, ratio * 50.0))
    else:
        price_f = MISSING_DATA_SCORE

    if qty_available is not None and target_qty and target_qty > 0:
        coverage = min(1.0, qty_available / target_qty)
        qty_f = coverage * 100.0
    elif qty_available is not None and qty_available > 0:
        qty_f = 60.0
    else:
        qty_f = MISSING_DATA_SCORE

    if age_hours is not None:
        freshness = max(0.0, 100.0 - (age_hours / 24.0) * 5.0)
    else:
        freshness = MISSING_DATA_SCORE

    fields_present = sum(1 for f in [has_price, has_qty, has_lead_time, has_condition] if f)
    completeness = (fields_present / 4.0) * 100.0

    total = trust * 0.30 + price_f * 0.25 + qty_f * 0.20 + freshness * 0.15 + completeness * 0.10
    components = {
        "trust": round(trust, 1),
        "price": round(price_f, 1),
        "qty": round(qty_f, 1),
        "freshness": round(freshness, 1),
        "completeness": round(completeness, 1),
    }
    return round(total, 1), components


def classify_lead(
    score: float,
    is_authorized: bool = False,
    has_price: bool = False,
    has_qty: bool = False,
    has_contact: bool = False,
    evidence_tier: str | None = None,
) -> str:
    """Classify a lead as 'strong', 'moderate', or 'weak' from a buyer's perspective.

    Strong = buyer should act on this now (has actionable data). Moderate = worth
    reviewing but missing something. Weak = noise — unlikely to result in a successful
    purchase.
    """
    if is_authorized and has_price:
        return "strong"

    tier = (evidence_tier or "").upper()

    actionable_fields = sum([has_price, has_qty, has_contact])

    if score >= 55 and actionable_fields >= 2:
        return "strong"

    if score >= 40 and actionable_fields >= 1:
        return "moderate"

    if tier in ("T1", "T2") and score >= 35:
        return "moderate"

    return "weak"


def explain_lead(
    vendor_name: str | None,
    is_authorized: bool = False,
    vendor_score: float | None = None,
    unit_price: float | None = None,
    median_price: float | None = None,
    qty_available: int | None = None,
    target_qty: int | None = None,
    has_contact: bool = False,
    evidence_tier: str | None = None,
    source_type: str | None = None,
    age_days: int | None = None,
) -> str:
    """One-line plain-English explanation of why this lead matters (or doesn't).

    Buyers should be able to glance at this and know whether to pursue.
    """
    parts: list[str] = []
    vendor = vendor_name or "Unknown vendor"

    if is_authorized:
        parts.append(f"{vendor} (authorized distributor)")
    elif vendor_score is not None and vendor_score >= 66:
        parts.append(f"{vendor} (proven vendor, score {int(vendor_score)})")
    elif vendor_score is not None and vendor_score >= 33:
        parts.append(f"{vendor} (developing vendor, score {int(vendor_score)})")
    else:
        parts.append(vendor)

    if unit_price is not None and qty_available is not None:
        qty_str = f"{qty_available:,}"
        price_str = f"${unit_price:.4f}" if unit_price < 1 else f"${unit_price:.2f}"
        parts.append(f"has {qty_str} pcs at {price_str}")
    elif qty_available is not None:
        parts.append(f"has {qty_available:,} pcs (no price listed)")
    elif unit_price is not None:
        price_str = f"${unit_price:.4f}" if unit_price < 1 else f"${unit_price:.2f}"
        parts.append(f"listed at {price_str} (qty unknown)")

    if unit_price and median_price and median_price > 0:
        pct = ((unit_price - median_price) / median_price) * 100
        if pct <= -10:
            parts.append(f"{abs(int(pct))}% below market")
        elif pct >= 20:
            parts.append(f"{int(pct)}% above market")

    if target_qty and qty_available is not None:
        coverage = qty_available / target_qty
        if coverage >= 1.0:
            parts.append("covers full order qty")
        elif coverage >= 0.5:
            parts.append(f"covers {int(coverage * 100)}% of order qty")

    if has_contact:
        parts.append("contact info available")
    elif not is_authorized:
        parts.append("no contact info")

    if age_days is not None and age_days > 30:
        parts.append(f"data is {age_days} days old")

    return " \u2014 ".join(parts)


def is_weak_lead(
    score: float,
    is_authorized: bool = False,
    has_price: bool = False,
    has_qty: bool = False,
    evidence_tier: str | None = None,
) -> bool:
    """True if this lead is too weak to show buyers. Prevents noise.

    Authorized distributor results are never filtered out. T1/T2 results are kept if
    they have any data. Everything else needs to clear the score threshold.
    """
    if is_authorized:
        return False

    tier = (evidence_tier or "").upper()

    if tier in ("T1", "T2") and (has_price or has_qty):
        return False

    if score < WEAK_LEAD_THRESHOLD and not has_price and not has_qty:
        return True

    return False


# ---------------------------------------------------------------------------
# Unified confidence scoring
# ---------------------------------------------------------------------------


def confidence_color(pct: int) -> str:
    """Map a confidence percentage to a traffic-light color string.

    >= 75 → "green", >= 50 → "amber", else → "red".
    """
    if pct >= 75:
        return "green"
    if pct >= 50:
        return "amber"
    return "red"


def score_unified(
    source_type: str,
    vendor_score: float | None = None,
    is_authorized: bool = False,
    unit_price: float | None = None,
    median_price: float | None = None,
    qty_available: int | None = None,
    target_qty: int | None = None,
    age_hours: float | None = None,
    has_price: bool = False,
    has_qty: bool = False,
    has_lead_time: bool = False,
    has_condition: bool = False,
    repeat_sighting_count: int = 0,
    claude_confidence: float | None = None,
) -> dict:
    """Unified confidence scoring across all search result types.

    Returns a dict with:
        score           – raw float (0-100)
        source_badge    – human-readable badge string
        confidence_pct  – integer 0-100
        confidence_color– "green" / "amber" / "red"
        components      – breakdown dict (varies by source type)
    """
    st = (source_type or "").lower()

    # -- Live API results -------------------------------------------------
    if st not in ("historical", "vendor_affinity", "ai_live_web"):
        raw_score, components = score_sighting_v2(
            vendor_score=vendor_score,
            is_authorized=is_authorized,
            unit_price=unit_price,
            median_price=median_price,
            qty_available=qty_available,
            target_qty=target_qty,
            age_hours=age_hours,
            has_price=has_price,
            has_qty=has_qty,
            has_lead_time=has_lead_time,
            has_condition=has_condition,
        )
        # Map the 0-100 raw score into the 70-95 confidence range
        pct = int(70 + (raw_score / 100.0) * 25)
        pct = max(70, min(95, pct))
        logger.debug("score_unified live: raw={} pct={}", raw_score, pct)
        return {
            "score": raw_score,
            "source_badge": "Live Stock",
            "confidence_pct": pct,
            "confidence_color": confidence_color(pct),
            "components": components,
        }

    # -- Historical sightings ---------------------------------------------
    if st == "historical":
        base = 80.0
        # Decay 5% per month (30 * 24 = 720 hours)
        if age_hours is not None and age_hours > 0:
            months_old = age_hours / 720.0
            base = base - (5.0 * months_old)
        # Repeat-sighting boost: +2% each, max +10%
        boost = min(10.0, repeat_sighting_count * 2.0)
        raw = max(0.0, min(100.0, base + boost))
        pct = int(round(raw))
        pct = max(0, min(100, pct))
        logger.debug(
            "score_unified historical: base={} boost={} pct={}",
            round(base, 1),
            boost,
            pct,
        )
        return {
            "score": round(raw, 1),
            "source_badge": "Historical",
            "confidence_pct": pct,
            "confidence_color": confidence_color(pct),
            "components": {
                "base": 80.0,
                "age_decay": round(80.0 - base, 1),
                "repeat_boost": boost,
            },
        }

    # -- Vendor affinity --------------------------------------------------
    if st == "vendor_affinity":
        conf = (claude_confidence or 0.0) * 100.0
        pct = int(round(max(0.0, min(100.0, conf))))
        logger.debug("score_unified vendor_affinity: pct={}", pct)
        return {
            "score": round(conf, 1),
            "source_badge": "Vendor Match",
            "confidence_pct": pct,
            "confidence_color": confidence_color(pct),
            "components": {"claude_confidence": claude_confidence or 0.0},
        }

    # -- AI research ------------------------------------------------------
    if st == "ai_live_web":
        conf = (claude_confidence or 0.0) * 100.0
        capped = min(60.0, conf)
        pct = int(round(max(0.0, capped)))
        logger.debug("score_unified ai_live_web: raw={} capped={}", conf, pct)
        return {
            "score": round(capped, 1),
            "source_badge": "AI Found",
            "confidence_pct": pct,
            "confidence_color": confidence_color(pct),
            "components": {
                "claude_confidence": claude_confidence or 0.0,
                "capped_at": 60,
            },
        }

    # Fallback (shouldn't happen)
    logger.warning("score_unified: unknown source_type={}", source_type)
    return {
        "score": 0.0,
        "source_badge": source_type,
        "confidence_pct": 0,
        "confidence_color": "red",
        "components": {},
    }


def score_requirement_priority(
    urgency: str = "normal",
    opportunity_value: float = 0,
    sighting_count: int = 0,
    days_since_created: float = 0,
    vendors_contacted: int = 0,
) -> float:
    """Compute buyer priority score (0-100) for a requirement.

    Weights: urgency 30%, customer value 20%, sighting scarcity 20%,
    age 15%, contact progress 15%.

    Called by: sightings router, priority refresh job
    Depends on: nothing (pure function)
    """
    import math

    urgency_map = {"critical": 100, "hot": 90, "urgent": 70, "normal": 30, "low": 10}
    urgency_score = urgency_map.get(urgency, 30)
    value_score = min(100, math.log10(max(opportunity_value, 1)) * 25) if opportunity_value > 0 else 20
    scarcity_score = max(0, 100 - sighting_count * 5)
    age_score = min(100, days_since_created * (100 / 30))
    contact_score = max(0, 100 - vendors_contacted * 20)

    total = urgency_score * 0.30 + value_score * 0.20 + scarcity_score * 0.20 + age_score * 0.15 + contact_score * 0.15
    return round(min(100, max(0, total)), 1)
