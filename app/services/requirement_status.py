"""requirement_status.py — Per-part sourcing status management.

Tracks where each requirement (part) is in the sourcing pipeline:
  open → sourcing → offered → quoted → won/lost

Auto-updates status when events happen (RFQ sent, offer created, quote built).
Also handles buyer claim on requisitions.

Called by: routers/rfq.py, routers/crm/offers.py, routers/crm/quotes.py
Depends on: models (Requirement, Requisition), enums (RequirementSourcingStatus)
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ActivityType
from ..constants import SourcingStatus as RequirementSourcingStatus
from ..models import ActivityLog, Requirement, Requisition, User
from .activity_service import log_activity
from .status_machine import SOURCING_TRANSITIONS

# Per-part status transitions. The single source of truth is
# status_machine.SOURCING_TRANSITIONS — validate_transition("requirement", …)
# and transition_requirement (below) MUST agree, so both reference the same
# table. `ALLOWED_TRANSITIONS` is kept as an alias for backwards compatibility
# with existing importers; it is the same object, not a copy.
ALLOWED_TRANSITIONS: dict[str, set[str]] = SOURCING_TRANSITIONS


def transition_requirement(
    requirement: Requirement,
    new_status: str | RequirementSourcingStatus,
    db: Session,
    actor: User | None = None,
) -> bool:
    """Move a requirement to a new sourcing status.

    Returns True if status changed, False if already at target status. Raises ValueError
    for illegal transitions.
    """
    old_status = requirement.sourcing_status or RequirementSourcingStatus.OPEN
    new_val = new_status.value if isinstance(new_status, RequirementSourcingStatus) else new_status

    if old_status == new_val:
        return False

    allowed = ALLOWED_TRANSITIONS.get(old_status, set())
    if new_val not in allowed:
        logger.warning(
            "Illegal requirement transition: {} → {} (req_id={}, mpn={})",
            old_status,
            new_val,
            requirement.id,
            requirement.primary_mpn,
        )
        raise ValueError(f"Invalid requirement transition: {old_status} → {new_val}")

    requirement.sourcing_status = new_val

    try:
        actor_id = actor.id if actor else None
        log_entry = ActivityLog(
            user_id=actor_id,
            activity_type=ActivityType.PART_STATUS_CHANGE,
            channel="system",
            requisition_id=requirement.requisition_id,
            subject=f"Part {requirement.primary_mpn}: {old_status} → {new_val}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.warning("Failed to log part status transition: {}", e)

    return True


def _advance_requirements(
    requirement_ids: list[int],
    target: str,
    from_statuses: tuple[str, ...],
    db: Session,
    actor: User | None,
) -> int:
    """Advance each requirement currently in *from_statuses* to *target*.

    Returns count of requirements that changed status. Illegal transitions are skipped,
    not raised.
    """
    changed = 0
    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    for req in requirements:
        if (req.sourcing_status or RequirementSourcingStatus.OPEN) in from_statuses:
            try:
                if transition_requirement(req, target, db, actor):
                    changed += 1
            except ValueError as e:
                logger.debug("Skipping requirement {} transition to {}: {}", req.id, target, e)
    return changed


def on_rfq_sent(requirement_ids: list[int], db: Session, actor: User | None = None) -> int:
    """Mark requirements as 'sourcing' when RFQs are sent for them.

    Returns count of requirements that changed status.
    """
    return _advance_requirements(
        requirement_ids, RequirementSourcingStatus.SOURCING, (RequirementSourcingStatus.OPEN,), db, actor
    )


def on_offer_created(requirement: Requirement, db: Session, actor: User | None = None) -> bool:
    """Advance requirement to 'offered' when a confirmed offer is created.

    Only advances if currently in open/sourcing state — doesn't demote from quoted/won.
    """
    current = requirement.sourcing_status or RequirementSourcingStatus.OPEN
    if current in (RequirementSourcingStatus.OPEN, RequirementSourcingStatus.SOURCING):
        try:
            return transition_requirement(requirement, RequirementSourcingStatus.OFFERED, db, actor)
        except ValueError as e:
            logger.debug("Skipping requirement {} transition to offered: {}", requirement.id, e)
            return False
    return False


def on_quote_built(requirement_ids: list[int], db: Session, actor: User | None = None) -> int:
    """Mark requirements as 'quoted' when included in a customer quote.

    Returns count of requirements that changed status.
    """
    return _advance_requirements(
        requirement_ids,
        RequirementSourcingStatus.QUOTED,
        (RequirementSourcingStatus.OPEN, RequirementSourcingStatus.SOURCING, RequirementSourcingStatus.OFFERED),
        db,
        actor,
    )


def claim_requisition(requisition: Requisition, buyer: User, db: Session) -> bool:
    """Buyer claims a requisition for sourcing.

    Returns True if claim was set, False if already claimed by this buyer. Raises
    ValueError if already claimed by someone else.
    """
    # Re-query with FOR UPDATE to prevent TOCTOU race between concurrent claims
    locked = db.query(Requisition).filter(Requisition.id == requisition.id).with_for_update().first()
    if locked is None:
        raise ValueError("Requisition not found")

    if locked.claimed_by_id == buyer.id:
        return False

    if locked.claimed_by_id is not None:
        raise ValueError(f"Requisition already claimed by user {locked.claimed_by_id}")

    locked.claimed_by_id = buyer.id
    locked.claimed_at = datetime.now(timezone.utc)

    log_activity(
        db,
        activity_type=ActivityType.ASSIGNMENT_CHANGED,
        requisition_id=locked.id,
        user_id=buyer.id,
        description=f"Requisition claimed by {buyer.name or buyer.email}",
        details={"action": "claimed", "claimed_by_id": buyer.id},
    )

    return True


def unclaim_requisition(requisition: Requisition, db: Session, actor: User | None = None) -> bool:
    """Release a buyer's claim on a requisition."""
    if requisition.claimed_by_id is None:
        return False

    old_claimer = requisition.claimed_by_id
    requisition.claimed_by_id = None
    requisition.claimed_at = None

    log_activity(
        db,
        activity_type=ActivityType.ASSIGNMENT_CHANGED,
        requisition_id=requisition.id,
        user_id=actor.id if actor else None,
        description="Requisition unclaimed",
        details={"action": "unclaimed", "previous_claimed_by_id": old_claimer},
    )

    return True
