"""Tests for the task manager page — view routes, status changes, filters, and sorts."""

from datetime import datetime, timedelta, timezone

from app.models.task import RequisitionTask
from app.models import User


def _make_task(
    db_session,
    requisition_id: int,
    *,
    title: str = "Test task",
    task_type: str = "sourcing",
    priority: int = 2,
    status: str = "todo",
    assigned_to_id: int | None = None,
    created_by: int | None = None,
    due_at: datetime | None = None,
    ai_risk_flag: str | None = None,
) -> RequisitionTask:
    task = RequisitionTask(
        requisition_id=requisition_id,
        title=title,
        task_type=task_type,
        priority=priority,
        status=status,
        assigned_to_id=assigned_to_id,
        created_by=created_by,
        due_at=due_at,
        ai_risk_flag=ai_risk_flag,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def test_task_list_page_loads(client, test_requisition, test_user, db_session):
    """GET /tasks returns 200 with task list content."""
    _make_task(db_session, test_requisition.id, assigned_to_id=test_user.id, title="Source LM317T")
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "Task Manager" in resp.text
    assert "Source LM317T" in resp.text


def _make_other_user(db_session) -> User:
    """Create a second user for delegation/assignment tests."""
    user = User(
        email="other@trioscs.com",
        name="Other User",
        role="buyer",
        azure_id="test-azure-id-other",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_task_list_my_tasks_tab(client, test_requisition, test_user, db_session):
    """My Tasks tab shows only tasks assigned to the current user."""
    other = _make_other_user(db_session)
    _make_task(db_session, test_requisition.id, assigned_to_id=test_user.id, title="My task")
    _make_task(db_session, test_requisition.id, assigned_to_id=other.id, title="Not my task")
    resp = client.get("/tasks?tab=my_tasks")
    assert resp.status_code == 200
    assert "My task" in resp.text
    assert "Not my task" not in resp.text


def test_task_list_waiting_on_tab(client, test_requisition, test_user, db_session):
    """Waiting On tab shows tasks created by user but assigned to someone else."""
    other = _make_other_user(db_session)
    _make_task(
        db_session, test_requisition.id,
        assigned_to_id=other.id, created_by=test_user.id, title="Delegated task",
    )
    _make_task(
        db_session, test_requisition.id,
        assigned_to_id=test_user.id, created_by=test_user.id, title="Own task",
    )
    resp = client.get("/tasks?tab=waiting_on")
    assert resp.status_code == 200
    assert "Delegated task" in resp.text
    assert "Own task" not in resp.text


def test_task_list_done_tab(client, test_requisition, test_user, db_session):
    """Done tab shows only completed tasks."""
    _make_task(db_session, test_requisition.id, status="done", title="Finished task")
    _make_task(db_session, test_requisition.id, status="todo", title="Open task")
    resp = client.get("/tasks?tab=done")
    assert resp.status_code == 200
    assert "Finished task" in resp.text
    assert "Open task" not in resp.text


def test_task_list_type_filter(client, test_requisition, test_user, db_session):
    """Type filter narrows results to a specific task_type."""
    _make_task(db_session, test_requisition.id, task_type="sourcing", title="Sourcing task")
    _make_task(db_session, test_requisition.id, task_type="sales", title="Sales task")
    resp = client.get("/tasks?tab=all&type_filter=sourcing")
    assert resp.status_code == 200
    assert "Sourcing task" in resp.text
    assert "Sales task" not in resp.text


def test_task_list_overdue_filter(client, test_requisition, test_user, db_session):
    """Overdue filter shows only tasks past their due date."""
    past = datetime.now(timezone.utc) - timedelta(days=2)
    future = datetime.now(timezone.utc) + timedelta(days=5)
    _make_task(db_session, test_requisition.id, due_at=past, title="Late task")
    _make_task(db_session, test_requisition.id, due_at=future, title="Future task")
    resp = client.get("/tasks?tab=all&type_filter=overdue")
    assert resp.status_code == 200
    assert "Late task" in resp.text
    assert "Future task" not in resp.text


def test_task_list_high_priority_filter(client, test_requisition, test_user, db_session):
    """High priority filter shows only priority=3 tasks."""
    _make_task(db_session, test_requisition.id, priority=3, title="Urgent task")
    _make_task(db_session, test_requisition.id, priority=1, title="Low prio task")
    resp = client.get("/tasks?tab=all&type_filter=high_priority")
    assert resp.status_code == 200
    assert "Urgent task" in resp.text
    assert "Low prio task" not in resp.text


def test_task_list_sort_due_date(client, test_requisition, test_user, db_session):
    """Sort by due_date orders tasks by due date ascending."""
    later = datetime.now(timezone.utc) + timedelta(days=10)
    sooner = datetime.now(timezone.utc) + timedelta(days=2)
    _make_task(db_session, test_requisition.id, due_at=later, title="Later task")
    _make_task(db_session, test_requisition.id, due_at=sooner, title="Sooner task")
    resp = client.get("/tasks?tab=all&sort_by=due_date")
    assert resp.status_code == 200
    # Sooner task should appear before later task
    sooner_pos = resp.text.index("Sooner task")
    later_pos = resp.text.index("Later task")
    assert sooner_pos < later_pos


def test_task_detail_loads(client, test_requisition, test_user, db_session):
    """GET /views/tasks/{id} returns task detail panel."""
    task = _make_task(
        db_session, test_requisition.id,
        assigned_to_id=test_user.id, title="Detail test task",
        task_type="sourcing", priority=3,
    )
    resp = client.get(f"/views/tasks/{task.id}")
    assert resp.status_code == 200
    assert "Detail test task" in resp.text
    assert "sourcing" in resp.text
    assert "High" in resp.text


def test_task_detail_404_for_missing(client):
    """GET /views/tasks/{id} returns 404 for nonexistent task."""
    resp = client.get("/views/tasks/99999")
    assert resp.status_code == 404


def test_task_status_change(client, test_requisition, test_user, db_session):
    """PATCH /api/tasks/{id}/status updates task status."""
    task = _make_task(db_session, test_requisition.id, assigned_to_id=test_user.id)
    resp = client.patch(f"/api/tasks/{task.id}/status", json={"status": "in_progress"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


def test_task_status_change_invalid(client, test_requisition, test_user, db_session):
    """PATCH /api/tasks/{id}/status rejects invalid status."""
    task = _make_task(db_session, test_requisition.id, assigned_to_id=test_user.id)
    resp = client.patch(f"/api/tasks/{task.id}/status", json={"status": "invalid_status"})
    assert resp.status_code == 400


def test_task_complete(client, test_requisition, test_user, db_session):
    """POST /api/tasks/{id}/complete marks task done with note."""
    task = _make_task(db_session, test_requisition.id, assigned_to_id=test_user.id)
    resp = client.post(
        f"/api/tasks/{task.id}/complete",
        json={"completion_note": "Found vendor, stock confirmed"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["completion_note"] == "Found vendor, stock confirmed"


def test_task_create_from_manager(client, test_requisition, test_user, db_session):
    """POST /api/requisitions/{id}/tasks creates a task visible in the manager."""
    due = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/tasks",
        json={
            "title": "New task from manager",
            "assigned_to_id": test_user.id,
            "due_at": due,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == "New task from manager"

    # Verify it appears in the task manager
    list_resp = client.get("/tasks?tab=my_tasks")
    assert list_resp.status_code == 200
    assert "New task from manager" in list_resp.text


def test_sidebar_has_tasks_link(client, test_requisition, test_user):
    """Sidebar navigation includes Tasks link."""
    resp = client.get("/tasks")
    assert resp.status_code == 200
    # The sidebar is included in the base template
    assert "/tasks" in resp.text
    assert "Tasks" in resp.text


# ---------------------------------------------------------------------------
# Auto-task creation tests (service-level)
# ---------------------------------------------------------------------------


def test_auto_task_on_requirement_added(db_session, test_requisition, test_user):
    """on_requirement_added creates a sourcing task for the MPN."""
    from app.services.task_service import on_requirement_added

    on_requirement_added(db_session, test_requisition.id, "NE555P", assigned_to_id=test_user.id)
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.source_ref == "source:NE555P")
        .first()
    )
    assert task is not None
    assert "NE555P" in task.title
    assert task.task_type == "sourcing"
    assert task.assigned_to_id == test_user.id
    assert task.source == "system"


def test_auto_task_on_offer_received(db_session, test_requisition):
    """on_offer_received creates a review task for the offer."""
    from app.services.task_service import on_offer_received

    on_offer_received(db_session, test_requisition.id, "Arrow Electronics", "LM317T", 42)
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.source_ref == "offer:42")
        .first()
    )
    assert task is not None
    assert "Arrow Electronics" in task.title
    assert "LM317T" in task.title
    assert task.task_type == "sourcing"


def test_auto_task_idempotent(db_session, test_requisition):
    """Calling the same auto-gen helper twice does not create duplicates."""
    from app.services.task_service import on_requirement_added

    on_requirement_added(db_session, test_requisition.id, "LM317T")
    on_requirement_added(db_session, test_requisition.id, "LM317T")
    tasks = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.source_ref == "source:LM317T")
        .all()
    )
    assert len(tasks) == 1


def test_auto_task_via_requirement_api(client, test_requisition, test_user, db_session):
    """Adding a requirement via API auto-creates a sourcing task."""
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/requirements",
        json={"primary_mpn": "SN74HC595N", "target_qty": 200},
    )
    assert resp.status_code == 200
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.source_ref == "source:SN74HC595N")
        .first()
    )
    assert task is not None
    assert "SN74HC595N" in task.title
    assert task.source == "system"
