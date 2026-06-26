"""requisition_service.py — Business logic for requisition lifecycle.

Handles validation, normalization, and error mapping for requisition
operations. Keeps routers thin.

Called by: routers/requisitions/, routers/crm/clone.py
Depends on: models (Requisition, Requirement, Offer), database
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..constants import RESTRICTED_ROLES, ActivityType, RequisitionStatus
from ..models import Offer, Requirement, Requisition, User
from ..utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
)
from .activity_service import log_activity

# ---------------------------------------------------------------------------
# Bulk archive / assign — UPDATE...RETURNING
# ---------------------------------------------------------------------------
#
# These helpers execute a single `UPDATE ... RETURNING id` per call and return
# the IDs that were actually updated. The caller controls the transaction
# (commit happens after activity-log writes) so the audit trail and the
# status mutation land atomically.


def bulk_archive_others(db: Session, user_id: int) -> list[int]:
    """Archive all active requisitions NOT created by `user_id`.

    Used by the admin-only `/api/requisitions/bulk-archive` route. Terminal statuses
    (won / lost / cancelled) and already-archived rows are excluded so the operation is
    idempotent. Returns the IDs that were actually flipped to archived (may be empty).
    """
    stmt = (
        update(Requisition)
        .where(
            Requisition.created_by != user_id,
            Requisition.status.notin_(RequisitionStatus.TERMINAL),
            Requisition.is_archived.is_(False),
        )
        .values(is_archived=True)
        .returning(Requisition.id)
        .execution_options(synchronize_session=False)
    )
    return list(db.execute(stmt).scalars().all())


def batch_archive_for_user(db: Session, user: User, ids: list[int]) -> list[int]:
    """Archive specific requisitions by ID list, respecting role-based ownership.

    Sales users may only archive their own requisitions; other roles may
    archive any. Terminal statuses and already-archived rows are excluded.
    Returns the IDs that were actually archived (which may be a strict subset
    of `ids`).
    """
    conditions = [
        Requisition.id.in_(ids),
        Requisition.status.notin_(RequisitionStatus.TERMINAL),
        Requisition.is_archived.is_(False),
    ]
    if user.role in RESTRICTED_ROLES:
        conditions.append(Requisition.created_by == user.id)

    stmt = (
        update(Requisition)
        .where(*conditions)
        .values(is_archived=True)
        .returning(Requisition.id)
        .execution_options(synchronize_session=False)
    )
    return list(db.execute(stmt).scalars().all())


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
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Validation / error mapping
# ---------------------------------------------------------------------------


def parse_date_field(value: str, field_name: str = "date") -> datetime:
    """Parse an ISO date string into a UTC datetime, raising HTTP 400 on failure."""
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, f"Invalid {field_name}: {value!r} — expected ISO 8601 format") from exc
    return to_utc(dt)  # type: ignore[return-value]


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
# Clone requisition
# ---------------------------------------------------------------------------


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
        cloned_mpn = normalize_mpn(r.primary_mpn) or r.primary_mpn
        seen_keys = {normalize_mpn_key(cloned_mpn)}
        deduped_subs: list[str] = []
        for s in r.substitutes or []:
            ns = normalize_mpn(s) or s
            key = normalize_mpn_key(ns)
            if key and key not in seen_keys:
                seen_keys.add(key)
                deduped_subs.append(ns)
        new_r = Requirement(
            requisition_id=new_req.id,
            primary_mpn=cloned_mpn,
            normalized_mpn=normalize_mpn_key(cloned_mpn),
            oem_pn=r.oem_pn,
            brand=r.brand,
            sku=r.sku,
            target_qty=r.target_qty,
            target_price=r.target_price,
            substitutes=deduped_subs[:20],
            condition=normalize_condition(r.condition) or r.condition,
            packaging=normalize_packaging(r.packaging) or r.packaging,
            notes=r.notes,
        )
        db.add(new_r)
        db.flush()
        req_map[r.id] = new_r.id

    for o in source_req.offers:
        if o.status in ("active", "selected"):
            new_o = Offer(
                requisition_id=new_req.id,
                requirement_id=req_map.get(o.requirement_id),
                vendor_card_id=o.vendor_card_id,
                vendor_name=o.vendor_name,
                vendor_name_normalized=o.vendor_name_normalized,
                mpn=o.mpn,
                manufacturer=o.manufacturer,
                qty_available=o.qty_available,
                unit_price=o.unit_price,
                lead_time=o.lead_time,
                date_code=o.date_code,
                condition=o.condition,
                packaging=o.packaging,
                moq=o.moq,
                source=o.source,
                entered_by_id=user_id,
                notes=f"Reference from REQ-{source_req.id:03d}",
                status="reference",
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
