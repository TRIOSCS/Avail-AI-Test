"""NetComponents sighting writer.

Converts parsed NcSighting objects into AVAIL Sighting records via the
shared save skeleton in search_worker_base.sighting_writer; this module
supplies only the NetComponents-specific Sighting field mapping (price
breaks, supplier product URLs, authorization flag, raw_data).

Called by: worker loop
Depends on: result_parser.NcSighting, search_worker_base.sighting_writer
"""

from sqlalchemy.orm import Session

from ..search_worker_base.sighting_writer import save_sightings
from .result_parser import NcSighting


def _sighting_fields(nc: NcSighting) -> dict:
    """NetComponents-specific Sighting kwargs for one parsed row."""
    # Extract best unit price from price breaks (lowest qty tier = unit price)
    unit_price = nc.price_breaks[0].price if nc.price_breaks else None

    # Build raw_data with all NC-specific fields
    raw_data = {
        "region": nc.region,
        "country": nc.country,
        "inventory_type": nc.inventory_type,
        "uploaded_date": nc.uploaded_date,
        "is_sponsor": nc.is_sponsor,
        "description": nc.description,
        "supplier_product_url": nc.supplier_product_url,
    }
    if nc.price_breaks:
        raw_data["price_breaks"] = [{"price": pb.price, "min_qty": pb.min_qty} for pb in nc.price_breaks]

    return {
        "unit_price": unit_price,
        "currency": nc.currency,
        "is_authorized": nc.is_authorized,
        "confidence": 0.6 if nc.inventory_type == "in_stock" else 0.3,
        "raw_data": raw_data,
    }


def save_nc_sightings(
    db: Session,
    queue_item,
    nc_sightings: list[NcSighting],
) -> int:
    """Save parsed NC sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    Returns count of sightings created.
    """
    return save_sightings(
        db,
        queue_item,
        nc_sightings,
        source_type="netcomponents",
        log_prefix="NC",
        build_sighting_fields=_sighting_fields,
    )
