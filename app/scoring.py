"""Sighting Score — vendor-score-based with multi-factor support.

Sighting score is derived from the vendor's unified vendor_score.
Authorized distributors always score 100. New vendors (no score yet)
get a baseline of 35 — high enough to appear among results but below
established vendors.  Zero is reserved for blacklisted vendors only.

score_sighting_v2() adds price, quantity, freshness, and completeness
factors for richer ranking when the caller supplies context.
"""

# Baseline score for vendors with no history.  Prevents burying first-time
# broker results below stale 90-day historical data (which scores ~30).
NEW_VENDOR_BASELINE = 35.0


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

    Returns (total_score, {"trust": .., "price": .., "qty": .., "freshness": .., "completeness": ..}).
    """
    # Factor 1: Vendor Trust (30%)
    if is_authorized:
        trust = 95.0
    elif vendor_score is not None:
        trust = vendor_score
    else:
        trust = NEW_VENDOR_BASELINE

    # Factor 2: Price Competitiveness (25%)
    if unit_price and median_price and median_price > 0 and unit_price > 0:
        ratio = median_price / unit_price
        price_f = min(100.0, max(0.0, ratio * 50.0))
    else:
        price_f = 50.0  # Unknown = neutral

    # Factor 3: Quantity Coverage (20%)
    if qty_available is not None and target_qty and target_qty > 0:
        coverage = min(1.0, qty_available / target_qty)
        qty_f = coverage * 100.0
    else:
        qty_f = 50.0

    # Factor 4: Freshness (15%)
    if age_hours is not None:
        freshness = max(0.0, 100.0 - (age_hours / 24.0) * 5.0)
    else:
        freshness = 50.0

    # Factor 5: Data Completeness (10%)
    fields_present = sum(1 for f in [has_price, has_qty, has_lead_time, has_condition] if f)
    completeness = (fields_present / 4.0) * 100.0

    total = (
        trust * 0.30
        + price_f * 0.25
        + qty_f * 0.20
        + freshness * 0.15
        + completeness * 0.10
    )
    components = {
        "trust": round(trust, 1),
        "price": round(price_f, 1),
        "qty": round(qty_f, 1),
        "freshness": round(freshness, 1),
        "completeness": round(completeness, 1),
    }
    return round(total, 1), components
