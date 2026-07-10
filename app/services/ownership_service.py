"""Customer ownership service — inactivity warnings + open-pool auto-claim.

The company ownership inactivity threshold is a SINGLE configurable setting,
``settings.account_sweep_inactivity_days`` (default 45). The SP4 account sweep
(``prospect_reclamation.job_account_sweep``) is the single park+cooldown+notify
path that clears an owner and drops the company into the prospect pool at that
threshold. This nightly sweep is WARNINGS-ONLY: it emails owners of accounts
approaching that threshold (``WARNING_LEAD_DAYS`` before it) and never clears
ownership itself — retiring the old clear removes the H5 race where the 12h /
30-day plain sweep nulled ``account_owner_id`` before SP4 could park + cool down
+ notify.

First new engagement (email or call) auto-claims an open pool account.

Usage:
    # Nightly cron job (warnings only)
    await run_ownership_sweep(db)

    # Called automatically from activity_service when activity is logged
    check_and_claim_open_account(company_id, user_id, db)
"""

import html
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ActivityLog, Company, CustomerSite, User

from ..constants import UserRole

# The at-risk warning fires this many days BEFORE the company inactivity threshold
# (settings.account_sweep_inactivity_days) — i.e. before the SP4 sweep would park it.
WARNING_LEAD_DAYS = 7

# ═══════════════════════════════════════════════════════════════════════
#  NIGHTLY SWEEP — WARNINGS ONLY (SP4 job_account_sweep does the clearing)
# ═══════════════════════════════════════════════════════════════════════


async def run_ownership_sweep(db: Session) -> dict:
    """Run the nightly ownership sweep — WARNINGS ONLY.

    Emails the owner of every account inside the warning zone
    (``days_inactive >= account_sweep_inactivity_days - WARNING_LEAD_DAYS``), once
    per day. It does NOT clear ownership: the SP4 account sweep
    (``prospect_reclamation.job_account_sweep``) is the single park+cooldown+notify
    path, and both it and this warning read the ONE threshold
    ``settings.account_sweep_inactivity_days`` (default 45). Enabling both the
    ownership-sweep flag and the account-sweep flag therefore no longer double-acts.

    Returns summary dict with counts.
    """
    now = datetime.now(UTC)
    warned = 0

    inactivity_limit = settings.account_sweep_inactivity_days
    warning_day = max(1, inactivity_limit - WARNING_LEAD_DAYS)

    # Get all owned companies
    owned = (
        db.query(Company)
        .filter(
            Company.account_owner_id.isnot(None),
            Company.is_active.is_(True),
        )
        .all()
    )

    for company in owned:
        days_inactive = _days_since_activity(company, now)
        if days_inactive is None:
            # No activity ever recorded — use created_at as baseline
            days_inactive = _days_since_created(company.created_at, now)

        # In (or past) the warning zone → send alert once per day. Parking the
        # account is SP4's job, so this sweep never clears ownership.
        if days_inactive >= warning_day:
            already_warned_today = _was_warned_today(company.id, company.account_owner_id, db)
            if not already_warned_today:
                await _send_warning_alert(company, days_inactive, inactivity_limit, db)
                warned += 1

    if warned:
        db.commit()

    result = {
        "total_owned": len(owned),
        "warned": warned,
        "timestamp": now.isoformat(),
    }
    logger.info(f"Ownership sweep complete (warnings-only): {result}")
    return result


# ═══════════════════════════════════════════════════════════════════════
#  OPEN POOL CLAIM — first to engage gets ownership
# ═══════════════════════════════════════════════════════════════════════


def check_and_claim_open_account(company_id: int, user_id: int, db: Session) -> bool:
    """Check if a company is in the open pool. If so, assign ownership to user.

    Called automatically after an activity is logged against a company. Returns True if
    ownership was claimed.
    """
    company = db.get(Company, company_id)
    if not company:
        return False

    # Check the user's role — only sales can own accounts
    user = db.get(User, user_id)
    if not user or user.role not in (UserRole.SALES, UserRole.TRADER):
        return False

    # Lock the row to prevent concurrent claims
    company = (
        db.query(Company).filter(Company.id == company_id, Company.account_owner_id.is_(None)).with_for_update().first()
    )
    if not company:
        return False

    company.account_owner_id = user_id
    company.ownership_cleared_at = None  # Clear the "was cleared" timestamp
    db.flush()

    logger.info(f"Account claimed: '{company.name}' (ID {company.id}) by user {user.name} (ID {user_id})")
    return True


# ═══════════════════════════════════════════════════════════════════════
#  QUERY HELPERS — dashboard data
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
#  SITE-LEVEL OWNERSHIP — Prospecting Pool
# ═══════════════════════════════════════════════════════════════════════


