"""The Broker Forum (TBF) sighting writer.

Converts parsed TbfSighting objects into AVAIL Sighting records via the
shared save skeleton in search_worker_base.sighting_writer; this module
supplies only the TBF-specific Sighting field mapping (vendor email and
phone from TBF results, currency, authorization flag, raw_data).

Called by: worker loop
Depends on: result_parser.TbfSighting, search_worker_base.sighting_writer
"""

from sqlalchemy.orm import Session

from ..search_worker_base.sighting_writer import save_sightings
from .result_parser import TbfSighting


def _sighting_fields(tbf: TbfSighting) -> dict:
    """TBF-specific Sighting kwargs for one parsed row."""
    return {
        "vendor_email": tbf.vendor_email or None,
        "vendor_phone": tbf.vendor_phone or None,
        "currency": tbf.currency or "USD",
        "confidence": 0.6 if tbf.in_stock else 0.3,
        "is_authorized": tbf.is_authorized,
        "raw_data": {
            "region": tbf.region,
            "country": tbf.country,
            "inventory_type": "in_stock" if tbf.in_stock else "brokered",
            "uploaded_date": tbf.uploaded_date,
            "vendor_company_id": tbf.vendor_company_id,
            "supplier_product_url": tbf.supplier_product_url,
            "price_breaks": tbf.price,
            "description": tbf.description,
        },
    }


def save_tbf_sightings(
    db: Session,
    queue_item,
    tbf_sightings: list[TbfSighting],
) -> int:
    """Save parsed TBF sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    Returns count of sightings created.
    """
    return save_sightings(
        db,
        queue_item,
        tbf_sightings,
        source_type="thebrokersite",
        log_prefix="TBF",
        build_sighting_fields=_sighting_fields,
    )
