"""Shared sighting writer for search worker packages.

One implementation of the save skeleton every browser-worker sighting writer
uses: requirement fetch (with missing-requirement guard), existing-sightings
dedup set keyed on vendor/mpn/qty, per-row normalize + dedup loop, durable
vendor-unavailability re-application BEFORE the commit (async results must
not resurrect a dead vendor), commit, and vendor-summary rebuild. Each
worker supplies only its marketplace-specific Sighting kwargs via
``build_sighting_fields``.

Called by: ics_worker/nc_worker/tbf_worker sighting_writer wrappers
Depends on: Requirement/Sighting models, vendor_unavailability, vendor_utils,
    mpn_normalizer
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.models import Requirement, Sighting
from app.services.vendor_unavailability import apply_to_fresh_sightings
from app.vendor_utils import normalize_vendor_name

from .mpn_normalizer import strip_packaging_suffixes


def save_sightings(
    db: Session,
    queue_item,
    parsed_rows: list,
    *,
    source_type: str,
    log_prefix: str,
    build_sighting_fields: Callable[[Any], dict],
) -> int:
    """Save parsed marketplace sightings to the AVAIL sightings table.

    Deduplicates by vendor_name + mpn + quantity combo to avoid duplicate records.
    ``build_sighting_fields(row)`` returns the marketplace-specific Sighting
    kwargs (confidence, raw_data, vendor contact / price / authorization fields).
    Returns count of sightings created.
    """
    req = db.get(Requirement, queue_item.requirement_id)
    if not req:
        logger.error("{} sighting writer: requirement {} not found", log_prefix, queue_item.requirement_id)
        return 0

    material_card_id = req.material_card_id
    now = datetime.now(UTC)

    # Build dedup set from existing sightings of this source for this requirement
    existing = (
        db.query(Sighting.vendor_name_normalized, Sighting.mpn_matched, Sighting.qty_available)
        .filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type == source_type,
        )
        .all()
    )
    existing_keys = {((v or "").lower(), (m or "").lower(), q) for v, m, q in existing}

    created = 0
    created_rows: list[Sighting] = []
    for row in parsed_rows:
        if not row.vendor_name:
            continue

        vendor_norm = normalize_vendor_name(row.vendor_name)
        mpn_norm = strip_packaging_suffixes(row.part_number)

        # Dedup check
        dedup_key = (vendor_norm.lower(), mpn_norm.lower(), row.quantity)
        if dedup_key in existing_keys:
            continue
        existing_keys.add(dedup_key)

        sighting = Sighting(
            requirement_id=req.id,
            material_card_id=material_card_id,
            vendor_name=row.vendor_name,
            vendor_name_normalized=vendor_norm,
            mpn_matched=row.part_number,
            normalized_mpn=mpn_norm,
            manufacturer=row.manufacturer,
            qty_available=row.quantity,
            source_type=source_type,
            source_searched_at=now,
            date_code=row.date_code or None,
            created_at=now,
            **build_sighting_fields(row),
        )
        db.add(sighting)
        created_rows.append(sighting)
        created += 1

    if created:
        # Re-apply durable vendor+part unavailability knowledge before the
        # commit — async results must not resurrect a dead vendor.
        apply_to_fresh_sightings(db, req, created_rows)
        db.commit()
        # Rebuild vendor-level summaries
        from app.services.sighting_aggregation import rebuild_vendor_summaries_from_sightings

        rebuild_vendor_summaries_from_sightings(db, req.id, parsed_rows)
    logger.info(
        "{} sighting writer: created {} sightings for requirement {} (from {} parsed)",
        log_prefix,
        created,
        req.id,
        len(parsed_rows),
    )
    return created
