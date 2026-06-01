"""test_lifecycle_activity_logging.py — lifecycle events write activity_log rows.

Covers Plan 2b: task_completed, assignment_changed, req_archived/req_unarchived,
and sales_note events route through activity_service.log_activity().

Called by: pytest
Depends on: app/services/activity_service.py, app/constants.py, conftest.py
"""

from app.constants import ActivityType
from app.models import ActivityLog


def _activity_rows(db, requisition_id, activity_type):
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.activity_type == activity_type,
        )
        .all()
    )


def test_complete_task_logs_task_completed(db_session, test_requisition, test_user):
    """Completing a task writes a task_completed activity row on its requisition."""
    from app.models import RequisitionTask
    from app.services.task_service import complete_task

    task = RequisitionTask(
        requisition_id=test_requisition.id,
        title="Follow up with vendor",
        created_by=test_user.id,
        assigned_to_id=test_user.id,
    )
    db_session.add(task)
    db_session.flush()

    complete_task(
        db=db_session,
        task_id=task.id,
        user_id=test_user.id,
        completion_note="Done",
    )
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.TASK_COMPLETED)
    assert len(rows) == 1


def test_claim_requisition_logs_assignment_changed(db_session, test_requisition, test_user):
    """Claiming a requisition writes an assignment_changed activity row (not a raw-
    string type)."""
    from app.services.requirement_status import claim_requisition

    claim_requisition(test_requisition, test_user, db_session)
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.ASSIGNMENT_CHANGED)
    assert len(rows) == 1
    legacy = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "requisition_claimed").all()
    assert legacy == []


def test_toggle_archive_logs_req_archived(client, db_session, test_requisition):
    """Archiving a requisition writes a req_archived activity row; toggling back writes
    a req_unarchived row."""
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.REQ_ARCHIVED)
    assert len(rows) == 1

    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.REQ_UNARCHIVED)
    assert len(rows) == 1


def test_batch_archive_logs_req_archived(client, db_session, test_requisition):
    """Bulk-archiving requisitions writes a req_archived row for each."""
    before = len(_activity_rows(db_session, test_requisition.id, ActivityType.REQ_ARCHIVED))
    resp = client.put("/api/requisitions/batch-archive", json={"ids": [test_requisition.id]})
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.REQ_ARCHIVED)
    assert len(rows) == before + 1


def test_save_part_notes_logs_sales_note(client, db_session, test_requisition):
    """Editing a requirement's sale notes writes a sales_note activity row."""
    requirement = test_requisition.requirements[0]
    resp = client.patch(
        f"/v2/partials/parts/{requirement.id}/notes",
        data={"sale_notes": "Customer wants expedited quote"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.SALES_NOTE)
    assert len(rows) == 1


def test_unclaim_requisition_logs_assignment_changed(db_session, test_requisition, test_user):
    """Unclaiming a requisition writes an assignment_changed row (not a raw-string
    type); the actor-None path still logs without raising."""
    from app.services.requirement_status import claim_requisition, unclaim_requisition

    claim_requisition(test_requisition, test_user, db_session)
    db_session.commit()

    # Unclaim with an explicit actor.
    changed = unclaim_requisition(test_requisition, db_session, actor=test_user)
    db_session.commit()
    assert changed is True

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.ASSIGNMENT_CHANGED)
    # One from the claim, one from the unclaim.
    assert len(rows) == 2
    legacy = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "requisition_unclaimed").all()
    assert legacy == []

    # actor=None path: re-claim then unclaim with no actor — must not raise and must log.
    claim_requisition(test_requisition, test_user, db_session)
    db_session.commit()
    changed = unclaim_requisition(test_requisition, db_session)
    db_session.commit()
    assert changed is True
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.ASSIGNMENT_CHANGED)
    assert len(rows) == 4


def test_batch_assign_logs_assignment_changed(client, db_session, test_requisition, test_user):
    """Batch-assigning requisitions writes one assignment_changed row per id."""
    resp = client.put(
        "/api/requisitions/batch-assign",
        json={"ids": [test_requisition.id], "owner_id": test_user.id},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.ASSIGNMENT_CHANGED)
    assert len(rows) == 1


def test_update_requirement_sales_note_logs(client, db_session, test_requisition):
    """Updating a requirement's sale_notes writes exactly one sales_note row; an update
    with the same value writes no second row (change-guard)."""
    requirement = test_requisition.requirements[0]
    resp = client.put(
        f"/api/requirements/{requirement.id}",
        json={"sale_notes": "Expedite this part"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.SALES_NOTE)
    assert len(rows) == 1

    # Same value again — change-guard must suppress a second row.
    resp = client.put(
        f"/api/requirements/{requirement.id}",
        json={"sale_notes": "Expedite this part"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.SALES_NOTE)
    assert len(rows) == 1


def test_reopen_task_logs_task_reopened(db_session, test_requisition, test_user):
    """Reopening a task writes a task_reopened activity row on its requisition."""
    from app.constants import TaskStatus
    from app.models import RequisitionTask
    from app.services.task_service import reopen_task

    task = RequisitionTask(
        requisition_id=test_requisition.id,
        title="Follow up with vendor",
        created_by=test_user.id,
        assigned_to_id=test_user.id,
        status=TaskStatus.DONE,
    )
    db_session.add(task)
    db_session.flush()

    reopen_task(
        db=db_session,
        task_id=task.id,
        user_id=test_user.id,
    )
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.TASK_REOPENED)
    assert len(rows) == 1
    assert rows[0].user_id == test_user.id
    assert rows[0].details["task_id"] == task.id


def test_mark_task_done_logs_task_completed(client, db_session, test_requisition, test_user):
    """Marking a task done via the htmx route writes a task_completed row."""
    from app.models import RequisitionTask

    requirement = test_requisition.requirements[0]
    task = RequisitionTask(
        requisition_id=test_requisition.id,
        requirement_id=requirement.id,
        title="Call the vendor",
        created_by=test_user.id,
        assigned_to_id=test_user.id,
    )
    db_session.add(task)
    db_session.commit()

    resp = client.post(f"/v2/partials/parts/tasks/{task.id}/done")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.TASK_COMPLETED)
    assert len(rows) == 1
