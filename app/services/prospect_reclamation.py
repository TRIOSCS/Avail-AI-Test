"""SP4 account reclamation service — park, sweep, notify, reclaim.

Called by: app/jobs/prospecting_jobs.py (scheduler jobs),
           app/routers/htmx_views.py (HTMX reclaim action)
Depends on: app/services/activity_service.py, app/services/prospect_claim.py,
            app/utils/graph_client.py, app/models/prospect_account.py,
            app/models/crm.py, app/utils/token_manager.py
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.auth import User
from ..models.crm import Company
from ..models.intelligence import ActivityLog  # noqa: F401 — imported for type clarity
from ..models.prospect_account import ProspectAccount

# ── Internal DB-injectable sweep (testable) ───────────────────────────────────


async def job_account_sweep() -> None:
    """Daily 1AM — sweep dormant owned accounts into prospecting pool.

    Opens its own database session. Delegates work to job_account_sweep_with_db().
    Called by: app/jobs/prospecting_jobs.py
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        await job_account_sweep_with_db(db)
    finally:
        db.close()


async def job_account_sweep_with_db(db: Session) -> None:
    """Core sweep logic — injectable session for testability.

    For each Company with account_owner_id IS NOT NULL:
    - Check if last activity was > inactivity_days ago (or never)
    - Skip if ProspectAccount with swept_at already exists (idempotent)
    - Call send_company_to_prospecting (clears owner)
    - Set swept_from_owner_id, swept_at, discovery_source on ProspectAccount
    - Send notification email to rep

    Called by: job_account_sweep(), tests
    """
    from .activity_service import get_last_activity_at
    from .prospect_claim import send_company_to_prospecting

    inactivity_days = settings.account_sweep_inactivity_days
    now = datetime.now(timezone.utc)

    owned_companies = db.query(Company).filter(Company.account_owner_id.is_not(None)).all()

    swept_count = 0
    skipped_count = 0

    for co in owned_companies:
        # Idempotency: skip if already swept
        existing_swept = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.company_id == co.id,
                ProspectAccount.swept_at.is_not(None),
            )
            .first()
        )
        if existing_swept:
            skipped_count += 1
            continue

        # Check dormancy
        last_activity = get_last_activity_at(co.id, db)
        if last_activity is not None:
            la = last_activity.replace(tzinfo=timezone.utc) if last_activity.tzinfo is None else last_activity
            days_dormant = (now - la).days
            if days_dormant < inactivity_days:
                skipped_count += 1
                continue
        # No activity ever also counts as dormant — sweep

        owner_id = co.account_owner_id
        owner = db.get(User, owner_id)
        if owner is None:
            logger.warning(
                "SP4 sweep: company {} has owner_id={} but user not found; skipping",
                co.id,
                owner_id,
            )
            skipped_count += 1
            continue

        try:
            result = send_company_to_prospecting(co.id, owner_id, db, is_admin=True)
            prospect_id = result.get("prospect_id")

            if prospect_id:
                pa = db.get(ProspectAccount, prospect_id)
                if pa:
                    pa.swept_from_owner_id = owner_id
                    pa.swept_at = now
                    pa.discovery_source = "auto_sweep"
                    db.commit()

                await _send_sweep_notification(
                    owner=owner,
                    company=co,
                    last_activity_at=last_activity,
                    prospect_id=prospect_id,
                    db=db,
                )

            swept_count += 1
            logger.info("SP4 sweep: swept company {} ({}) from owner {}", co.name, co.id, owner_id)
        except Exception:
            logger.exception("SP4 sweep: failed to sweep company {} ({})", co.name, co.id)
            db.rollback()

    logger.info("SP4 sweep complete: swept={}, skipped={}", swept_count, skipped_count)


async def _send_sweep_notification(
    owner: User,
    company: Company,
    last_activity_at: datetime | None,
    prospect_id: int,
    db: Session,
) -> None:
    """Send Graph /me/sendMail loss-notification to owner.

    Uses get_valid_token(owner, db). On missing token: log warning, return.
    CC: settings.account_sweep_manager_email if set.

    Called by: job_account_sweep_with_db
    """
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

    token = await get_valid_token(owner, db)
    if not token:
        logger.warning(
            "SP4 sweep notification: no valid token for {} ({}); skipping email",
            owner.email,
            owner.id,
        )
        return

    last_active_str = last_activity_at.strftime("%Y-%m-%d") if last_activity_at else "never"

    cc_recipients = []
    if settings.account_sweep_manager_email:
        cc_recipients = [{"emailAddress": {"address": settings.account_sweep_manager_email}}]

    payload = {
        "message": {
            "subject": f"[AVAIL] Account swept to prospecting pool: {company.name}",
            "body": {
                "contentType": "HTML",
                "content": (
                    f"<p>Your account <strong>{company.name}</strong> has been automatically moved to the "
                    f"prospecting pool due to inactivity.</p>"
                    f"<p><strong>Last activity:</strong> {last_active_str}</p>"
                    f"<p><strong>Inactivity threshold:</strong> {settings.account_sweep_inactivity_days} days</p>"
                    f"<p>You can reclaim this account from the Prospecting tab if needed.</p>"
                ),
            },
            "toRecipients": [{"emailAddress": {"address": owner.email}}],
            "ccRecipients": cc_recipients,
        },
        "saveToSentItems": "false",
    }

    gc = GraphClient(token)
    try:
        await gc.post_json("/me/sendMail", payload)
        logger.info("SP4 sweep notification sent to {} for company {}", owner.email, company.name)
    except Exception:
        logger.exception("SP4 sweep notification failed for {} company {}", owner.email, company.name)


# ── Auto-surface (Task 6 stub) ────────────────────────────────────────────────


async def job_auto_surface_reactivation() -> None:
    """Daily 2AM — surface unassigned past-customer Companies as reactivation prospects.

    Stub — implemented in Task 6.
    """
    raise NotImplementedError("job_auto_surface_reactivation not yet implemented")
