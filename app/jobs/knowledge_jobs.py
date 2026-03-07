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
    scheduler.add_job(
        _job_deliver_question_batches,
        IntervalTrigger(hours=1),
        id="knowledge_deliver_batches",
        name="Deliver batched Q&A questions to buyers",
    )
    scheduler.add_job(
        _job_send_knowledge_digests,
        IntervalTrigger(hours=1),
        id="knowledge_send_digests",
        name="Send daily knowledge digests",
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


async def _job_deliver_question_batches():
    """Deliver batched question cards to buyers whose digest hour matches now.

    Runs every hour. For each user with TeamsAlertConfig, checks if current UTC hour
    matches their knowledge_digest_hour or knowledge_digest_hour + 6. If so, sends batch.
    """
    from app.database import SessionLocal
    from app.models.teams_alert_config import TeamsAlertConfig
    from app.services.teams_qa_service import deliver_question_batch

    db = SessionLocal()
    try:
        current_hour = datetime.now(timezone.utc).hour
        configs = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.alerts_enabled.is_(True)).all()

        delivered_total = 0
        for config in configs:
            digest_hour = config.knowledge_digest_hour or 14
            if current_hour not in (digest_hour % 24, (digest_hour + 6) % 24):
                continue
            try:
                count = await deliver_question_batch(db, config.user_id)
                delivered_total += count
            except Exception as e:
                logger.warning("Batch delivery failed for user {}: {}", config.user_id, e)

        if delivered_total:
            logger.info("Delivered {} questions across batch runs", delivered_total)
    except Exception as e:
        logger.error("deliver_question_batches job failed: {}", e)
    finally:
        db.close()


async def _job_send_knowledge_digests():
    """Send daily knowledge digests to users whose digest hour matches now.

    Runs every hour. Only sends at the user's configured knowledge_digest_hour.
    """
    from app.database import SessionLocal
    from app.models.teams_alert_config import TeamsAlertConfig
    from app.services.teams_qa_service import deliver_knowledge_digest

    db = SessionLocal()
    try:
        current_hour = datetime.now(timezone.utc).hour
        configs = db.query(TeamsAlertConfig).filter(TeamsAlertConfig.alerts_enabled.is_(True)).all()

        sent_count = 0
        for config in configs:
            digest_hour = config.knowledge_digest_hour or 14
            if current_hour != digest_hour % 24:
                continue
            try:
                sent = await deliver_knowledge_digest(db, config.user_id)
                if sent:
                    sent_count += 1
            except Exception as e:
                logger.warning("Digest delivery failed for user {}: {}", config.user_id, e)

        if sent_count:
            logger.info("Sent {} knowledge digests", sent_count)
    except Exception as e:
        logger.error("send_knowledge_digests job failed: {}", e)
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
