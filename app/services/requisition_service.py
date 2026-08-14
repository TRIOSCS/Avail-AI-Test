"""requisition_service.py — Business logic for requisition lifecycle.

Handles validation, normalization, and error mapping for requisition
operations. Keeps routers thin.

Called by: routers/requisitions/, routers/crm/clone.py, routers/htmx/parts.py
Depends on: models (Requisition, Requirement, Offer), database
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..constants import ActivityType, RequisitionStatus, SourcingStatus
from ..models import Offer, Requirement, Requisition, User
from ..utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
)
from .activity_service import log_activity

# ---------------------------------------------------------------------------
# Bulk assign — UPDATE...RETURNING
# ---------------------------------------------------------------------------
#
# This helper executes a single `UPDATE ... RETURNING id` and returns the IDs
# that were actually updated. The caller controls the transaction (commit
# happens after activity-log writes) so the audit trail and the mutation land
# atomically.


def batch_assign_owner(db: Session, ids: list[int], owner_id: int) -> list[int]:
    """Re-assign owner on specific requisitions by ID list.

    Used by the admin-only `/api/requisitions/batch-assign` route. The caller
    must verify `owner_id` references an existing user; this helper performs
    the UPDATE only. Returns the IDs that were actually re-assigned.
    """
    stmt = (
        update(Requisition)
        .where(Requisition.id.in_(ids))
        .values(claimed_by_id=owner_id)
        .returning(Requisition.id)
        .execution_options(synchronize_session=False)
    )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def to_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to UTC.

    Returns None for None input.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ---------------------------------------------------------------------------
# Validation / error mapping
# ---------------------------------------------------------------------------


def parse_date_field(value: str, field_name: str = "date") -> datetime:
    """Parse an ISO date string into a UTC datetime, raising HTTP 400 on failure."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Invalid {field_name}: {value!r} — expected ISO 8601 format") from exc
    return to_utc(dt)


def parse_positive_int(value: str | int, field_name: str = "value") -> int:
    """Parse a value as a positive integer, raising HTTP 400 on failure."""
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Invalid {field_name}: {value!r} — expected a positive integer") from exc
    if result <= 0:
        raise HTTPException(400, f"Invalid {field_name}: must be a positive integer, got {result}")
    return result


