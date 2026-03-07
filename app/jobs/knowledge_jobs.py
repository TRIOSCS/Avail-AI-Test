"""Background jobs for the Knowledge Ledger.

- refresh_active_insights: Re-generate AI insights for recently active reqs (every 6h)
- expire_stale_entries: Mark expired entries (daily 3AM)

Called by: app/jobs/__init__.py via register_knowledge_jobs()
Depends on: services/knowledge_service.py
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


def register_knowledge_jobs(scheduler, settings):
    """Register knowledge ledger background jobs."""
    scheduler.add_job(
        _job_refresh_insights,
        IntervalTrigger(hours=6),
        id="knowledge_refresh_insights",
        name="Refresh AI insights for active requisitions",
    )
    scheduler.add_job(
        _job_expire_stale,
        CronTrigger(hour=3, minute=0),
        id="knowledge_expire_stale",
        name="Mark expired knowledge entries",
    )


async def _job_refresh_insights():
    """Re-generate insights for reqs updated in the last 24h, cap 50."""
    from app.database import SessionLocal
    from app.models.sourcing import Requisition
    from app.services import knowledge_service

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        active_reqs = (
            db.query(Requisition.id)
            .filter(Requisition.updated_at >= cutoff)
            .order_by(Requisition.updated_at.desc())
            .limit(50)
            .all()
        )
        count = 0
        for (req_id,) in active_reqs:
            try:
                entries = await knowledge_service.generate_insights(db, req_id)
                if entries:
                    count += 1
            except Exception as e:
                logger.warning("Insight generation failed for req {}: {}", req_id, e)
        logger.info("Refreshed insights for {}/{} active reqs", count, len(active_reqs))
    except Exception as e:
        logger.error("refresh_active_insights job failed: {}", e)
    finally:
        db.close()


async def _job_expire_stale():
    """Log count of expired entries for monitoring. Expiry is handled at query time."""
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
        logger.error("expire_stale job failed: {}", e)
    finally:
        db.close()
