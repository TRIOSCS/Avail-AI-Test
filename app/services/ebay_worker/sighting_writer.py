"""EBay sighting writer.

Converts parsed EbaySighting objects into AVAIL Sighting records via the
shared save skeleton in search_worker_base.sighting_writer; this module
supplies only the eBay-specific Sighting field mapping and the eBay dedup key.

Dedup key: (vendor, eBay item id) rather than the shared (vendor, mpn, qty)
triple. One eBay seller routinely lists the same part several separate times
at the same quantity — distinct listings at distinct prices — and the default
triple would collapse all of them into a single sighting. When a row carries
no item id (a legacy row written before the worker existed) the key falls back
to the shared triple, so nothing already stored is silently merged.

Called by: worker loop
Depends on: result_parser.EbaySighting, search_worker_base.sighting_writer
"""

from sqlalchemy.orm import Session

from ..search_worker_base.sighting_writer import default_dedup_key, save_sightings
from .result_parser import EbaySighting


def ebay_dedup_key(*, vendor_norm: str, mpn: str, qty, raw_data: dict | None) -> tuple:
    """(vendor, ebay_item_id) — falling back to the shared triple when no id."""
    item_id = str((raw_data or {}).get("ebay_item_id") or "").strip()
    if not item_id:
        return default_dedup_key(vendor_norm=vendor_norm, mpn=mpn, qty=qty, raw_data=raw_data)
    return ((vendor_norm or "").lower(), item_id.lower())


def _sighting_fields(row: EbaySighting) -> dict:
    """EBay-specific Sighting kwargs for one parsed listing."""
    return {
        "unit_price": row.unit_price,
        "currency": row.currency or "USD",
        "condition": row.condition,
        # Every eBay seller is an open-marketplace seller, never a franchise
        # distributor — is_authorized is False by construction, not by lookup.
        "is_authorized": False,
        "confidence": row.confidence,
        "raw_data": {
            "click_url": row.click_url,
            "ebay_item_id": row.item_id,
            "ebay_title": row.title,
            "ebay_condition": row.raw_condition,
            "ebay_condition_id": row.condition_id,
            "seller_feedback_pct": row.seller_feedback_pct,
            "seller_feedback_score": row.seller_feedback_score,
            "item_location_country": row.item_location_country,
            "buying_options": row.buying_options,
            "image_url": row.image_url,
            "fetched_at": row.fetched_at,
        },
    }


def save_ebay_sightings(
    db: Session,
    queue_item,
    ebay_sightings: list[EbaySighting],
) -> int:
    """Save parsed eBay sightings to the AVAIL sightings table.

    Deduplicates by (vendor, eBay item id). Returns count of sightings created.
    """
    return save_sightings(
        db,
        queue_item,
        ebay_sightings,
        source_type="ebay",
        log_prefix="EBAY",
        build_sighting_fields=_sighting_fields,
        dedup_key_fn=ebay_dedup_key,
    )
