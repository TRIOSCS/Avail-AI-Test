"""excess_service.py — Business logic for excess inventory lists and CSV import.

Handles CRUD operations for ExcessList and bulk import of ExcessLineItems
with flexible header detection (part_number/mpn, quantity/qty, etc.).
Phase 4 adds: stats, normalization display, proactive matching, email solicitations.

Called by: routers/excess.py (Phase 3+)
Depends on: models (ExcessList, ExcessLineItem, Company), database
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..constants import BidSolicitationStatus, BidStatus, ExcessLineItemStatus
from ..models import Company, CustomerSite
from ..models.excess import Bid, BidSolicitation, ExcessLineItem, ExcessList
from ..models.intelligence import ProactiveMatch
from ..models.offers import Offer
from ..models.sourcing import Requirement, Requisition
from ..utils.normalization import normalize_mpn_key

_ACTIVE_REQ_STATUSES = {"active", "open", "sourcing"}

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
            normalized_part_number=normalize_mpn_key(part_number) or None,
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
        pn = row["part_number"]
        item = ExcessLineItem(
            excess_list_id=list_id,
            part_number=pn,
            normalized_part_number=normalize_mpn_key(pn) or None,
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


# ---------------------------------------------------------------------------
# Demand matching
# ---------------------------------------------------------------------------


def match_excess_demand(db: Session, list_id: int, *, user_id: int) -> dict:
    """Match excess line items against active requirements and create Offers.

    For each excess line item, finds requirements with matching normalized_mpn on active
    requisitions. Creates one Offer per match. Avoids duplicates.

    Returns {matches_created: int, items_matched: int}.
    """
    excess_list = get_excess_list(db, list_id)
    line_items = db.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()

    matches_created = 0
    items_matched = 0

    for item in line_items:
        norm_key = normalize_mpn_key(item.part_number)
        if not norm_key:
            continue

        requirements = (
            db.query(Requirement)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requirement.normalized_mpn == norm_key,
                Requisition.status.in_(_ACTIVE_REQ_STATUSES),
            )
            .all()
        )

        if not requirements:
            continue

        vendor_name = excess_list.company.name if excess_list.company else "Unknown"
        item_matches = 0
        for req in requirements:
            # Avoid duplicates
            existing = (
                db.query(Offer)
                .filter(
                    Offer.source == "excess",
                    Offer.requisition_id == req.requisition_id,
                    Offer.normalized_mpn == norm_key,
                    Offer.vendor_name == vendor_name,
                )
                .first()
            )
            if existing:
                continue

            offer = Offer(
                requisition_id=req.requisition_id,
                requirement_id=req.id,
                excess_line_item_id=item.id,
                vendor_name=vendor_name,
                mpn=item.part_number,
                normalized_mpn=norm_key,
                manufacturer=item.manufacturer,
                qty_available=item.quantity,
                unit_price=item.asking_price,
                source="excess",
                condition=item.condition,
                date_code=item.date_code,
                entered_by_id=user_id,
                notes=f"Auto-matched from excess list: {excess_list.title}",
            )
            db.add(offer)
            item_matches += 1
            matches_created += 1

        if item_matches > 0:
            item.demand_match_count = (item.demand_match_count or 0) + item_matches
            items_matched += 1

    if matches_created > 0:
        _safe_commit(db, entity="excess demand matches")

    logger.info(
        "Demand matching for ExcessList id={}: {} matches across {} items",
        list_id,
        matches_created,
        items_matched,
    )
    return {"matches_created": matches_created, "items_matched": items_matched}


# ---------------------------------------------------------------------------
# Bid operations
# ---------------------------------------------------------------------------


def create_bid(
    db: Session,
    *,
    line_item_id: int,
    list_id: int,
    unit_price: float,
    quantity_wanted: int,
    user_id: int,
    bidder_company_id: int | None = None,
    bidder_vendor_card_id: int | None = None,
    lead_time_days: int | None = None,
    source: str = "manual",
    notes: str | None = None,
) -> Bid:
    """Create a bid on an excess line item.

    Validates that the list and item exist, and that the item belongs to the list.
    """
    excess_list = get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, line_item_id)
    if not item or item.excess_list_id != excess_list.id:
        raise HTTPException(404, f"Line item {line_item_id} not found in list {list_id}")

    bid = Bid(
        excess_line_item_id=line_item_id,
        unit_price=unit_price,
        quantity_wanted=quantity_wanted,
        lead_time_days=lead_time_days,
        bidder_company_id=bidder_company_id,
        bidder_vendor_card_id=bidder_vendor_card_id,
        source=source,
        notes=notes,
        created_by=user_id,
    )
    db.add(bid)
    _safe_commit(db, entity="bid")
    db.refresh(bid)
    logger.info("Created Bid id={} on line_item={} list={}", bid.id, line_item_id, list_id)
    return bid


def list_bids(db: Session, line_item_id: int, list_id: int) -> list[Bid]:
    """List all bids for a line item, sorted by unit_price ASC (best first).

    Validates that the list and item exist.
    """
    excess_list = get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, line_item_id)
    if not item or item.excess_list_id != excess_list.id:
        raise HTTPException(404, f"Line item {line_item_id} not found in list {list_id}")

    return db.query(Bid).filter(Bid.excess_line_item_id == line_item_id).order_by(Bid.unit_price.asc()).all()


def accept_bid(db: Session, bid_id: int, line_item_id: int, list_id: int) -> Bid:
    """Accept a bid: mark it accepted, reject other pending bids, award the line item.

    Validates list/item/bid ownership chain.
    """
    excess_list = get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, line_item_id)
    if not item or item.excess_list_id != excess_list.id:
        raise HTTPException(404, f"Line item {line_item_id} not found in list {list_id}")

    bid = db.get(Bid, bid_id)
    if not bid or bid.excess_line_item_id != line_item_id:
        raise HTTPException(404, f"Bid {bid_id} not found on line item {line_item_id}")

    bid.status = BidStatus.ACCEPTED

    # Reject all other pending bids on the same line item
    db.query(Bid).filter(
        Bid.excess_line_item_id == line_item_id,
        Bid.id != bid_id,
        Bid.status == BidStatus.PENDING,
    ).update({"status": BidStatus.REJECTED})

    # Award the line item
    item.status = ExcessLineItemStatus.AWARDED

    _safe_commit(db, entity="bid acceptance")
    db.refresh(bid)
    logger.info("Accepted Bid id={} on line_item={} list={}", bid_id, line_item_id, list_id)
    return bid


# ---------------------------------------------------------------------------
# Phase 4: Stats
# ---------------------------------------------------------------------------


def get_excess_stats(db: Session) -> dict:
    """Compute aggregate stats for the excess list view.

    Returns {total_lists, total_line_items, pending_bids, matched_items, total_bids,
    awarded_items}.
    """
    total_lists = db.query(func.count(ExcessList.id)).scalar() or 0
    total_line_items = db.query(func.count(ExcessLineItem.id)).scalar() or 0
    pending_bids = db.query(func.count(Bid.id)).filter(Bid.status == BidStatus.PENDING).scalar() or 0
    total_bids = db.query(func.count(Bid.id)).scalar() or 0
    matched_items = db.query(func.count(ExcessLineItem.id)).filter(ExcessLineItem.demand_match_count > 0).scalar() or 0
    awarded_items = (
        db.query(func.count(ExcessLineItem.id)).filter(ExcessLineItem.status == ExcessLineItemStatus.AWARDED).scalar()
        or 0
    )

    return {
        "total_lists": total_lists,
        "total_line_items": total_line_items,
        "pending_bids": pending_bids,
        "matched_items": matched_items,
        "total_bids": total_bids,
        "awarded_items": awarded_items,
    }


# ---------------------------------------------------------------------------
# Phase 4: Normalization backfill
# ---------------------------------------------------------------------------


def backfill_normalized_part_numbers(db: Session) -> int:
    """Backfill normalized_part_number for existing line items that lack it.

    Returns count of items updated.
    """
    items = db.query(ExcessLineItem).filter(ExcessLineItem.normalized_part_number.is_(None)).all()
    updated = 0
    for item in items:
        norm = normalize_mpn_key(item.part_number)
        if norm:
            item.normalized_part_number = norm
            updated += 1
    if updated > 0:
        _safe_commit(db, entity="normalization backfill")
    logger.info("Backfilled normalized_part_number for {} excess line items", updated)
    return updated


# ---------------------------------------------------------------------------
# Phase 4: ProactiveMatch creation for archived deals
# ---------------------------------------------------------------------------

_ARCHIVED_STATUSES = {"closed", "expired"}


def create_proactive_matches_for_excess(
    db: Session,
    list_id: int,
    *,
    user_id: int,
) -> dict:
    """When an excess list is archived (closed/expired), create ProactiveMatch entries.

    Matches each line item against archived requirements so future offers can be
    surfaced to salespeople.

    Returns {matches_created: int}.
    """
    excess_list = get_excess_list(db, list_id)
    if excess_list.status not in _ARCHIVED_STATUSES:
        return {"matches_created": 0}

    line_items = db.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()
    matches_created = 0

    for item in line_items:
        norm_key = normalize_mpn_key(item.part_number)
        if not norm_key:
            continue

        # Find archived requirements with matching MPN
        requirements = (
            db.query(Requirement)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Requirement.normalized_mpn == norm_key,
                Requisition.status.in_({"archived", "closed"}),
            )
            .all()
        )

        if not requirements:
            continue

        # Find the most recent offer from this excess list for this item
        offer = (
            db.query(Offer)
            .filter(
                Offer.source == "excess",
                Offer.normalized_mpn == norm_key,
                Offer.excess_line_item_id == item.id,
            )
            .first()
        )

        # If no offer exists from demand matching, create a standalone one
        if not offer and requirements:
            req = requirements[0]
            vendor_name = excess_list.company.name if excess_list.company else "Unknown"
            offer = Offer(
                requisition_id=req.requisition_id,
                requirement_id=req.id,
                excess_line_item_id=item.id,
                vendor_name=vendor_name,
                mpn=item.part_number,
                normalized_mpn=norm_key,
                manufacturer=item.manufacturer,
                qty_available=item.quantity,
                unit_price=item.asking_price,
                source="excess",
                condition=item.condition,
                date_code=item.date_code,
                entered_by_id=user_id,
                notes=f"Proactive match from archived excess list: {excess_list.title}",
            )
            db.add(offer)
            db.flush()

        if not offer:
            continue

        for req in requirements:
            # Get the customer site from the requisition (the buyer's side)
            customer_site_id = None
            requisition = db.get(Requisition, req.requisition_id) if req.requisition_id else None

            if requisition and getattr(requisition, "customer_site_id", None):
                customer_site_id = requisition.customer_site_id

            # Fall back to a site from the requisition's company (the buyer), not the seller
            if not customer_site_id and requisition and requisition.company_id:
                site = db.query(CustomerSite).filter_by(company_id=requisition.company_id).first()
                customer_site_id = site.id if site else None

            if not customer_site_id:
                continue

            # Check for existing proactive match
            existing = (
                db.query(ProactiveMatch)
                .filter(
                    ProactiveMatch.offer_id == offer.id,
                    ProactiveMatch.requirement_id == req.id,
                )
                .first()
            )
            if existing:
                continue

            pm = ProactiveMatch(
                offer_id=offer.id,
                requirement_id=req.id,
                requisition_id=req.requisition_id,
                customer_site_id=customer_site_id,
                salesperson_id=user_id,
                mpn=item.part_number,
                status="new",
                our_cost=float(item.asking_price) if item.asking_price else None,
            )
            db.add(pm)
            matches_created += 1

    if matches_created > 0:
        _safe_commit(db, entity="proactive matches")

    logger.info(
        "Created {} proactive matches for archived ExcessList id={}",
        matches_created,
        list_id,
    )
    return {"matches_created": matches_created}


# ---------------------------------------------------------------------------
# Phase 4: Email bid solicitations
# ---------------------------------------------------------------------------


def _build_solicitation_html(item: ExcessLineItem, body_text: str, recipient_name: str | None) -> str:
    """Build inline HTML email body for a bid solicitation — parts table with
    details."""
    greeting = f"Hi {recipient_name}," if recipient_name else "Hello,"
    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px;">
        <p>{greeting}</p>
        <p>{body_text}</p>
        <table style="border-collapse: collapse; width: 100%; margin: 16px 0;">
            <thead>
                <tr style="background: #f3f4f6;">
                    <th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">MPN</th>
                    <th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">Manufacturer</th>
                    <th style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">Qty</th>
                    <th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">Condition</th>
                    <th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">Date Code</th>
                    <th style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">Asking Price</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td style="border: 1px solid #d1d5db; padding: 8px;">{item.part_number or "—"}</td>
                    <td style="border: 1px solid #d1d5db; padding: 8px;">{item.manufacturer or "—"}</td>
                    <td style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">{item.quantity or "—"}</td>
                    <td style="border: 1px solid #d1d5db; padding: 8px;">{getattr(item, "condition", None) or "—"}</td>
                    <td style="border: 1px solid #d1d5db; padding: 8px;">{getattr(item, "date_code", None) or "—"}</td>
                    <td style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">{("$" + str(item.asking_price)) if getattr(item, "asking_price", None) else "—"}</td>
                </tr>
            </tbody>
        </table>
        <p style="color: #6b7280; font-size: 12px;">
            This email was sent via AvailAI. Please reply with your best bid.
        </p>
    </div>
    """


