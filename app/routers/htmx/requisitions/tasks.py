"""Requisition Task board mutations — create/complete/delete/edit endpoints.

W4.8 split of the 1,473-line app/routers/htmx/requisitions.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    TaskStatus,
    UserRole,
)
from ....database import get_db
from ....dependencies import require_requisition_access, require_user
from ....models import (
    Requisition,
    RequisitionTask,
    User,
)
from ....services.task_service import create_requisition_task, delete_task, is_task_mutation_authorized, update_task
from ....template_env import template_response
from ..._lookup_helpers import get_requisition_or_404
from .._shared import _base_ctx, _parse_task_due_date
from .common import router

# ── Requisition Task board mutations ─────────────────────────────────────
# These back the create/complete/delete buttons on the requisition detail
# "Tasks" tab (requisitions/tabs/tasks.html). A requisition-board task is a
# RequisitionTask with requisition_id set and requirement_id NULL. The board is
# shared per requisition for viewing/creating (gated by
# require_requisition_access), but mutations additionally pass the shared
# task-mutation gate (creator | assignee | admin —
# task_service.is_task_mutation_authorized). Templates: _task_list.html
# (create swap) / _task_row.html (complete swap).


def _coerce_task_priority(raw: str | None) -> int:
    """Map a submitted priority ('1'|'2'|'3') to a valid int, defaulting to 2
    (medium)."""
    try:
        p = int(raw) if raw not in (None, "") else 2
    except (TypeError, ValueError):
        return 2
    return p if p in (1, 2, 3) else 2


def _parse_int_or_none(raw: str | None) -> int | None:
    """Parse an optional integer form field ('' / None → None)."""
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.post("/api/requisitions/{req_id}/tasks", response_class=HTMLResponse)
async def create_requisition_task_endpoint(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a task on a requisition's Task board; return the re-rendered list body.

    The board form carries title + type + priority + assignee + due date and swaps the
    response into #task-list (innerHTML), so we return the full list partial (this also
    clears the empty state on the first add). Gated by require_requisition_access.
    """
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)

    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Title is required")
    assigned_to_id = _parse_int_or_none(form.get("assigned_to_id"))
    if assigned_to_id is None:
        raise HTTPException(422, "Assignee is required")

    create_requisition_task(
        db,
        requisition_id=req_id,
        title=title,
        task_type=(form.get("task_type") or "general").strip() or "general",
        priority=_coerce_task_priority(form.get("priority")),
        assigned_to_id=assigned_to_id,
        created_by=user.id,
        due_at=_parse_task_due_date(form.get("due_at")),
    )
    logger.info("Requisition task '{}' created on req {} by {}", title, req_id, user.email)

    tasks = (
        db.query(RequisitionTask)
        .options(joinedload(RequisitionTask.assignee))
        .filter(RequisitionTask.requisition_id == req_id)
        .order_by(RequisitionTask.priority.desc(), RequisitionTask.created_at.desc().nullslast())
        .all()
    )
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["tasks"] = tasks
    return template_response("htmx/partials/requisitions/tabs/_task_list.html", ctx)


@router.post("/api/requisitions/{req_id}/tasks/{task_id}/complete", response_class=HTMLResponse)
async def complete_requisition_task_endpoint(
    req_id: int,
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a requisition-board task done; return the re-rendered row (outerHTML swap).

    Gated by require_requisition_access, IDOR-checked to the requisition so a task from
    another requisition can't be completed via a crafted URL, then held to the shared
    mutation gate (creator | assignee | admin).
    """
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    task = db.get(RequisitionTask, task_id)
    if not task or task.requisition_id != req_id:
        raise HTTPException(404, "Task not found")
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "Only the task's creator, assignee, or an admin can modify it")

    task = update_task(db, task_id, status=TaskStatus.DONE)
    logger.info("Requisition task {} completed on req {} by {}", task_id, req_id, user.email)

    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["t"] = task
    return template_response("htmx/partials/requisitions/tabs/_task_row.html", ctx)


@router.delete("/api/requisitions/{req_id}/tasks/{task_id}", response_class=HTMLResponse)
async def delete_requisition_task_endpoint(
    req_id: int,
    task_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requisition-board task.

    The button uses hx-swap=delete, so the row is removed client-side and we return an
    empty 200. Gated by require_requisition_access, IDOR-checked to the requisition,
    then held to the shared mutation gate (creator | assignee | admin).
    """
    get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    task = db.get(RequisitionTask, task_id)
    if not task or task.requisition_id != req_id:
        raise HTTPException(404, "Task not found")
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "Only the task's creator, assignee, or an admin can modify it")

    delete_task(db, task_id)
    logger.info("Requisition task {} deleted from req {} by {}", task_id, req_id, user.email)
    return HTMLResponse("")


def _get_board_task_or_403(db: Session, req_id: int, task_id: int, user: User) -> tuple[Requisition, RequisitionTask]:
    """Shared guard chain for the board task edit endpoints.

    req 404 → requisition access → task IDOR 404 → mutation gate 403 (same skeleton as
    the complete endpoint). Returns (req, task) once every check passes.
    """
    req = get_requisition_or_404(db, req_id)
    require_requisition_access(db, req_id, user)
    task = db.get(RequisitionTask, task_id)
    if not task or task.requisition_id != req_id:
        raise HTTPException(404, "Task not found")
    if not is_task_mutation_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "Only the task's creator, assignee, or an admin can modify it")
    return req, task


@router.get("/api/requisitions/{req_id}/tasks/{task_id}/row", response_class=HTMLResponse)
async def requisition_task_row_endpoint(
    req_id: int,
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-render one board task row (outerHTML swap) — the edit form's Cancel target."""
    req, task = _get_board_task_or_403(db, req_id, task_id, user)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["t"] = task
    return template_response("htmx/partials/requisitions/tabs/_task_row.html", ctx)


@router.get("/api/requisitions/{req_id}/tasks/{task_id}/edit-form", response_class=HTMLResponse)
async def requisition_task_edit_form_endpoint(
    req_id: int,
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the inline title+due edit form for a board task (outerHTML row swap)."""
    req, task = _get_board_task_or_403(db, req_id, task_id, user)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["t"] = task
    ctx["error"] = None
    return template_response("htmx/partials/requisitions/tabs/_task_edit_form.html", ctx)


@router.post("/api/requisitions/{req_id}/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_requisition_task_endpoint(
    req_id: int,
    task_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save title + due for a board task; return the re-rendered row (outerHTML swap).

    Empty title re-renders the edit form with an inline error. Due is parsed via the
    shared _parse_task_due_date; an empty value clears the stored due date (explicit
    set, same rationale as the CRM edit in archive.py). Same guard chain as
    complete/delete plus the shared mutation gate.
    """
    req, task = _get_board_task_or_403(db, req_id, task_id, user)
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        ctx = _base_ctx(request, user, "requisitions")
        ctx["req"] = req
        ctx["t"] = task
        ctx["error"] = "Title is required."
        return template_response("htmx/partials/requisitions/tabs/_task_edit_form.html", ctx)
    due_at = _parse_task_due_date(form.get("due_at"))
    # Set both controlled fields directly so an empty due_at clears the existing value
    # (update_task skips None values; bypass that for explicit edits — see archive.py).
    task.title = title
    task.due_at = due_at
    db.commit()
    db.refresh(task)
    logger.info("Requisition task {} edited on req {} by {}", task_id, req_id, user.email)
    ctx = _base_ctx(request, user, "requisitions")
    ctx["req"] = req
    ctx["t"] = task
    return template_response("htmx/partials/requisitions/tabs/_task_row.html", ctx)
