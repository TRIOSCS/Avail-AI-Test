"""Background job registry — delegates to domain-specific modules.

Called by: app/scheduler.py via configure_scheduler()
Each sub-module exposes a register_*_jobs(scheduler, settings) function.
"""

from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


def register_all_jobs(scheduler, settings):
    """Register all background jobs from domain modules.

    When MVP_MODE is enabled, enrichment and Teams alert jobs are skipped.
    """
    from .core_jobs import register_core_jobs
    from .eight_by_eight_jobs import register_eight_by_eight_jobs
    from .email_jobs import register_email_jobs
    from .enrichment_jobs import _job_engagement_scoring
    from .health_jobs import register_health_jobs
    from .inventory_jobs import register_inventory_jobs
    from .knowledge_jobs import register_knowledge_jobs
    from .maintenance_jobs import register_maintenance_jobs
    from .offers_jobs import register_offers_jobs
    from .prospecting_jobs import register_prospecting_jobs
    from .sourcing_refresh_jobs import register_sourcing_refresh_jobs
    from .tagging_jobs import register_tagging_jobs

    register_core_jobs(scheduler, settings)
    register_email_jobs(scheduler, settings)
    register_inventory_jobs(scheduler, settings)
    register_offers_jobs(scheduler, settings)
    register_prospecting_jobs(scheduler, settings)
    register_sourcing_refresh_jobs(scheduler, settings)
    register_tagging_jobs(scheduler, settings)
    register_maintenance_jobs(scheduler, settings)
    register_health_jobs(scheduler, settings)
    register_eight_by_eight_jobs(scheduler, settings)
    register_knowledge_jobs(scheduler, settings)
    if not scheduler.get_job("engagement_scoring"):
        scheduler.add_job(
            _job_engagement_scoring, IntervalTrigger(hours=12), id="engagement_scoring", name="Engagement scoring"
        )

    # Full-version-only jobs (disabled in MVP mode)
    mvp_mode = settings.mvp_mode if isinstance(getattr(settings, "mvp_mode", False), bool) else False
    if not mvp_mode:
        from .enrichment_jobs import register_enrichment_jobs
        from .teams_alert_jobs import register_teams_alert_jobs

        register_enrichment_jobs(scheduler, settings)
        register_teams_alert_jobs(scheduler, settings)
        logger.info("Full mode: enrichment + Teams alert jobs registered")
    else:
        logger.info("MVP mode: skipping enrichment + Teams alert jobs")

    job_count = len(scheduler.get_jobs())
    logger.info(f"APScheduler configured with {job_count} jobs")
