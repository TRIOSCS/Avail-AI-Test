"""excess_service.py — Business logic for excess inventory lists and CSV import.

Handles CRUD operations for ExcessList and bulk import of ExcessLineItems
with flexible header detection (part_number/mpn, quantity/qty, etc.).

Called by: routers/excess.py (Phase 3+)
Depends on: models (ExcessList, ExcessLineItem, Company), database
"""

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Company
from ..models.excess import ExcessLineItem, ExcessList

# ---------------------------------------------------------------------------
# Header aliases for flexible CSV/Excel import
# ---------------------------------------------------------------------------

_HEADER_MAP: dict[str, str] = {
    # part_number aliases
    "part_number": "part_number",
    "mpn": "part_number",
    "pn": "part_number",
    "part": "part_number",
    "part_no": "part_number",
    # quantity aliases
    "quantity": "quantity",
    "qty": "quantity",
    "qnty": "quantity",
    # asking_price aliases
    "asking_price": "asking_price",
    "price": "asking_price",
    "unit_price": "asking_price",
    "cost": "asking_price",
    # manufacturer aliases
    "manufacturer": "manufacturer",
    "mfr": "manufacturer",
    "mfg": "manufacturer",
    "brand": "manufacturer",
    # date_code aliases
    "date_code": "date_code",
    "dc": "date_code",
    "datecode": "date_code",
    # condition aliases
    "condition": "condition",
    "cond": "condition",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_commit(db: Session, *, entity: str = "record") -> None:
    """Commit the session, mapping IntegrityError to HTTP 409."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("IntegrityError on {}: {}", entity, exc)
        raise HTTPException(409, f"Duplicate or conflicting {entity}") from exc


def _normalize_row(raw: dict) -> dict:
    """Map flexible header names to canonical field names."""
    result: dict[str, str | None] = {}
    for key, value in raw.items():
        canonical = _HEADER_MAP.get(key.strip().lower().replace(" ", "_"))
        if canonical and canonical not in result:
            result[canonical] = value
    return result


def _parse_quantity(value) -> int | None:
    """Parse a quantity value, returning None if invalid."""
    if value is None:
        return None
    try:
        qty = int(float(str(value).strip().replace(",", "")))
        return qty if qty > 0 else None
    except (ValueError, TypeError):
        return None


def _parse_price(value) -> Decimal | None:
    """Parse a price value, returning None if invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        cleaned = str(value).strip().lstrip("$").replace(",", "")
        price = Decimal(cleaned)
        return price if price >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def create_excess_list(
    db: Session,
    *,
    title: str,
    company_id: int,
    owner_id: int,
    customer_site_id: int | None = None,
    notes: str | None = None,
    source_filename: str | None = None,
) -> ExcessList:
    """Create a new excess inventory list.

    Validates that company_id exists; raises 404 if not.
    """
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")

    excess_list = ExcessList(
        title=title,
        company_id=company_id,
        owner_id=owner_id,
        customer_site_id=customer_site_id,
        notes=notes,
        source_filename=source_filename,
    )
    db.add(excess_list)
    _safe_commit(db, entity="excess list")
    db.refresh(excess_list)
    logger.info("Created ExcessList id={} title={!r} for company={}", excess_list.id, title, company_id)
    return excess_list


def get_excess_list(db: Session, list_id: int) -> ExcessList:
    """Fetch an excess list by ID; raises 404 if not found."""
    excess_list = db.get(ExcessList, list_id)
    if not excess_list:
        raise HTTPException(404, f"ExcessList {list_id} not found")
    return excess_list


def list_excess_lists(
    db: Session,
    *,
    q: str = "",
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List excess lists with search, status filter, and pagination.

    Returns {items, total, limit, offset}.
    """
    query = db.query(ExcessList)

    if q:
        query = query.filter(ExcessList.title.ilike(f"%{q}%"))
    if status:
        query = query.filter(ExcessList.status == status)

    total = query.count()
    items = query.order_by(ExcessList.id.desc()).offset(offset).limit(limit).all()

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def update_excess_list(db: Session, list_id: int, **kwargs) -> ExcessList:
    """Update an excess list — only sets non-None values."""
    excess_list = get_excess_list(db, list_id)

    for key, value in kwargs.items():
        if value is not None and hasattr(excess_list, key):
            setattr(excess_list, key, value)

    _safe_commit(db, entity="excess list")
    db.refresh(excess_list)
    logger.info("Updated ExcessList id={} fields={}", list_id, list(kwargs.keys()))
    return excess_list


def delete_excess_list(db: Session, list_id: int) -> None:
    """Hard-delete an excess list (cascades to line items)."""
    excess_list = get_excess_list(db, list_id)
    db.delete(excess_list)
    _safe_commit(db, entity="excess list")
    logger.info("Deleted ExcessList id={}", list_id)


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------


def import_line_items(db: Session, list_id: int, rows: list[dict]) -> dict:
    """Import line items from parsed CSV/Excel rows into an excess list.

    Flexible header detection maps common column names to canonical fields. Skips rows
    with blank part_number or invalid quantity.

    Returns {imported: int, skipped: int, errors: list[str]}.
    """
    excess_list = get_excess_list(db, list_id)

    imported = 0
    skipped = 0
    errors: list[str] = []

    for i, raw_row in enumerate(rows, start=1):
        row = _normalize_row(raw_row)
        part_number = (row.get("part_number") or "").strip()
        if not part_number:
            skipped += 1
            errors.append(f"Row {i}: blank part_number — skipped")
            continue

        quantity = _parse_quantity(row.get("quantity"))
        if quantity is None:
            skipped += 1
            errors.append(f"Row {i}: invalid quantity — skipped")
            continue

        asking_price = _parse_price(row.get("asking_price"))
        manufacturer = (row.get("manufacturer") or "").strip() or None
        date_code = (row.get("date_code") or "").strip() or None
        condition = (row.get("condition") or "").strip() or "New"

        item = ExcessLineItem(
            excess_list_id=list_id,
            part_number=part_number,
            manufacturer=manufacturer,
            quantity=quantity,
            date_code=date_code,
            condition=condition,
            asking_price=asking_price,
        )
        db.add(item)
        imported += 1

    # Update total_line_items counter
    if imported > 0:
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")

    logger.info(
        "Imported {} line items into ExcessList id={} (skipped={})",
        imported,
        list_id,
        skipped,
    )
    return {"imported": imported, "skipped": skipped, "errors": errors}


def preview_import(rows: list[dict]) -> dict:
    """Parse rows and return a preview with validation results.

    No DB access.
    """
    valid_rows = []
    errors = []
    column_mapping = {}

    for i, raw_row in enumerate(rows, start=1):
        for key in raw_row:
            canonical = _HEADER_MAP.get(key.strip().lower().replace(" ", "_"))
            if canonical and key not in column_mapping:
                column_mapping[key] = canonical

        row = _normalize_row(raw_row)
        part_number = (row.get("part_number") or "").strip()
        if not part_number:
            errors.append(f"Row {i}: blank part_number — will be skipped")
            continue

        quantity = _parse_quantity(row.get("quantity"))
        if quantity is None:
            errors.append(f"Row {i}: invalid quantity — will be skipped")
            continue

        asking_price = _parse_price(row.get("asking_price"))
        manufacturer = (row.get("manufacturer") or "").strip() or None
        date_code = (row.get("date_code") or "").strip() or None
        condition = (row.get("condition") or "").strip() or "New"

        valid_rows.append(
            {
                "part_number": part_number,
                "manufacturer": manufacturer,
                "quantity": quantity,
                "date_code": date_code,
                "condition": condition,
                "asking_price": float(asking_price) if asking_price is not None else None,
            }
        )

    return {
        "valid_count": len(valid_rows),
        "error_count": len(errors),
        "errors": errors,
        "preview_rows": valid_rows[:10],
        "all_valid_rows": valid_rows,
        "column_mapping": column_mapping,
    }


def confirm_import(db: Session, list_id: int, validated_rows: list[dict]) -> dict:
    """Import pre-validated rows into an excess list.

    Returns {imported: int}.
    """
    excess_list = get_excess_list(db, list_id)
    imported = 0
    for row in validated_rows:
        item = ExcessLineItem(
            excess_list_id=list_id,
            part_number=row["part_number"],
            manufacturer=row.get("manufacturer"),
            quantity=row["quantity"],
            date_code=row.get("date_code"),
            condition=row.get("condition", "New"),
            asking_price=row.get("asking_price"),
        )
        db.add(item)
        imported += 1
    if imported > 0:
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")
    logger.info("Confirmed import of {} items into ExcessList id={}", imported, list_id)
    return {"imported": imported}
