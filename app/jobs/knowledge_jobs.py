"""Background jobs for the Knowledge Ledger.

- refresh_active_insights: Re-generate AI insights for recently active reqs (every 6h)
- _job_expire_stale: Log a count of expired entries for monitoring — expiry itself is
  applied at query time, not by this job (daily 3AM)

Called by: app/jobs/__init__.py via register_knowledge_jobs()
Depends on: services/knowledge_service.py
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


async def _refresh_each(rows, generate, *, noun, warn_template):
    """Generate insights for each id in ``rows`` (list of 1-tuples), tolerating per-id
    failures, and log a ``successes/total`` summary.

    ``generate`` is an async callable ``(id) -> entries``; a non-empty result counts as a
    success. ``warn_template`` is a Loguru ``"... {} ... {}"`` format taking (id, error).
    """
    ok = 0
    for (item_id,) in rows:
        try:
            entries = await generate(item_id)
            if entries:
                ok += 1
        except Exception as e:
            logger.warning(warn_template, item_id, e)
    logger.info("Refreshed insights for {}/{} active {}", ok, len(rows), noun)


def register_knowledge_jobs(scheduler, settings):
    """Register knowledge ledger background jobs."""
    # DISABLED (2026-03-26) — Knowledge insights not active in UI; heavy Anthropic
    # API cost (~141 Sonnet calls w/ extended thinking every 6h).
    # scheduler.add_job(
    #     _job_refresh_insights,
    #     IntervalTrigger(hours=6),
    #     id="knowledge_refresh_insights",
    #     name="Refresh AI insights for active requisitions",
    # )
    scheduler.add_job(
        _job_expire_stale,
        CronTrigger(hour=3, minute=0),
        id="knowledge_expire_stale",
        name="Mark expired knowledge entries",
    )


@_traced_job
async def _job_refresh_insights():
    """Re-generate insights for recently active reqs, vendors, companies, MPNs, and
    pipeline."""
    from sqlalchemy import func

    from app.database import SessionLocal
    from app.models.crm import CustomerSite
    from app.models.offers import Offer
    from app.models.sourcing import Requisition
    from app.services import knowledge_service

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        # --- Requisition insights (top 50 recently active) ---
        try:
            active_reqs = (
                db.query(Requisition.id)
                .filter(Requisition.updated_at >= cutoff)
                .order_by(Requisition.updated_at.desc())
                .limit(50)
                .all()
            )
            await _refresh_each(
                active_reqs,
                lambda req_id: knowledge_service.generate_insights(db, req_id),
                noun="reqs",
                warn_template="Insight generation failed for req {}: {}",
            )
        except Exception as e:
            logger.error("Requisition insight refresh failed: {}", e)

        # --- Pipeline insights (1 per run) ---
        try:
            entries = await knowledge_service.generate_pipeline_insights(db)
            logger.info("Pipeline insights generated: {} entries", len(entries) if entries else 0)
        except Exception as e:
            logger.error("Pipeline insight refresh failed: {}", e)

        # --- Vendor insights (top 20 most active by recent offers) ---
        try:
            top_vendors = (
                db.query(Offer.vendor_card_id)
                .filter(Offer.created_at >= cutoff, Offer.vendor_card_id.isnot(None))
                .group_by(Offer.vendor_card_id)
                .order_by(func.count(Offer.id).desc())
                .limit(20)
                .all()
            )
            await _refresh_each(
                top_vendors,
                lambda vid: knowledge_service.generate_vendor_insights(db, vid),
                noun="vendors",
                warn_template="Vendor insight failed for vendor_card {}: {}",
            )
        except Exception as e:
            logger.error("Vendor insight refresh failed: {}", e)

        # --- Company insights (top 20 most active by recent requisitions) ---
        try:
            top_companies = (
                db.query(CustomerSite.company_id)
                .join(Requisition, Requisition.customer_site_id == CustomerSite.id)
                .filter(Requisition.updated_at >= cutoff, CustomerSite.company_id.isnot(None))
                .group_by(CustomerSite.company_id)
                .order_by(func.count(Requisition.id).desc())
                .limit(20)
                .all()
            )
            await _refresh_each(
                top_companies,
                lambda cid: knowledge_service.generate_company_insights(db, cid),
                noun="companies",
                warn_template="Company insight failed for company {}: {}",
            )
        except Exception as e:
            logger.error("Company insight refresh failed: {}", e)

        # --- MPN insights (top 50 most-quoted MPNs) ---
        try:
            top_mpns = (
                db.query(Offer.mpn)
                .filter(Offer.created_at >= cutoff, Offer.mpn.isnot(None), Offer.mpn != "")
                .group_by(Offer.mpn)
                .order_by(func.count(Offer.id).desc())
                .limit(50)
                .all()
            )
            await _refresh_each(
                top_mpns,
                lambda mpn: knowledge_service.generate_mpn_insights(db, mpn),
                noun="MPNs",
                warn_template="MPN insight failed for {}: {}",
            )
        except Exception as e:
            logger.error("MPN insight refresh failed: {}", e)

    except Exception as e:
        logger.exception("refresh_active_insights job failed: {}", e)
        db.rollback()
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        db.close()


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
