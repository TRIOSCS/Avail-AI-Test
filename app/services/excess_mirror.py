"""excess_mirror.py — Sighting live-mirror for posted excess lines (Chunk C).

What: makes every posted ``ExcessLineItem`` surface to the EXISTING matcher for free by
      mirroring it into the ``Sighting`` table. A single dual-write owner
      (``sync_list_mirror``) keeps the line and its Sighting from drifting — a qty drop,
      award, or withdraw updates or retires the mirrored row.

Why a virtual requirement: ``Sighting.requirement_id`` is NOT NULL but an excess line
      isn't tied to one buyer requirement. Per spec §"Resolved-for-v1 #1" we hang the
      mirror on ONE system-owned "Customer Excess" Requisition + Requirement per
      ExcessList. System-owned == ``Requisition.is_scratch=True`` — the established,
      queryable marker that normal sales views already filter out
      (``Requisition.is_scratch.is_(False)`` in routers.htmx_views), so the virtual rows
      never pollute the requisitions list. The link is deterministic via a stable
      requisition ``name`` keyed on the list id (``_VIRTUAL_REQ_NAME_PREFIX``) — no new
      schema in this additive chunk.

Dedup trap: ``search_service._save_sightings`` deletes sightings by
      ``(requirement_id, source_type)``. We deliberately do NOT route through that path.
      The mirror upserts by ``(source_company_id, material_card_id)`` so a re-publish
      updates the line's own row and never wipes a SIBLING list's ``customer_excess``
      rows. (Each list also gets its own virtual requirement, a second layer of safety.)

Calls: models (ExcessList, ExcessLineItem, Requisition, Requirement, Sighting),
       sighting_ingest.sighting_from_row (the dict→ORM single source of truth),
       search_service.resolve_material_card (lazy card link), normalize_mpn_key.
Depends on: a request-scoped Session. Flushes so ids are set; the CALLER commits
       (publish_list commits itself, matching excess_service's _safe_commit style).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import (
    ExcessLineItemStatus,
    ExcessListStatus,
    RequisitionStatus,
    SourcingStatus,
)
from ..models.excess import ExcessLineItem, ExcessList
from ..models.sourcing import Requirement, Requisition, Sighting
from ..utils.normalization import normalize_mpn_key
from .sighting_ingest import sighting_from_row

# Synthesized internal vendor label for mirrored excess sightings. NEVER the customer
# company name — feeding the seller's name into the VendorCard/sighting vendor dedup
# would leak the customer and pollute vendor analytics (spec §"Sighting live-mirror"
# vendor_name trap). The mirror never touches VendorCard for this source.
EXCESS_VENDOR_LABEL = "Customer Excess"

# Deterministic name marker for the per-list virtual requisition. Keyed on the list id
# so the get-or-create lookup is exact and collision-free without a new FK column.
_VIRTUAL_REQ_NAME_PREFIX = "Customer Excess (list "

# Line statuses whose Sighting should be live (mirrored). Anything else (awarded,
# withdrawn) is retired so the matcher stops seeing dead supply.
_ACTIVE_LINE_STATUSES = frozenset({ExcessLineItemStatus.AVAILABLE, ExcessLineItemStatus.BIDDING})

# List statuses where the posting window has CLOSED — the deal is out, done, or the
# window lapsed (M5). No line of such a list advertises as live supply regardless of its
# own status, so a re-sync (close_list, the nightly expiry job, or awarding a late offer)
# retires the WHOLE mirror. The pre-close statuses (draft/open/collecting) fall back to
# the per-line active check, so publishing + collecting behave exactly as before.
_POSTING_CLOSED_STATUSES = frozenset(
    {
        ExcessListStatus.BID_OUT,
        ExcessListStatus.AWARDED,
        ExcessListStatus.CLOSED,
        ExcessListStatus.EXPIRED,
    }
)


def _virtual_req_name(excess_list: ExcessList) -> str:
    """Deterministic, queryable name for *excess_list*'s virtual requisition."""
    return f"{_VIRTUAL_REQ_NAME_PREFIX}{excess_list.id})"


def ensure_virtual_requirement(db: Session, excess_list: ExcessList) -> Requirement:
    """Get-or-create the ONE system-owned virtual Requirement for *excess_list*.

    The mirrored ``Sighting.requirement_id`` (NOT NULL) hangs here. Creates a single
    ``is_scratch=True`` "Customer Excess" Requisition + Requirement per list, found
    deterministically by the requisition's stable name (``_virtual_req_name``).
    Idempotent: a second call returns the same Requirement (publishing twice does NOT
    create a second virtual req). Flushes so ids are set; does NOT commit.
    """
    name = _virtual_req_name(excess_list)
    req = (
        db.query(Requisition)
        .filter(Requisition.is_scratch.is_(True), Requisition.name == name)
        .order_by(Requisition.id.asc())
        .first()
    )
    if req is None:
        req = Requisition(
            name=name,
            customer_name=None,
            status=RequisitionStatus.OPEN,
            is_scratch=True,
            created_by=excess_list.owner_id,
        )
        db.add(req)
        db.flush()
        logger.info("excess-mirror: created virtual requisition {} for excess_list {}", req.id, excess_list.id)

    requirement = (
        db.query(Requirement).filter(Requirement.requisition_id == req.id).order_by(Requirement.id.asc()).first()
    )
    if requirement is None:
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn=None,
            normalized_mpn=None,
            sourcing_status=SourcingStatus.OPEN,
        )
        db.add(requirement)
        db.flush()
    return requirement