def _build_bundled_solicitation_html(
    items: list[ExcessLineItem],
    body_text: str,
    recipient_name: str | None,
) -> str:
    """Build inline HTML email body for a bundled bid solicitation with multi-row parts
    table."""
    greeting = f"Hi {recipient_name}," if recipient_name else "Hello,"
    rows = ""
    for item in items:
        rows += (
            "<tr>"
            f'<td style="border: 1px solid #d1d5db; padding: 8px;">{item.part_number or "\u2014"}</td>'
            f'<td style="border: 1px solid #d1d5db; padding: 8px;">{item.manufacturer or "\u2014"}</td>'
            f'<td style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">{item.quantity or "\u2014"}</td>'
            f'<td style="border: 1px solid #d1d5db; padding: 8px;">{getattr(item, "condition", None) or "\u2014"}</td>'
            f'<td style="border: 1px solid #d1d5db; padding: 8px;">{getattr(item, "date_code", None) or "\u2014"}</td>'
            f'<td style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">'
            f"{'$' + str(item.asking_price) if getattr(item, 'asking_price', None) else '\u2014'}</td>"
            "</tr>"
        )
    return (
        '<div style="font-family: Arial, sans-serif; max-width: 600px;">'
        f"<p>{greeting}</p>"
        f"<p>{body_text}</p>"
        '<table style="border-collapse: collapse; width: 100%; margin: 16px 0;">'
        '<thead><tr style="background: #f3f4f6;">'
        '<th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">MPN</th>'
        '<th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">Manufacturer</th>'
        '<th style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">Qty</th>'
        '<th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">Condition</th>'
        '<th style="border: 1px solid #d1d5db; padding: 8px; text-align: left;">Date Code</th>'
        '<th style="border: 1px solid #d1d5db; padding: 8px; text-align: right;">Asking Price</th>'
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        '<p style="color: #6b7280; font-size: 12px;">'
        "This email was sent via AvailAI. Please reply with your best bid."
        "</p></div>"
    )


