"""Task Board service — CRUD, auto-generation, auto-close, AI scoring.

Manages requisition tasks through pipeline stages. Generates tasks
from system events (offers, RFQs, quotes) and auto-closes them when
the triggering action is completed.

Called by: routers/task.py, services/knowledge_service.py, jobs/
Depends on: models/task.py, models/auth.py
"""

import json
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models.crm import Company
from app.models.task import RequisitionTask


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC-aware (SQLite can return naive values)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_task(
    db: Session,
    *,
    requisition_id: int,
    title: str,
    description: str | None = None,
    task_type: str = "general",
    priority: int = 2,
    assigned_to_id: int | None = None,
    created_by: int | None = None,
    source: str = "manual",
    source_ref: str | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Create a task on a requisition.

    For manual tasks, assigned_to_id and due_at are required and due_at must be >= 24
    hours from now (enforced by schema).
    """
    # Belt-and-suspenders 24h check for manual tasks (schema also validates)
    if source == "manual" and due_at:
        now = datetime.now(timezone.utc)
        if _as_utc(due_at) < now + timedelta(hours=24):
            raise ValueError("Due date must be at least 24 hours from now")
    task = RequisitionTask(
        requisition_id=requisition_id,
        title=title,
        description=description,
        task_type=task_type,
        priority=priority,
        assigned_to_id=assigned_to_id,
        created_by=created_by,
        source=source,
        source_ref=source_ref,
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("Task created: {} (req={}, type={}, source={})", task.id, requisition_id, task_type, source)
    return task


def get_tasks(
    db: Session,
    requisition_id: int,
    *,
    status: str | None = None,
    task_type: str | None = None,
    assigned_to_id: int | None = None,
) -> list[RequisitionTask]:
    """Get tasks for a requisition with optional filters."""
    q = db.query(RequisitionTask).filter(RequisitionTask.requisition_id == requisition_id)
    if status:
        q = q.filter(RequisitionTask.status == status)
    if task_type:
        q = q.filter(RequisitionTask.task_type == task_type)
    if assigned_to_id:
        q = q.filter(RequisitionTask.assigned_to_id == assigned_to_id)
    return q.order_by(RequisitionTask.priority.desc(), RequisitionTask.created_at).all()


def get_my_tasks(
    db: Session,
    user_id: int,
    *,
    status: str | None = None,
) -> list[RequisitionTask]:
    """Get all tasks assigned to a user across all requisitions."""
    q = db.query(RequisitionTask).filter(RequisitionTask.assigned_to_id == user_id)
    if status:
        q = q.filter(RequisitionTask.status == status)
    else:
        # Default: exclude done tasks
        q = q.filter(RequisitionTask.status != TaskStatus.DONE)
    return q.order_by(
        RequisitionTask.due_at.asc().nullslast(),
        RequisitionTask.created_at,
    ).all()


def get_my_tasks_summary(db: Session, user_id: int) -> dict:
    """Get task counts for sidebar badge: assigned_to_me, waiting_on, overdue."""
    now = datetime.now(timezone.utc)
    assigned_to_me = (
        db.query(func.count(RequisitionTask.id))
        .filter(
            RequisitionTask.assigned_to_id == user_id,
            RequisitionTask.status != TaskStatus.DONE,
        )
        .scalar()
    ) or 0
    waiting_on = (
        db.query(func.count(RequisitionTask.id))
        .filter(
            RequisitionTask.created_by == user_id,
            RequisitionTask.assigned_to_id != user_id,
            RequisitionTask.status != TaskStatus.DONE,
        )
        .scalar()
    ) or 0
    overdue = (
        db.query(func.count(RequisitionTask.id))
        .filter(
            RequisitionTask.assigned_to_id == user_id,
            RequisitionTask.status != TaskStatus.DONE,
            RequisitionTask.due_at < now,
        )
        .scalar()
    ) or 0
    return {
        "assigned_to_me": assigned_to_me,
        "waiting_on": waiting_on,
        "overdue": overdue,
    }


def get_task(db: Session, task_id: int) -> RequisitionTask | None:
    """Get a single task by ID."""
    return db.query(RequisitionTask).filter(RequisitionTask.id == task_id).first()


def update_task(db: Session, task_id: int, **kwargs) -> RequisitionTask | None:
    """Update task fields.

    Returns None if not found.
    """
    task = db.query(RequisitionTask).filter(RequisitionTask.id == task_id).first()
    if not task:
        return None
    for key, val in kwargs.items():
        if val is not None and hasattr(task, key):
            setattr(task, key, val)
    # Sync completed_at with any status change: set on transition to done, clear otherwise
    new_status = kwargs.get("status")
    if new_status == TaskStatus.DONE:
        if not task.completed_at:
            task.completed_at = datetime.now(timezone.utc)
    elif new_status:
        task.completed_at = None
    db.commit()
    db.refresh(task)
    return task


def update_task_status(db: Session, task_id: int, status: str) -> RequisitionTask | None:
    """Quick status change (drag-drop).

    Returns None if not found.
    """
    return update_task(db, task_id, status=status)


def complete_task(
    db: Session,
    task_id: int,
    user_id: int,
    completion_note: str = "",
) -> RequisitionTask | None:
    """Complete a task. Only the assignee can complete it.

    Returns the updated task, or None if not found. Raises PermissionError if the caller
    is not the assignee.
    """
    task = db.query(RequisitionTask).filter(RequisitionTask.id == task_id).first()
    if not task:
        return None
    if task.assigned_to_id != user_id:
        raise PermissionError("Only the assignee can complete this task")
    task.status = TaskStatus.DONE
    task.completed_at = datetime.now(timezone.utc)
    task.completion_note = completion_note
    db.commit()
    db.refresh(task)
    logger.info("Task {} completed by user {}", task_id, user_id)
    return task


def reopen_task(
    db: Session,
    task_id: int,
    user_id: int,
) -> RequisitionTask | None:
    """Reopen a completed task. Only the assignee can reopen it.

    Returns the updated task, or None if not found. Raises PermissionError if the caller
    is not the assignee.
    """
    task = db.query(RequisitionTask).filter(RequisitionTask.id == task_id).first()
    if not task:
        return None
    if task.assigned_to_id != user_id:
        raise PermissionError("Only the assignee can reopen this task")
    task.status = TaskStatus.TODO
    task.completed_at = None
    db.commit()
    db.refresh(task)
    logger.info("Task {} reopened by user {}", task_id, user_id)
    return task


def get_waiting_on_tasks(db: Session, user_id: int) -> list[RequisitionTask]:
    """Get tasks created by the user but assigned to someone else (not done)."""
    return (
        db.query(RequisitionTask)
        .filter(
            RequisitionTask.created_by == user_id,
            RequisitionTask.assigned_to_id != user_id,
            RequisitionTask.status != TaskStatus.DONE,
        )
        .order_by(RequisitionTask.due_at.asc().nullslast(), RequisitionTask.created_at)
        .all()
    )


def delete_task(db: Session, task_id: int) -> bool:
    """Delete a task.

    Returns True if deleted.
    """
    task = db.query(RequisitionTask).filter(RequisitionTask.id == task_id).first()
    if not task:
        return False
    db.delete(task)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# CRM Tasks — account-scoped and contact-scoped
# ---------------------------------------------------------------------------


def create_company_task(
    db: Session,
    *,
    company_id: int,
    title: str,
    description: str | None = None,
    priority: int = 2,
    assigned_to_id: int | None = None,
    created_by: int | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Create a task scoped to an account (company)."""
    task = RequisitionTask(
        company_id=company_id,
        title=title,
        description=description,
        task_type="general",
        priority=priority,
        assigned_to_id=assigned_to_id,
        created_by=created_by,
        source="manual",
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("Account task created: {} (company={})", task.id, company_id)
    return task


def create_contact_task(
    db: Session,
    *,
    site_contact_id: int,
    title: str,
    description: str | None = None,
    priority: int = 2,
    assigned_to_id: int | None = None,
    created_by: int | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Create a task scoped to a contact."""
    task = RequisitionTask(
        site_contact_id=site_contact_id,
        title=title,
        description=description,
        task_type="general",
        priority=priority,
        assigned_to_id=assigned_to_id,
        created_by=created_by,
        source="manual",
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("Contact task created: {} (contact={})", task.id, site_contact_id)
    return task


def get_open_tasks_for_company(db: Session, company_id: int) -> list[RequisitionTask]:
    """Return open tasks scoped to a company, ordered by due_at asc (nulls last)."""
    return (
        db.query(RequisitionTask)
        .filter(RequisitionTask.company_id == company_id, RequisitionTask.status != TaskStatus.DONE)
        .order_by(RequisitionTask.due_at.asc().nullslast(), RequisitionTask.created_at)
        .all()
    )


def get_open_tasks_for_contact(db: Session, site_contact_id: int) -> list[RequisitionTask]:
    """Return open tasks scoped to a contact, ordered by due_at asc (nulls last)."""
    return (
        db.query(RequisitionTask)
        .filter(RequisitionTask.site_contact_id == site_contact_id, RequisitionTask.status != TaskStatus.DONE)
        .order_by(RequisitionTask.due_at.asc().nullslast(), RequisitionTask.created_at)
        .all()
    )


def get_next_task_for_company(db: Session, company_id: int) -> RequisitionTask | None:
    """Return the soonest open task for a company (the 'next step')."""
    return (
        db.query(RequisitionTask)
        .filter(RequisitionTask.company_id == company_id, RequisitionTask.status != TaskStatus.DONE)
        .order_by(RequisitionTask.due_at.asc().nullslast(), RequisitionTask.created_at)
        .first()
    )


def create_vendor_task(
    db: Session,
    *,
    vendor_card_id: int,
    title: str,
    description: str | None = None,
    priority: int = 2,
    assigned_to_id: int | None = None,
    created_by: int | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Create a task scoped to a vendor card."""
    task = RequisitionTask(
        vendor_card_id=vendor_card_id,
        title=title,
        description=description,
        task_type="general",
        priority=priority,
        assigned_to_id=assigned_to_id,
        created_by=created_by,
        source="manual",
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("Vendor task created: {} (vendor_card={})", task.id, vendor_card_id)
    return task


def get_open_tasks_for_vendor_card(db: Session, vendor_card_id: int) -> list[RequisitionTask]:
    """Return open tasks scoped to a vendor card, ordered by due_at asc (nulls last)."""
    # NOTE: Only queries by vendor_card_id. Tasks scoped to vendor_contact only are not surfaced here.
    return (
        db.query(RequisitionTask)
        .filter(
            RequisitionTask.vendor_card_id == vendor_card_id,
            RequisitionTask.status != TaskStatus.DONE,
        )
        .order_by(RequisitionTask.due_at.asc().nullslast(), RequisitionTask.created_at)
        .all()
    )


def _is_crm_task_authorized(db: Session, task: RequisitionTask, user_id: int, is_admin: bool) -> bool:
    """Return True if user_id is allowed to mutate the given CRM task.

    Allowed if any of:
      - user is an admin
      - user is the assignee
      - user is the creator
      - user is the account_owner of the task's parent company (via company_id directly,
        or via the contact's site → company for contact-scoped tasks)
      - task is vendor-scoped (any authenticated user may mutate vendor tasks)
    """
    if is_admin:
        return True
    if task.assigned_to_id == user_id:
        return True
    if task.created_by == user_id:
        return True
    # Vendor-scoped tasks: any authenticated user may complete/edit them.
    # Delete requires admin — callers that enforce admin-only must do so before calling here.
    if task.vendor_card_id is not None or task.vendor_contact_id is not None:
        return True
    # Check parent company owner
    company_id: int | None = task.company_id
    if company_id is None and task.site_contact_id is not None:
        # Resolve contact → site → company
        from app.models.crm import CustomerSite, SiteContact

        contact = db.get(SiteContact, task.site_contact_id)
        if contact and contact.customer_site_id:
            site = db.get(CustomerSite, contact.customer_site_id)
            if site:
                company_id = site.company_id
    if company_id is not None:
        company = db.get(Company, company_id)
        if company and company.account_owner_id == user_id:
            return True
    return False


def complete_crm_task(
    db: Session,
    task_id: int,
    user_id: int,
    completion_note: str = "",
    is_admin: bool = False,
) -> RequisitionTask | None:
    """Complete a CRM task (account or contact scoped). No activity log is created.

    Returns the updated task, or None if not found. Raises PermissionError if the caller
    is not the assignee, creator, parent account owner, or an admin.
    """
    task = db.get(RequisitionTask, task_id)
    if not task:
        return None
    if not _is_crm_task_authorized(db, task, user_id, is_admin):
        raise PermissionError("Not authorized to complete this task")
    task.status = TaskStatus.DONE
    task.completed_at = datetime.now(timezone.utc)
    task.completion_note = completion_note
    db.commit()
    db.refresh(task)
    logger.info("CRM task {} completed by user {}", task_id, user_id)
    return task


# ---------------------------------------------------------------------------
# Auto-Generation — call these from existing service hooks
# ---------------------------------------------------------------------------


def _find_open_task_by_ref(db: Session, requisition_id: int, source_ref: str) -> RequisitionTask | None:
    """Find the non-done task matching a (requisition, source_ref) pair, if any."""
    return (
        db.query(RequisitionTask)
        .filter(
            RequisitionTask.requisition_id == requisition_id,
            RequisitionTask.source_ref == source_ref,
            RequisitionTask.status != TaskStatus.DONE,
        )
        .first()
    )


def auto_create_task(
    db: Session,
    *,
    requisition_id: int,
    title: str,
    task_type: str,
    source_ref: str,
    priority: int = 2,
    assigned_to_id: int | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask | None:
    """Create a system-generated task, skipping if a matching source_ref already
    exists."""
    if _find_open_task_by_ref(db, requisition_id, source_ref):
        return None  # Don't create duplicates
    return create_task(
        db,
        requisition_id=requisition_id,
        title=title,
        task_type=task_type,
        priority=priority,
        assigned_to_id=assigned_to_id,
        source="system",
        source_ref=source_ref,
        due_at=due_at,
    )


def auto_close_task(db: Session, requisition_id: int, source_ref: str) -> RequisitionTask | None:
    """Auto-close a system task by source_ref when the triggering action completes."""
    task = _find_open_task_by_ref(db, requisition_id, source_ref)
    if task:
        task.status = TaskStatus.DONE
        task.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(task)
        logger.info("Auto-closed task {} (ref={})", task.id, source_ref)
    return task


# ---------------------------------------------------------------------------
# Convenience auto-gen helpers for common events
# ---------------------------------------------------------------------------


def on_requirement_added(db: Session, requisition_id: int, mpn: str, assigned_to_id: int | None = None):
    """Auto-generate 'Source MPN' task when a new requirement is added."""
    auto_create_task(
        db,
        requisition_id=requisition_id,
        title=f"Source {mpn} — find vendors",
        task_type="sourcing",
        source_ref=f"source:{mpn}",
        priority=2,
        assigned_to_id=assigned_to_id,
    )


def on_offer_received(db: Session, requisition_id: int, vendor_name: str, mpn: str, offer_id: int):
    """Auto-generate 'Review offer' task when a new offer comes in."""
    auto_create_task(
        db,
        requisition_id=requisition_id,
        title=f"Review offer from {vendor_name} for {mpn}",
        task_type="sourcing",
        source_ref=f"offer:{offer_id}",
        priority=2,
    )


def on_email_offer_parsed(db: Session, requisition_id: int | None, vendor_name: str, mpn: str, offer_id: int):
    """Auto-generate 'Review email offer' task when email intelligence parses an offer.

    Unsolicited offers (requisition_id=None) have no requisition context, so the task
    cannot be attached — skip silently.
    """
    if requisition_id is None:
        return
    auto_create_task(
        db,
        requisition_id=requisition_id,
        title=f"Email offer from {vendor_name} for {mpn} — review",
        task_type="sourcing",
        source_ref=f"email_offer:{offer_id}",
        priority=3,
    )


def on_buy_plan_assigned(
    db: Session,
    requisition_id: int,
    buyer_id: int,
    vendor_name: str,
    mpn: str,
    line_id: int,
):
    """Auto-generate 'Cut PO' task when a buy plan line is assigned to a buyer."""
    auto_create_task(
        db,
        requisition_id=requisition_id,
        title=f"Cut PO — {vendor_name} for {mpn}",
        task_type="buying",
        source_ref=f"buyline:{line_id}",
        priority=3,
        assigned_to_id=buyer_id,
    )


def on_bid_due_soon(db: Session, requisition_id: int, deadline: str, req_name: str):
    """Auto-generate 'Bid due' alert task for a requisition approaching deadline."""
    auto_create_task(
        db,
        requisition_id=requisition_id,
        title=f"Bid due {deadline} — {req_name}",
        task_type="sourcing",
        source_ref=f"bid_due:{requisition_id}",
        priority=3,
        due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


# ---------------------------------------------------------------------------
# Task-to-response helper
# ---------------------------------------------------------------------------


def task_to_response(task: RequisitionTask) -> dict:
    """Convert a RequisitionTask to a response dict with assignee/creator names."""
    assignee_name = None
    if task.assignee:
        assignee_name = task.assignee.name or task.assignee.email
    creator_name = None
    if task.creator:
        creator_name = task.creator.name or task.creator.email
    requisition_name = None
    if task.requisition:
        requisition_name = getattr(task.requisition, "name", None)
    return {
        "id": task.id,
        "requisition_id": task.requisition_id,
        "requisition_name": requisition_name,
        "title": task.title,
        "description": task.description,
        "task_type": task.task_type,
        "status": task.status,
        "priority": task.priority,
        "ai_priority_score": task.ai_priority_score,
        "ai_risk_flag": task.ai_risk_flag,
        "assigned_to_id": task.assigned_to_id,
        "assignee_name": assignee_name,
        "created_by": task.created_by,
        "creator_name": creator_name,
        "source": task.source,
        "source_ref": task.source_ref,
        "completion_note": task.completion_note,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


# ---------------------------------------------------------------------------
# AI Priority Scoring & Risk Alerts
# ---------------------------------------------------------------------------

TASK_SCORING_PROMPT = """You are a procurement task priority analyst. Given a list of tasks for electronic component sourcing, score each task's urgency and identify risks.

For each task, return:
- priority_score: float 0.0-1.0 (1.0 = most urgent)
- risk_flag: string or null (short risk alert, max 50 chars)

Scoring factors:
- Due date proximity (overdue = highest)
- Task type: buying tasks (cut PO) are urgent when plan is active
- Priority level set by buyer
- How long the task has been open
- Tasks from system events (auto-generated) should score slightly lower unless overdue

Return JSON array matching input order:
[{"priority_score": 0.85, "risk_flag": "Bid due tomorrow"}, ...]
"""


async def score_tasks_with_ai(db: Session, tasks: list[RequisitionTask]) -> None:
    """Use AI to score task priority and set risk flags.

    Updates DB in place.
    """
    if not tasks:
        return

    from app.utils.claude_client import claude_json

    now = datetime.now(timezone.utc)
    task_descriptions = []
    for t in tasks:
        days_open = (now - t.created_at).days if t.created_at else 0
        days_until_due = None
        if t.due_at:
            days_until_due = (t.due_at - now).days
        task_descriptions.append(
            {
                "id": t.id,
                "title": t.title,
                "task_type": t.task_type,
                "priority": t.priority,
                "status": t.status,
                "days_open": days_open,
                "days_until_due": days_until_due,
                "source": t.source,
            }
        )

    try:
        prompt = f"Score these procurement tasks:\n{json.dumps(task_descriptions, indent=2)}"
        result = await claude_json(prompt, system=TASK_SCORING_PROMPT, model_tier="fast", max_tokens=512)
        if not result or not isinstance(result, list):
            return
        for i, score_data in enumerate(result):
            if i >= len(tasks):
                break
            tasks[i].ai_priority_score = score_data.get("priority_score")
            tasks[i].ai_risk_flag = score_data.get("risk_flag")
        db.commit()
        logger.info("AI scored {} tasks", len(tasks))
    except Exception as e:
        logger.warning("AI task scoring failed: {}", str(e))


def compute_simple_priority(task: RequisitionTask) -> float:
    """Fallback priority scoring without AI (rule-based)."""
    now = datetime.now(timezone.utc)
    score = 0.3  # base

    # Priority boost
    if task.priority == 3:
        score += 0.3
    elif task.priority == 2:
        score += 0.15

    # Due date urgency
    if task.due_at:
        due = _as_utc(task.due_at)
        days_left = (due - now).total_seconds() / 86400
        if days_left < 0:
            score += 0.4  # overdue
        elif days_left < 1:
            score += 0.3  # due today
        elif days_left < 3:
            score += 0.15

    # Task type boost
    if task.task_type == "sales":
        score += 0.05

    return min(score, 1.0)


def apply_simple_scoring(db: Session, tasks: list[RequisitionTask]) -> None:
    """Apply rule-based scoring to tasks (fast, no AI needed)."""
    now = datetime.now(timezone.utc)
    for t in tasks:
        t.ai_priority_score = compute_simple_priority(t)
        # Simple risk flags — handle naive datetimes from SQLite
        due = _as_utc(t.due_at)
        created = _as_utc(t.created_at)
        if due and due < now:
            t.ai_risk_flag = "Overdue"
        elif due and (due - now).days <= 1:
            t.ai_risk_flag = "Due today"
        elif created and (now - created).days >= 3 and t.status == TaskStatus.TODO:
            t.ai_risk_flag = "No activity in 3+ days"
    db.commit()
