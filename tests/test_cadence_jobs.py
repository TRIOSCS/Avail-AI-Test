from datetime import UTC, datetime

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


def test_cadence_job_registered():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.jobs.cadence_jobs import register_cadence_jobs

    sched = AsyncIOScheduler()
    register_cadence_jobs(sched, settings=None)
    assert sched.get_job("cadence_materialize") is not None
