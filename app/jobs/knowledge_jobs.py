"""Background jobs for the Knowledge Ledger.

- _job_expire_stale: Log a count of expired entries for monitoring — expiry itself is
  applied at query time, not by this job (daily 3AM)

The KB-insight refresh job (`knowledge_refresh_insights`) was deleted 2026-07-06: it had
no UI consumer and burned Anthropic API cost when enabled. Recoverable from git history.

Called by: app/jobs/__init__.py via register_knowledge_jobs()
Depends on: app/database.py, models/knowledge.py
"""

from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_knowledge_jobs(scheduler, settings):
    """Register knowledge ledger background jobs."""
    scheduler.add_job(
        _job_expire_stale,
        CronTrigger(hour=3, minute=0),
        id="knowledge_expire_stale",
        name="Mark expired knowledge entries",
    )


@_traced_job
async def _job_expire_stale():
    """Log count of expired entries for monitoring.

    Expiry is handled at query time.
    """
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeEntry

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired_count = (
            db.query(KnowledgeEntry)
            .filter(KnowledgeEntry.expires_at.isnot(None), KnowledgeEntry.expires_at < now)
            .count()
        )
        total = db.query(KnowledgeEntry).count()
        logger.info("Knowledge entries: {} total, {} expired", total, expired_count)
    except Exception as e:
        logger.exception("expire_stale job failed: {}", e)
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        db.close()
