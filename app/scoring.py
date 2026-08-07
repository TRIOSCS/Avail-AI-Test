"""Sighting Score — buyer-usefulness-oriented multi-factor scoring.

Optimized for lead usefulness: a smaller number of strong, explainable
leads beats a large number of weak ones.  Missing data is penalized (not
neutral) because a buyer can't act on a lead that has no price or qty.

score_sighting_v2() is the ONE score (spec §9): computed once at save time
(search_service.persistence._save_sightings) and persisted to
sightings.score + sightings.score_components — every display path READS
the persisted value instead of re-deriving it. classify_lead() and
explain_lead() add human-readable quality labels and plain-English reasons
a buyer should care; confidence_color() / source_badge() map a persisted
score and source_type to display chrome. The older generations
(score_sighting v1 trust-only, score_unified v3 display-time re-derivation)
were removed in the §9 scoring cut.

Called by: search_service.persistence._save_sightings(),
           search_service.presentation.sighting_to_dict(),
           search_service.pipeline.search_requirement()
Depends on: app.config (SIGHTING_WEIGHT_* settings); otherwise pure logic
"""

from .config import settings

NEW_VENDOR_BASELINE = 35.0

MISSING_DATA_SCORE = 25.0

WEAK_LEAD_THRESHOLD = 30.0


def _build_sighting_weights(cfg) -> dict[str, float]:
    """Map the five typed SIGHTING_WEIGHT_* Settings fields onto the canonical factor
    keys.

    Settings validates the sum (must be 1.0, fail-fast at startup), so this is a pure
    projection.
    """
    return {
        "trust": cfg.sighting_weight_trust,
        "price": cfg.sighting_weight_price,
        "qty": cfg.sighting_weight_quantity,
        "freshness": cfg.sighting_weight_freshness,
        "completeness": cfg.sighting_weight_completeness,
    }


# Single source of truth for the score_sighting_v2 factor weights. Both the score
# (score_sighting_v2) AND its deterministic breakdown (score_sighting_v2_breakdown)
# read this map, so the weighted drivers a hover shows can never drift from the number.
# Built ONCE at import from settings (env knobs SIGHTING_WEIGHT_TRUST/PRICE/QUANTITY/
# FRESHNESS/COMPLETENESS; defaults preserve the historical hardcoded split, and
# Settings.validate_sighting_weights fail-fasts a set that doesn't sum to 1.0).
SIGHTING_V2_WEIGHTS: dict[str, float] = _build_sighting_weights(settings)
# Human-readable labels for the same five factors (used by the breakdown hover).
SIGHTING_V2_LABELS: dict[str, str] = {
    "trust": "Vendor trust",
    "price": "Price competitiveness",
    "qty": "Quantity coverage",
    "freshness": "Freshness",
    "completeness": "Data completeness",
}


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

    factors = {
        "trust": trust,
        "price": price_f,
        "qty": qty_f,
        "freshness": freshness,
        "completeness": completeness,
    }
    total = sum(factors[k] * SIGHTING_V2_WEIGHTS[k] for k in SIGHTING_V2_WEIGHTS)
    components = {k: round(v, 1) for k, v in factors.items()}
    return round(total, 1), components


def score_sighting_v2_breakdown(components: dict) -> list[tuple[str, float]]:
    """Deterministic weighted contributions behind a sighting's score_components.

    Takes the persisted ``{trust, price, qty, freshness, completeness}`` factor
    values (0-100 each, as returned by ``score_sighting_v2`` / stored on
    ``Sighting.score_components``) and multiplies each by its ``SIGHTING_V2_WEIGHTS``
    weight — the SAME weights the score itself uses. The returned contributions sum
    to the sighting's total score (within factor-rounding), so a hover shows the real
    drivers, not a re-derivation. Factors absent from ``components`` are skipped.

    Returns a list of ``(label, contribution)`` ordered by the canonical factor order.
    """
    out: list[tuple[str, float]] = []
    for key, weight in SIGHTING_V2_WEIGHTS.items():
        val = components.get(key) if components else None
        if val is None:
            continue
        out.append((SIGHTING_V2_LABELS[key], round(float(val) * weight, 1)))
    return out


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
# Display chrome for the persisted v2 score
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


# Static source_type → badge map (spec §9): a badge is a LABEL for where the row
# came from, not a score product. Anything not listed is a live market source.
_SOURCE_BADGES = {
    "historical": "Historical",
    "vendor_affinity": "Vendor Match",
    "ai_live_web": "AI Found",
}


def source_badge(source_type: str | None) -> str:
    """Human-readable badge for a search-result row's source_type."""
    return _SOURCE_BADGES.get((source_type or "").lower(), "Live Stock")
