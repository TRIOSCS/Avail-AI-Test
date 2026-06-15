"""Build a Sighting ORM row from a market-result dict.

What: single source of truth for mapping a search-result row -> Sighting. Shared by the
      requisition shortlist flow (routers.htmx_views.add_to_requisition) and the
      quick-source / scratch-req flow (services.quick_source_service) so both ingestion
      paths produce identical rows.
Calls: instantiates models.sourcing.Sighting (caller adds + flushes + commits).
Depends on: models.sourcing.Sighting.
"""

from __future__ import annotations

from ..models.sourcing import Sighting


def sighting_from_row(requirement_id: int, item: dict) -> Sighting:
    """Map a market-result row dict to an unsaved Sighting bound to ``requirement_id``.

    Mirrors the historical inline mapping in ``add_to_requisition`` exactly so both
    ingestion paths stay byte-for-byte equivalent.
    """
    return Sighting(
        requirement_id=requirement_id,
        vendor_name=item.get("vendor_name", "Unknown"),
        # Shortlist rows post the MPN under "mpn"; fall back so the sighting carries it.
        mpn_matched=item.get("mpn_matched") or item.get("mpn"),
        manufacturer=item.get("manufacturer"),
        qty_available=item.get("qty_available"),
        unit_price=item.get("unit_price"),
        currency=item.get("currency", "USD"),
        source_type=item.get("source_type"),
        is_authorized=item.get("is_authorized", False),
        confidence=item.get("confidence", 0),
        score=item.get("score", 0),
        evidence_tier=item.get("evidence_tier"),
        moq=item.get("moq"),
        lead_time=item.get("lead_time"),
        condition=item.get("condition"),
        date_code=item.get("date_code"),
        packaging=item.get("packaging"),
        vendor_email=item.get("vendor_email"),
        vendor_phone=item.get("vendor_phone"),
        raw_data={
            "vendor_url": item.get("vendor_url"),
            "click_url": item.get("click_url"),
            "octopart_url": item.get("octopart_url"),
            "vendor_sku": item.get("vendor_sku"),
        },
    )
