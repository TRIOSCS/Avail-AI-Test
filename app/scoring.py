"""Sighting Score — buyer-usefulness-oriented multi-factor scoring.

Optimized for lead usefulness: a smaller number of strong, explainable
leads beats a large number of weak ones.  Missing data is penalized (not
neutral) because a buyer can't act on a lead that has no price or qty.

score_sighting_v2() weights trust, price, quantity, freshness, and
completeness.  classify_lead() and explain_lead() add human-readable
quality labels and plain-English reasons a buyer should care.

Called by: search_service._save_sightings(), sighting_to_dict()
Depends on: nothing (pure logic)
"""

NEW_VENDOR_BASELINE = 35.0

MISSING_DATA_SCORE = 25.0

WEAK_LEAD_THRESHOLD = 30.0


def score_sighting(vendor_score: float | None, is_authorized: bool) -> float:
    """Score a sighting based on the vendor's unified score. Returns 0-100."""
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

    Missing data is penalized (25/100) rather than treated as neutral (50),
    because a buyer can't act on a lead without price or quantity info.

    Returns (total_score, {"trust": .., "price": .., "qty": .., "freshness": .., "completeness": ..}).
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

    Strong = buyer should act on this now (has actionable data).
    Moderate = worth reviewing but missing something.
    Weak = noise — unlikely to result in a successful purchase.
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

    Authorized distributor results are never filtered out.
    T1/T2 results are kept if they have any data.
    Everything else needs to clear the score threshold.
    """
    if is_authorized:
        return False

    tier = (evidence_tier or "").upper()

    if tier in ("T1", "T2") and (has_price or has_qty):
        return False

    if score < WEAK_LEAD_THRESHOLD and not has_price and not has_qty:
        return True

    return False
