"""Background job registry — delegates to domain-specific modules.

Called by: app/scheduler.py via configure_scheduler()
Each sub-module exposes a register_*_jobs(scheduler, settings) function.
"""

from loguru import logger


def register_all_jobs(scheduler, settings):
    """Register all background jobs from domain modules."""
    from ..database import SessionLocal
    from .approval_outbox import register_approval_outbox_job
    from .cadence_jobs import register_cadence_jobs
    from .core_jobs import register_core_jobs
    from .eight_by_eight_jobs import register_eight_by_eight_jobs
    from .email_jobs import register_email_jobs
    from .health_jobs import register_health_jobs
    from .inventory_jobs import register_inventory_jobs
    from .knowledge_jobs import register_knowledge_jobs
    from .maintenance_jobs import register_maintenance_jobs
    from .offers_jobs import register_offers_jobs
    from .prospecting_jobs import register_prospecting_jobs, register_sweep_jobs
    from .quality_jobs import register_quality_jobs
    from .tagging_jobs import register_tagging_jobs
    from .task_jobs import register_task_jobs
    from .teams_call_jobs import register_teams_call_jobs
    from .worker_liveness_jobs import register_worker_liveness_jobs

    # Short-lived session so flag-reading registrars resolve the DB-authoritative
    # value (system_config row overrides env) at scheduler-config time. Closed in
    # finally. run_startup_migrations() has already seeded + reconciled the rows.
    db = SessionLocal()
    try:
        register_cadence_jobs(scheduler, settings)
        register_core_jobs(scheduler, settings, db)
        register_email_jobs(scheduler, settings, db)
        register_inventory_jobs(scheduler, settings)
        register_offers_jobs(scheduler, settings, db)
        register_prospecting_jobs(scheduler, settings)
        register_sweep_jobs(scheduler, settings)
        register_tagging_jobs(scheduler, settings)
        register_maintenance_jobs(scheduler, settings)
        register_health_jobs(scheduler, settings)
        register_knowledge_jobs(scheduler, settings)
        register_eight_by_eight_jobs(scheduler, settings)
        register_task_jobs(scheduler, settings)
        register_quality_jobs(scheduler, settings)
        register_teams_call_jobs(scheduler, settings)
        register_worker_liveness_jobs(scheduler, settings)
        register_approval_outbox_job(scheduler)
    finally:
        db.close()
    job_count = len(scheduler.get_jobs())
    logger.info(f"APScheduler configured with {job_count} jobs")
