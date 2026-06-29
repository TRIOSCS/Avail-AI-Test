"""CRM field-history audit trail — record + query per-field old→new changes.

What: thin helpers over the ``crm_field_history`` table. ``record_field_change``
appends one row when (and only when) a single CRM field actually changed value;
``field_history_for`` returns a record's change history newest-first for the
History tab (company) / History modal (contact).

Called by: app/routers/htmx/companies.py (inline field POST handlers + history
    surfaces).
Depends on: app/models/crm.CrmFieldHistory.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import CrmFieldHistory

# Polymorphic entity-type discriminators stored in crm_field_history.entity_type.
ENTITY_COMPANY = "company"
ENTITY_CONTACT = "contact"


def _canonical(value: object) -> str:
    """Canonical string form of a field value for change comparison.

    None and blank/whitespace both collapse to "" so a None→"" edit is a no-op (matches
    the inline-edit semantics where a cleared field stores NULL).
    """
    if value is None:
        return ""
    return str(value).strip()


def record_field_change(
    db: Session,
    *,
    entity_type: str,
    entity_id: int,
    field_name: str,
    old_value: object,
    new_value: object,
    user_id: int | None,
) -> CrmFieldHistory | None:
    """Append a field-history row iff the value actually changed (no commit).

    Returns the new row, or None when old == new (no row written). The caller owns the
    transaction (commits alongside the field write) so the history row and the edit land
    atomically.
    """
    old_c = _canonical(old_value)
    new_c = _canonical(new_value)
    if old_c == new_c:
        return None
    row = CrmFieldHistory(
        entity_type=entity_type,
        entity_id=entity_id,
        field_name=field_name,
        old_value=old_c or None,
        new_value=new_c or None,
        changed_by_id=user_id,
    )
    db.add(row)
    return row


def field_history_for(
    db: Session,
    entity_type: str,
    entity_id: int,
    limit: int = 50,
) -> list[CrmFieldHistory]:
    """Return a record's field-change history, newest-first (joined to editor)."""
    from sqlalchemy.orm import joinedload

    return (
        db.query(CrmFieldHistory)
        .options(joinedload(CrmFieldHistory.changed_by))
        .filter(
            CrmFieldHistory.entity_type == entity_type,
            CrmFieldHistory.entity_id == entity_id,
        )
        .order_by(CrmFieldHistory.created_at.desc(), CrmFieldHistory.id.desc())
        .limit(limit)
        .all()
    )
