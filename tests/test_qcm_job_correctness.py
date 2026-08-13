"""test_qcm_job_correctness.py — background-job correctness fixes.

Two 2026-08-08 QC audit findings:
- on_bid_due_soon returned None, so the bid-due job's per-run cap and success
  logging were dead (it counts non-None results).
- _find_optimistic_row scanned ALL unreconciled call logs with no time bound, so
  never-reconciled manual logs grew into an ever-larger full-table scan.

Called by: pytest
Depends on: app.services.task_service, app.jobs.eight_by_eight_jobs, conftest
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.constants import ActivityType, Channel
from app.models import ActivityLog


def test_on_bid_due_soon_returns_the_task(db_session: Session, test_requisition):
    """The task is returned so the job can count it against its cap + log it."""
    from app.services.task_service import on_bid_due_soon

    task = on_bid_due_soon(db_session, test_requisition.id, "2026-09-01", test_requisition.name or "R")
    assert task is not None
    assert task.id is not None


def test_find_optimistic_row_matches_in_window_with_old_decoy(db_session: Session, test_user):
    """A valid in-window unreconciled call log still matches even with the new scan
    bound in place; an old no-occurred_at decoy (below the created_at floor) does
    not."""
    from app.jobs.eight_by_eight_jobs import _find_optimistic_row

    now = datetime.now(UTC)
    phone = "+14155551212"
    good = ActivityLog(
        activity_type=ActivityType.CALL_LOGGED,
        channel=Channel.PHONE,
        external_id=None,
        user_id=test_user.id,
        direction="outbound",
        contact_phone=phone,
        occurred_at=now,
    )
    decoy = ActivityLog(
        activity_type=ActivityType.CALL_LOGGED,
        channel=Channel.PHONE,
        external_id=None,
        user_id=test_user.id,
        direction="outbound",
        contact_phone=phone,
        occurred_at=None,
        created_at=now - timedelta(days=10),
    )
    db_session.add_all([good, decoy])
    db_session.commit()

    match = _find_optimistic_row(db_session, test_user.id, "outbound", phone, now)
    assert match is not None
    assert match.id == good.id
