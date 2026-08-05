"""excess_mirror.py — read-side helpers + deletion teardown for the RETIRED
resell→Sighting mirror.

What: the dual-write that mirrored every posted ``ExcessLineItem`` into the ``Sighting``
      table is STOPPED (SIMPLIFICATION_SPEC §5.3): nothing reads the mirror while the
      internal-trader lane and Proactive matching are parked. Posting/awarding/closing a
      list no longer writes, updates, or retires Sightings. The write path (the old
      ``sync_list_mirror`` / ``mirror_line`` / ``retire_line`` /
      ``ensure_virtual_requirement``) was deleted, not flagged — the mirror RETURNS with
      whichever unparks first (trader lane or Proactive) via ``git restore`` of this file
      and its call sites.

What remains, and why:
    - ``MIRROR_SOURCE_TYPE`` + ``mirror_sighting_filter()`` — mirror rows written before
      the stop STAY in the table (tables are never dropped), so every surface that shows
      REAL vendor intelligence (global search, part history, vendor affinity, material
      cards) must keep excluding them. Use the ONE canonical predicate below, never an
      inlined ``source_type`` literal. See docs/APP_MAP_INTERACTIONS.md
      "Mirror-consumer exclusions".
    - ``teardown_list_mirror`` — deleting a list / merging a company must still clean up
      any PRE-EXISTING mirror rows + the virtual scratch requisition, or they orphan.
    - ``publish_list`` — the posting state transition (draft → open, ``open_at`` /
      ``close_at`` stamping) lives here historically; it now performs ONLY the status
      flip.

Calls: models (ExcessList, Requisition, Requirement, Sighting),
       sighting_aggregation.rebuild_vendor_summaries (teardown-time
       VendorSightingSummary invalidation).
Depends on: a request-scoped Session. Flushes so ids are set; the CALLER commits
       (publish_list commits itself, matching excess_service's _safe_commit style).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.orm import Session

from ..constants import ExcessListStatus
from ..models.excess import ExcessList
from ..models.sourcing import Requirement, Requisition, Sighting

# The mirror's explicit source_type marker (stamped by the retired write path on every
# row it wrote). The single robust, no-join-required signal that a Sighting is a
# synthetic excess-mirror advertising row rather than real third-party vendor
# intelligence. Rows carrying it may still exist; only new writes stopped.
MIRROR_SOURCE_TYPE = "customer_excess"


def mirror_sighting_filter() -> ColumnElement[bool]:
    """The ONE canonical SQLAlchemy predicate for excluding synthetic mirror Sightings.

    Use this at every consumer that must surface only REAL vendor intelligence — global
    search, vendor-affinity aggregation, etc. — instead of re-inlining the
    ``source_type == "customer_excess"`` literal (finding #25/#28/#59, THEME F). Reads
    ``Sighting.source_type`` directly (always set by the retired write path, no join
    needed). Uses ``is_distinct_from`` (NULL-safe) rather than plain ``!=`` — SQL's
    three-valued logic means ``source_type != 'customer_excess'`` is NULL (excluded!) for
    the many real sightings whose ``source_type`` is NULL, which would have silently
    dropped them from every consumer of this filter.
    """
    return Sighting.source_type.is_distinct_from(MIRROR_SOURCE_TYPE)


# Deterministic name marker for the per-list virtual requisition the retired write path
# created (``is_scratch=True``, one per mirrored list). Keyed on the list id so the
# teardown lookup is exact and collision-free.
_VIRTUAL_REQ_NAME_PREFIX = "Customer Excess (list "


def _virtual_req_name(excess_list: ExcessList) -> str:
    """Deterministic, queryable name for *excess_list*'s virtual requisition."""
    return f"{_VIRTUAL_REQ_NAME_PREFIX}{excess_list.id})"


def _invalidate_vendor_summaries_for_cards(db: Session, material_card_ids: set[int | None]) -> int:
    """Recompute (dropping if now absent) the 'customer excess' VendorSightingSummary
    row on every REAL requirement whose vendor board could have aggregated a mirrored
    Sighting for one of *material_card_ids*.

    ``rebuild_vendor_summaries`` pulls sightings by MaterialCard linkage keyed on the
    requirement's own ``normalized_mpn`` — so a requirement sharing a torn-down line's
    normalized MPN is a candidate. Scoping via the indexed ``Requirement.normalized_mpn``
    column (populated at requirement-creation) avoids a full-table scan, and rebuilding
    drops the stale 'customer excess' row when it's no longer backed by a live sighting
    (sighting_aggregation.rebuild_vendor_summaries). Without this, tearing down a mirror
    leaves the requirement's vendor board advertising the now-deleted supply forever
    (finding #27, THEME F). Returns the number of requirements refreshed.
    """
    from ..models.intelligence import MaterialCard
    from .sighting_aggregation import rebuild_vendor_summaries

    card_ids = {c for c in material_card_ids if c is not None}
    if not card_ids:
        return 0

    norm_keys = {
        k
        for (k,) in db.query(MaterialCard.normalized_mpn)
        .filter(MaterialCard.id.in_(card_ids), MaterialCard.normalized_mpn.isnot(None))
        .all()
    }
    if not norm_keys:
        return 0

    requirement_ids = [
        rid for (rid,) in db.query(Requirement.id).filter(Requirement.normalized_mpn.in_(norm_keys)).all()
    ]
    for rid in requirement_ids:
        # skip_ai_estimates: this runs inside user-facing delete/merge transactions — a
        # synchronous Claude call here would stall the request; the next routine search
        # rebuild re-estimates.
        rebuild_vendor_summaries(db, rid, skip_ai_estimates=True)
    return len(requirement_ids)


def teardown_list_mirror(db: Session, excess_list: ExcessList) -> dict:
    """Delete a list's ENTIRE pre-existing Sighting mirror + its virtual scratch
    requisition — for list/company DELETION only.

    The dual-write is retired, but rows written BEFORE the stop remain — deleting their
    list (or merging away their company) must still clean them up. Looks up the per-list
    virtual Requisition by its deterministic name (``_virtual_req_name``), then,
    leaf→root (SQLite FKs are enforced in tests):
      1. bulk-deletes every ``customer_excess`` Sighting hanging on that requisition's
         Requirement(s) — keyed on ``requirement_id`` (always set), so it is robust to
         ``material_card_id`` / ``source_company_id`` being NULL;
      2. deletes the virtual Requirement(s) and the Requisition, so no orphan scratch req
         survives to advertise deleted supply.

    Scoped STRICTLY to *excess_list*'s own virtual req — a sibling list for the same company
    owns a DISTINCT virtual req and is untouched (never wipe by company). Must run BEFORE the
    ``ExcessList`` / company rows are deleted (it needs ``excess_list.id``). A no-op (all
    zeros) when the list was never mirrored. Flushes; the CALLER commits. Returns
    ``{"sightings": int, "requirements": int, "requisitions": int}``.
    """
    name = _virtual_req_name(excess_list)
    req = (
        db.execute(
            select(Requisition)
            .where(Requisition.is_scratch.is_(True), Requisition.name == name)
            .order_by(Requisition.id.asc())
        )
        .scalars()
        .first()
    )
    if req is None:
        return {"sightings": 0, "requirements": 0, "requisitions": 0}

    requirement_ids = list(
        db.execute(select(Requirement.id).where(Requirement.requisition_id == req.id)).scalars().all()
    )
    sightings_deleted = 0
    affected_card_ids: set[int | None] = set()
    if requirement_ids:
        # Capture the material cards BEFORE the bulk delete so the post-teardown
        # VendorSightingSummary invalidation (#27) knows which real requirements' vendor
        # boards may have aggregated this supply.
        affected_card_ids = set(
            db.execute(
                select(Sighting.material_card_id).where(
                    Sighting.source_type == MIRROR_SOURCE_TYPE,
                    Sighting.requirement_id.in_(requirement_ids),
                )
            )
            .scalars()
            .all()
        )
        sightings_deleted = (
            db.execute(
                delete(Sighting)
                .where(
                    Sighting.source_type == MIRROR_SOURCE_TYPE,
                    Sighting.requirement_id.in_(requirement_ids),
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            or 0
        )
    requirements_deleted = (
        db.execute(
            delete(Requirement).where(Requirement.requisition_id == req.id).execution_options(synchronize_session=False)
        ).rowcount
        or 0
    )
    db.execute(delete(Requisition).where(Requisition.id == req.id).execution_options(synchronize_session=False))
    db.flush()
    _invalidate_vendor_summaries_for_cards(db, affected_card_ids)
    logger.info(
        "excess-mirror: tore down list {} mirror ({} sightings, {} requirements, 1 requisition)",
        excess_list.id,
        sightings_deleted,
        requirements_deleted,
    )
    return {"sightings": sightings_deleted, "requirements": requirements_deleted, "requisitions": 1}


def publish_list(db: Session, list_id: int, user) -> ExcessList:
    """Publish an excess list: flip it to ``open``.

    The testable entry point for posting. Guards that the list is a ``draft`` (409
    otherwise — mirrors ``excess_service.close_list``: re-publishing an already-posted or
    resolved list would reopen a decided posting). Sets ``status=open``, stamps both
    ``open_at`` (the posting-window start, Chunk E) and ``updated_at``, PRESERVES a
    future ``close_at`` (the D1 owner-set posting deadline) and clears only a stale/past
    one (an open posting must not advertise a lapsed close time). Writes NO Sightings —
    the resell→Sighting dual-write is retired (SIMPLIFICATION_SPEC §5.3). Commits.
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

    db.commit()
    db.refresh(excess_list)
    logger.info("excess-mirror: published list {} (status=open) by user {}", list_id, getattr(user, "id", None))
    return excess_list
