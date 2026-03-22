"""Enrich material cards from sighting data.

Extracts description, manufacturer, and datasheet_url from sighting
raw_data to populate empty fields on material cards. Prefers authorized
sources (DigiKey, Mouser, etc.) over broker listings.

Called by: jobs/enrichment_jobs.py, tests/test_enrich_from_sightings.py
Depends on: app.models.materials
"""

# Sources that are authorized distributors (higher trust)
AUTHORIZED_SOURCES = {"digikey", "mouser", "element14", "newark", "oemsecrets", "arrow", "avnet"}


def _extract_description(raw_data: dict | None, source: str) -> str | None:
    """Extract a description string from sighting raw_data.

    Args:
        raw_data: The raw_data JSON from a sighting record.
        source: The source name (digikey, mouser, ebay, etc.)

    Returns:
        Cleaned description string, or None.
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    # Try standard description field
    desc = raw_data.get("description")

    # eBay fallback: use ebay_title
    if not desc and source == "ebay":
        desc = raw_data.get("ebay_title")

    if not desc or not isinstance(desc, str):
        return None

    desc = desc.strip()

    # Skip very short descriptions
    if len(desc) < 5:
        return None

    # Truncate to 1000 chars
    return desc[:1000]


def _extract_datasheet_url(raw_data: dict | None) -> str | None:
    """Extract a datasheet URL from sighting raw_data.

    Args:
        raw_data: The raw_data JSON from a sighting record.

    Returns:
        Valid HTTP(S) URL string, or None.
    """
    if not raw_data or not isinstance(raw_data, dict):
        return None

    url = raw_data.get("datasheet_url")
    if not url or not isinstance(url, str):
        return None

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None

    return url[:1000]


def enrich_card_from_sightings(
    card,
    sightings: list[tuple],
    dry_run: bool = True,
) -> dict:
    """Enrich a material card from its sighting data.

    Args:
        card: MaterialCard (or duck-typed object with description, manufacturer,
              datasheet_url, enrichment_source attributes).
        sightings: List of (source, manufacturer, is_authorized, raw_data) tuples.
        dry_run: If True, return updates dict without modifying card.

    Returns:
        Dict of field updates that were (or would be) applied.
    """
    updates = {}

    # Should we update description?
    needs_description = not card.description or (card.enrichment_source == "claude_ai" and any(s[2] for s in sightings))

    # Should we update manufacturer?
    needs_manufacturer = not card.manufacturer

    # Should we update datasheet_url?
    needs_datasheet = not card.datasheet_url

    if not needs_description and not needs_manufacturer and not needs_datasheet:
        return updates

    # Sort sightings: authorized sources first
    sorted_sightings = sorted(sightings, key=lambda s: (not s[2], s[0]))

    for source, manufacturer, is_authorized, raw_data in sorted_sightings:
        # Description
        if needs_description and "description" not in updates:
            desc = _extract_description(raw_data, source)
            if desc:
                updates["description"] = desc
                if is_authorized:
                    needs_description = False  # Got from authorized, stop looking

        # Manufacturer
        if needs_manufacturer and "manufacturer" not in updates:
            if manufacturer and isinstance(manufacturer, str) and manufacturer.strip():
                updates["manufacturer"] = manufacturer.strip()

        # Datasheet URL
        if needs_datasheet and "datasheet_url" not in updates:
            url = _extract_datasheet_url(raw_data)
            if url:
                updates["datasheet_url"] = url

    # Apply updates if not dry run
    if not dry_run and updates:
        for field, value in updates.items():
            setattr(card, field, value)
        card.enrichment_source = "sighting_extraction"

    return updates
