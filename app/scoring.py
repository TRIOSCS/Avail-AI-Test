"""Sighting Score — vendor-score-based.

Sighting score is derived from the vendor's unified vendor_score.
Authorized distributors always score 100. New vendors (no score) score 0.
"""


def score_sighting(vendor_score: float | None, is_authorized: bool) -> float:
    """Score a sighting based on the vendor's unified score. Returns 0-100."""
    if is_authorized:
        return 100.0
    if vendor_score is None:
        return 0.0
    return round(vendor_score, 1)
