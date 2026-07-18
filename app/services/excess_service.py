"""excess_service.py — Business logic for the Resell workspace.

Handles CRUD operations for ExcessList, bulk import of ExcessLineItems with
flexible header detection (part_number/mpn, quantity/qty, etc.), MaterialCard
resolution, role-derived capabilities, inbound broker offers (ExcessOffer /
ExcessOfferLine), the best-price rollup, list close, and aggregate stats.

Called by: routers/resell.py
Depends on: models (ExcessList, ExcessLineItem, ExcessOffer, Company), database
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..constants import (
    ActivityType,
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    OfferLineMatchStatus,
    UserRole,
)
from ..models import ActivityLog, Company, User
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from ..utils.normalization import normalize_mpn_key
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


def confirm_import(db: Session, list_id: int, rows: list[dict]) -> dict:
    """Import client-submitted rows into an excess list — RE-VALIDATED server-side (L3).

    The preview grid round-trips its rows back through a hidden form field, so *rows* is
    client-controlled and MUST NOT be trusted: a hand-crafted POST could otherwise inject
    part numbers / prices / conditions that never passed preview validation. Every row is
    re-run through :func:`_parse_import_row` (the SAME parser the preview uses) before
    insert — a row that fails server-side (blank part number, non-positive/invalid
    quantity) is rejected and skipped, never inserted — and only the parser's canonical,
    normalized fields are persisted (the round-tripped values are re-derived, not trusted).

    Returns {imported, skipped}.
    """
    excess_list = get_excess_list(db, list_id)
    imported = 0
    skipped = 0
    for raw in rows:
        fields, reason = _parse_import_row(raw)
        if fields is None:
            skipped += 1
            logger.warning("confirm_import rejected a row on ExcessList id={}: {}", list_id, reason)
            continue
        pn = fields["part_number"]
        item = ExcessLineItem(
            excess_list_id=list_id,
            part_number=pn,
            normalized_part_number=normalize_mpn_key(pn) or None,
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
        excess_list.total_line_items = (excess_list.total_line_items or 0) + imported
        _safe_commit(db, entity="excess line items")
    logger.info("Confirmed import of {} items into ExcessList id={} (skipped={})", imported, list_id, skipped)
    return {"imported": imported, "skipped": skipped}


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

# Offer statuses whose lines count toward a line's best-price rollup (live states). LATE
# is included: a late bid (landed after the window closed) is still counted in the stat
# strip (open/late), shown in the Offers tab, awardable, and treated as a live competitor
# by _close_competing_offers — so the rollup (offer_count / best_offer_id /
# best_offer_unit_price) must include it too, else the line reads 0-covered while the strip
# says there's an offer to review, and a higher late bid is never marked "Best".
_ROLLUP_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.WON, ExcessOfferStatus.LATE)

# List statuses that mean the posting window is over: an inbound offer landing now is
# accepted but flagged ``late`` and queued for review — never dropped (spec §Resolved-for
# -v1 #3, constants.ExcessOfferStatus.LATE). On {open, collecting, draft} the offer is
# on-time (``open``).
_CLOSED_LIST_STATUSES = (
    ExcessListStatus.BID_OUT,
    ExcessListStatus.AWARDED,
    ExcessListStatus.CLOSED,
    ExcessListStatus.EXPIRED,
)


def offer_status_for_list(list_status: str) -> ExcessOfferStatus:
    """The status a NEW inbound offer takes given the list's status at submit time.

    ``late`` when the posting window has already closed (bid_out/awarded/closed/expired) —
    the offer is still accepted and queued for review, never dropped — else ``open``.
    Shared by :func:`submit_offer` and the inbound-email path
    (``resell_outreach_service._link_inbound_offer``) so both entry points flag lateness
    identically.
    """
    return ExcessOfferStatus.LATE if list_status in _CLOSED_LIST_STATUSES else ExcessOfferStatus.OPEN


def notify_owner_of_offer(
    db: Session,
    *,
    excess_list: ExcessList,
    activity_type: str,
    buyer_ref: str,
    buyer_label: str,
    vendor_card_id: int | None = None,
) -> None:
    """Emit a deduplicated in-app notification to the list owner on a new inbound offer
    / buyer reply (M6).

    The point of a time-boxed posting window is to act on offers promptly, but the flow
    never told the owner one arrived — they had to reload the workspace. This writes one
    ``channel="system"`` ActivityLog targeting ``excess_list.owner_id`` (the app's shared
    in-app-notification primitive — same shape as ``crm/offers._upsert_notification`` and
    ``buyplan_notifications``). Deduplicated per (list, buyer): a stable ``external_id``
    token encodes the buyer, so a multi-line bid — or a second reply from the same buyer —
    REFRESHES the existing row instead of stacking a new one. A distinct buyer on the same
    list gets its own row. Does NOT commit — the caller owns the transaction boundary.
    """
    token = f"resell-offer:{excess_list.id}:{buyer_ref}"
    # #12: reference the list neutrally by id, NEVER the free-text title — this row carries
    # the buyer's ``vendor_card_id`` and renders on the SHARED cross-trader buyer timeline
    # (the vendor-card Activity tab + GET /api/vendors/{id}/activities are keyed on
    # vendor_card_id only), so a customer-named title would leak the customer to any other
    # trader viewing that buyer. Same anonymization gate as the T3 ``_log_outreach_activity``
    # "list #N" fix and the non-owner "Excess listing #N" label.
    subject = f"New offer from {buyer_label} on list #{excess_list.id}"[:500]
    existing = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == excess_list.owner_id,
            ActivityLog.activity_type == activity_type,
            ActivityLog.excess_list_id == excess_list.id,
            ActivityLog.external_id == token,
            ActivityLog.dismissed_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        existing.subject = subject
        existing.created_at = datetime.now(UTC)
        return
    db.add(
        ActivityLog(
            user_id=excess_list.owner_id,
            activity_type=activity_type,
            channel="system",
            excess_list_id=excess_list.id,
            vendor_card_id=vendor_card_id,
            external_id=token,
            contact_name=buyer_label[:255],
            subject=subject,
        )
    )


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
        status=offer_status_for_list(excess_list.status),
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

    # M6: notify the owner an inbound offer arrived (deduped per (list, buyer)).
    notify_owner_of_offer(
        db,
        excess_list=excess_list,
        activity_type=ActivityType.NEW_OFFER,
        buyer_ref=f"user-{user.id}",
        buyer_label=user.name or user.email,
        vendor_card_id=offer.offerer_vendor_card_id,
    )

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

    An inbound ExcessOffer is a broker bidding to BUY the excess, so the BEST bid is the
    HIGHEST ``unit_price`` — the most money for the parts and the correct award target
    (this is the inverse of the sourcing side, where best = cheapest supply). Sets
    ``best_offer_unit_price`` to the max ``unit_price`` across the line's ExcessOfferLines
    whose parent offer is in an active state (open/won) and whose ``unit_price`` is not
    null (None when no priced active offers); ``best_offer_id`` to the ExcessOffer
    providing that max; ``offer_count`` to the number of DISTINCT offers touching the line
    (priced or not). Idempotent — safe to call after a land or a withdraw.
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
        # Best buy-side bid = the HIGHEST unit_price. None prices are filtered above so
        # they never reach max(); ties resolve to the first row in query order.
        best = max(priced, key=lambda r: r.unit_price)
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


# Offer statuses an award / withdraw may act on: an inbound bid still in play. A won offer
# must be UNAWARDED first (withdrawing it would strand its awarded lines); a lost/withdrawn
# offer is already closed. Mirrors ``routers.resell._WITHDRAWABLE_OFFER_STATUSES`` so a
# direct service call is guarded even when the (defence-in-depth) router guard is bypassed.
_ACTIONABLE_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE)


def withdraw_offer(db: Session, offer_id: int) -> ExcessOffer:
    """Withdraw an inbound offer and recompute the rollup of every line it touched.

    Marks the offer ``withdrawn`` so its lines drop out of the active-state rollup, then
    recomputes ``best_offer_unit_price`` / ``best_offer_id`` / ``offer_count`` for each
    line item the offer referenced. Raises 404 if the offer does not exist, 409 unless the
    offer is still in play (``open``/``late``) — a won offer must be unawarded first, a
    lost/withdrawn one is already closed.
    """
    offer = db.get(ExcessOffer, offer_id)
    if not offer:
        raise HTTPException(404, f"ExcessOffer {offer_id} not found")
    # Serialize vs a concurrent award/unaward of this offer's list (M9): lock the list +
    # its lines and refresh THIS offer so the status guard below reads freshly-committed
    # state. Without the lock a concurrent award can commit (offer->won, line->awarded)
    # between our unlocked read and our unconditional UPDATE, overwriting won->withdrawn
    # and leaving an awarded line pointing at a withdrawn offer.
    _lock_list_for_award(db, offer, offer.excess_list_id)
    if offer.status not in {s.value for s in _ACTIONABLE_OFFER_STATUSES}:
        raise HTTPException(409, "Only an open or late offer can be withdrawn — unaward a won offer first")

    affected = {line.excess_line_item_id for line in offer.lines if line.excess_line_item_id is not None}
    offer.status = ExcessOfferStatus.WITHDRAWN
    db.flush()

    for line_item_id in affected:
        recompute_line_rollup(db, line_item_id)

    _safe_commit(db, entity="excess offer withdrawal")
    db.refresh(offer)
    logger.info("Withdrew ExcessOffer id={} ({} lines recomputed)", offer_id, len(affected))
    return offer


def assign_offer_line(
    db: Session, list_id: int, offer_line_id: int, target_line_item_id: int, owner: User
) -> ExcessOfferLine:
    """Assign an unmatched/ambiguous offer line to a posted line (owner-only; finding
    #15).

    The queued-never-dropped matcher parks an ``ExcessOfferLine`` whose ``mpn_raw`` didn't
    cleanly resolve in the unmatched queue; this is the manual resolution: the owner points
    it at the intended ``ExcessLineItem``, so the salvaged bid becomes a real matched offer
    (and thus awardable). Sets ``excess_line_item_id`` + flips ``match_status`` → MATCHED,
    then recomputes the target line's best-price rollup (and, on a RE-assign, the line it
    moved off of, so the old line no longer counts the moved bid). Guards: the list exists
    (404) + *owner* owns it (403); the offer line belongs to this list (404); the target
    line is on this list (404, never another list's line). Commits.
    """
    excess_list = get_excess_list(db, list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can assign an offer line")

    offer_line = db.get(ExcessOfferLine, offer_line_id)
    if offer_line is None or offer_line.offer is None or offer_line.offer.excess_list_id != list_id:
        raise HTTPException(404, f"Offer line {offer_line_id} not found on list {list_id}")

    target = db.get(ExcessLineItem, target_line_item_id)
    if target is None or target.excess_list_id != list_id:
        raise HTTPException(404, f"Line {target_line_item_id} not found on list {list_id}")

    previous_line_item_id = offer_line.excess_line_item_id
    offer_line.excess_line_item_id = target.id
    offer_line.match_status = OfferLineMatchStatus.MATCHED
    db.flush()

    # Recompute the new target, and the line it moved off of (a re-assign), so both rollups
    # reflect the move. A first assign from the unmatched queue has no previous line.
    if previous_line_item_id is not None and previous_line_item_id != target.id:
        recompute_line_rollup(db, previous_line_item_id)
    recompute_line_rollup(db, target.id)

    _safe_commit(db, entity="offer line assignment")
    db.refresh(offer_line)
    logger.info(
        "Assigned ExcessOfferLine id={} → line_item={} on list={} by owner={}",
        offer_line_id,
        target.id,
        list_id,
        owner.id,
    )
    return offer_line


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


def _close_competing_offers(excess_list: ExcessList, winner: ExcessOffer) -> set[int]:
    """Mark every still-open/late offer that can no longer win a line ``lost`` (M1).

    Called right after *winner* is awarded (its lines already flipped to ``awarded``).
    ``lost`` was a defined-but-never-assigned state, so losing bids lingered ``open`` —
    kept counting in the review glance (open/late) and could still own a line's
    ``best_offer_id`` (the rollup counts open/won). An offer is closed when it can no
    longer win ANY line:

    * ``take_all`` competitor → closed only when *winner* is itself a ``take_all`` (the
      whole list is gone); a per-line award leaves it OPEN (blocked, revivable on unaward).
    * ``per_line`` competitor → closed when it has matched lines and NONE of them is still
      winnable (all decided — awarded or withdrawn). An offer still bidding on an
      un-decided line stays OPEN; an all-unmatched queue offer (no matched line) is left
      alone for manual resolution.

    Returns the line-item ids the newly-lost offers touched, so the caller recomputes
    those rollups (a losing bid that owned ``best_offer_id`` must be recomputed away).
    """
    winner_takes_all = winner.scope == ExcessOfferScope.TAKE_ALL
    touched: set[int] = set()
    for other in excess_list.offers:
        if other.id == winner.id or other.status not in (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE):
            continue
        if other.scope == ExcessOfferScope.TAKE_ALL:
            should_close = winner_takes_all
        else:
            matched = [ln.excess_line_item for ln in other.lines if ln.excess_line_item is not None]
            should_close = bool(matched) and not any(li.status not in _DECIDED_LINE_STATUSES for li in matched)
        if should_close:
            other.status = ExcessOfferStatus.LOST
            touched.update(ln.excess_line_item_id for ln in other.lines if ln.excess_line_item_id is not None)
    return touched


def _reopen_competing_offers(excess_list: ExcessList, unawarded: ExcessOffer) -> set[int]:
    """Inverse of :func:`_close_competing_offers` — revive the ``lost`` closures an
    award made.

    After *unawarded* is reversed (its lines flipped back to ``available``), re-open every
    ``lost`` offer that once again has a line it could win: a ``per_line`` offer with any
    matched line no longer decided, or a ``take_all`` offer once NO line is awarded.
    ``lost`` is only ever set by :func:`_close_competing_offers`, so every ``lost`` offer
    here was closed by an award and is safe to revive. Returns the touched line ids for
    rollup recompute.
    """
    any_awarded = any(li.status == ExcessLineItemStatus.AWARDED for li in excess_list.line_items)
    touched: set[int] = set()
    for other in excess_list.offers:
        if other.id == unawarded.id or other.status != ExcessOfferStatus.LOST:
            continue
        if other.scope == ExcessOfferScope.TAKE_ALL:
            reopen = not any_awarded
        else:
            matched = [ln.excess_line_item for ln in other.lines if ln.excess_line_item is not None]
            reopen = any(li.status not in _DECIDED_LINE_STATUSES for li in matched)
        if reopen:
            other.status = ExcessOfferStatus.OPEN
            touched.update(ln.excess_line_item_id for ln in other.lines if ln.excess_line_item_id is not None)
    return touched


def _lock_list_for_award(db: Session, offer: ExcessOffer, excess_list_id: int) -> None:
    """Take row-level locks that serialize concurrent award/unaward of one list (M9).

    Award reads each line's status, checks "already awarded", then writes — with no lock,
    two concurrent awards touching an overlapping line can both pass the guard before
    either commits and double-award it (double-firing the buyer-score / mirror hooks).
    Mirroring ``claim_prospect``'s ``with_for_update`` pattern, this locks the list row
    and every one of its line items up front: a second concurrent award BLOCKS here until
    the first commits, then sees the awarded line status and fails the already-awarded
    guard (or the idempotency check) instead of racing it. ``populate_existing`` refreshes
    any identity-mapped line so the guard reads freshly-committed state, and ``db.refresh``
    does the same for the offer (so a concurrent flip of THIS offer is seen as idempotent).
    ``with_for_update`` is a no-op on SQLite (tests) and enforced on PostgreSQL (prod).
    """
    db.query(ExcessList).filter(ExcessList.id == excess_list_id).with_for_update().first()
    db.query(ExcessLineItem).filter(
        ExcessLineItem.excess_list_id == excess_list_id
    ).with_for_update().populate_existing().all()
    db.refresh(offer)


def award_offer(db: Session, offer_id: int, owner: User) -> ExcessOffer:
    """Award an inbound offer — the single chokepoint where an ExcessOffer becomes
    ``won``.

    Owner-only (the list owner is the only one who may pick a winner): raises 404 if the
    offer does not exist, 403 if *owner* does not own the offer's list. Idempotent — an
    already-won offer is returned unchanged; a lost/withdrawn offer is 409 (only an
    ``open``/``late`` offer is awardable). Otherwise: guards that none of the awarded
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

    _lock_list_for_award(db, offer, excess_list.id)

    if offer.status == ExcessOfferStatus.WON:
        return offer  # idempotent — a double-award is a no-op, not a second flip

    if offer.status not in {s.value for s in _ACTIONABLE_OFFER_STATUSES}:
        # A lost/withdrawn offer is already closed — awarding it would resurrect a dead
        # bid (a won offer already returned above via the idempotency guard).
        raise HTTPException(409, "Only an open or late offer can be awarded")

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

    # Close every other open/late offer that can no longer win a line (M1: mark them
    # ``lost`` so losing bids stop counting in review and stop owning the rollup).
    closed_line_ids = _close_competing_offers(excess_list, offer)
    db.flush()

    for line_id in {it.id for it in affected} | closed_line_ids:
        recompute_line_rollup(db, line_id)

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

    _lock_list_for_award(db, offer, excess_list.id)

    if offer.status != ExcessOfferStatus.WON:
        raise HTTPException(409, "This offer is not awarded — nothing to reverse")

    affected = [it for it in _award_scope_items(db, offer, excess_list) if it.status == ExcessLineItemStatus.AWARDED]
    offer.status = ExcessOfferStatus.OPEN
    for it in affected:
        it.status = ExcessLineItemStatus.AVAILABLE
    db.flush()

    # Revive the competing offers this award had closed (M1 inverse) now that its lines
    # are back in the pool.
    reopened_line_ids = _reopen_competing_offers(excess_list, offer)
    db.flush()

    for line_id in {it.id for it in affected} | reopened_line_ids:
        recompute_line_rollup(db, line_id)

    recompute_buyer_score_on_win(db, offer)

    # Step the list status back off ``awarded`` BEFORE re-mirroring so the mirror re-sync
    # sees the reverted posting status (M5): a list stepping back to ``collecting``
    # re-advertises its now-live lines, while one stepping back to ``bid_out`` (a window
    # that had already closed) stays retired — a closed posting never re-advertises.
    if excess_list.status == ExcessListStatus.AWARDED:
        excess_list.status = ExcessListStatus.BID_OUT if excess_list.close_at else ExcessListStatus.COLLECTING

    from . import excess_mirror

    excess_mirror.sync_list_mirror(db, excess_list)

    _safe_commit(db, entity="excess offer unaward")
    db.refresh(offer)
    logger.info(
        "Unawarded ExcessOffer id={} (status=open, {} lines reverted) by owner={}", offer_id, len(affected), owner.id
    )
    return offer


# ---------------------------------------------------------------------------
# Phase 4: Draft editing (finding #14 / D4)
# ---------------------------------------------------------------------------
#
# Before a list is posted it is a private working draft the owner may correct in place;
# once posted the lines lock. All four editors below are DRAFT-ONLY + owner-only. A draft
# carries no offers and no Sighting mirror, so these are side-effect-free except the
# ``total_line_items`` counter (kept in step with the actual line rows on delete).

# The honest 409 the draft-lock guards raise (replaces the old, false "revise as a new
# version" copy — there is no versioned-revise flow; the real path is close + re-create).
_POSTED_LOCKED_MSG = "Posted lists are locked. Close this list and create a new one to make changes."


def _require_owned_draft(db: Session, list_id: int, owner: User) -> ExcessList:
    """Load a list and assert *owner* may edit it as a DRAFT (404 → 403 → 409).

    Shared guard for the draft-edit set: the list must exist (404), *owner* must own it
    (403), and it must still be a draft (409, honest copy) — mirrors ``close_list``'s
    guard order so a direct service call is protected even if the router guard is bypassed.
    """
    el = get_excess_list(db, list_id)
    if el.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can edit it")
    if el.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, _POSTED_LOCKED_MSG)
    return el


