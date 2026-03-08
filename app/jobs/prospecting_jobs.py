"""Prospecting background jobs — pool health, discovery, enrichment, contacts, scoring.

Called by: app/jobs/__init__.py via register_prospecting_jobs()
Depends on: app.services.prospect_scheduler
"""

from apscheduler.triggers.cron import CronTrigger

from ..scheduler import _traced_job


def register_prospecting_jobs(scheduler, settings):
    """Register prospecting jobs with the scheduler."""
    if not settings.prospecting_enabled:
        return

    scheduler.add_job(
        _job_pool_health_report,
        CronTrigger(day=1, hour=8, minute=0),
        id="pool_health_report",
        name="Pool health report",
    )
    scheduler.add_job(
        _job_discover_prospects,
        CronTrigger(day=1, hour=21, minute=0),
        id="discover_prospects",
        name="Prospect discovery",
    )
    scheduler.add_job(_job_enrich_pool, CronTrigger(day=2, hour=2, minute=0), id="enrich_pool", name="Pool enrichment")
    scheduler.add_job(
        _job_find_contacts,
        CronTrigger(day=3, hour=2, minute=0),
        id="find_contacts",
        name="Prospect contact enrichment",
    )
    scheduler.add_job(
        _job_refresh_scores,
        CronTrigger(day=15, hour=2, minute=0),
        id="refresh_scores",
        name="Prospect score refresh",
    )
    scheduler.add_job(
        _job_expire_and_resurface,
        CronTrigger(day="last", hour=21, minute=0),
        id="expire_and_resurface",
        name="Expire and resurface prospects",
    )


@_traced_job
async def _job_pool_health_report():
    """1st of month 8AM — log pool statistics."""
    from ..services.prospect_scheduler import job_pool_health_report

    await job_pool_health_report()


@_traced_job
async def _job_discover_prospects():
    """1st of month 9PM — run discovery for next segment slice."""
    from ..services.prospect_scheduler import job_discover_prospects

    await job_discover_prospects()


@_traced_job
async def _job_enrich_pool():
    """2nd of month 2AM — enrich signals, similar customers, AI writeups."""
    from ..services.prospect_scheduler import job_enrich_pool

    await job_enrich_pool()


@_traced_job
async def _job_find_contacts():
    """3rd of month 2AM — find procurement contacts."""
    from ..services.prospect_scheduler import job_find_contacts

    await job_find_contacts()


@_traced_job
async def _job_refresh_scores():
    """15th of month 2AM — re-score all suggested prospects."""
    from ..services.prospect_scheduler import job_refresh_scores

    await job_refresh_scores()


@_traced_job
async def _job_expire_and_resurface():
    """Last day of month 9PM — expire stale, resurface refreshed."""
    from ..services.prospect_scheduler import job_expire_and_resurface

    await job_expire_and_resurface()
