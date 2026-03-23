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

from ..scheduler import _traced_job


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
            req_ok = 0
            for (req_id,) in active_reqs:
                try:
                    entries = await knowledge_service.generate_insights(db, req_id)
                    if entries:
                        req_ok += 1
                except Exception as e:
                    logger.warning("Insight generation failed for req {}: {}", req_id, e)
            logger.info("Refreshed insights for {}/{} active reqs", req_ok, len(active_reqs))
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
            vendor_ok = 0
            for (vid,) in top_vendors:
                try:
                    entries = await knowledge_service.generate_vendor_insights(db, vid)
                    if entries:
                        vendor_ok += 1
                except Exception as e:
                    logger.warning("Vendor insight failed for vendor_card {}: {}", vid, e)
            logger.info("Refreshed insights for {}/{} active vendors", vendor_ok, len(top_vendors))
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
            company_ok = 0
            for (cid,) in top_companies:
                try:
                    entries = await knowledge_service.generate_company_insights(db, cid)
                    if entries:
                        company_ok += 1
                except Exception as e:
                    logger.warning("Company insight failed for company {}: {}", cid, e)
            logger.info("Refreshed insights for {}/{} active companies", company_ok, len(top_companies))
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
            mpn_ok = 0
            for (mpn,) in top_mpns:
                try:
                    entries = await knowledge_service.generate_mpn_insights(db, mpn)
                    if entries:
                        mpn_ok += 1
                except Exception as e:
                    logger.warning("MPN insight failed for {}: {}", mpn, e)
            logger.info("Refreshed insights for {}/{} active MPNs", mpn_ok, len(top_mpns))
        except Exception as e:
            logger.error("MPN insight refresh failed: {}", e)

    except Exception as e:
        logger.error("refresh_active_insights job failed: {}", e)
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        db.close()


@_traced_job
async def _job_deliver_question_batches():
    """Deliver batched question cards to buyers (Teams removed — no-op)."""
    logger.debug("Teams question batch delivery skipped (removed)")


@_traced_job
async def _job_send_knowledge_digests():
    """Send daily knowledge digests (Teams removed — no-op)."""
    logger.debug("Teams knowledge digest delivery skipped (removed)")


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
        logger.error("expire_stale job failed: {}", e)
        raise  # Re-raise so _traced_job / Sentry can capture
    finally:
        db.close()