async def _find_sent_message(gc, subject: str) -> dict | None:
    """Find the just-sent message in Sent Items to get its ID and conversationId.

    Retries with backoff (1s, 2s) to handle Graph API propagation delays.
    """
    import asyncio

    delays = [1, 2]
    for delay in delays:
        try:
            await asyncio.sleep(delay)
            data = await gc.get_json(
                "/me/mailFolders/sentItems/messages",
                params={
                    "$top": "5",
                    "$orderby": "sentDateTime desc",
                    "$select": "id,conversationId,subject",
                },
            )
            msgs = data.get("value", []) if data else []
            for m in msgs:
                if m.get("subject", "").strip() == subject.strip():
                    return m
        except Exception as e:
            logger.warning(f"Sent message lookup attempt failed: {e}")
    return None


async def send_bid_solicitation(
    db: Session,
    *,
    list_id: int,
    line_item_ids: list[int],
    recipient_email: str,
    recipient_name: str | None,
    contact_id: int,
    user_id: int,
    token: str,
    subject: str | None = None,
    message: str | None = None,
    bundled: bool = True,
) -> list[BidSolicitation]:
    """Create bid solicitation records and send emails via Microsoft Graph API.

    If bundled=True, sends ONE email containing all items in a multi-row table. If
    bundled=False, sends a separate email per item (split mode).

    Returns list of created BidSolicitation records.
    """
    from ..utils.graph_client import GraphClient

    excess_list = get_excess_list(db, list_id)
    gc = GraphClient(token)
    solicitations: list[BidSolicitation] = []

    # Validate all line items exist first
    validated_items: list[ExcessLineItem] = []
    for item_id in line_item_ids:
        item = db.get(ExcessLineItem, item_id)
        if not item or item.excess_list_id != list_id:
            raise HTTPException(404, f"Line item {item_id} not found in list {list_id}")
        validated_items.append(item)

    if bundled:
        # ── Bundled mode: one email with all items ──
        body_text = message or (
            f"We have {len(validated_items)} excess parts available. Please review and send your best bid."
        )

        # Create all solicitation records (status=pending)
        for item in validated_items:
            solicitation = BidSolicitation(
                excess_line_item_id=item.id,
                contact_id=contact_id,
                sent_by=user_id,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                body_preview=body_text[:500],
                status=BidSolicitationStatus.PENDING,
            )
            db.add(solicitation)
            solicitations.append(solicitation)

        db.flush()  # get all solicitation IDs

        # Tag subject with first solicitation's ID
        first_id = solicitations[0].id
        base_subject = subject or f"Bid Request: {len(validated_items)} parts \u2014 {excess_list.title}"
        email_subject = f"{base_subject} [EXCESS-BID-{first_id}]"
        for s in solicitations:
            s.subject = email_subject

        # Build bundled HTML email with all items
        html_body = _build_bundled_solicitation_html(validated_items, body_text, recipient_name)

        # Send ONE email via Graph API
        try:
            await gc.post_json(
                "/me/sendMail",
                {
                    "message": {
                        "subject": email_subject,
                        "body": {"contentType": "HTML", "content": html_body},
                        "toRecipients": [{"emailAddress": {"address": recipient_email}}],
                    },
                    "saveToSentItems": "true",
                },
            )
            # Look up sent message for graph_message_id
            sent_msg = await _find_sent_message(gc, email_subject)
            graph_msg_id = sent_msg.get("id") if sent_msg else None
            now = datetime.now(timezone.utc)
            for s in solicitations:
                s.graph_message_id = graph_msg_id
                s.status = BidSolicitationStatus.SENT
                s.sent_at = now
        except Exception as exc:
            for s in solicitations:
                s.status = BidSolicitationStatus.FAILED
            logger.error(
                "Failed to send bundled bid solicitation to {}: {}",
                recipient_email,
                exc,
            )
    else:
        # ── Split mode: one email per item ──
        for item in validated_items:
            body_text = message or (
                f"We have {item.quantity} pcs of {item.part_number}"
                f"{' (' + item.manufacturer + ')' if item.manufacturer else ''}"
                f" available. Please send your best bid."
            )

            solicitation = BidSolicitation(
                excess_line_item_id=item.id,
                contact_id=contact_id,
                sent_by=user_id,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                body_preview=body_text[:500],
                status=BidSolicitationStatus.PENDING,
            )
            db.add(solicitation)
            db.flush()  # get solicitation.id

            base_subject = subject or (f"Bid Request: {item.part_number} x {item.quantity} \u2014 {excess_list.title}")
            email_subject = f"{base_subject} [EXCESS-BID-{solicitation.id}]"
            solicitation.subject = email_subject

            html_body = _build_solicitation_html(item, body_text, recipient_name)

            try:
                await gc.post_json(
                    "/me/sendMail",
                    {
                        "message": {
                            "subject": email_subject,
                            "body": {"contentType": "HTML", "content": html_body},
                            "toRecipients": [{"emailAddress": {"address": recipient_email}}],
                        },
                        "saveToSentItems": "true",
                    },
                )
                sent_msg = await _find_sent_message(gc, email_subject)
                solicitation.graph_message_id = sent_msg.get("id") if sent_msg else None
                solicitation.status = BidSolicitationStatus.SENT
                solicitation.sent_at = datetime.now(timezone.utc)
            except Exception as exc:
                solicitation.status = BidSolicitationStatus.FAILED
                logger.error(
                    "Failed to send bid solicitation {} to {}: {}",
                    solicitation.id,
                    recipient_email,
                    exc,
                )

            solicitations.append(solicitation)

    if solicitations:
        _safe_commit(db, entity="bid solicitations")
        for s in solicitations:
            db.refresh(s)

    logger.info(
        "Created {} bid solicitations for ExcessList id={} to {} (bundled={})",
        len(solicitations),
        list_id,
        recipient_email,
        bundled,
    )
    return solicitations


