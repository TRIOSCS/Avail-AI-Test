"""Customer Enrichment Batch — automated batch enrichment for customer accounts.

Runs as a scheduled job to enrich customer accounts that are stale or missing
contacts. Prioritizes assigned accounts over unassigned.

Called by: scheduler.py (quarterly sweep), enrichment router (manual trigger).
Depends on: customer_enrichment_service.py, credit_manager.py.
"""

import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.enrichment import EnrichmentJob
from .credit_manager import can_use_credits
from .customer_enrichment_service import enrich_customer_account, get_enrichment_gaps


async def run_customer_enrichment_batch(
    db: Session,
    user_id: int | None = None,
    max_accounts: int = 50,
    assigned_only: bool = False,
) -> dict:
    """Run batch enrichment for customer accounts needing contacts.

    Processes accounts in priority order: assigned first, then unassigned.
    Stops early if credit budgets are exhausted.
    """
    if not settings.customer_enrichment_enabled:
        return {"status": "disabled", "processed": 0}

    gaps = get_enrichment_gaps(db, limit=max_accounts)
    if assigned_only:
        gaps = [g for g in gaps if g.get("account_owner_id")]

    if not gaps:
        return {"status": "no_gaps", "processed": 0}

    job = EnrichmentJob(
        job_type="customer_enrichment_batch",
        status="running",
        total_items=len(gaps),
        started_by_id=user_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()

    processed = 0
    enriched = 0
    errors = []

    for gap in gaps:
        # Stop early if all credit budgets are exhausted
        if not any(can_use_credits(db, p) for p in ["apollo", "hunter_verify", "lusha"]):
            logger.info("All credit budgets exhausted — stopping batch early at %d/%d", processed, len(gaps))
            break

        try:
            result = await enrich_customer_account(gap["company_id"], db, force=False)
            processed += 1
            if result.get("ok") and result.get("contacts_added", 0) > 0:
                enriched += 1
            db.flush()
        except Exception as e:
            processed += 1
            errors.append(f"Company {gap['company_id']}: {str(e)[:100]}")
            logger.warning("Batch enrichment error for company %d: %s", gap["company_id"], e)

        # Small delay between accounts to avoid rate limiting
        await asyncio.sleep(0.5)

    job.processed_items = processed
    job.enriched_items = enriched
    job.error_count = len(errors)
    job.error_log = errors[:20]
    job.status = "completed"
    job.completed_at = datetime.now(timezone.utc)
    db.flush()

    logger.info(
        "Customer enrichment batch complete: %d processed, %d enriched, %d errors",
        processed,
        enriched,
        len(errors),
    )
    return {
        "status": "completed",
        "job_id": job.id,
        "processed": processed,
        "enriched": enriched,
        "errors": len(errors),
    }


async def run_email_reverification(db: Session, max_contacts: int = 200) -> dict:
    """Re-verify emails for contacts that were verified more than 90 days ago.

    Runs as a quarterly maintenance job.
    """
    from ..connectors.hunter_client import verify_email
    from ..models.crm import SiteContact
    from .credit_manager import record_credit_usage

    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)  # Only re-verify contacts verified before this
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=90)

    contacts = (
        db.query(SiteContact)
        .filter(
            SiteContact.email_verified == True,  # noqa: E712
            SiteContact.email_verified_at.isnot(None),
            SiteContact.email_verified_at < cutoff,
            SiteContact.is_active == True,  # noqa: E712
        )
        .limit(max_contacts)
        .all()
    )

    if not contacts:
        return {"status": "no_contacts_to_reverify", "processed": 0}

    processed = 0
    invalidated = 0
    for contact in contacts:
        if not can_use_credits(db, "hunter_verify", 1):
            break

        result = await verify_email(contact.email)
        record_credit_usage(db, "hunter_verify", 1)
        processed += 1

        if result:
            status = result.get("status", "unknown")
            contact.email_verification_status = status
            contact.email_verified_at = datetime.now(timezone.utc)
            if status == "invalid":
                contact.email_verified = False
                contact.needs_refresh = True
                invalidated += 1

        await asyncio.sleep(0.2)

    db.flush()
    logger.info("Email re-verification: %d processed, %d invalidated", processed, invalidated)
    return {"status": "completed", "processed": processed, "invalidated": invalidated}
