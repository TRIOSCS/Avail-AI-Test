"""Task Board API — CRUD endpoints for requisition pipeline tasks.

Provides per-requisition task management, cross-req "My Tasks" and
"Waiting On" endpoints for the buyer sidebar widget, and a task
completion endpoint.

Called by: frontend (app.js)
Depends on: services/task_service.py, dependencies.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.schemas.task import TaskComplete, TaskCreate, TaskUpdate
from app.services import task_service

router = APIRouter(prefix="/api/requisitions", tags=["tasks"])
my_tasks_router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ---------------------------------------------------------------------------
# Per-requisition task endpoints
# ---------------------------------------------------------------------------


@router.get("/{req_id}/tasks")
def list_tasks(
    req_id: int,
    status: str | None = Query(None),
    task_type: str | None = Query(None),
    assigned_to_id: int | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    tasks = task_service.get_tasks(db, req_id, status=status, task_type=task_type, assigned_to_id=assigned_to_id)
    # Apply rule-based scoring on every fetch (fast, no AI call)
    pending = [t for t in tasks if t.status != "done" and t.ai_priority_score is None]
    if pending:
        task_service.apply_simple_scoring(db, pending)
    return [task_service.task_to_response(t) for t in tasks]


@router.post("/{req_id}/tasks/score")
async def score_tasks(
    req_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Trigger AI-powered task scoring for this requisition."""
    tasks = task_service.get_tasks(db, req_id, status=None)
    pending = [t for t in tasks if t.status != "done"]
    await task_service.score_tasks_with_ai(db, pending)
    return {"scored": len(pending)}


@router.post("/{req_id}/tasks", status_code=201)
def create_task(
    req_id: int,
    body: TaskCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    try:
        task = task_service.create_task(
            db,
            requisition_id=req_id,
            title=body.title,
            description=body.description,
            assigned_to_id=body.assigned_to_id,
            due_at=body.due_at,
            created_by=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return task_service.task_to_response(task)


@router.put("/{req_id}/tasks/{task_id}")
def update_task(
    req_id: int,
    task_id: int,
    body: TaskUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    task = task_service.get_task(db, task_id)
    if not task or task.requisition_id != req_id:
        raise HTTPException(404, "Task not found")
    updated = task_service.update_task(db, task_id, **body.model_dump(exclude_unset=True))
    return task_service.task_to_response(updated)


@router.patch("/{req_id}/tasks/{task_id}/status")
def patch_task_status(
    req_id: int,
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Quick status change (todo/in_progress/done) for drag-drop or checkbox."""
    task = task_service.get_task(db, task_id)
    if not task or task.requisition_id != req_id:
        raise HTTPException(404, "Task not found")
    new_status = body.get("status")
    if new_status not in ("todo", "in_progress", "done"):
        raise HTTPException(400, "Invalid status")
    updated = task_service.update_task_status(db, task_id, new_status)
    return task_service.task_to_response(updated)


@router.delete("/{req_id}/tasks/{task_id}", status_code=204)
def delete_task(
    req_id: int,
    task_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    task = task_service.get_task(db, task_id)
    if not task or task.requisition_id != req_id:
        raise HTTPException(404, "Task not found")
    task_service.delete_task(db, task_id)


# ---------------------------------------------------------------------------
# Cross-req "My Tasks" and "Waiting On" endpoints (sidebar widget)
# ---------------------------------------------------------------------------


@my_tasks_router.get("/mine")
def get_my_tasks(
    status: str | None = Query(None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    tasks = task_service.get_my_tasks(db, user.id, status=status)
    return [task_service.task_to_response(t) for t in tasks]


@my_tasks_router.get("/mine/summary")
def get_my_tasks_summary(
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    return task_service.get_my_tasks_summary(db, user.id)


@my_tasks_router.get("/waiting")
def get_waiting_on_tasks(
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Get tasks created by the current user but assigned to someone else."""
    tasks = task_service.get_waiting_on_tasks(db, user.id)
    return [task_service.task_to_response(t) for t in tasks]


@my_tasks_router.get("/team")
def get_team_workload(
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Team workload overview: task counts per user."""
    return task_service.get_team_workload(db)


@my_tasks_router.get("/done-feed")
def get_done_feed(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Recently completed tasks across the team."""
    tasks = task_service.get_done_feed(db, limit=limit)
    return [task_service.task_to_response(t) for t in tasks]


@my_tasks_router.get("/search")
def search_tasks(
    status: str | None = Query(None),
    priority: int | None = Query(None),
    requisition_id: int | None = Query(None),
    assigned_to_id: int | None = Query(None),
    q: str | None = Query(None, min_length=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Search and filter tasks across all requisitions."""
    tasks = task_service.search_tasks(
        db,
        user_id=assigned_to_id,
        status=status,
        priority=priority,
        requisition_id=requisition_id,
        query=q,
        limit=limit,
    )
    return [task_service.task_to_response(t) for t in tasks]


@my_tasks_router.patch("/{task_id}/status")
def patch_my_task_status(
    task_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Quick status change from the cross-req Task Queue view."""
    task = task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    new_status = body.get("status")
    if new_status not in ("todo", "in_progress", "done"):
        raise HTTPException(400, "Invalid status")
    updated = task_service.update_task_status(db, task_id, new_status)
    return task_service.task_to_response(updated)


@my_tasks_router.post("/{task_id}/complete")
def complete_task(
    task_id: int,
    body: TaskComplete,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Complete a task with a resolution note. Only the assignee can complete."""
    task = task_service.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    try:
        completed = task_service.complete_task(db, task_id, user.id, body.completion_note)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    return task_service.task_to_response(completed)
