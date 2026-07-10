from datetime import UTC, datetime, timedelta

from app.constants import ActivityType, Channel, Direction
from app.management.backfill_cadence_clocks import backfill_for_session
from app.models.crm import Company
from app.models.intelligence import ActivityLog

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


def test_backfill_populates_existing_companies(db_session):
    co = Company(name="Backfill Co")  # clock columns start NULL
    db_session.add(co)
    db_session.flush()
    db_session.add(
        ActivityLog(
            activity_type=ActivityType.RFQ_SENT,
            channel=Channel.EMAIL,
            company_id=co.id,
            direction=Direction.OUTBOUND,
            created_at=NOW - timedelta(days=4),
            occurred_at=NOW - timedelta(days=4),
        )
    )
    db_session.commit()

    count = backfill_for_session(db_session)
    db_session.commit()
    db_session.refresh(co)
    assert count == 1
    assert co.last_outbound_at == NOW - timedelta(days=4)


def test_cadence_job_registered():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.jobs.cadence_jobs import register_cadence_jobs

    sched = AsyncIOScheduler()
    register_cadence_jobs(sched, settings=None)
    assert sched.get_job("cadence_materialize") is not None
