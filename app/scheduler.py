"""Background scheduler — APScheduler coordinator.

Job implementations live in app/jobs/ domain modules. This module provides:
  - _traced_job decorator (used by all job modules)
  - Global scheduler instance
  - Token management re-exports
  - configure_scheduler() entry point
"""

import uuid
from functools import wraps

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger


def _traced_job(func):
    """Wrap scheduler jobs with a unique trace_id for log correlation."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        trace_id = str(uuid.uuid4())[:8]
        with logger.contextualize(trace_id=trace_id, job=func.__name__):
            logger.debug("Job started")
            try:
                return await func(*args, **kwargs)
            except Exception:
                logger.exception("Job failed")
                raise
            finally:
                logger.debug("Job finished")

    return wrapper


# Global scheduler instance
scheduler = AsyncIOScheduler(
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    }
)


# ── Token Management (re-exported from utils.token_manager) ─────────────
from .utils.token_manager import (  # noqa: E402, F401
    _refresh_access_token,
    _utc,
    get_valid_token,
    refresh_user_token,
)

# ── Scheduler Configuration ────────────────────────────────────────────


def configure_scheduler():
    """Register all background jobs with the APScheduler instance."""
    from .config import settings
    from .jobs import register_all_jobs

    register_all_jobs(scheduler, settings)


# ── Re-exports for backward compatibility ──────────────────────────────
# Tests and other modules import job functions from app.scheduler; keep those
# paths working by re-exporting everything from the domain modules.

from .jobs.core_jobs import (  # noqa: E402, F401
    _job_auto_archive,
    _job_batch_results,
    _job_inbox_scan,
    _job_token_refresh,
    _job_webhook_subscriptions,
)
from .jobs.email_jobs import (  # noqa: E402, F401
    _compute_vendor_scores_job,
    _job_calendar_scan,
    _job_contact_scoring,
    _job_contact_status_compute,
    _job_contacts_sync,
    _job_deep_email_mining,
    _job_email_health_update,
    _job_email_reverification,
    _job_ownership_sweep,
    _job_site_ownership_sweep,
    _mine_vendor_contacts,
    _scan_outbound_rfqs,
    _scan_user_inbox,
    _sync_user_contacts,
)
from .jobs.enrichment_jobs import (  # noqa: E402, F401
    _job_customer_enrichment_sweep,
    _job_deep_enrichment,
    _job_engagement_scoring,
    _job_monthly_enrichment_refresh,
)
from .jobs.health_jobs import (  # noqa: E402, F401
    _job_cleanup_usage_log,
    _job_health_deep,
    _job_health_ping,
    _job_reset_monthly_usage,
)
from .jobs.inventory_jobs import (  # noqa: E402, F401
    _download_and_import_stock_list,
    _job_po_verification,
    _job_stock_autocomplete,
    _parse_stock_file,
    _scan_stock_list_attachments,
)
from .jobs.maintenance_jobs import (  # noqa: E402, F401
    _job_auto_attribute_activities,
    _job_auto_dedup,
    _job_cache_cleanup,
    _job_integrity_check,
    _job_reset_connector_errors,
)
from .jobs.offers_jobs import (  # noqa: E402, F401
    _job_flag_stale_offers,
    _job_performance_tracking,
    _job_proactive_matching,
    _job_proactive_offer_expiry,
)
from .jobs.prospecting_jobs import (  # noqa: E402, F401
    _job_discover_prospects,
    _job_enrich_pool,
    _job_expire_and_resurface,
    _job_find_contacts,
    _job_pool_health_report,
    _job_refresh_scores,
)
from .jobs.selfheal_jobs import (  # noqa: E402, F401
    _job_self_heal_auto_close,
    _job_self_heal_weekly_report,
)
from .jobs.tagging_jobs import (  # noqa: E402, F401
    _job_connector_enrichment,
    _job_internal_boost,
    _job_material_enrichment,
    _job_nexar_validate,
    _job_prefix_backfill,
    _job_sighting_mining,
    _job_tagging_backfill,
)
