"""Evidence tier definitions — data provenance tags for sightings and offers.

Called by: search_service._save_sightings(), email_service._apply_parsed_result()
Depends on: nothing (pure logic)

Tiers indicate how trustworthy a data point is:
  T1 — Authorized distributor API (e.g., DigiKey, Mouser, Element14 with is_authorized)
  T2 — Direct API from known connector (Nexar/Octopart, BrokerBin, etc.)
  T3 — Broker marketplace or scraper (eBay, ICS scrape, OEMSecrets)
  T4 — AI-parsed email, medium confidence (0.5–0.8) — needs human review
  T5 — AI-parsed email, high confidence (>=0.8) — auto-applied
  T6 — Manual entry by a buyer
  T7 — Historical/material vendor history (stale, no live confirmation)
"""

# Authorized distributor APIs
_AUTHORIZED_SOURCES = {"digikey", "mouser", "element14"}

# Direct API connectors (reliable structured data)
_API_SOURCES = {"nexar", "octopart", "brokerbin", "sourcengine"}

# Marketplace / scraper sources (less structured)
_MARKETPLACE_SOURCES = {"ebay", "oemsecrets", "ics", "ics_scrape"}


def tier_for_sighting(source_type: str | None, is_authorized: bool) -> str:
    """Assign evidence tier based on source_type and authorization status."""
    if is_authorized:
        return "T1"

    src = (source_type or "").lower().strip()

    if src in _AUTHORIZED_SOURCES:
        # DigiKey/Mouser/Element14 results that aren't flagged authorized
        # still come from reliable structured APIs
        return "T2"

    if src in _API_SOURCES:
        return "T2"

    if src in _MARKETPLACE_SOURCES:
        return "T3"

    if src in ("email_parse", "email_auto_import", "email"):
        return "T5"  # Default to high-confidence; caller can override to T4

    if src in ("manual", ""):
        return "T6"

    if src in ("material_history", "stock_list", "excess_list"):
        return "T7"

    # Unknown source — treat as marketplace-level trust
    return "T3"


def tier_for_parsed_offer(confidence: float | None) -> str:
    """Assign evidence tier for an AI-parsed email offer."""
    if confidence is None:
        return "T4"
    if confidence >= 0.8:
        return "T5"
    return "T4"