def parse_bid_response(
    db: Session,
    *,
    solicitation_id: int,
    unit_price: float,
    quantity_wanted: int,
    lead_time_days: int | None = None,
    notes: str | None = None,
) -> Bid:
    """Parse an incoming bid response and create a Bid from a solicitation.

    Links the created bid back to the solicitation.

    Returns the created Bid.
    """
    solicitation = db.get(BidSolicitation, solicitation_id)
    if not solicitation:
        raise HTTPException(404, f"BidSolicitation {solicitation_id} not found")

    item = db.get(ExcessLineItem, solicitation.excess_line_item_id)
    if not item:
        raise HTTPException(404, f"Line item {solicitation.excess_line_item_id} not found")

    bid = Bid(
        excess_line_item_id=solicitation.excess_line_item_id,
        unit_price=unit_price,
        quantity_wanted=quantity_wanted,
        lead_time_days=lead_time_days,
        source="email_parsed",
        notes=notes or f"Parsed from solicitation to {solicitation.recipient_email}",
        created_by=solicitation.sent_by,
    )
    db.add(bid)
    db.flush()

    solicitation.status = BidSolicitationStatus.RESPONDED
    solicitation.response_received_at = datetime.now(timezone.utc)
    solicitation.parsed_bid_id = bid.id

    _safe_commit(db, entity="parsed bid response")
    db.refresh(bid)
    logger.info(
        "Parsed bid response: Bid id={} from solicitation id={}",
        bid.id,
        solicitation_id,
    )
    return bid