def delete_line(db: Session, list_id: int, line_id: int, owner: User) -> ExcessList:
    """Delete one line from a draft list (owner-only, draft-only); returns the list.

    404 if the line does not exist or belongs to a different list (never touch another
    list's line). Decrements ``total_line_items`` (floored at 0) so the counter stays in
    step with the actual rows. Commits; returns the refreshed list for the detail re-render.
    """
    el = _require_owned_draft(db, list_id, owner)
    line = db.get(ExcessLineItem, line_id)
    if line is None or line.excess_list_id != el.id:
        raise HTTPException(404, f"Line {line_id} not found on list {list_id}")
    db.delete(line)
    el.total_line_items = max((el.total_line_items or 0) - 1, 0)
    _safe_commit(db, entity="excess line delete")
    db.refresh(el)
    logger.info("Deleted ExcessLineItem id={} from draft list={} by owner={}", line_id, list_id, owner.id)
    return el


def update_line(
    db: Session,
    list_id: int,
    line_id: int,
    owner: User,
    *,
    part_number: str,
    quantity: int,
    manufacturer: str | None = None,
    condition: str | None = None,
    date_code: str | None = None,
    asking_price: Decimal | None = None,
) -> ExcessList:
    """Edit one line on a draft list (owner-only, draft-only); returns the list.

    404 across lists. Re-validates ``quantity > 0`` HERE (400) — otherwise it reaches the
    ``ExcessLineItem.@validates('quantity')`` ValueError as an unhandled 500. When the part
    number or manufacturer changes, the stale MaterialCard link is dropped and re-resolved
    (the resolve is find-or-create and never raises on an unresolvable MPN). Commits.
    """
    el = _require_owned_draft(db, list_id, owner)
    line = db.get(ExcessLineItem, line_id)
    if line is None or line.excess_list_id != el.id:
        raise HTTPException(404, f"Line {line_id} not found on list {list_id}")
    if quantity is None or quantity <= 0:
        raise HTTPException(400, "Quantity must be a positive whole number")

    identity_changed = (part_number or "").strip() != (line.part_number or "") or (manufacturer or None) != (
        line.manufacturer or None
    )
    line.part_number = part_number.strip()
    line.normalized_part_number = normalize_mpn_key(part_number) or None
    line.quantity = quantity
    line.manufacturer = manufacturer or None
    line.condition = condition or "New"
    line.date_code = date_code or None
    line.asking_price = asking_price
    if identity_changed:
        # Re-resolve the material-card link off the new MPN/manufacturer (the mirror needs
        # a correct card at post time). Drop the stale link first so the resolver runs.
        line.material_card_id = None
        _resolve_line_material_card(db, line)
    _safe_commit(db, entity="excess line update")
    db.refresh(el)
    logger.info("Updated ExcessLineItem id={} on draft list={} by owner={}", line_id, list_id, owner.id)
    return el


