"""Enrichment background jobs — deep enrichment, monthly refresh, customer sweep, scoring.

Called by: app/jobs/__init__.py via register_enrichment_jobs()
Depends on: app.database, app.models, app.services.deep_enrichment_service, app.services.customer_enrichment_batch
"""

import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_enrichment_jobs(scheduler, settings):
    """Register enrichment jobs with the scheduler."""
    if not scheduler.get_job("engagement_scoring"):
        scheduler.add_job(
            _job_engagement_scoring, IntervalTrigger(hours=12), id="engagement_scoring", name="Engagement scoring"
        )

    # Apollo, Hunter, Gradient are OK to use at full capacity
    if settings.deep_enrichment_enabled:
        scheduler.add_job(
            _job_deep_enrichment, IntervalTrigger(hours=12), id="deep_enrichment", name="Deep enrichment sweep"
        )
        scheduler.add_job(
            _job_monthly_enrichment_refresh,
            CronTrigger(day=1, hour=4, minute=0),
            id="monthly_enrichment_refresh",
            name="Monthly enrichment refresh (credit reset)",
        )

    if settings.customer_enrichment_enabled:
        scheduler.add_job(
            _job_customer_enrichment_sweep,
            CronTrigger(month="1,4,7,10", day=1, hour=5, minute=0),
            id="customer_enrichment_sweep",
            name="Quarterly customer enrichment sweep",
        )


@_traced_job
async def _job_engagement_scoring():
    """Compute unified vendor scores for all vendors."""
    from ..database import SessionLocal
    from ..models import VendorCard
    from .email_jobs import _compute_vendor_scores_job

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        latest = (
            db.query(VendorCard.vendor_score_computed_at)
            .filter(VendorCard.vendor_score_computed_at.isnot(None))
            .order_by(VendorCard.vendor_score_computed_at.desc())
            .first()
        )

        should_compute = True
        if latest and latest[0]:
            last_computed = latest[0]
            if last_computed.tzinfo is None:
                last_computed = last_computed.replace(tzinfo=timezone.utc)
            if now - last_computed < timedelta(hours=12):
                should_compute = False

        if should_compute:
            await _compute_vendor_scores_job(db)
    except Exception as e:
        logger.error(f"Vendor scoring error: {e}")
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_deep_enrichment():
    """Deep enrichment sweep — companies first, then vendors."""
    from ..config import settings
    from ..database import SessionLocal
    from ..models import Company, VendorCard

    selector_db = SessionLocal()
    stale_company_ids: list[int] = []
    stale_vendor_ids: list[int] = []
    recent_vendor_ids: list[int] = []
    try:
        now = datetime.now(timezone.utc)
        # Companies FIRST — customer accounts are highest priority (up to 50)
        stale_companies = (
            selector_db.query(Company.id)
            .filter(
                (Company.deep_enrichment_at.is_(None))
                | (Company.deep_enrichment_at < now - timedelta(days=settings.deep_enrichment_stale_days))
            )
            .order_by(Company.last_activity_at.desc().nullslast())
            .limit(50)
            .all()
        )
        stale_company_ids = [cid for (cid,) in stale_companies]

        # Vendors second — most active first (up to 50)
        stale_vendors = (
            selector_db.query(VendorCard.id)
            .filter(
                (VendorCard.deep_enrichment_at.is_(None))
                | (VendorCard.deep_enrichment_at < now - timedelta(days=settings.deep_enrichment_stale_days))
            )
            .order_by(
                VendorCard.last_activity_at.desc().nullslast(),
                VendorCard.sighting_count.desc().nullslast(),
            )
            .limit(50)
            .all()
        )
        stale_vendor_ids = [vid for (vid,) in stale_vendors]

        # Recently created vendors (last 24h, no enrichment yet)
        recent_vendors = (
            selector_db.query(VendorCard.id)
            .filter(
                VendorCard.created_at > now - timedelta(hours=24),
                VendorCard.deep_enrichment_at.is_(None),
            )
            .limit(20)
            .all()
        )
        recent_vendor_ids = [vid for (vid,) in recent_vendors]
    except Exception as e:
        logger.error(f"Deep enrichment sweep error: {e}")
        return
    finally:
        selector_db.close()

    from ..services.deep_enrichment_service import deep_enrich_company, deep_enrich_vendor

    async def _safe_enrich_company(cid: int):
        task_db = SessionLocal()
        try:
            await deep_enrich_company(cid, task_db)
            task_db.commit()
        except Exception as e:
            logger.warning(f"Enrichment sweep company {cid} error: {e}")
            task_db.rollback()
        finally:
            task_db.close()

    async def _safe_enrich_vendor(vid: int):
        task_db = SessionLocal()
        try:
            await deep_enrich_vendor(vid, task_db)
            task_db.commit()
        except Exception as e:
            logger.warning(f"Enrichment sweep vendor {vid} error: {e}")
            task_db.rollback()
        finally:
            task_db.close()

    if stale_company_ids:
        for i in range(0, len(stale_company_ids), 10):
            batch = stale_company_ids[i : i + 10]
            await asyncio.wait_for(
                asyncio.gather(*[_safe_enrich_company(cid) for cid in batch], return_exceptions=True),
                timeout=300,
            )

    all_vendor_ids = stale_vendor_ids + recent_vendor_ids
    if all_vendor_ids:
        for i in range(0, len(all_vendor_ids), 10):
            batch = all_vendor_ids[i : i + 10]
            await asyncio.wait_for(
                asyncio.gather(*[_safe_enrich_vendor(vid) for vid in batch], return_exceptions=True),
                timeout=300,
            )

    logger.info(
        f"Deep enrichment sweep: {len(stale_company_ids)} companies, "
        f"{len(stale_vendor_ids)} vendors, {len(recent_vendor_ids)} new vendors"
    )