def list_solicitations(
    db: Session,
    list_id: int,
    item_id: int | None = None,
) -> list[BidSolicitation]:
    """List solicitations for an excess list, optionally filtered by line item.

    Returns list of BidSolicitation records.
    """
    get_excess_list(db, list_id)
    query = (
        db.query(BidSolicitation)
        .join(ExcessLineItem, BidSolicitation.excess_line_item_id == ExcessLineItem.id)
        .filter(ExcessLineItem.excess_list_id == list_id)
    )
    if item_id:
        query = query.filter(BidSolicitation.excess_line_item_id == item_id)
    return query.order_by(BidSolicitation.created_at.desc()).all()


async def _call_claude_bid_parse(email_body: str) -> str:
    """Call Claude to extract structured bid data from an email body.

    Returns raw text response from Claude (expected to be JSON).
    Called by: parse_bid_from_email
    Depends on: claude_client
    """
    from app.utils.claude_client import claude_text

    prompt = (
        "Extract bid information from this email response to a parts solicitation. "
        'Return ONLY valid JSON: {"unit_price": float|null, "quantity_wanted": int|null, '
        '"lead_time_days": int|null, "notes": str|null}. '
        'If the email declines or is not a bid response, return: {"declined": true}\n\n'
        f"{email_body[:2000]}"
    )
    result = await claude_text(
        prompt=prompt,
        model_tier="smart",
        max_tokens=256,
    )
    return result or ""


