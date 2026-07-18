"""field_audit.py — Field-diff audit layer for the Approvals Workspace.

Purpose: Structured who/field/old→new audit rows on buy-plan / line / prepayment edits.
         Every edit path computes a diff (diff_fields), then writes ONE
         ActivityType.FIELD_EDIT ActivityLog row per save (log_field_edits) with
         details={"edits": [{field, old, new}, ...]} — batched, never one row per
         field. The change summary shown at approve time reads back through
         edits_since; the kanban "edited-by-manager" marker through
         manager_edited_line_ids.

Values are stringified once, identically on SQLite and PostgreSQL (dates → ISO-8601
UTC, Decimal → str, bool → yes/no, None → ""), so the stored JSON is
dialect-independent and diff comparison can't false-positive on type coercion.

Called by: buy-plan / QP / prepayment edit routes (app/routers/htmx/buy_plans.py,
           Phase 2), _apply_line_edit (buyplan_workflow/buyplan_lines.py),
           the approve-time change summary (_change_summary.html context builders)
Depends on: app.services.activity_service.log_activity, app.models.intelligence
            .ActivityLog, app.constants (ActivityType, Channel, UserRole),
            app.dependencies.is_manager_or_admin
"""

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ..constants import ActivityType, Channel, UserRole
from ..models.auth import User
from ..models.buy_plan import BuyPlan
from ..models.intelligence import ActivityLog


@dataclass(frozen=True)
class FieldEdit:
    """One field change within a single save: field name + stringified old/new.

    ``line_id`` (optional) attributes the edit to a specific buy-plan line when ONE
    audit row batches edits across several lines (the bulk "save all" path — one row
    per save, D4). It is a plain integer in the details JSON, NOT an FK, so it
    survives the line's deletion (the "line removed" edit). Single-line saves leave
    it None and attribute via the row's buy_plan_line_id column instead.
    """

    field: str
    old: str
    new: str
    line_id: int | None = None


@dataclass(frozen=True)
class EditRow:
    """A flattened field edit read back from the audit trail (edits_since)."""

    field: str
    old: str
    new: str
    user_id: int | None
    user_name: str | None
    at: datetime | None
    buy_plan_line_id: int | None
    prepayment_id: int | None


def _stringify(value: Any) -> str:
    """Normalize a value to its canonical audit string.

    Same serializer on both SQLite and PostgreSQL: datetimes → ISO-8601 in UTC
    (naive assumed UTC), dates → ISO, Decimal → str, bool → yes/no, None → "".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        # Local name keeps the datetime narrowing (re-assigning the Any-typed
        # parameter would widen back to Any → a no-any-return mypy error).
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def diff_fields(obj: Any, updates: dict[str, Any]) -> list[FieldEdit]:
    """Diff *updates* against *obj*'s current attribute values.

    Returns one FieldEdit per field whose stringified value actually changes; an
    unchanged field (after normalization — e.g. Decimal("5.00") vs "5.00" is a
    change, Decimal("5.00") vs Decimal("5.00") is not) produces nothing, so a no-op
    save writes no audit row.
    """
    edits: list[FieldEdit] = []
    for field, new_value in updates.items():
        old_str = _stringify(getattr(obj, field, None))
        new_str = _stringify(new_value)
        if old_str != new_str:
            edits.append(FieldEdit(field=field, old=old_str, new=new_str))
    return edits


def log_field_edits(
    db: Session,
    *,
    user: User,
    buy_plan_id: int,
    buy_plan_line_id: int | None = None,
    prepayment_id: int | None = None,
    edits: list[FieldEdit],
) -> ActivityLog | None:
    """Write ONE FIELD_EDIT ActivityLog row for a save's batched edits.

    No-op (returns None, writes nothing) when *edits* is empty. summary is
    "Edited <field, field, ...>" (truncated to the 500-char column);
    details={"edits": [{field, old, new}, ...]} carries the full diff.
    """
    if not edits:
        return None

    from .activity_service import log_activity

    summary = "Edited " + ", ".join(edit.field for edit in edits)
    # Drop the None line_id key so single-line saves keep the original 3-key edit
    # shape ({field, old, new}) — only bulk multi-line rows carry per-edit line_ids.
    serialized = []
    for edit in edits:
        entry = asdict(edit)
        if entry.get("line_id") is None:
            entry.pop("line_id", None)
        serialized.append(entry)
    return log_activity(
        db,
        activity_type=ActivityType.FIELD_EDIT,
        channel=Channel.SYSTEM,
        user_id=user.id,
        buy_plan_id=buy_plan_id,
        buy_plan_line_id=buy_plan_line_id,
        prepayment_id=prepayment_id,
        summary=summary[:500],
        details={"edits": serialized},
    )


def edits_since(db: Session, *, buy_plan_id: int, since: datetime | None) -> list[EditRow]:
    """All field edits on a plan (any subject: plan / line / prepayment) since *since*,
    flattened one EditRow per changed field, oldest row first.

    since=None returns the plan's full edit history. Backs the approve-time change
    summary (_change_summary.html).
    """
    query = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.activity_type == ActivityType.FIELD_EDIT.value,
            ActivityLog.buy_plan_id == buy_plan_id,
        )
        .order_by(ActivityLog.created_at.asc(), ActivityLog.id.asc())
    )
    if since is not None:
        query = query.filter(ActivityLog.created_at >= since)

    rows: list[EditRow] = []
    for record in query.all():
        user_name = record.user.name if record.user else None
        for edit in (record.details or {}).get("edits", []):
            rows.append(
                EditRow(
                    field=edit.get("field", ""),
                    old=edit.get("old", ""),
                    new=edit.get("new", ""),
                    user_id=record.user_id,
                    user_name=user_name,
                    at=record.created_at,
                    # Per-edit line attribution (bulk rows) wins over the row's column.
                    buy_plan_line_id=edit.get("line_id") or record.buy_plan_line_id,
                    prepayment_id=record.prepayment_id,
                )
            )
    return rows


def format_change_summary(rows: list[EditRow], limit: int = 25) -> str:
    """Render EditRows as the plain-text "was X → now Y" change summary (one line per
    field) for the in-app notification body. Empty input → empty string (callers skip
    the notification — spec §7: the summary is empty if nothing changed)."""
    lines = [f"{row.field}: was {row.old or '—'} → now {row.new or '—'}" for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"… and {len(rows) - limit} more change(s)")
    return "\n".join(lines)


def manager_edited_line_ids(db: Session, plan: BuyPlan) -> set[int]:
    """IDs of *plan*'s lines that a manager/admin has field-edited.

    Drives the kanban card's "edited by manager" marker. Same supervisor tier as
    dependencies.is_manager_or_admin (MANAGER or ADMIN role).
    """
    rows = (
        db.query(ActivityLog.buy_plan_line_id)
        .join(User, User.id == ActivityLog.user_id)
        .filter(
            ActivityLog.activity_type == ActivityType.FIELD_EDIT.value,
            ActivityLog.buy_plan_id == plan.id,
            ActivityLog.buy_plan_line_id.isnot(None),
            User.role.in_((UserRole.MANAGER.value, UserRole.ADMIN.value)),
        )
        .distinct()
        .all()
    )
    return {line_id for (line_id,) in rows}