def run_site_ownership_sweep(db: Session) -> dict:
    """Nightly sweep: clear stale site ownership, log warnings.

    Sites with owner_id set and no activity for 30 days lose ownership.
    Warning zone starts at day 23 (7 days before expiration).
    """
    now = datetime.now(UTC)
    warned = 0
    cleared = 0
    inactivity_limit = settings.customer_inactivity_days  # 30 days
    warning_day = inactivity_limit - 7

    owned = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.owner_id.isnot(None),
            CustomerSite.is_active.is_(True),
        )
        .all()
    )

    for site in owned:
        days_inactive = _site_days_since_activity(site, now)
        if days_inactive is None:
            days_inactive = _days_since_created(site.created_at, now)

        if days_inactive >= inactivity_limit:
            site.owner_id = None
            site.ownership_cleared_at = now
            db.flush()
            cleared += 1
            logger.info(f"Site ownership cleared: '{site.site_name}' (ID {site.id}) — {days_inactive} days inactive")
            continue

        if days_inactive >= warning_day:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            already_warned = (
                db.query(ActivityLog)
                .filter(
                    ActivityLog.customer_site_id == site.id,
                    ActivityLog.activity_type == "ownership_warning",
                    ActivityLog.created_at >= today_start,
                )
                .first()
            )
            if not already_warned and site.owner_id:
                warning = ActivityLog(
                    user_id=site.owner_id,
                    activity_type="ownership_warning",
                    channel="system",
                    company_id=site.company_id,
                    customer_site_id=site.id,
                    contact_name=site.site_name,
                    subject=f"Site ownership warning: {inactivity_limit - days_inactive} days remaining on {site.site_name}",
                )
                db.add(warning)
                db.flush()
                warned += 1

    if cleared or warned:
        db.commit()

    result = {
        "total_owned": len(owned),
        "warned": warned,
        "cleared": cleared,
    }
    logger.info(f"Site ownership sweep complete: {result}")
    return result


def _site_days_since_activity(site: CustomerSite, now: datetime) -> int | None:
    """Calculate days since last activity for a site."""
    return _days_since(site.last_activity_at, now)


# ═══════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _days_since(last_activity_at: datetime | None, now: datetime) -> int | None:
    """Whole days between a (possibly naive) timestamp and now, or None if unset."""
    if not last_activity_at:
        return None
    last = last_activity_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last).days


def _days_since_created(created_at: datetime | None, now: datetime) -> int:
    """Days since creation, used as the inactivity baseline when no activity exists.

    Returns 999 (force-clear) when no creation timestamp is available.
    """
    days = _days_since(created_at, now)
    return days if days is not None else 999


def _days_since_activity(company: Company, now: datetime) -> int | None:
    """Calculate days since last activity for a company.

    Uses company.last_activity_at (precomputed field updated on each activity log).
    """
    return _days_since(company.last_activity_at, now)


def _was_warned_today(company_id: int, owner_id: int, db: Session) -> bool:
    """Check if we already sent a warning alert for this account today.

    Uses a simple activity_log check — warnings are logged as system activities.
    """
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    existing = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.company_id == company_id,
            ActivityLog.activity_type == "ownership_warning",
            ActivityLog.created_at >= today_start,
        )
        .first()
    )
    return existing is not None


async def _send_warning_alert(company: Company, days_inactive: int, inactivity_limit: int, db: Session):
    """Send day-23 (or equivalent) warning to the account owner.

    1. Send email via Graph API
    2. Log a dashboard notification (as an activity_log entry with type 'ownership_warning')
    """
    owner = db.get(User, company.account_owner_id)
    if not owner:
        return

    days_remaining = max(0, inactivity_limit - days_inactive)

    # Log the warning as a system activity (also serves as dedup + dashboard notification)
    warning_record = ActivityLog(
        user_id=owner.id,
        activity_type="ownership_warning",
        channel="system",
        company_id=company.id,
        contact_name=company.name,
        subject=f"Ownership warning: {days_remaining} days remaining on {company.name}",
    )
    db.add(warning_record)
    db.flush()

    # Send email alert
    try:
        from app.scheduler import get_valid_token
        from app.utils.graph_client import GraphClient

        token = await get_valid_token(owner, db)
        if not token:
            logger.warning(f"No token for {owner.email}, skipping warning email for {company.name}")
            return

        gc = GraphClient(token)
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px;">
            <h2 style="color: #d97706;">⚠️ Account Ownership Warning</h2>
            <p>No activity has been logged on <strong>{html.escape(str(company.name))}</strong> in <strong>{days_inactive} days</strong>.</p>
            <p>You'll lose ownership in <strong>{days_remaining} day{"s" if days_remaining != 1 else ""}</strong> unless you make contact.</p>
            <p style="margin-top: 20px;">
                <a href="{settings.app_url}/companies/{company.id}"
                   style="background: #2563eb; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">
                    View Account & Re-engage
                </a>
            </p>
            <p style="color: #6b7280; font-size: 12px; margin-top: 20px;">
                This is an automated alert from AVAIL. Activity (email or call) auto-logs and resets the clock.
            </p>
        </div>
        """

        payload = {
            "message": {
                "subject": f"[AVAIL] ⚠️ {days_remaining} days left on {company.name}",
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": owner.email}}],
            },
            "saveToSentItems": "false",  # Don't clutter sent items with system alerts
        }
        await gc.post_json("/me/sendMail", payload)
        logger.info(f"Warning email sent to {owner.email} for {company.name} ({days_remaining} days remaining)")

    except Exception as e:
        logger.error(f"Failed to send warning email to {owner.email} for {company.name}: {e}")
