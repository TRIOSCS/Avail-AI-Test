"""Material Card Audit Service.

Logs material card lifecycle events to the material_card_audit table.
Events: created, linked, unlinked, deleted, merged, healed, restored, soft_deleted.

All functions accept a db Session and do NOT commit — caller is responsible for commit.
"""

from sqlalchemy.orm import Session

from ..models import MaterialCardAudit


def log_audit(
    db: Session,
    *,
    material_card_id: int | None,
    action: str,
    normalized_mpn: str | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    old_card_id: int | None = None,
    new_card_id: int | None = None,
    details: dict | None = None,
    created_by: str = "system",
) -> MaterialCardAudit:
    """Write a single audit record.

    Does not commit.
    """
    entry = MaterialCardAudit(
        material_card_id=material_card_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        old_card_id=old_card_id,
        new_card_id=new_card_id,
        normalized_mpn=normalized_mpn,
        details=details,
        created_by=created_by,
    )
    db.add(entry)
    return entry
