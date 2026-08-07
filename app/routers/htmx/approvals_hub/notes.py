"""Approvals Workspace notes + attachments routes (2.4 — one thread per item, never
status-locked): subject resolution, the thread re-render, and the add-note / add-
attachment / remove-attachment routes.

W4.8 split of app/routers/htmx/approvals_hub.py — pure structural move: URLs and
behavior unchanged; routes attach to the shared router imported from .common.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_user
from ....models import BuyPlanLine, User
from ....template_env import template_response
from .._shared import _base_ctx
from .common import _notes_ctx, router


def _resolve_note_subject(db: Session, user: User, form) -> tuple[int, int | None, int | None]:
    """Resolve + authorize the (plan_id, line_id, prepayment_id) subject from a
    notes/attachments form — exactly ONE subject id must be posted (400 otherwise).

    Access rides get_buyplan_for_user on the owning plan (restricted non-owner → 404).
    NO status guard on purpose: notes and files never lock (spec §7).
    """
    from ....dependencies import get_buyplan_for_user
    from ....models.quality_plan import Prepayment

    def _opt_int(name: str) -> int | None:
        raw = (form.get(name) or "").strip() if form.get(name) is not None else ""
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"{name} must be a whole number.") from e

    plan_id = _opt_int("buy_plan_id")
    line_id = _opt_int("buy_plan_line_id")
    prepayment_id = _opt_int("prepayment_id")
    if sum(1 for v in (plan_id, line_id, prepayment_id) if v is not None) != 1:
        raise HTTPException(400, "Exactly one of buy_plan_id, buy_plan_line_id or prepayment_id is required.")

    if line_id is not None:
        line = db.get(BuyPlanLine, line_id)
        if line is None:
            raise HTTPException(404, "PO line not found")
        plan_id = line.buy_plan_id
    elif prepayment_id is not None:
        pp = db.get(Prepayment, prepayment_id)
        if pp is None:
            raise HTTPException(404, "Prepayment not found")
        plan_id = pp.buy_plan_id

    assert plan_id is not None  # all three branches resolve it
    get_buyplan_for_user(db, user, plan_id)
    return plan_id, line_id, prepayment_id


def _render_notes_thread(
    request: Request, user: User, db: Session, *, plan_id: int, line_id: int | None, prepayment_id: int | None
) -> HTMLResponse:
    """Re-render just the notes thread block for its subject (the notes/attachments
    POST/DELETE responses — the forms target #aw-notes-thread with outerHTML)."""
    ctx = _base_ctx(request, user, "buy-plans")
    ctx.update(_notes_ctx(db, user, plan_id=plan_id, line_id=line_id, prepayment_id=prepayment_id))
    return template_response("htmx/partials/approvals/_notes_thread.html", ctx)


@router.post("/v2/partials/approvals/notes", response_class=HTMLResponse)
async def approvals_add_note(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a note to a sales order / PO line / prepayment thread (design D2).

    Notes are NEVER status-locked — field locks never lock conversation. The note lands
    as an ActivityLog NOTE row on the narrowest subject; re-renders the thread.
    """
    from ....services.workspace_notes import add_note

    form = await request.form()
    plan_id, line_id, prepayment_id = _resolve_note_subject(db, user, form)

    try:
        add_note(
            db,
            user=user,
            body=str(form.get("body") or ""),
            buy_plan_id=plan_id,
            buy_plan_line_id=line_id,
            prepayment_id=prepayment_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _render_notes_thread(request, user, db, plan_id=plan_id, line_id=line_id, prepayment_id=prepayment_id)


@router.post("/v2/partials/approvals/attachments", response_class=HTMLResponse)
async def approvals_add_attachment(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file onto a sales order / PO line / prepayment (design D3).

    Multipart → the shared attachment_service.store_and_attach with BuyPlanAttachment
    and the subject's fk_field; the exactly-one-subject invariant is asserted via
    validate_subject() on the constructed kwargs BEFORE store_and_attach runs (the app-
    level stand-in for a DB CHECK — store_and_attach commits internally, so a post-store
    check could never undo a bad row). The upload is ActivityLog'd (ATTACH_ADDED). Never
    status-locked.
    """
    from ....constants import ActivityType, Channel
    from ....models.buy_plan import BuyPlanAttachment
    from ....services import attachment_service
    from ....services.activity_service import log_activity

    form = await request.form()
    plan_id, line_id, prepayment_id = _resolve_note_subject(db, user, form)

    file = form.get("file")
    if file is None or isinstance(file, str):
        raise HTTPException(400, "A file is required.")

    if line_id is not None:
        fk_field, entity_label, entity_id = "buy_plan_line_id", "BuyPlanLines", line_id
    elif prepayment_id is not None:
        fk_field, entity_label, entity_id = "prepayment_id", "Prepayments", prepayment_id
    else:
        fk_field, entity_label, entity_id = "buy_plan_id", "BuyPlans", plan_id

    # Exactly-one-subject invariant, asserted on the constructed kwargs BEFORE the
    # upload/insert — store_and_attach commits internally, so validating afterwards
    # would leave an invalid row already persisted.
    try:
        BuyPlanAttachment(**{fk_field: entity_id}).validate_subject()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    att = await attachment_service.store_and_attach(
        db,
        model=BuyPlanAttachment,
        fk_field=fk_field,
        entity_label=entity_label,
        entity_id=entity_id,
        file=file,
        user=user,
    )

    log_activity(
        db,
        activity_type=ActivityType.ATTACH_ADDED,
        channel=Channel.SYSTEM,
        user_id=user.id,
        buy_plan_id=plan_id,
        buy_plan_line_id=line_id,
        prepayment_id=prepayment_id,
        summary=f"Attached {att.file_name}",
    )
    db.commit()
    return _render_notes_thread(request, user, db, plan_id=plan_id, line_id=line_id, prepayment_id=prepayment_id)


@router.delete("/v2/partials/approvals/attachments/{attachment_id:int}", response_class=HTMLResponse)
async def approvals_remove_attachment(
    request: Request,
    attachment_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Remove a workspace attachment — the uploader or a manager/admin (spec §7).

    Best-effort cloud delete + DB delete via the shared remove_attachment; the removal
    is ActivityLog'd (ATTACH_REMOVED). Re-renders the subject's thread.
    """
    from ....constants import ActivityType, Channel
    from ....dependencies import is_manager_or_admin
    from ....models.buy_plan import BuyPlanAttachment
    from ....services import attachment_service
    from ....services.activity_service import log_activity

    att = db.get(BuyPlanAttachment, attachment_id)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    if att.uploaded_by_id != user.id and not is_manager_or_admin(user):
        raise HTTPException(403, "Only the uploader or a manager can remove an attachment.")

    # Capture the subject BEFORE the delete — the row is gone afterwards.
    line_id = att.buy_plan_line_id
    prepayment_id = att.prepayment_id
    if line_id is not None:
        line = db.get(BuyPlanLine, line_id)
        plan_id = line.buy_plan_id if line is not None else None
    elif prepayment_id is not None:
        from ....models.quality_plan import Prepayment

        pp = db.get(Prepayment, prepayment_id)
        plan_id = pp.buy_plan_id if pp is not None else None
    else:
        plan_id = att.buy_plan_id
    if plan_id is None:
        raise HTTPException(404, "Attachment subject not found")
    file_name = att.file_name

    await attachment_service.remove_attachment(db, att, user)

    log_activity(
        db,
        activity_type=ActivityType.ATTACH_REMOVED,
        channel=Channel.SYSTEM,
        user_id=user.id,
        buy_plan_id=plan_id,
        buy_plan_line_id=line_id,
        prepayment_id=prepayment_id,
        summary=f"Removed attachment {file_name}",
    )
    db.commit()
    return _render_notes_thread(request, user, db, plan_id=plan_id, line_id=line_id, prepayment_id=prepayment_id)
