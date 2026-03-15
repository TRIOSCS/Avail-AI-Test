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

from ..enums import RequirementSourcingStatus
from ..models import ActivityLog, Requirement, Requisition, User

# Valid per-part status transitions
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "open": {"sourcing", "offered", "quoted", "won", "lost"},
    "sourcing": {"offered", "quoted", "won", "lost", "open"},
    "offered": {"quoted", "won", "lost", "sourcing"},
    "quoted": {"won", "lost", "offered"},
    "won": {"lost"},
    "lost": {"open", "sourcing"},
}


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
    old_status = requirement.sourcing_status or "open"
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
            activity_type="part_status_change",
            channel="system",
            requisition_id=requirement.requisition_id,
            subject=f"Part {requirement.primary_mpn}: {old_status} → {new_val}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.debug("Failed to log part status transition: %s", e)

    return True


def on_rfq_sent(requirement_ids: list[int], db: Session, actor: User | None = None) -> int:
    """Mark requirements as 'sourcing' when RFQs are sent for them.

    Returns count of requirements that changed status.
    """
    changed = 0
    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    for req in requirements:
        if (req.sourcing_status or "open") == "open":
            try:
                if transition_requirement(req, "sourcing", db, actor):
                    changed += 1
            except ValueError:
                pass
    return changed


def on_offer_created(requirement: Requirement, db: Session, actor: User | None = None) -> bool:
    """Advance requirement to 'offered' when a confirmed offer is created.

    Only advances if currently in open/sourcing state — doesn't demote from quoted/won.
    """
    current = requirement.sourcing_status or "open"
    if current in ("open", "sourcing"):
        try:
            return transition_requirement(requirement, "offered", db, actor)
        except ValueError:
            return False
    return False


def on_quote_built(requirement_ids: list[int], db: Session, actor: User | None = None) -> int:
    """Mark requirements as 'quoted' when included in a customer quote.

    Returns count of requirements that changed status.
    """
    changed = 0
    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()
    for req in requirements:
        current = req.sourcing_status or "open"
        if current in ("open", "sourcing", "offered"):
            try:
                if transition_requirement(req, "quoted", db, actor):
                    changed += 1
            except ValueError:
                pass
    return changed


def claim_requisition(requisition: Requisition, buyer: User, db: Session) -> bool:
    """Buyer claims a requisition for sourcing.

    Returns True if claim was set, False if already claimed by this buyer. Raises
    ValueError if already claimed by someone else.
    """
    if requisition.claimed_by_id == buyer.id:
        return False

    if requisition.claimed_by_id is not None:
        raise ValueError(f"Requisition already claimed by user {requisition.claimed_by_id}")

    requisition.claimed_by_id = buyer.id
    requisition.claimed_at = datetime.now(timezone.utc)

    try:
        log_entry = ActivityLog(
            user_id=buyer.id,
            activity_type="requisition_claimed",
            channel="system",
            requisition_id=requisition.id,
            subject=f"Claimed by {buyer.name or buyer.email}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.debug("Failed to log requisition claim: %s", e)

    return True


def unclaim_requisition(requisition: Requisition, db: Session, actor: User | None = None) -> bool:
    """Release a buyer's claim on a requisition."""
    if requisition.claimed_by_id is None:
        return False

    old_claimer = requisition.claimed_by_id
    requisition.claimed_by_id = None
    requisition.claimed_at = None

    try:
        actor_id = actor.id if actor else None
        log_entry = ActivityLog(
            user_id=actor_id,
            activity_type="requisition_unclaimed",
            channel="system",
            requisition_id=requisition.id,
            subject=f"Released from user {old_claimer}",
        )
        db.add(log_entry)
    except Exception as e:
        logger.debug("Failed to log requisition unclaim: %s", e)

    return True
