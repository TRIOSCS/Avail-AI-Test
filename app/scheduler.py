"""Background scheduler — APScheduler coordinator.

Job implementations live in app/jobs/ domain modules. This module provides:
  - _traced_job decorator (used by all job modules)
  - Global scheduler instance
  - configure_scheduler() entry point

Token management lives in app/utils/token_manager.
"""

import time
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
            start = time.monotonic()
            try:
                return await func(*args, **kwargs)
            except Exception:
                logger.exception("Job failed")
                raise
            finally:
                elapsed = time.monotonic() - start
                logger.debug(f"Job finished: {func.__name__} [{trace_id}, {elapsed:.1f}s]")

    return wrapper


# Global scheduler instance
scheduler = AsyncIOScheduler(
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    }
)


# Backward-compatible re-exports for test files that import token functions
# from app.scheduler. Production code should import from app.utils.token_manager.
from .utils.token_manager import (  # noqa: E402, F401
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
