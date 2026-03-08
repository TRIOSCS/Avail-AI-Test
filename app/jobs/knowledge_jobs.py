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
    scheduler.add_job(
        _job_precompute_briefings,
        CronTrigger(hour=6, minute=0),
        id="knowledge_precompute_briefings",
        name="Pre-compute morning briefings at 6 AM UTC",
    )


async def _job_refresh_insights():
    """Re-generate insights for recently active reqs, vendors, companies, MPNs, and pipeline."""
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

        # Also deliver morning briefing to Teams via webhook
        from app.models.auth import User
        from app.services.dashboard_briefing import generate_briefing

        briefing_count = 0
        for config in configs:
            if current_hour != (config.knowledge_digest_hour or 14) % 24:
                continue
            if not config.teams_webhook_url:
                continue
            try:
                user = db.get(User, config.user_id)
                if not user or not user.is_active:
                    continue
                role = getattr(user, "role", "buyer") or "buyer"
                briefing = generate_briefing(db=db, user_id=config.user_id, role=role)
                if briefing["total_items"] > 0:
                    await _send_briefing_to_teams(
                        config.teams_webhook_url, briefing, user.display_name or user.email
                    )
                    briefing_count += 1
            except Exception as e:
                logger.warning("Briefing Teams delivery failed for user {}: {}", config.user_id, e)

        if briefing_count:
            logger.info("Sent {} morning briefings to Teams", briefing_count)
    except Exception as e:
        logger.error("send_knowledge_digests job failed: {}", e)
    finally:
        db.close()


async def _job_precompute_briefings():
    """Pre-compute briefings for all users at 6 AM UTC, warming the cache."""
    from app.database import SessionLocal
    from app.models.auth import User
    from app.services.dashboard_briefing import generate_briefing

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.is_active.is_(True)).all()
        ok = 0
        for user in users:
            try:
                role = getattr(user, "role", "buyer") or "buyer"
                generate_briefing(db=db, user_id=user.id, role=role)
                ok += 1
            except Exception as e:
                logger.warning("Briefing pre-compute failed for user {}: {}", user.id, e)
        logger.info("Pre-computed briefings for {}/{} users", ok, len(users))
    except Exception as e:
        logger.error("precompute_briefings job failed: {}", e)
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


async def _send_briefing_to_teams(webhook_url: str, briefing: dict, user_name: str):
    """Send morning briefing as a Teams message via incoming webhook.

    Called by: _job_send_knowledge_digests()
    Depends on: httpx
    """
    import httpx

    sections_text = []
    for s in briefing["sections"]:
        if s["count"] > 0:
            items_preview = []
            for item in s["items"][:3]:
                items_preview.append("  - {}".format(item.get("title", "")))
            sections_text.append("**{}** ({})".format(s["label"], s["count"]))
            sections_text.extend(items_preview)
            if s["count"] > 3:
                sections_text.append("  - +{} more".format(s["count"] - 3))

    body = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {
                        "type": "TextBlock",
                        "text": "Morning Briefing for {}".format(user_name),
                        "weight": "bolder",
                        "size": "medium",
                    },
                    {
                        "type": "TextBlock",
                        "text": "{} items need attention".format(briefing["total_items"]),
                        "isSubtle": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": "\n".join(sections_text),
                        "wrap": True,
                    },
                ],
            },
        }],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(webhook_url, json=body)
        resp.raise_for_status()
