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