def update_excess_list(
    db: Session,
    list_id: int,
    owner: User,
    *,
    title: str,
    notes: str | None = None,
    company_id: int | None = None,
    customer_site_id: int | None = None,
) -> ExcessList:
    """Edit a draft list's header (owner-only, draft-only); returns the refreshed list.

    Updates ``title`` / ``notes`` / ``customer_site_id`` and, when ``company_id`` is given
    and differs, re-points the seller company (404 if it does not exist). Commits.
    """
    el = _require_owned_draft(db, list_id, owner)
    if company_id is not None and company_id != el.company_id:
        company = db.get(Company, company_id)
        if not company:
            raise HTTPException(404, f"Company {company_id} not found")
        el.company_id = company_id
    el.title = title
    el.notes = notes
    el.customer_site_id = customer_site_id
    _safe_commit(db, entity="excess list update")
    db.refresh(el)
    logger.info("Updated draft ExcessList id={} by owner={}", list_id, owner.id)
    return el


def delete_excess_list(db: Session, list_id: int, owner: User) -> None:
    """Delete a whole draft list (owner-only, draft-only); cascades to its children.

    The ORM ``cascade="all, delete-orphan"`` on line_items/offers/customer_bids cleans the
    children (a draft has no offers/bids, but the cascade is defence-in-depth). Commits.
    """
    el = _require_owned_draft(db, list_id, owner)
    db.delete(el)
    _safe_commit(db, entity="excess list delete")
    logger.info("Deleted draft ExcessList id={} by owner={}", list_id, owner.id)


