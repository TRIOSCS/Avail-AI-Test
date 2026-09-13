"""Shared sighting writer for search worker packages.

One implementation of the save skeleton every worker sighting writer uses:
requirement fetch (with missing-requirement guard), existing-sightings dedup
set, per-row normalize + dedup loop, durable vendor-unavailability
re-application BEFORE the commit (async results must not resurrect a dead
vendor), commit, and vendor-summary rebuild. Each worker supplies only its
marketplace-specific Sighting kwargs via ``build_sighting_fields``.

Dedup defaults to the (vendor, mpn, qty) triple every browser worker uses.
A worker whose marketplace carries a stronger listing identity passes
``dedup_key_fn`` instead — the eBay worker keys on (vendor, eBay item id),
because one seller routinely lists the same part several times at the same
quantity and the default triple would collapse those into one sighting.

Called by: ics_worker/nc_worker/tbf_worker/ebay_worker sighting_writer wrappers
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


def default_dedup_key(*, vendor_norm: str, mpn: str, qty, raw_data: dict | None) -> tuple:
    """The historical dedup key: (vendor, mpn, quantity), case-folded.

    ``raw_data`` is unused here — it is part of the hook signature so a worker
    with a stronger listing identity (eBay's item id) can reach it.
    """
    del raw_data  # part of the hook contract; the default key ignores it
    return ((vendor_norm or "").lower(), (mpn or "").lower(), qty)


def save_sightings(
    db: Session,
    queue_item,
    parsed_rows: list,
    *,
    source_type: str,
    log_prefix: str,
    build_sighting_fields: Callable[[Any], dict],
    dedup_key_fn: Callable[..., tuple] | None = None,
) -> int:
    """Save parsed marketplace sightings to the AVAIL sightings table.

    Deduplicates on ``dedup_key_fn`` (default: the vendor + mpn + quantity
    triple) against both the rows already stored for this requirement/source
    and the rows created earlier in this same batch.
    ``build_sighting_fields(row)`` returns the marketplace-specific Sighting
    kwargs (confidence, raw_data, vendor contact / price / authorization fields);
    it is evaluated BEFORE the dedup check so a key function can read the
    marketplace's raw_data (e.g. the eBay item id).
    Returns count of sightings created.
    """
    dedup_key_fn = dedup_key_fn or default_dedup_key
    req = db.get(Requirement, queue_item.requirement_id)
    if not req:
        logger.error("{} sighting writer: requirement {} not found", log_prefix, queue_item.requirement_id)
        return 0

    material_card_id = req.material_card_id
    now = datetime.now(UTC)

    # Build dedup set from existing sightings of this source for this requirement.
    # raw_data comes along so a custom key can read the marketplace's own listing
    # id back out of an already-stored row.
    existing = (
        db.query(
            Sighting.vendor_name_normalized,
            Sighting.mpn_matched,
            Sighting.qty_available,
            Sighting.raw_data,
        )
        .filter(
            Sighting.requirement_id == req.id,
            Sighting.source_type == source_type,
        )
        .all()
    )
    existing_keys = {
        dedup_key_fn(vendor_norm=v or "", mpn=m or "", qty=q, raw_data=rd if isinstance(rd, dict) else None)
        for v, m, q, rd in existing
    }

    created = 0
    created_rows: list[Sighting] = []
    for row in parsed_rows:
        if not row.vendor_name:
            continue

        vendor_norm = normalize_vendor_name(row.vendor_name)
        mpn_norm = strip_packaging_suffixes(row.part_number)
        extra_fields = build_sighting_fields(row)

        # Dedup check
        dedup_key = dedup_key_fn(
            vendor_norm=vendor_norm,
            mpn=mpn_norm,
            qty=row.quantity,
            raw_data=extra_fields.get("raw_data"),
        )
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
            **extra_fields,
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
