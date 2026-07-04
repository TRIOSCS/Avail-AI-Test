"""Customer email re-verification job.

The batch customer-enrichment sweep (run_customer_enrichment_batch) and its
credit-gate helper (can_use_credits) were removed as dead code — nothing in the
scheduler or routers called them after the Apollo/Hunter/Lusha providers were
retired. Only the email re-verification stub remains.

Called by: app/jobs/email_jobs.py (run_email_reverification).
Depends on: loguru, sqlalchemy.
"""

from loguru import logger
from sqlalchemy.orm import Session


async def run_email_reverification(db: Session, _max_contacts: int = 200) -> dict:
    """Re-verify emails for contacts that were verified more than 90 days ago.

    Returns a stub result until an email verification provider is configured.
    """
    logger.info("Email re-verification skipped — Hunter connector removed, no provider configured")
    return {"status": "no_provider", "processed": 0, "invalidated": 0}
