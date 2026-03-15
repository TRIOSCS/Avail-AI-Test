"""Selective auto-task scheduler — RFQ follow-ups and quote expiry alerts.

Called by: app/jobs/__init__.py via register_task_jobs()
Depends on: app.models (Contact, Quote, Requisition), app.services.task_service

Design: Highly selective to avoid noise.
  - RFQ follow-ups: only for contacts 3-14 days old on active requisitions,
    where the vendor hasn't responded and no follow-up task already exists.
    Capped at 25 tasks per run.
  - Quote expiry: only for sent quotes expiring within 2 days that haven't
    been resolved (won/lost) and haven't already been alerted.
    Capped at 15 tasks per run.
"""

from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from ..scheduler import _traced_job


def register_task_jobs(scheduler, settings):
    """Register task auto-generation jobs with the scheduler."""
    scheduler.add_job(
        _job_rfq_followup_tasks,
        CronTrigger(hour=9, minute=0),  # Once daily at 9 AM
        id="rfq_followup_tasks",
        name="Create follow-up tasks for stale RFQs",
    )
    scheduler.add_job(
        _job_quote_expiry_tasks,
        CronTrigger(hour=9, minute=15),  # Once daily at 9:15 AM
        id="quote_expiry_tasks",
        name="Create expiry tasks for quotes expiring soon",
    )


# Maximum tasks created per run to prevent noise
_RFQ_FOLLOWUP_CAP = 25
_QUOTE_EXPIRY_CAP = 15

# Only create follow-ups for RFQs between 3 and 14 days old
_RFQ_MIN_AGE_DAYS = 3
_RFQ_MAX_AGE_DAYS = 14

# Active requisition statuses (skip archived/won/lost)
_ACTIVE_REQ_STATUSES = {"open", "sourcing", "quoted", "rfq_sent"}


@_traced_job
async def _job_rfq_followup_tasks():
    """Create 'Follow up RFQ' tasks for stale contacts on active requisitions.

    Selective filters:
    - Contact status is 'sent' or 'opened' (no vendor response)
    - Contact is 3-14 days old (not brand new, not ancient)
    - Requisition is active (not archived/won/lost)
    - No existing open follow-up task for this contact
    """
    from ..database import SessionLocal
    from ..models.offers import Contact
    from ..models.sourcing import Requisition
    from ..services.task_service import on_rfq_no_response

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        min_created = now - timedelta(days=_RFQ_MAX_AGE_DAYS)
        max_created = now - timedelta(days=_RFQ_MIN_AGE_DAYS)

        stale_contacts = (
            db.query(Contact)
            .join(Requisition, Requisition.id == Contact.requisition_id)
            .filter(
                Contact.status.in_(["sent", "opened"]),
                Contact.created_at >= min_created,
                Contact.created_at <= max_created,
                Requisition.status.in_(_ACTIVE_REQ_STATUSES),
            )
            .order_by(Contact.created_at.asc())
            .limit(_RFQ_FOLLOWUP_CAP * 2)  # Fetch extra to account for dedup skips
            .all()
        )

        created = 0
        for contact in stale_contacts:
            if created >= _RFQ_FOLLOWUP_CAP:
                break
            task = on_rfq_no_response(
                db,
                contact.requisition_id,
                contact.vendor_name or "Unknown vendor",
                contact.id,
            )
            if task:
                created += 1

        if created:
            logger.info("RFQ follow-up tasks created: {}", created)
        else:
            logger.debug("RFQ follow-up job: no new tasks needed")
    except Exception as e:
        logger.error("RFQ follow-up task job failed: {}", str(e))
        db.rollback()
    finally:
        db.close()


@_traced_job
async def _job_quote_expiry_tasks():
    """Create 'Quote expires soon' tasks for quotes expiring within 2 days.

    Selective filters:
    - Quote status is 'sent' (not draft, not already won/lost)
    - Quote has a sent_at date
    - Expiry date is within 2 days from now
    - Quote result is not already set (won/lost)
    - No followup_alert_sent_at already set (prevents re-alerting)
    - Requisition is active
    """
    from ..database import SessionLocal
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition
    from ..services.task_service import on_quote_expiring

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        sent_quotes = (
            db.query(Quote)
            .join(Requisition, Requisition.id == Quote.requisition_id)
            .filter(
                Quote.status == "sent",
                Quote.sent_at.isnot(None),
                Quote.result.is_(None),
                Quote.followup_alert_sent_at.is_(None),
                Requisition.status.in_(_ACTIVE_REQ_STATUSES),
            )
            .limit(_QUOTE_EXPIRY_CAP * 2)
            .all()
        )

        created = 0
        for quote in sent_quotes:
            if created >= _QUOTE_EXPIRY_CAP:
                break

            validity = quote.validity_days or 7
            sent_at = quote.sent_at
            if sent_at and not sent_at.tzinfo:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            expires_at = sent_at + timedelta(days=validity)
            days_left = (expires_at - now).total_seconds() / 86400

            # Only alert if expiring within 2 days (but not already expired > 3 days ago)
            if days_left > 2 or days_left < -3:
                continue

            task = on_quote_expiring(db, quote.requisition_id, quote.id)
            if task:
                quote.followup_alert_sent_at = now
                db.commit()
                created += 1

        if created:
            logger.info("Quote expiry tasks created: {}", created)
        else:
            logger.debug("Quote expiry job: no new tasks needed")
    except Exception as e:
        logger.error("Quote expiry task job failed: {}", str(e))
        db.rollback()
    finally:
        db.close()