def _find_mirror(
    db: Session, source_company_id: int | None, material_card_id: int, requirement_id: int
) -> Sighting | None:
    """Find the existing mirrored Sighting for the upsert key.

    Upsert key is ``(source_company_id, material_card_id, requirement_id)`` scoped to
    ``source_type='customer_excess'`` — the explicit key that sidesteps the connector-
    aware delete-by-(requirement_id, source_type) dedup trap. Including ``requirement_id``
    (one per list's virtual requirement) ensures two lists for the same company+part
    each keep their own distinct Sighting rather than collapsing into one row.
    """
    return (
        db.query(Sighting)
        .filter(
            Sighting.source_type == "customer_excess",
            Sighting.source_company_id == source_company_id,
            Sighting.material_card_id == material_card_id,
            Sighting.requirement_id == requirement_id,
        )
        .order_by(Sighting.id.asc())
        .first()
    )


def mirror_line(db: Session, line: ExcessLineItem) -> Sighting | None:
    """Upsert the mirrored Sighting for one ``ExcessLineItem``.

    Builds the row via ``sighting_from_row`` (the dict→ORM single source of truth) then
    sets the excess-specific fields the contract requires:
    ``source_type='customer_excess'``, ``source_company_id`` (the customer-hiding hook),
    ``requirement_id`` = the list's virtual requirement, ``material_card_id`` +
    ``normalized_mpn`` (via ``normalize_mpn_key``) for matcher linkage, a synthesized
    internal ``vendor_name`` (NEVER the customer name), and qty/condition from the line.

    Upserts by ``(source_company_id, material_card_id)`` — a re-sync UPDATES the existing
    row instead of inserting a duplicate, and never deletes sibling ``customer_excess``
    rows. Returns the Sighting, or ``None`` when the line has no MaterialCard (the upsert
    key needs one; an unresolvable MPN is skipped, never raised). Flushes; does NOT
    commit.
    """
    excess_list = line.excess_list or db.get(ExcessList, line.excess_list_id)
    if excess_list is None:  # pragma: no cover - defensive
        logger.warning("excess-mirror: line {} has no parent excess_list; skipping mirror", line.id)
        return None

    # The upsert key requires a material_card_id. Lines resolve their card on create
    # (_resolve_line_material_card); heal a missing link lazily here, but skip cleanly
    # if the MPN still won't resolve — never raise on an unmirrorable line.
    if line.material_card_id is None:
        from ..search_service import resolve_material_card

        card = resolve_material_card(line.part_number, db, manufacturer=line.manufacturer or "")
        if card is None:
            logger.info("excess-mirror: line {} ({}) has no MaterialCard; skipping mirror", line.id, line.part_number)
            return None
        line.material_card_id = card.id

    requirement = ensure_virtual_requirement(db, excess_list)
    norm_key = normalize_mpn_key(line.part_number)

    # Synthesize the market-result dict sighting_from_row consumes. vendor_name is the
    # internal label, NOT the customer; source_type/source_company drive hiding + dedup.
    row = {
        "vendor_name": EXCESS_VENDOR_LABEL,
        "mpn_matched": line.part_number,
        "manufacturer": line.manufacturer,
        "qty_available": line.quantity,
        "unit_price": line.asking_price,
        "source_type": "customer_excess",
        "condition": line.condition,
        "date_code": line.date_code,
    }

    existing = _find_mirror(db, excess_list.company_id, line.material_card_id, requirement.id)
    if existing is None:
        sighting = sighting_from_row(requirement.id, row)
    else:
        # Re-bind the existing row to the freshly built values (in-place update keeps the
        # same Sighting id — proves the (source_company_id, material_card_id) upsert).
        sighting = sighting_from_row(requirement.id, row)
        existing.requirement_id = requirement.id
        existing.vendor_name = sighting.vendor_name
        existing.mpn_matched = sighting.mpn_matched
        existing.manufacturer = sighting.manufacturer
        existing.qty_available = sighting.qty_available
        existing.unit_price = sighting.unit_price
        existing.condition = sighting.condition
        existing.date_code = sighting.date_code
        existing.raw_data = sighting.raw_data
        existing.is_unavailable = False
        sighting = existing

    # Excess-specific columns sighting_from_row does not set.
    sighting.source_company_id = excess_list.company_id
    sighting.material_card_id = line.material_card_id
    sighting.normalized_mpn = norm_key or None

    if existing is None:
        db.add(sighting)
    db.flush()
    return sighting


