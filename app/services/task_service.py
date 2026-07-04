"""Task Board service — CRUD, auto-generation, auto-close.

Manages requisition tasks through pipeline stages. Generates tasks
from system events (offers, RFQs, quotes) and auto-closes them when
the triggering action is completed.

Called by: routers/htmx/* task endpoints, routers/htmx_views.py (My Day),
    jobs/task_jobs.py, and service hooks (email_service, buyplan_workflow, resell).
Depends on: models/task.py, models/auth.py
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.constants import TaskStatus
from app.models.crm import Company
from app.models.task import RequisitionTask

# How far back the My Day "Done" filter reaches, and the hard row cap on that query.
# The completed-task list is otherwise unbounded (a long-tenured user could load
# thousands of rows into one fragment) — cap it to a recent window plus a LIMIT.
_DONE_WINDOW_DAYS = 30
_DONE_LIMIT = 200


def _as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC-aware (SQLite can return naive values)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _persist_task(db: Session, **fields) -> RequisitionTask:
    """Build, persist, refresh, and return a RequisitionTask from the given column
    values.

    Shared constructor core for the create_* entry points below — they are otherwise
    copy-paste bodies that differ only in which parent FK they set, so this centralizes
    the identical ``RequisitionTask(...)`` + add/commit/refresh. Callers pass exactly the
    columns they set, so behavior is unchanged from an inline construction.
    """
    task = RequisitionTask(**fields)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


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

    For manual tasks that pass a due_at, the due date must be >= 24 hours from now.
    """
    # Belt-and-suspenders 24h check for manual tasks with an explicit due date.
    if source == "manual" and due_at:
        now = datetime.now(timezone.utc)
        if _as_utc(due_at) < now + timedelta(hours=24):
            raise ValueError("Due date must be at least 24 hours from now")
    task = _persist_task(
        db,
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
    logger.info("Task created: {} (req={}, type={}, source={})", task.id, requisition_id, task_type, source)
    return task


def create_requisition_task(
    db: Session,
    *,
    requisition_id: int,
    title: str,
    description: str | None = None,
    task_type: str = "general",
    priority: int = 2,
    assigned_to_id: int | None = None,
    created_by: int | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Create a manual task on a requisition's Task board.

    Mirrors create_company_task / create_contact_task (no 24h due-date floor — the board
    allows near-term due dates, matching the CRM create surfaces). requirement_id stays
    NULL: these are requisition-level tasks, not part-level ones. Callers must pass an
    already-parsed aware `due_at` datetime (never a raw date string — see UTCDateTime).
    """
    task = RequisitionTask(
        requisition_id=requisition_id,
        title=title,
        description=description,
        task_type=task_type,
        priority=priority,
        assigned_to_id=assigned_to_id,
        created_by=created_by,
        source="manual",
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("Requisition task created: {} (req={}, type={})", task.id, requisition_id, task_type)
    return task


# ---------------------------------------------------------------------------
# Personal / standalone tasks (Tasks-page "New task" affordance)
# ---------------------------------------------------------------------------

# A personal to-do created from the Tasks page has no natural business parent, but
# ``ck_task_has_parent`` requires at least one, so personal tasks hang off a per-user
# hidden "scratch" requisition — the established codebase pattern (see
# services/quick_source_service.py) for giving a parentless work item a home. is_scratch
# reqs are excluded from every requisition list, picker, and global search, so this parent
# never pollutes a real customer / vendor / requisition surface.
_PERSONAL_REQ_NAME = "Personal"


def get_or_create_personal_requisition(db: Session, user_id: int):
    """Return the user's hidden "Personal" requisition, creating it once on first use.

    Idempotent per user (reuses the most-recent match). A benign duplicate on a
    concurrent first create is harmless — both rows are hidden (is_scratch) and equally
    reusable.
    """
    from app.models.sourcing import Requisition

    req = (
        db.query(Requisition)
        .filter(
            Requisition.created_by == user_id,
            Requisition.is_scratch.is_(True),
            Requisition.name == _PERSONAL_REQ_NAME,
        )
        .order_by(Requisition.id.desc())
        .first()
    )
    if req is not None:
        return req
    req = Requisition(
        name=_PERSONAL_REQ_NAME,
        status="open",
        is_scratch=True,
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    logger.info("Personal requisition created for user {} (req={})", user_id, req.id)
    return req


def create_personal_task(
    db: Session,
    *,
    user_id: int,
    title: str,
    priority: int = 2,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Create a standalone/personal to-do from the Tasks page, assigned to the creator.

    Hangs the task off the creator's hidden "Personal" requisition
    (``get_or_create_personal_requisition``) so ``ck_task_has_parent`` is satisfied without
    a real business parent. task_type="general", source="manual", status="todo". Callers
    must pass an already-parsed aware ``due_at`` datetime (never a raw date string).
    """
    req = get_or_create_personal_requisition(db, user_id)
    task = RequisitionTask(
        requisition_id=req.id,
        title=title,
        task_type="general",
        status=TaskStatus.TODO,
        priority=priority,
        assigned_to_id=user_id,
        created_by=user_id,
        source="manual",
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info("Personal task created: {} (user={}, req={})", task.id, user_id, req.id)
    return task


def get_my_tasks(
    db: Session,
    user_id: int,
    *,
    status: str | None = None,
) -> list[RequisitionTask]:
    """Get all tasks assigned to a user across all requisitions (My Day worklist).

    Eager-loads the parent relationships the results template touches per row
    (company, site_contact, requisition, assignee) so rendering the list is a single
    query, not one-per-relationship-per-row (N+1).

    When ``status`` is ``done`` the result is BOUNDED — only tasks completed within the
    last ``_DONE_WINDOW_DAYS`` days, most-recent first, capped at ``_DONE_LIMIT`` rows —
    so the "Done" filter can't load a long-tenured user's entire completion history into
    one fragment. Open queries stay ordered soonest-due first (nulls last), then created.
    """
    q = (
        db.query(RequisitionTask)
        .options(
            joinedload(RequisitionTask.company),
            joinedload(RequisitionTask.site_contact),
            joinedload(RequisitionTask.requisition),
            joinedload(RequisitionTask.assignee),
        )
        .filter(RequisitionTask.assigned_to_id == user_id)
    )
    if status == TaskStatus.DONE:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_DONE_WINDOW_DAYS)
        return (
            q.filter(
                RequisitionTask.status == TaskStatus.DONE,
                RequisitionTask.completed_at >= cutoff,
            )
            .order_by(RequisitionTask.completed_at.desc().nullslast())
            .limit(_DONE_LIMIT)
            .all()
        )
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
    task = _persist_task(
        db,
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
    task = _persist_task(
        db,
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
    task = _persist_task(
        db,
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
    """Return True if user_id is allowed to mutate the given CRM/vendor task.

    Allowed if any of:
      - user is an admin
      - user is the assignee
      - user is the creator
      - user is the account_owner of the task's parent company (via company_id directly,
        or via the contact's site → company for contact-scoped tasks)

    Vendor-scoped tasks have no account-owner concept, so they resolve to the shared
    creator/assignee/admin gate above (there is no blanket "any authenticated user" pass).
    """
    if is_admin:
        return True
    if task.assigned_to_id == user_id:
        return True
    if task.created_by == user_id:
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


def _default_auto_assignee(db: Session, requisition_id: int) -> int | None:
    """Resolve the default assignee for a system-generated task from its requisition.

    Auto-tasks created with no explicit assignee used to be left unassigned, so they never
    surfaced on anyone's My Day (which filters ``assigned_to_id == user.id``) — the
    auto-generation machinery fed a void (audit finding L1). Default them to the
    requisition's claiming buyer if one has picked it up, else the requisition creator:
    ``coalesce(claimed_by_id, created_by)``. (The requisition's buyer-claim field is
    ``claimed_by_id`` — "which buyer picked up this requisition for sourcing"; the
    requisition has no ``assigned_buyer_id`` column, so the claim field is the source.)

    Returns ``None`` only when the requisition row can't be resolved (e.g. it was deleted),
    leaving the task unassigned as before — there is no other context to fall back to.
    """
    from app.models.sourcing import Requisition

    req = db.get(Requisition, requisition_id)
    if req is None:
        return None
    return req.claimed_by_id or req.created_by


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
    """Create a system-generated task, skipping if a matching source_ref already exists.

    When no ``assigned_to_id`` is provided, the task defaults to the requisition's claiming
    buyer, else its creator (``coalesce(claimed_by_id, created_by)`` via
    ``_default_auto_assignee``) so it lands on that user's My Day. An explicitly-passed
    ``assigned_to_id`` is never overridden.
    """
    if _find_open_task_by_ref(db, requisition_id, source_ref):
        return None  # Don't create duplicates
    if assigned_to_id is None:
        assigned_to_id = _default_auto_assignee(db, requisition_id)
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


def auto_create_resell_followup_task(
    db: Session,
    *,
    excess_list_id: int,
    vendor_card_id: int,
    owner_id: int,
    buyer_name: str,
    list_title: str | None = None,
    due_at: datetime | None = None,
) -> RequisitionTask:
    """Idempotently create the list owner's My-Day follow-up for a buyer the resell
    "usually offered, not yet this round" nudge surfaces.

    Scoped to the buyer's vendor card (the buyer-side "who"), assigned to the list
    owner, so the nudge survives a page close as a durable task. Keyed by (excess list,
    buyer card, owner) via source_ref + assignee REGARDLESS of status: reloading the
    strip never duplicates a buyer's task, and a follow-up the owner has already
    completed is not re-created. Returns the task that now represents the nudge
    (existing or freshly created).
    """
    source_ref = f"resell_notyet:{excess_list_id}:{vendor_card_id}"
    existing = (
        db.query(RequisitionTask)
        .filter(
            RequisitionTask.source_ref == source_ref,
            RequisitionTask.assigned_to_id == owner_id,
        )
        .first()
    )
    if existing:
        return existing
    suffix = f" on {list_title}" if list_title else ""
    title = f"Follow up: offer {buyer_name}{suffix} this round"[:255]
    task = RequisitionTask(
        vendor_card_id=vendor_card_id,
        title=title,
        task_type="sales",
        priority=2,
        assigned_to_id=owner_id,
        created_by=owner_id,
        source="system",
        source_ref=source_ref,
        due_at=due_at,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info(
        "Resell follow-up task created: {} (list={}, buyer_card={}, owner={})",
        task.id,
        excess_list_id,
        vendor_card_id,
        owner_id,
    )
    return task


def snooze_task(db: Session, task_id: int, *, days: int | None = None) -> RequisitionTask | None:
    """Push a task's due_at forward.

    Default (``days=None``): +1 week if the task already has a due date, else tomorrow at
    midnight UTC — the delta the CRM/vendor Snooze action relies on. When ``days`` is given
    (the Tasks-page quick options +1d / +3d / +1w), advance an existing due_at by exactly
    that many days, or, for an undated task, set it that many days out at midnight UTC.
    Returns the updated task, or None if not found.
    """
    task = db.get(RequisitionTask, task_id)
    if not task:
        return None
    if days is None:
        if task.due_at:
            task.due_at = task.due_at + timedelta(weeks=1)
        else:
            task.due_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
    elif task.due_at:
        task.due_at = task.due_at + timedelta(days=days)
    else:
        task.due_at = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    db.commit()
    db.refresh(task)
    return task
