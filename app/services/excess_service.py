"""excess_service.py — Business logic for the Resell workspace.

Handles CRUD operations for ExcessList, bulk import of ExcessLineItems with
flexible header detection (part_number/mpn, quantity/qty, etc.), MaterialCard
resolution, role-derived capabilities, inbound broker offers (ExcessOffer /
ExcessOfferLine), the best-price rollup, list close, and aggregate stats.

Called by: routers/resell.py
Depends on: models (ExcessList, ExcessLineItem, ExcessOffer, Company), database
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..constants import (
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    OfferLineMatchStatus,
    UserRole,
)
from ..models import Company, User
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from ..utils.normalization import normalize_mpn_key
from ..utils.sql_helpers import escape_like
from .buyer_affinity_service import recompute_buyer_score_on_win

# ---------------------------------------------------------------------------
# Resell capabilities — role-derived powers (spec §"Roles & capabilities")
# ---------------------------------------------------------------------------
#
# Two powers modelled as capabilities (NOT scattered ``role == 'trader'`` checks),
# mirroring dependencies.BUYER_ROLES/has_buyer_role:
#   can_post  = sell-side intake & posting → sales + trader (admin/manager too).
#   can_offer = buy-side offers on a posting → buyer + trader (admin/manager too).
# Traders are on both sides — the primary users of this module. AGENT (the
# non-interactive service account) holds neither, matching require_buyer.

_CAN_POST_ROLES = frozenset({UserRole.SALES, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN})
_CAN_OFFER_ROLES = frozenset({UserRole.BUYER, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN})


def can_post(user: User | None) -> bool:
    """True when *user* may intake/post an excess list (sales + traders)."""
    return user is not None and user.role in _CAN_POST_ROLES


def can_offer(user: User | None) -> bool:
    """True when *user* may submit an offer on a posting (buyers + traders)."""
    return user is not None and user.role in _CAN_OFFER_ROLES


def _resolve_line_material_card(db: Session, item: ExcessLineItem) -> None:
    """Resolve (find-or-create) the MaterialCard for *item* and set material_card_id.

    Mirrors what Requirements/Offers/Sightings do (integrity_service heals the same
    way) — the Sighting live-mirror needs the card link (spec §Data-model). Reuses the
    canonical resolver; leaves material_card_id null if the MPN won't resolve. Additive
    and safe: never raises on an unresolvable part, never overwrites an existing link.
    """
    from ..search_service import resolve_material_card

    if item.material_card_id is not None:
        return
    card = resolve_material_card(item.part_number, db, manufacturer=item.manufacturer or "")
    if card:
        item.material_card_id = card.id


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


def _parse_import_row(raw_row: dict) -> tuple[dict | None, str | None]:
    """Normalize and validate one import row.

    Returns (fields, None) for a valid row, where fields holds the canonical parsed
    values (asking_price as Decimal|None), or (None, reason) when the row should be
    skipped — reason is "blank part_number" or "invalid quantity".
    """
    row = _normalize_row(raw_row)
    part_number = (row.get("part_number") or "").strip()
    if not part_number:
        return None, "blank part_number"

    quantity = _parse_quantity(row.get("quantity"))
    if quantity is None:
        return None, "invalid quantity"

    fields = {
        "part_number": part_number,
        "manufacturer": (row.get("manufacturer") or "").strip() or None,
        "quantity": quantity,
        "date_code": (row.get("date_code") or "").strip() or None,
        "condition": (row.get("condition") or "").strip() or "New",
        "asking_price": _parse_price(row.get("asking_price")),
    }
    return fields, None


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
        query = query.filter(ExcessList.title.ilike(f"%{escape_like(q)}%", escape="\\"))
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

        fields, error_reason = _parse_import_row(raw_row)
        if fields is None:
            errors.append(f"Row {i}: {error_reason} — will be skipped")
            continue

        asking_price = fields["asking_price"]
        valid_rows.append(
            {
                "part_number": fields["part_number"],
                "manufacturer": fields["manufacturer"],
                "quantity": fields["quantity"],
                "date_code": fields["date_code"],
                "condition": fields["condition"],
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
        _resolve_line_material_card(db, item)
        imported += 1
    if imported > 0:
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")
    logger.info("Confirmed import of {} items into ExcessList id={}", imported, list_id)
    return {"imported": imported}


# ---------------------------------------------------------------------------
# Resell: inbound offers (ExcessOffer / ExcessOfferLine) + best-price rollup
# ---------------------------------------------------------------------------
#
# An inbound offer is a broker's offer to BUY a posted excess list. It is either
# ``take_all`` (binds the whole list, no line rows, optional lump price) or
# ``per_line`` (one ExcessOfferLine per part the broker will buy). Matching is part
# number only via normalize_mpn_key — price NEVER affects matching, unit_price is
# nullable, and unmatched/ambiguous rows are QUEUED (kept with mpn_raw), never dropped
# (spec §"Offer collection").

# Offer statuses whose lines count toward a line's best-price rollup (active states).
_ROLLUP_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.WON)


def submit_offer(
    db: Session,
    *,
    list_id: int,
    user: User,
    scope: str,
    notes: str | None = None,
    valid_until: datetime | None = None,
    lines: list[dict] | None = None,
    take_all_total_price: Decimal | None = None,
) -> ExcessOffer:
    """Submit an inbound offer (a broker's offer to BUY) on a posted excess list.

    ``scope='take_all'`` → one ExcessOffer (status open), optional lump
    ``take_all_total_price``, NO lines. ``scope='per_line'`` → the offer plus one
    ExcessOfferLine per input row in *lines*; each row's ``mpn_raw`` is normalized and
    matched against the list's line items by part number only (exactly one →
    ``matched`` + ``excess_line_item_id``; none → ``unmatched``; multiple →
    ``ambiguous``). Unmatched/ambiguous rows keep ``mpn_raw`` and are QUEUED for manual
    resolution — never dropped. Affected matched lines get their best-price rollup
    recomputed.

    Guards (raise HTTPException, never silent): the list must exist (404); *user* must
    have ``can_offer`` (403); and *user* must not own the list — self-offer blocked
    (403). Returns the persisted ExcessOffer.

    Row dict keys: ``mpn_raw`` (required), ``quantity`` (required), ``unit_price``,
    ``lead_time_days``, ``terms_text`` (all optional).
    """
    excess_list = get_excess_list(db, list_id)

    if not can_offer(user):
        raise HTTPException(403, "You do not have permission to submit offers")
    if user.id == excess_list.owner_id:
        raise HTTPException(403, "You cannot offer on your own excess list")

    scope_value = ExcessOfferScope(scope).value  # raises ValueError on a bad scope

    offer = ExcessOffer(
        excess_list_id=list_id,
        submitted_by=user.id,
        scope=scope_value,
        notes=notes,
        valid_until=valid_until,
        status=ExcessOfferStatus.OPEN,
        take_all_total_price=take_all_total_price if scope_value == ExcessOfferScope.TAKE_ALL else None,
    )
    db.add(offer)
    db.flush()  # need offer.id before attaching lines

    affected_line_item_ids: set[int] = set()
    if scope_value == ExcessOfferScope.PER_LINE:
        # Index the posting's lines by normalized part number to classify each row.
        posted = db.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()
        by_norm: dict[str, list[ExcessLineItem]] = {}
        for li in posted:
            key = li.normalized_part_number or normalize_mpn_key(li.part_number)
            if key:
                by_norm.setdefault(key, []).append(li)

        for row in lines or []:
            mpn_raw = (row.get("mpn_raw") or "").strip()
            norm_key = normalize_mpn_key(mpn_raw)
            candidates = by_norm.get(norm_key, []) if norm_key else []
            if len(candidates) == 1:
                match_status = OfferLineMatchStatus.MATCHED
                matched_id = candidates[0].id
                affected_line_item_ids.add(matched_id)
            elif len(candidates) > 1:
                match_status = OfferLineMatchStatus.AMBIGUOUS  # queued, never dropped
                matched_id = None
            else:
                match_status = OfferLineMatchStatus.UNMATCHED  # queued, never dropped
                matched_id = None

            db.add(
                ExcessOfferLine(
                    offer_id=offer.id,
                    excess_line_item_id=matched_id,
                    mpn_raw=mpn_raw,
                    quantity=row["quantity"],
                    unit_price=row.get("unit_price"),
                    lead_time_days=row.get("lead_time_days"),
                    terms_text=row.get("terms_text"),
                    match_status=match_status,
                )
            )

    db.flush()  # persist lines so the rollup query sees them
    for line_item_id in affected_line_item_ids:
        recompute_line_rollup(db, line_item_id)

    # Any first offer on an OPEN list signals active collection — flip to COLLECTING.
    if excess_list.status == ExcessListStatus.OPEN:
        excess_list.status = ExcessListStatus.COLLECTING

    _safe_commit(db, entity="excess offer")
    db.refresh(offer)
    logger.info(
        "Submitted ExcessOffer id={} scope={} on list={} by user={} ({} matched lines)",
        offer.id,
        scope_value,
        list_id,
        user.id,
        len(affected_line_item_ids),
    )
    return offer


def recompute_line_rollup(db: Session, excess_line_item_id: int) -> None:
    """Recompute the best-price rollup for one ExcessLineItem from its offers.

    Sets ``best_offer_unit_price`` to the min ``unit_price`` across the line's
    ExcessOfferLines whose parent offer is in an active state (open/won) and whose
    ``unit_price`` is not null (None when no priced active offers); ``best_offer_id`` to
    the ExcessOffer providing that min; ``offer_count`` to the number of DISTINCT offers
    touching the line (priced or not). Mirrors the spirit of
    sighting_aggregation.rebuild_vendor_summaries (best = min price) but offer-based and
    keyed on the line. Idempotent — safe to call after a land or a withdraw.
    """
    item = db.get(ExcessLineItem, excess_line_item_id)
    if not item:
        return

    rows = (
        db.query(ExcessOfferLine)
        .join(ExcessOffer, ExcessOfferLine.offer_id == ExcessOffer.id)
        .filter(
            ExcessOfferLine.excess_line_item_id == excess_line_item_id,
            ExcessOffer.status.in_([s.value for s in _ROLLUP_OFFER_STATUSES]),
        )
        .all()
    )

    item.offer_count = len({r.offer_id for r in rows})

    priced = [r for r in rows if r.unit_price is not None]
    if priced:
        best = min(priced, key=lambda r: r.unit_price)
        item.best_offer_unit_price = best.unit_price
        item.best_offer_id = best.offer_id
    else:
        item.best_offer_unit_price = None
        item.best_offer_id = None

    logger.debug(
        "Recomputed rollup for line_item={}: count={} best_price={} best_offer={}",
        excess_line_item_id,
        item.offer_count,
        item.best_offer_unit_price,
        item.best_offer_id,
    )


def withdraw_offer(db: Session, offer_id: int) -> ExcessOffer:
    """Withdraw an inbound offer and recompute the rollup of every line it touched.

    Marks the offer ``withdrawn`` so its lines drop out of the active-state rollup, then
    recomputes ``best_offer_unit_price`` / ``best_offer_id`` / ``offer_count`` for each
    line item the offer referenced. Raises 404 if the offer does not exist.
    """
    offer = db.get(ExcessOffer, offer_id)
    if not offer:
        raise HTTPException(404, f"ExcessOffer {offer_id} not found")

    affected = {line.excess_line_item_id for line in offer.lines if line.excess_line_item_id is not None}
    offer.status = ExcessOfferStatus.WITHDRAWN
    db.flush()

    for line_item_id in affected:
        recompute_line_rollup(db, line_item_id)

    _safe_commit(db, entity="excess offer withdrawal")
    db.refresh(offer)
    logger.info("Withdrew ExcessOffer id={} ({} lines recomputed)", offer_id, len(affected))
    return offer


# Line statuses that are decided (no longer collecting offers) for list-status derivation.
_DECIDED_LINE_STATUSES = (ExcessLineItemStatus.AWARDED, ExcessLineItemStatus.WITHDRAWN)


def _award_scope_items(db: Session, offer: ExcessOffer, excess_list: ExcessList) -> list[ExcessLineItem]:
    """The line items an award/unaward of *offer* acts on.

    A ``take_all`` offer carries NO ``ExcessOfferLine`` rows — it binds the whole list, so
    its scope is every non-withdrawn line (FIX: take_all used to award zero lines because
    it derived the scope from the empty ``offer.lines``). A ``per_line`` offer's scope is
    the distinct matched line items its lines point at.
    """
    if offer.scope == ExcessOfferScope.TAKE_ALL:
        return [li for li in excess_list.line_items if li.status != ExcessLineItemStatus.WITHDRAWN]
    # per_line: the distinct matched line items its lines point at (via the loaded
    # relationship — no need to re-fetch each id).
    return list({line.excess_line_item for line in offer.lines if line.excess_line_item is not None})


def _apply_award_list_status(excess_list: ExcessList) -> None:
    """Derive the list's own status once its lines are all decided (FIX #1).

    Nothing else flips an ExcessList to ``awarded``, so the workspace "Awarded" glance
    stayed empty. When every line is decided (awarded or withdrawn) AND at least one was
    awarded, the list itself is awarded. A partial award (some lines still open) does NOT
    flip it — offers are still being collected on the rest.
    """
    items = excess_list.line_items
    if (
        items
        and all(it.status in _DECIDED_LINE_STATUSES for it in items)
        and any(it.status == ExcessLineItemStatus.AWARDED for it in items)
    ):
        excess_list.status = ExcessListStatus.AWARDED


def award_offer(db: Session, offer_id: int, owner: User) -> ExcessOffer:
    """Award an inbound offer — the single chokepoint where an ExcessOffer becomes
    ``won``.

    Owner-only (the list owner is the only one who may pick a winner): raises 404 if the
    offer does not exist, 403 if *owner* does not own the offer's list. Idempotent — an
    already-won offer is returned unchanged. Otherwise: guards that none of the awarded
    lines are already sold to a different offer (409, ``unaward first``), flips the offer
    to ``won`` and its lines to ``awarded``, recomputes each touched line's best-price
    rollup, recomputes the winning buyer's ``BuyerScore`` (``recompute_buyer_score_on_win``),
    retires the sold lines from the Sighting mirror (``sync_list_mirror`` — a sold line
    must stop advertising as live supply), and derives the list's own ``awarded`` status
    when every line is decided. All in one transaction, committed here.
    """
    offer = db.get(ExcessOffer, offer_id)
    if not offer:
        raise HTTPException(404, f"ExcessOffer {offer_id} not found")

    excess_list = get_excess_list(db, offer.excess_list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can award an offer")

    if offer.status == ExcessOfferStatus.WON:
        return offer  # idempotent — a double-award is a no-op, not a second flip

    affected = _award_scope_items(db, offer, excess_list)
    if not affected:
        # No live lines to award (a take_all on an all-withdrawn/empty list, or a per_line
        # offer whose lines matched nothing). Flipping the offer to WON here would be a
        # fake success — the "Offer awarded" toast with zero lines actually awarded.
        raise HTTPException(409, "This offer has no live lines to award.")
    already = next((it for it in affected if it.status == ExcessLineItemStatus.AWARDED), None)
    if already is not None:
        raise HTTPException(409, f"Line '{already.part_number}' is already awarded — unaward the winner first")

    offer.status = ExcessOfferStatus.WON
    for it in affected:
        it.status = ExcessLineItemStatus.AWARDED
    db.flush()

    for it in affected:
        recompute_line_rollup(db, it.id)

    # Recompute the winning buyer's scorecard before the commit — this path owns the
    # transaction (the hook returns None / no-ops for an offer with no canonical buyer).
    recompute_buyer_score_on_win(db, offer)

    # Retire the sold lines from the Sighting live-mirror — a lazy import breaks the
    # excess_mirror ↔ excess_service cycle (excess_mirror imports get_excess_list).
    from . import excess_mirror

    excess_mirror.sync_list_mirror(db, excess_list)
    _apply_award_list_status(excess_list)

    _safe_commit(db, entity="excess offer award")
    db.refresh(offer)
    logger.info(
        "Awarded ExcessOffer id={} (status=won, {} lines awarded) by owner={}", offer_id, len(affected), owner.id
    )
    return offer


def unaward_offer(db: Session, offer_id: int, owner: User) -> ExcessOffer:
    """Reverse an award — the explicit inverse of :func:`award_offer`.

    Owner-only (404 missing, 403 non-owner, same order as award). Raises 409 if the offer
    is not ``won`` (there is nothing to reverse — we never silently re-pick a different
    winner). Flips the offer back to ``open`` and its awarded lines back to ``available``,
    recomputes each line's rollup, recomputes the buyer's ``BuyerScore`` (a full-history
    recompute self-heals the win count back down), re-mirrors the now-live lines
    (``sync_list_mirror``), and steps the list's own status back off ``awarded`` — to
    ``bid_out`` when the posting window has closed, else ``collecting``. One transaction.
    """
    offer = db.get(ExcessOffer, offer_id)
    if not offer:
        raise HTTPException(404, f"ExcessOffer {offer_id} not found")

    excess_list = get_excess_list(db, offer.excess_list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can reverse an award")

    if offer.status != ExcessOfferStatus.WON:
        raise HTTPException(409, "This offer is not awarded — nothing to reverse")

    affected = [it for it in _award_scope_items(db, offer, excess_list) if it.status == ExcessLineItemStatus.AWARDED]
    offer.status = ExcessOfferStatus.OPEN
    for it in affected:
        it.status = ExcessLineItemStatus.AVAILABLE
    db.flush()

    for it in affected:
        recompute_line_rollup(db, it.id)

    recompute_buyer_score_on_win(db, offer)

    from . import excess_mirror

    excess_mirror.sync_list_mirror(db, excess_list)
    if excess_list.status == ExcessListStatus.AWARDED:
        excess_list.status = ExcessListStatus.BID_OUT if excess_list.close_at else ExcessListStatus.COLLECTING

    _safe_commit(db, entity="excess offer unaward")
    db.refresh(offer)
    logger.info(
        "Unawarded ExcessOffer id={} (status=open, {} lines reverted) by owner={}", offer_id, len(affected), owner.id
    )
    return offer


def close_list(db: Session, list_id: int, owner: User) -> ExcessList:
    """Close a posted list — owner-only — flip status to ``bid_out`` + stamp
    ``close_at``.

    The posting-window counterpart to ``excess_mirror.publish_list`` (which stamps
    ``open_at``): once the trader has assembled and sent the bid back, closing the list
    flips it to ``bid_out`` and records ``close_at`` (Chunk E). Guards: the list must
    exist (404) and *owner* must own it (403). Commits. Returns the refreshed list.
    """
    excess_list = get_excess_list(db, list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can close it")

    excess_list.status = ExcessListStatus.BID_OUT
    excess_list.close_at = datetime.now(timezone.utc)
    _safe_commit(db, entity="excess list close")
    db.refresh(excess_list)
    logger.info("Closed ExcessList id={} (status=bid_out) by owner={}", list_id, owner.id)
    return excess_list


# ---------------------------------------------------------------------------
# Phase 4: Stats
# ---------------------------------------------------------------------------


def get_excess_stats(db: Session) -> dict:
    """Compute aggregate stats for the Resell workspace (offer counts, not bid counts).

    Returns {total_lists, total_line_items, open_offers, matched_items, total_offers,
    awarded_items}.
    """
    total_lists = db.query(func.count(ExcessList.id)).scalar() or 0
    total_line_items = db.query(func.count(ExcessLineItem.id)).scalar() or 0
    open_offers = (
        db.query(func.count(ExcessOffer.id)).filter(ExcessOffer.status == ExcessOfferStatus.OPEN).scalar() or 0
    )
    total_offers = db.query(func.count(ExcessOffer.id)).scalar() or 0
    matched_items = db.query(func.count(ExcessLineItem.id)).filter(ExcessLineItem.offer_count > 0).scalar() or 0
    awarded_items = (
        db.query(func.count(ExcessLineItem.id)).filter(ExcessLineItem.status == ExcessLineItemStatus.AWARDED).scalar()
        or 0
    )

    return {
        "total_lists": total_lists,
        "total_line_items": total_line_items,
        "open_offers": open_offers,
        "matched_items": matched_items,
        "total_offers": total_offers,
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
