"""Auto-progress sourcing status based on workflow events.

Called by: sightings router (send-inquiry), offers router
Depends on: status_machine.py transitions, ActivityLog model
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import SourcingStatus
from ..models.intelligence import ActivityLog
from ..services.status_machine import validate_transition

# Ordered status progression — forward-only
_STATUS_ORDER = [
    SourcingStatus.OPEN,
    SourcingStatus.SOURCING,
    SourcingStatus.OFFERED,
    SourcingStatus.QUOTED,
    SourcingStatus.WON,
]


def auto_progress_status(
    requirement,
    target_status: SourcingStatus,
    db: Session,
    user_id: int | None = None,
) -> bool:
    """Auto-progress requirement's sourcing_status if it's behind target.

    Returns True if status was updated, False if already at or ahead. Never goes
    backwards. Logs ActivityLog on change.
    """
    current = requirement.sourcing_status
    if current == target_status:
        return False

    # Check ordering — only progress forward
    try:
        current_idx = _STATUS_ORDER.index(current)
        target_idx = _STATUS_ORDER.index(target_status)
    except ValueError:
        return False  # Status not in progression (e.g., LOST, ARCHIVED)

    if current_idx >= target_idx:
        return False  # Already at or ahead

    # Validate the transition is allowed (no raise — returns bool)
    if not validate_transition("requirement", current, target_status, raise_on_invalid=False):
        logger.warning(
            "Auto-progress blocked: %s → %s not a valid transition",
            current,
            target_status,
        )
        return False

    old_status = requirement.sourcing_status
    requirement.sourcing_status = target_status

    # Log status change
    db.add(
        ActivityLog(
            requirement_id=requirement.id,
            requisition_id=requirement.requisition_id,
            user_id=user_id,
            activity_type="status_change",
            channel="system",
            notes=f"Auto-progressed from {old_status} to {target_status}",
        )
    )

    logger.info(
        "Auto-progressed requirement %d: %s → %s",
        requirement.id,
        old_status,
        target_status,
    )
    return True