def safe_commit(db: Session, *, entity: str = "record") -> None:
    """Commit the session, mapping IntegrityError to HTTP 409."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("IntegrityError on {}: {}", entity, exc)
        raise HTTPException(409, f"Duplicate or conflicting {entity}") from exc


# ---------------------------------------------------------------------------
# Clone requisition / Clone-to-Active
# ---------------------------------------------------------------------------


def _copy_requirement_fields(source: Requirement, target_requisition_id: int) -> Requirement:
    """Build (but do not persist) a copy of ``source`` attached to another requisition.

    Copies the full sourcing spec — identity (MPN/OEM PN/brand/manufacturer/SKU/
    customer PN), commercials (qty/price), spec fields (condition, packaging, date
    codes, firmware, hardware codes, package type, revision, description, oem_hint),
    notes, material card link, and buyer assignment — with the same MPN normalization +
    substitute dedup the requisition clone always applied.

    Deliberately NOT copied (fresh-start semantics): need_by_date, sale_notes,
    outcome_reason, priority_score, last_searched_at, and sourcing_status (the caller
    decides the new part's status). Caller adds/flushes/commits.
    """
    cloned_mpn = normalize_mpn(source.primary_mpn) or source.primary_mpn
    seen_keys = {normalize_mpn_key(cloned_mpn)}
    deduped_subs: list[str] = []
    for s in source.substitutes or []:
        ns = normalize_mpn(s) or s
        key = normalize_mpn_key(ns)
        if key and key not in seen_keys:
            seen_keys.add(key)
            deduped_subs.append(ns)
    return Requirement(
        requisition_id=target_requisition_id,
        primary_mpn=cloned_mpn,
        normalized_mpn=normalize_mpn_key(cloned_mpn),
        oem_pn=source.oem_pn,
        brand=source.brand,
        manufacturer=source.manufacturer,
        sku=source.sku,
        target_qty=source.target_qty,
        target_price=source.target_price,
        substitutes=deduped_subs[:20],
        condition=normalize_condition(source.condition) or source.condition,
        packaging=normalize_packaging(source.packaging) or source.packaging,
        date_codes=source.date_codes,
        firmware=source.firmware,
        hardware_codes=source.hardware_codes,
        package_type=source.package_type,
        revision=source.revision,
        customer_pn=source.customer_pn,
        description=source.description,
        oem_hint=source.oem_hint,
        notes=source.notes,
        material_card_id=source.material_card_id,
        assigned_buyer_id=source.assigned_buyer_id,
    )


def _copy_reference_offer(
    offer: Offer,
    *,
    new_requisition_id: int,
    new_requirement_id: int | None,
    source_req_id: int,
    user_id: int,
) -> Offer:
    """Build (but do not persist) a "reference" copy of an active/selected offer."""
    return Offer(
        requisition_id=new_requisition_id,
        requirement_id=new_requirement_id,
        vendor_card_id=offer.vendor_card_id,
        vendor_name=offer.vendor_name,
        vendor_name_normalized=offer.vendor_name_normalized,
        mpn=offer.mpn,
        manufacturer=offer.manufacturer,
        qty_available=offer.qty_available,
        unit_price=offer.unit_price,
        lead_time=offer.lead_time,
        date_code=offer.date_code,
        condition=offer.condition,
        packaging=offer.packaging,
        moq=offer.moq,
        source=offer.source,
        entered_by_id=user_id,
        notes=f"Reference from REQ-{source_req_id:03d}",
        status="reference",
    )


def clone_requisition(
    db: Session,
    source_req: Requisition,
    user_id: int,
) -> Requisition:
    """Clone a requisition with its requirements and active/selected offers.

    Returns the newly created Requisition (already committed).
    """
    new_req = Requisition(
        name=f"{source_req.name} (clone)",
        customer_name=source_req.customer_name,
        customer_site_id=source_req.customer_site_id,
        status=RequisitionStatus.OPEN,
        cloned_from_id=source_req.id,
        created_by=user_id,
    )
    db.add(new_req)
    db.flush()

    # Clone requirements with MPN normalization + substitute dedup.
    # Keep a deterministic old->new ID map to avoid collisions on duplicate MPNs.
    req_map: dict[int, int] = {}
    for r in source_req.requirements:
        new_r = _copy_requirement_fields(r, new_req.id)
        db.add(new_r)
        db.flush()
        req_map[r.id] = new_r.id

    for o in source_req.offers:
        if o.status in ("active", "selected"):
            new_o = _copy_reference_offer(
                o,
                new_requisition_id=new_req.id,
                new_requirement_id=req_map.get(o.requirement_id),
                source_req_id=source_req.id,
                user_id=user_id,
            )
            db.add(new_o)
            db.flush()
            log_activity(
                db,
                activity_type=ActivityType.OFFER_CREATED,
                requisition_id=new_o.requisition_id,
                requirement_id=new_o.requirement_id,
                user_id=user_id,
                vendor_card_id=new_o.vendor_card_id,
                description=f"Offer added: {new_o.vendor_name} — {new_o.mpn}",
                details={"offer_id": new_o.id, "source": new_o.source},
            )

    safe_commit(db, entity="requisition clone")
    logger.info("Cloned requisition {} → {} for user {}", source_req.id, new_req.id, user_id)
    return new_req


# ---------------------------------------------------------------------------
# Clone-to-Active (per-part)
# ---------------------------------------------------------------------------


def clone_parts_to_active(db: Session, parts: list[Requirement], user: User) -> list[Requisition]:
    """Clone selected parts (any sourcing status) into fresh OPEN requisitions.

    "I need this part again": one new OPEN requisition is created per SOURCE
    requisition (parts grouped by ``requisition_id``, input order preserved) and
    each selected part is copied in as a new active (OPEN) requirement with
    provenance stamped via ``Requirement.cloned_from_id``. Source parts and their
    requisitions are left untouched. Active/selected offers on each source part
    are copied as "reference" offers; a PART_CLONED activity is written on both
    the source and the new requisition per part; a "Source {MPN}" task lands with
    the part's assigned buyer (falling back to the cloning user).

    Commits once (the auto-task helper commits per task afterwards — it is a
    non-critical post-commit side effect). Returns the new requisitions in
    creation order.
    """
    groups: dict[int, list[Requirement]] = {}
    for part in parts:
        groups.setdefault(part.requisition_id, []).append(part)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    new_reqs: list[Requisition] = []
    # (new_req, new_part, source_part) triples for post-commit task creation.
    cloned: list[tuple[Requisition, Requirement, Requirement]] = []

    for source_req_id, group in groups.items():
        source_req = db.get(Requisition, source_req_id)
        if source_req is None:  # FK guarantees existence; guard for safety
            logger.warning("clone_parts_to_active: source requisition {} not found; skipping", source_req_id)
            continue
        new_req = Requisition(
            name=f"{source_req.customer_name or source_req.name} — Reorder {today}",
            customer_name=source_req.customer_name,
            customer_site_id=source_req.customer_site_id,
            company_id=source_req.company_id,
            status=RequisitionStatus.OPEN,
            cloned_from_id=source_req.id,
            created_by=user.id,
            claimed_by_id=user.id,
            urgency="normal",
        )
        db.add(new_req)
        db.flush()
        new_reqs.append(new_req)

        for part in group:
            new_r = _copy_requirement_fields(part, new_req.id)
            new_r.sourcing_status = SourcingStatus.OPEN
            new_r.cloned_from_id = part.id
            db.add(new_r)
            db.flush()
            cloned.append((new_req, new_r, part))

            # Carry the SOURCE PART's live offers over as reference intel.
            part_offers = db.scalars(
                select(Offer).where(Offer.requirement_id == part.id, Offer.status.in_(("active", "selected")))
            ).all()
            for o in part_offers:
                new_o = _copy_reference_offer(
                    o,
                    new_requisition_id=new_req.id,
                    new_requirement_id=new_r.id,
                    source_req_id=source_req.id,
                    user_id=user.id,
                )
                db.add(new_o)
                db.flush()
                log_activity(
                    db,
                    activity_type=ActivityType.OFFER_CREATED,
                    requisition_id=new_o.requisition_id,
                    requirement_id=new_o.requirement_id,
                    user_id=user.id,
                    vendor_card_id=new_o.vendor_card_id,
                    description=f"Offer added: {new_o.vendor_name} — {new_o.mpn}",
                    details={"offer_id": new_o.id, "source": new_o.source},
                )

            # Provenance trail on BOTH sides.
            log_activity(
                db,
                activity_type=ActivityType.PART_CLONED,
                requisition_id=source_req.id,
                requirement_id=part.id,
                user_id=user.id,
                description=f"Part {new_r.primary_mpn} cloned to REQ-{new_req.id:03d}",
                details={"target_requisition_id": new_req.id, "target_requirement_id": new_r.id},
            )
            log_activity(
                db,
                activity_type=ActivityType.PART_CLONED,
                requisition_id=new_req.id,
                requirement_id=new_r.id,
                user_id=user.id,
                description=f"Part {new_r.primary_mpn} cloned from REQ-{source_req.id:03d}",
                details={"source_requisition_id": source_req.id, "source_requirement_id": part.id},
            )

    safe_commit(db, entity="part clone")

    # Auto "Source {MPN}" tasks — non-critical side effect AFTER the one commit
    # (task_service commits internally; running it post-commit keeps the clone
    # itself atomic). Mirrors routers/requisitions/requirements.py.
    from . import task_service

    for new_req, new_r, part in cloned:
        try:
            task_service.on_requirement_added(
                db,
                new_req.id,
                new_r.primary_mpn,
                assigned_to_id=part.assigned_buyer_id or user.id,
            )
        except Exception:  # noqa: BLE001 — non-critical side effect: a task failure must never undo the committed clone
            logger.warning("Task auto-gen failed for cloned requirement {}", new_r.id, exc_info=True)

    logger.info(
        "Clone-to-Active by user {}: {} part(s) → {} new requisition(s) {}",
        user.id,
        len(cloned),
        len(new_reqs),
        [r.id for r in new_reqs],
    )
    return new_reqs