def retire_line(db: Session, line: ExcessLineItem) -> None:
    """Retire (delete) the mirrored Sighting for *line* on award / withdraw / qty→0.

    Deletes the row outright (consistent with how ``_save_sightings`` removes stale
    sightings — the matcher reads live rows, never a soft-deleted excess shadow). A
    no-op when the line has no mirror or no MaterialCard. Flushes; does NOT commit.
    """
    if line.material_card_id is None:
        return
    excess_list = line.excess_list or db.get(ExcessList, line.excess_list_id)
    if excess_list is None:
        return
    company_id = excess_list.company_id
    requirement = ensure_virtual_requirement(db, excess_list)
    existing = _find_mirror(db, company_id, line.material_card_id, requirement.id)
    if existing is not None:
        db.delete(existing)
        db.flush()
        logger.info("excess-mirror: retired sighting {} for line {}", existing.id, line.id)


def _line_is_active(line: ExcessLineItem) -> bool:
    """A line is mirrored only when AVAILABLE/BIDDING with positive quantity."""
    return line.status in _ACTIVE_LINE_STATUSES and (line.quantity or 0) > 0


def _posting_is_closed(excess_list: ExcessList) -> bool:
    """True when the list's posting window has closed (bid_out / awarded / closed /
    expired)."""
    return excess_list.status in _POSTING_CLOSED_STATUSES


def sync_list_mirror(db: Session, excess_list: ExcessList) -> dict:
    """Own the dual-write for a WHOLE list: mirror active lines, retire inactive ones.

    This is the single method callers use so the line table and the Sighting table never
    drift. Ensures the virtual requirement, then for every line of *excess_list*:
    mirrors it when the posting is still open AND the line is active (AVAILABLE/BIDDING,
    qty>0), otherwise retires its mirror. A line is retired when it is individually
    inactive (awarded / withdrawn / qty→0) OR when the LIST's posting window has closed
    (bid_out / awarded / closed / expired) — a closed posting stops advertising ALL its
    supply as live, no matter the per-line status (M5). Flushes; does NOT commit (the
    caller / publish_list commits). Returns ``{"mirrored": int, "retired": int}``.
    """
    ensure_virtual_requirement(db, excess_list)
    lines = db.query(ExcessLineItem).filter_by(excess_list_id=excess_list.id).all()

    posting_closed = _posting_is_closed(excess_list)
    mirrored = 0
    retired = 0
    for line in lines:
        if not posting_closed and _line_is_active(line):
            if mirror_line(db, line) is not None:
                mirrored += 1
        else:
            retire_line(db, line)
            retired += 1

    db.flush()
    logger.info(
        "excess-mirror: synced list {} ({} mirrored, {} retired)",
        excess_list.id,
        mirrored,
        retired,
    )
    return {"mirrored": mirrored, "retired": retired}


def publish_list(db: Session, list_id: int, user) -> ExcessList:
    """Publish an excess list: flip to ``open`` then live-mirror every active line.

    The testable entry point for posting. Guards that the list is a ``draft`` (409
    otherwise — mirrors ``excess_service.close_list``: re-publishing an already-posted or
    resolved list would reopen a decided posting and re-mirror sold-through supply). Sets
    ``status=open``, stamps both ``open_at`` (the posting-window start, Chunk E) and
    ``updated_at``, PRESERVES a future ``close_at`` (the D1 owner-set posting deadline) and
    clears only a stale/past one (an open posting must not advertise a lapsed close time),
    then runs ``sync_list_mirror`` so the posted lines surface to the matcher. Commits.
    Returns the refreshed list.
    """
    from .excess_service import get_excess_list

    excess_list = get_excess_list(db, list_id)
    if excess_list.status != ExcessListStatus.DRAFT:
        raise HTTPException(409, "Only a draft list can be published")
    now = datetime.now(UTC)
    excess_list.status = ExcessListStatus.OPEN
    excess_list.open_at = now
    # Preserve a future create/draft-set deadline so the nightly expiry backstop has a real
    # window; clear only a stale (past/now) one. SQLite strips tzinfo, so stamp UTC before
    # comparing (mirrors resell._hours_until / excess_service._validate_draft_close_at).
    if excess_list.close_at is not None:
        close_at = excess_list.close_at
        if close_at.tzinfo is None:
            close_at = close_at.replace(tzinfo=UTC)
        if close_at <= now:
            excess_list.close_at = None
    excess_list.updated_at = now
    db.flush()

    sync_list_mirror(db, excess_list)

    db.commit()
    db.refresh(excess_list)
    logger.info("excess-mirror: published list {} (status=open) by user {}", list_id, getattr(user, "id", None))
    return excess_list
