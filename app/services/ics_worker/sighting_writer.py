"""ICsource sighting writer.

Converts parsed IcsSighting objects into AVAIL Sighting records via the
shared save skeleton in search_worker_base.sighting_writer; this module
supplies only the ICsource-specific Sighting field mapping (vendor email
and phone from ICsource results, in-stock confidence, raw_data).

Called by: worker loop
Depends on: result_parser.IcsSighting, search_worker_base.sighting_writer
"""

from sqlalchemy.orm import Session

from ..search_worker_base.sighting_writer import save_sightings
from .result_parser import IcsSighting


def _sighting_fields(ics: IcsSighting) -> dict:
    """ICsource-specific Sighting kwargs for one parsed row."""
    return {
        "vendor_email": ics.vendor_email or None,
        "vendor_phone": ics.vendor_phone or None,
        "confidence": 0.6 if ics.in_stock else 0.3,
        "raw_data": {
            "vendor_company_id": ics.vendor_company_id,
            "uploaded_date": ics.uploaded_date,
            "description": ics.description,
            "price": ics.price,
            "in_stock": ics.in_stock,
        },
    }


def save_ics_sightings(
    db: Session,
    queue_item,
    ics_sightings: list[IcsSighting],
) -> int:
    """Save parsed ICS sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    Returns count of sightings created.
    """
    return save_sightings(
        db,
        queue_item,
        ics_sightings,
        source_type="icsource",
        log_prefix="ICS",
        build_sighting_fields=_sighting_fields,
    )
