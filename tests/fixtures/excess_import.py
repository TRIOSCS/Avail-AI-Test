"""excess_import.py — test-only seeding helper for excess-list line items.

The old ``excess_service.import_line_items`` was the unguarded legacy twin of the
live ``preview_import``/``confirm_import`` path (no route reached it; QC item #33).
It moved here because ~15 tests use it to seed DRAFT lists directly at the service
layer without the preview round-trip.

Called by: tests (fixture-style seeding)
Depends on: app.services.excess_service internals (_parse_import_row et al.)
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.excess import ExcessLineItem
from app.services.excess_service import (
    _parse_import_row,
    _resolve_line_material_card,
    _safe_commit,
    get_excess_list,
)
from app.utils.normalization import normalize_mpn_key


def import_line_items(db: Session, list_id: int, rows: list[dict]) -> dict:
    """Import line items from parsed CSV/Excel rows into an excess list.

    Flexible header detection maps common column names to canonical fields. Skips rows
    with blank part_number or invalid quantity.

    Returns {imported: int, skipped: int, errors: list[str]}.
    """
    get_excess_list(db, list_id)  # 404 if the list doesn't exist

    imported = 0
    skipped = 0
    errors: list[str] = []

    for i, raw_row in enumerate(rows, start=1):
        fields, error_reason = _parse_import_row(raw_row)
        if fields is None:
            skipped += 1
            errors.append(f"Row {i}: {error_reason} — skipped")
            continue

        part_number = fields["part_number"]
        item = ExcessLineItem(
            excess_list_id=list_id,
            part_number=part_number,
            normalized_part_number=normalize_mpn_key(part_number) or None,
            manufacturer=fields["manufacturer"],
            quantity=fields["quantity"],
            date_code=fields["date_code"],
            condition=fields["condition"],
            asking_price=fields["asking_price"],
        )
        db.add(item)
        _resolve_line_material_card(db, item)
        imported += 1

    if imported > 0:
        _safe_commit(db, entity="excess line items")

    logger.info(
        "Imported {} line items into ExcessList id={} (skipped={})",
        imported,
        list_id,
        skipped,
    )
    return {"imported": imported, "skipped": skipped, "errors": errors}