# List statuses a manual close may act on: an actively-posted window. A draft was never
# published (nothing to close), and a bid_out/awarded/closed/expired list is already
# resolved — re-closing it is a no-op the endpoint should reject (M5).
_CLOSEABLE_LIST_STATUSES = (ExcessListStatus.OPEN, ExcessListStatus.COLLECTING)


def _end_posting_window(db: Session, list_id: int, owner: User, *, target_status: str) -> ExcessList:
    """Close a posted list into a resolved *target_status* — owner-only — + stamp
    ``close_at``.

    Shared engine for both posting-window exits: ``bid_out`` (bids went out) and ``closed``
    (closed without bidding — D5). Guards: the list must exist (404), *owner* must own it
    (403), and it must be actively posted (``open``/``collecting``) — a draft or an
    already-resolved list is 409 (M5). RETIRES the Sighting mirror (``sync_list_mirror`` on
    a now-closed posting drops every line's live-supply row — both ``bid_out`` and ``closed``
    are in the mirror's posting-closed set). Commits. Returns the refreshed list.
    """
    excess_list = get_excess_list(db, list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can close it")
    if excess_list.status not in {s.value for s in _CLOSEABLE_LIST_STATUSES}:
        raise HTTPException(409, "Only an open or collecting list can be closed")

    excess_list.status = target_status
    excess_list.close_at = datetime.now(UTC)
    db.flush()

    # Retire the live-mirror: a closed posting window must stop advertising its supply
    # (lazy import breaks the excess_mirror ↔ excess_service cycle).
    from . import excess_mirror

    excess_mirror.sync_list_mirror(db, excess_list)

    _safe_commit(db, entity="excess list close")
    db.refresh(excess_list)
    logger.info("Closed ExcessList id={} (status={}, mirror retired) by owner={}", list_id, target_status, owner.id)
    return excess_list


def close_list(db: Session, list_id: int, owner: User) -> ExcessList:
    """Close a posted list — owner-only — flip status to ``bid_out`` + stamp
    ``close_at``.

    The posting-window counterpart to ``excess_mirror.publish_list`` (which stamps
    ``open_at``): once the trader has assembled and sent the bid back, closing the list
    flips it to ``bid_out`` and records ``close_at`` (Chunk E). See ``_end_posting_window``
    for the guards and mirror-retire behaviour.
    """
    return _end_posting_window(db, list_id, owner, target_status=ExcessListStatus.BID_OUT)


def close_list_without_bid(db: Session, list_id: int, owner: User) -> ExcessList:
    """Close a posted list WITHOUT bidding → the terminal ``closed`` state (D5, finding
    #14).

    The deliberate "nothing came of this — end it" exit, distinct from the ``bid_out``
    (bids went out) path: an owner ends a posting that drew no usable bid instead of leaving
    it advertising forever. ``closed`` is TERMINAL — it is not swept by the nightly expiry
    (only open/collecting are) and there is no reopen; it retires the mirror like any closed
    window. Same owner + open/collecting guards as ``close_list`` (409 on a draft/resolved
    list). Commits. Returns the refreshed list.
    """
    return _end_posting_window(db, list_id, owner, target_status=ExcessListStatus.CLOSED)


# List statuses that are still "in flight" (the posting window has not resolved) and so
# are eligible for auto-expiry once past ``close_at`` (M5 nightly job).
_UNRESOLVED_LIST_STATUSES = (ExcessListStatus.OPEN, ExcessListStatus.COLLECTING)


def expire_overdue_lists(db: Session, *, now: datetime | None = None) -> int:
    """Flip every unresolved list past its ``close_at`` to ``expired`` — the nightly
    backstop.

    An ``open``/``collecting`` list whose ``close_at`` deadline has passed without being
    awarded or closed is stale: it auto-expires so it stops advertising supply and drops
    out of the offerable ("Open to Me") lens. For each expired list the Sighting mirror is
    retired (``sync_list_mirror`` on the now-closed posting). Idempotent — a list already
    ``expired``/``awarded``/``bid_out`` is skipped.

    Each list's flip + mirror-sync + commit is ISOLATED in its own try/except: one list
    whose mirror-sync raises is rolled back and skipped, and the batch continues expiring
    the others (a single bad list must never silently strand the whole nightly sweep).
    Returns the count SUCCESSFULLY expired.
    """
    from . import excess_mirror

    now = now or datetime.now(UTC)
    overdue = (
        db.query(ExcessList)
        .filter(
            ExcessList.status.in_([s.value for s in _UNRESOLVED_LIST_STATUSES]),
            ExcessList.close_at.isnot(None),
            ExcessList.close_at < now,
        )
        .all()
    )
    expired_count = 0
    for excess_list in overdue:
        try:
            excess_list.status = ExcessListStatus.EXPIRED
            db.flush()
            excess_mirror.sync_list_mirror(db, excess_list)
            _safe_commit(db, entity="excess list expiry")
            expired_count += 1
        except Exception:  # noqa: BLE001 — deliberate per-list isolation: ANY failure (mirror sync, commit) on one list must be logged and skipped so the batch keeps expiring the rest
            logger.exception(
                "Auto-expiry failed for list={} — rolling back this list, continuing the batch",
                excess_list.id,
            )
            db.rollback()
    logger.info("Expired {} of {} overdue excess list(s) past close_at", expired_count, len(overdue))
    return expired_count


# ---------------------------------------------------------------------------
# Phase 4: Stats
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 4: Normalization backfill
# ---------------------------------------------------------------------------