async def parse_bid_from_email(
    db: Session,
    solicitation_id: int,
    email_body: str,
) -> Bid | None:
    """Use Claude to parse a bid response email and create a Bid record.

    Returns the created Bid, or None if parsing fails / email is a decline.
    Called by: inbox scanner (Task 3)
    Depends on: _call_claude_bid_parse, parse_bid_response
    """
    import json
    import re

    solicitation = db.get(BidSolicitation, solicitation_id)
    if not solicitation:
        logger.warning("BidSolicitation {} not found for email parsing", solicitation_id)
        return None

    raw = await _call_claude_bid_parse(email_body)

    # Strip markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "Failed to parse Claude bid response as JSON for solicitation {}: {}",
            solicitation_id,
            raw[:200],
        )
        return None

    if data.get("declined"):
        solicitation.status = BidSolicitationStatus.RESPONDED
        solicitation.response_received_at = datetime.now(timezone.utc)
        _safe_commit(db, entity="declined bid solicitation")
        logger.info("Solicitation {} marked as declined", solicitation_id)
        return None

    unit_price = data.get("unit_price")
    quantity_wanted = data.get("quantity_wanted")

    if unit_price is None or quantity_wanted is None:
        logger.warning(
            "Incomplete bid data from Claude for solicitation {}: {}",
            solicitation_id,
            data,
        )
        return None

    return parse_bid_response(
        db,
        solicitation_id=solicitation_id,
        unit_price=float(unit_price),
        quantity_wanted=int(quantity_wanted),
        lead_time_days=data.get("lead_time_days"),
        notes=data.get("notes"),
    )
