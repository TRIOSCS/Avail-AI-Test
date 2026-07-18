"""source_trust.py — Single authority for source-type reliability and evidence-tier
trust ordering.

What it does:
  1. SOURCE_RELIABILITY_BASE — source_type string (e.g. "digikey", "brokerbin") ->
     base reliability score (0-100), before any evidence-tier adjustment.
  2. EVIDENCE_TIER_BONUS — evidence tier (T1-T7) -> score bonus/penalty applied on top
     of the base reliability. T6 (manual entry by a buyer) ranks ABOVE T3 (marketplace
     scrape) — a human-verified manual entry is more trustworthy than an anonymous
     scrape result, even though it wasn't pulled from a live API.
  3. Source-type category sets (AUTHORIZED_SOURCES / API_SOURCES / MARKETPLACE_SOURCES
     / EMAIL_SOURCES / MANUAL_SOURCES / HISTORY_SOURCES) so evidence_tiers.py and
     sourcing_leads.py derive tier/score decisions from the same membership lists
     instead of maintaining separate, drifting copies.
  4. VENDOR_RELIABILITY_UNKNOWN / VENDOR_RELIABILITY_KNOWN_NO_SCORE — fallback
     reliability constants for offers whose vendor has no computed vendor_score yet
     (buyplan_scoring.score_offer's "reliability" component). Not source-type based,
     but unified here so every reliability default in the app lives in one file.

Trust ordering (evidence tiers, highest to lowest reliability bonus):
  T1 (authorized distributor API) > T2 (direct connector API) >
  T6 (manual buyer entry) > T3 (marketplace/scrape) >
  T4/T5 (AI-parsed email, medium/high confidence) > T7 (historical/stale)

Called by:
  - app.evidence_tiers (tier_for_sighting source-category membership)
  - app.services.sourcing_leads (_source_reliability)
  - app.services.buyplan_scoring (score_offer vendor-reliability fallback constants)

Depends on: nothing (pure data/lookup module).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Source-type category membership — canonical groupings shared by evidence_tiers
# (tier assignment) and sourcing_leads (base reliability lookup).
# ---------------------------------------------------------------------------

AUTHORIZED_SOURCES: frozenset[str] = frozenset({"digikey", "mouser", "element14"})
API_SOURCES: frozenset[str] = frozenset({"nexar", "octopart", "brokerbin", "sourcengine"})
MARKETPLACE_SOURCES: frozenset[str] = frozenset({"ebay", "oemsecrets", "ics", "ics_scrape"})
EMAIL_SOURCES: frozenset[str] = frozenset({"email_parse", "email_auto_import", "email"})
MANUAL_SOURCES: frozenset[str] = frozenset({"manual", ""})
HISTORY_SOURCES: frozenset[str] = frozenset({"material_history", "stock_list", "excess_list"})

# ---------------------------------------------------------------------------
# Base source reliability (0-100) — sourcing_leads._source_reliability()'s base score
# before the evidence-tier bonus below is applied. Kept as the exact bucket values
# sourcing_leads previously hardcoded so refactoring this out doesn't change scores.
# ---------------------------------------------------------------------------

SOURCE_RELIABILITY_BASE: dict[str, float] = {
    **dict.fromkeys({"digikey", "mouser", "farnell", "element14", "nexar", "octopart"}, 90.0),
    **dict.fromkeys({"netcomponents", "icsource", "thebrokersite", "brokerbin", "sourcengine"}, 72.0),
    **dict.fromkeys({"salesforce", "avail_history"}, 85.0),
    **dict.fromkeys({"ai", "web"}, 40.0),
}
SOURCE_RELIABILITY_DEFAULT = 60.0

# ---------------------------------------------------------------------------
# Evidence-tier bonus applied on top of SOURCE_RELIABILITY_BASE. T6 was previously
# -10 (below T3's +2) — a manual buyer entry scored worse than an anonymous
# marketplace scrape. Fixed here: T6 now sits between T2 and T3. Every other tier
# keeps its original value.
# ---------------------------------------------------------------------------

EVIDENCE_TIER_BONUS: dict[str, float] = {
    "T1": 8.0,
    "T2": 5.0,
    "T6": 3.0,
    "T3": 2.0,
    "T4": 0.0,
    "T5": -5.0,
    "T7": -15.0,
}

# ---------------------------------------------------------------------------
# Vendor-reliability fallback constants for buyplan_scoring.score_offer's
# "reliability" component (vendor performance, not source-type reliability).
# ---------------------------------------------------------------------------

VENDOR_RELIABILITY_UNKNOWN = 25.0
VENDOR_RELIABILITY_KNOWN_NO_SCORE = 50.0


def source_reliability_base(source_type: str | None) -> float:
    """Base reliability (0-100) for a raw connector/source_type string."""
    return SOURCE_RELIABILITY_BASE.get((source_type or "").lower().strip(), SOURCE_RELIABILITY_DEFAULT)


def evidence_tier_bonus(evidence_tier: str | None) -> float:
    """Bonus/penalty added to base reliability for an evidence tier string (e.g.
    "T1")."""
    if not evidence_tier:
        return 0.0
    return EVIDENCE_TIER_BONUS.get(evidence_tier.upper().strip(), 0.0)
