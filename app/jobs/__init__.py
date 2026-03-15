"""Background job registry — delegates to domain-specific modules.

Called by: app/scheduler.py via configure_scheduler()
Each sub-module exposes a register_*_jobs(scheduler, settings) function.
"""

from loguru import logger


def register_all_jobs(scheduler, settings):
    """Register all background jobs from domain modules."""
    from .core_jobs import register_core_jobs
    from .eight_by_eight_jobs import register_eight_by_eight_jobs
    from .email_jobs import register_email_jobs
    from .health_jobs import register_health_jobs
    from .inventory_jobs import register_inventory_jobs
    from .knowledge_jobs import register_knowledge_jobs
    from .maintenance_jobs import register_maintenance_jobs
    from .offers_jobs import register_offers_jobs
    from .prospecting_jobs import register_prospecting_jobs
    from .sourcing_refresh_jobs import register_sourcing_refresh_jobs
    from .tagging_jobs import register_tagging_jobs
    from .task_jobs import register_task_jobs

    register_core_jobs(scheduler, settings)
    register_email_jobs(scheduler, settings)
    register_inventory_jobs(scheduler, settings)
    register_offers_jobs(scheduler, settings)
    register_prospecting_jobs(scheduler, settings)
    register_sourcing_refresh_jobs(scheduler, settings)
    register_tagging_jobs(scheduler, settings)
    register_maintenance_jobs(scheduler, settings)
    register_health_jobs(scheduler, settings)
    register_knowledge_jobs(scheduler, settings)
    register_eight_by_eight_jobs(scheduler, settings)
    register_task_jobs(scheduler, settings)

    job_count = len(scheduler.get_jobs())
    logger.info(f"APScheduler configured with {job_count} jobs")
