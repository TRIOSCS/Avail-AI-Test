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
