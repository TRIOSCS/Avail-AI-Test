"""routers/htmx/my_day.py — My Day / Tasks worklist partials (HTMX + Alpine).

The Tasks page (`/v2/my-day`): a filterable worklist of every system task assigned
to the current user, plus create / snooze / reopen mutations. Extracted verbatim
from htmx_views.py (same `/v2/partials/my-day...` paths, same `htmx-views` tag).

Called by: app/routers/htmx_views.py (aggregated into the single exported router).
Depends on: app.models.task, app.services.task_service, ._shared
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import AccessKey, TaskStatus, UserRole
from ...database import get_db
from ...dependencies import require_access
from ...models import User
from ...models.task import RequisitionTask
from ...services.task_service import (
    _is_crm_task_authorized,
    create_personal_task,
    snooze_task,
    update_task,
)
from ...services.task_service import (
    get_my_tasks as _get_my_tasks,
)
from ...template_env import _task_due_state, template_response
from ._shared import _base_ctx, _parse_task_due_date
from .requisitions import _coerce_task_priority

router = APIRouter(tags=["htmx-views"])


def _my_day_filtered_tasks(db, user_id, *, status, priority, due, now):
    """Return the user's My-Day tasks after applying the status / priority / due
    filters.

    ``status`` flows through get_my_tasks (query-level; defaults to open, excludes done).
    ``priority`` (int 1-3) and ``due`` are applied here since the helper supports neither.
    The due predicate reuses the SAME ``task_due_state`` the results template groups by, so
    the filter and the on-screen "Overdue"/"Due soon" headings can never contradict each
    other (a task due earlier today filters as "today" AND renders under "Due soon"). Due
    dates are calendar-day, so "upcoming" is a strictly future day — neither overdue nor
    today — matching the template's "Later" group. Shared by the Tasks-page read route and
    its create / snooze / reopen mutations so every render stays consistent.
    """
    tasks = _get_my_tasks(db, user_id, status=status or None)
    if priority in ("1", "2", "3"):
        want = int(priority)
        tasks = [t for t in tasks if t.priority == want]
    if due == "overdue":
        tasks = [t for t in tasks if _task_due_state(t, now)[0]]
    elif due == "today":
        tasks = [t for t in tasks if _task_due_state(t, now)[1]]
    elif due == "upcoming":
        tasks = [t for t in tasks if t.due_at is not None and _task_due_state(t, now) == (False, False)]
    elif due == "none":
        tasks = [t for t in tasks if t.due_at is None]
    return tasks


def _my_day_results_response(request, user, db, *, status="", priority="", due=""):
    """Render the results-only Tasks fragment (tasks/_results.html) for the given
    filters.

    Used by the create / snooze / reopen mutations to re-render #tasks-results in place
    so the changed task re-buckets (snooze) or leaves the Done view (reopen)
    immediately.
    """
    now = datetime.now(UTC)
    tasks = _my_day_filtered_tasks(db, user.id, status=status, priority=priority, due=due, now=now)
    ctx = _base_ctx(request, user, "my-day")
    ctx["tasks"] = tasks
    ctx["now_utc"] = now
    ctx["filter_status"] = status
    ctx["filter_priority"] = priority
    ctx["filter_due"] = due
    return template_response("htmx/partials/tasks/_results.html", ctx)


@router.get("/v2/partials/my-day", response_class=HTMLResponse)
async def my_day_partial(
    request: Request,
    user: User = Depends(require_access(AccessKey.MY_DAY)),
    db: Session = Depends(get_db),
):
    """Tasks page — a filterable worklist of every system task assigned to me.

    (Formerly "My Day", which also carried a follow-up-accounts call-down section;
    that account cadence now lives in CRM, so this page is tasks-only.)

    Reuses task_service.get_my_tasks + _my_day_filtered_tasks (status/priority/due). The
    template groups the rows by urgency (Overdue → Due soon → Later → No due date). The
    filter bar's hx-get carries an EXPLICIT hx-target on the inner results container (so it
    never inherits #main-content and replaces the whole page).

    Called by: /v2/my-day full-page shell and nav hx-get, plus the filter-bar selects.
    Depends on: _my_day_filtered_tasks → task_service.get_my_tasks.
    """
    now = datetime.now(UTC)
    status = request.query_params.get("status", "").strip()
    priority = request.query_params.get("priority", "").strip()
    due = request.query_params.get("due", "").strip()

    tasks = _my_day_filtered_tasks(db, user.id, status=status, priority=priority, due=due, now=now)

    ctx = _base_ctx(request, user, "my-day")
    ctx["tasks"] = tasks
    ctx["now_utc"] = now
    ctx["filter_status"] = status
    ctx["filter_priority"] = priority
    ctx["filter_due"] = due
    # Filter-bar changes target the inner #tasks-results container — return the
    # results-only fragment so the filter bar (and its selected values) stay put.
    if request.headers.get("HX-Target") == "tasks-results":
        return template_response("htmx/partials/tasks/_results.html", ctx)
    return template_response("htmx/partials/tasks/list.html", ctx)


@router.post("/v2/partials/my-day/tasks", response_class=HTMLResponse)
async def create_my_day_task(
    request: Request,
    user: User = Depends(require_access(AccessKey.MY_DAY)),
    db: Session = Depends(get_db),
):
    """Create a personal/standalone task from the Tasks page, assigned to the creator.

    Standalone tasks have no natural business parent, but ck_task_has_parent still
    requires one, so task_service.create_personal_task hangs the task off the user's
    hidden "Personal" requisition (see that service). Title required; optional due date
    (parsed to aware-UTC midnight via the shared _parse_task_due_date) and priority
    (default medium). Re-renders the results list (default open filter) so the new task
    appears immediately.
    """
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        raise HTTPException(422, "Title is required")
    create_personal_task(
        db,
        user_id=user.id,
        title=title,
        priority=_coerce_task_priority(form.get("task_priority")),
        due_at=_parse_task_due_date(form.get("due_at")),
    )
    logger.info("Personal task '{}' created from My Day by {}", title, user.email)
    return _my_day_results_response(request, user, db)


@router.post("/v2/partials/my-day/tasks/{task_id}/snooze", response_class=HTMLResponse)
async def snooze_my_day_task(
    task_id: int,
    request: Request,
    days: int = Query(1),
    user: User = Depends(require_access(AccessKey.MY_DAY)),
    db: Session = Depends(get_db),
):
    """Snooze one of my tasks forward by ``days`` (quick options 1 / 3 / 7). Re-renders
    the filtered results so the task re-buckets in place.

    Authz mirrors the CRM/vendor Snooze gate (task_service._is_crm_task_authorized):
    assignee, creator, parent account owner, or admin. A personal / requisition-scoped
    task has no parent company, so that resolves to assignee-or-creator-or-admin — a
    non-owner cannot snooze someone else's task.
    """
    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to snooze this task")
    snooze_task(db, task_id, days=days if days in (1, 3, 7) else 1)
    logger.info("Task {} snoozed +{}d from My Day by {}", task_id, days, user.email)
    form = await request.form()
    return _my_day_results_response(
        request,
        user,
        db,
        status=(form.get("status") or "").strip(),
        priority=(form.get("priority") or "").strip(),
        due=(form.get("due") or "").strip(),
    )


@router.post("/v2/partials/my-day/tasks/{task_id}/reopen", response_class=HTMLResponse)
async def reopen_my_day_task(
    task_id: int,
    request: Request,
    user: User = Depends(require_access(AccessKey.MY_DAY)),
    db: Session = Depends(get_db),
):
    """Reopen one of my done tasks (status → todo, clears completed_at). Re-renders the
    filtered results so the task leaves the Done view.

    Authz mirrors the Snooze gate (task_service._is_crm_task_authorized): assignee,
    creator, parent account owner, or admin — a non-owner cannot reopen someone else's
    task. Reuses update_task, which clears completed_at on any non-done status
    transition.
    """
    task = db.get(RequisitionTask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not _is_crm_task_authorized(db, task, user.id, is_admin=(user.role == UserRole.ADMIN)):
        raise HTTPException(403, "You are not allowed to reopen this task")
    update_task(db, task_id, status=TaskStatus.TODO)
    logger.info("Task {} reopened from My Day by {}", task_id, user.email)
    form = await request.form()
    return _my_day_results_response(
        request,
        user,
        db,
        status=(form.get("status") or "").strip(),
        priority=(form.get("priority") or "").strip(),
        due=(form.get("due") or "").strip(),
    )