@_traced_job
async def _job_monthly_enrichment_refresh():
    """1st of month 4AM UTC — full enrichment refresh when API credits reset.

    Runs a large backfill (2000 items) using all available providers:
    Apollo, Gradient, Hunter, Anthropic AI. Entities are prioritized
    by most recent activity so the most-used accounts get fresh data first.
    Force-refreshes entities whose enrichment data is older than 30 days.
    """
    from ..database import SessionLocal
    from ..models import EnrichmentJob
    from ..services.deep_enrichment_service import run_backfill_job

    db = SessionLocal()
    try:
        # Skip if there's already a running job
        running = db.query(EnrichmentJob).filter(EnrichmentJob.status == "running").first()
        if running:
            logger.info(f"Monthly enrichment skipped — job #{running.id} already running")
            return

        # Flush enrichment cache so providers are re-queried with fresh credits
        from ..cache.intel_cache import flush_enrichment_cache

        cleared = flush_enrichment_cache()
        logger.info(f"Monthly enrichment: cleared {cleared} expired cache entries")

        job_id = await run_backfill_job(
            db,
            started_by_id=1,  # System/admin
            scope={
                "entity_types": ["vendor", "company"],
                "max_items": 2000,
                "include_deep_email": True,
                "lookback_days": 365,
            },
        )
        logger.info(f"Monthly enrichment refresh started: job #{job_id}")
    except Exception as e:
        logger.error(f"Monthly enrichment refresh error: {e}")
    finally:
        db.close()


@_traced_job
async def _job_customer_enrichment_sweep():
    """Quarterly: enrich customer accounts missing contacts. Assigned first."""
    from ..database import SessionLocal
    from ..services.customer_enrichment_batch import run_customer_enrichment_batch

    db = SessionLocal()
    try:
        result = await run_customer_enrichment_batch(db, user_id=None, max_accounts=100)
        db.commit()
        logger.info(
            "Customer enrichment sweep: %d processed, %d enriched",
            result.get("processed", 0),
            result.get("enriched", 0),
        )
    except Exception as e:
        logger.error(f"Customer enrichment sweep error: {e}")
        db.rollback()
    finally:
        db.close()
