"""Customer ownership service — 30-day inactivity rule with open pool.

Nightly sweep checks all owned accounts. If no auto-logged activity
in the trailing window (30 days standard, 90 days strategic), ownership
clears and the account drops to the open pool. Day-23 warning alerts
fire 7 days before expiration.

First new engagement (email or call) auto-claims an open pool account.

Usage:
    # Nightly cron job
    await run_ownership_sweep(db)

    # Called automatically from activity_service when activity is logged
    check_and_claim_open_account(company_id, user_id, db)
"""

import asyncio
import html
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ActivityLog, Company, CustomerSite, User

# ═══════════════════════════════════════════════════════════════════════
#  NIGHTLY SWEEP — clear stale ownership, send warnings
# ═══════════════════════════════════════════════════════════════════════


async def run_ownership_sweep(db: Session) -> dict:
    """Run the nightly ownership sweep.

    1. Find accounts in the warning zone (day 23+) → send alerts
    2. Find accounts past their inactivity limit → clear ownership

    Returns summary dict with counts.
    """
    now = datetime.now(timezone.utc)
    warned = 0
    cleared = 0

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
        inactivity_limit = (
            settings.strategic_inactivity_days if company.is_strategic else settings.customer_inactivity_days
        )
        warning_day = inactivity_limit - 7  # 7 days before expiration

        days_inactive = _days_since_activity(company, now)
        if days_inactive is None:
            # No activity ever recorded — use created_at as baseline
            if company.created_at:
                created = (
                    company.created_at.replace(tzinfo=timezone.utc)
                    if company.created_at.tzinfo is None
                    else company.created_at
                )
                days_inactive = (now - created).days
            else:
                days_inactive = 999  # Force clear

        # Past limit → clear ownership
        if days_inactive >= inactivity_limit:
            _clear_ownership(company, db)
            cleared += 1
            logger.info(
                f"Ownership cleared: '{company.name}' (ID {company.id}) — "
                f"{days_inactive} days inactive (limit: {inactivity_limit})"
            )
            continue

        # In warning zone → send alert (only once per day)
        if days_inactive >= warning_day:
            already_warned_today = _was_warned_today(company.id, company.account_owner_id, db)
            if not already_warned_today:
                await _send_warning_alert(company, days_inactive, inactivity_limit, db)
                warned += 1

    if cleared or warned:
        db.commit()

    result = {
        "total_owned": len(owned),
        "warned": warned,
        "cleared": cleared,
        "timestamp": now.isoformat(),
    }
    logger.info(f"Ownership sweep complete: {result}")
    return result


# ═══════════════════════════════════════════════════════════════════════
#  OPEN POOL CLAIM — first to engage gets ownership
# ═══════════════════════════════════════════════════════════════════════


def check_and_claim_open_account(company_id: int, user_id: int, db: Session) -> bool:
    """Check if a company is in the open pool. If so, assign ownership to user.

    Called automatically after an activity is logged against a company.
    Returns True if ownership was claimed.
    """
    company = db.get(Company, company_id)
    if not company:
        return False

    # Check the user's role — only sales can own accounts
    user = db.get(User, user_id)
    if not user or user.role not in ("sales", "trader"):
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


def get_accounts_at_risk(db: Session) -> list[dict]:
    """Get all owned accounts approaching the warning zone.

    Returns accounts where days_inactive >= warning_day.
    Sorted by days remaining (most urgent first).
    """
    now = datetime.now(timezone.utc)
    owned = (
        db.query(Company, User)
        .outerjoin(User, Company.account_owner_id == User.id)
        .filter(
            Company.account_owner_id.isnot(None),
            Company.is_active.is_(True),
        )
        .all()
    )

    at_risk = []
    for company, owner in owned:
        inactivity_limit = (
            settings.strategic_inactivity_days if company.is_strategic else settings.customer_inactivity_days
        )
        warning_day = inactivity_limit - 7

        days_inactive = _days_since_activity(company, now)
        if days_inactive is None:
            days_inactive = 999

        if days_inactive >= warning_day:
            days_remaining = max(0, inactivity_limit - days_inactive)
            at_risk.append(
                {
                    "company_id": company.id,
                    "company_name": company.name,
                    "owner_id": company.account_owner_id,
                    "owner_name": owner.name if owner else None,
                    "owner_email": owner.email if owner else None,
                    "days_inactive": days_inactive,
                    "days_remaining": days_remaining,
                    "inactivity_limit": inactivity_limit,
                    "is_strategic": company.is_strategic or False,
                }
            )

    # Sort: most urgent first
    at_risk.sort(key=lambda x: x["days_remaining"])
    return at_risk


def get_open_pool_accounts(db: Session) -> list[dict]:
    """Get all unowned active companies (open pool)."""
    companies = (
        db.query(Company)
        .filter(
            Company.account_owner_id.is_(None),
            Company.is_active.is_(True),
        )
        .order_by(Company.name)
        .all()
    )

    return [
        {
            "company_id": c.id,
            "company_name": c.name,
            "ownership_cleared_at": c.ownership_cleared_at.isoformat() if c.ownership_cleared_at else None,
            "last_activity_at": c.last_activity_at.isoformat() if c.last_activity_at else None,
            "is_strategic": c.is_strategic or False,
        }
        for c in companies
    ]


def get_my_accounts(user_id: int, db: Session) -> list[dict]:
    """Get all accounts owned by a specific user with activity health."""
    now = datetime.now(timezone.utc)
    companies = (
        db.query(Company)
        .filter(
            Company.account_owner_id == user_id,
            Company.is_active.is_(True),
        )
        .order_by(Company.name)
        .all()
    )

    results = []
    for c in companies:
        inactivity_limit = settings.strategic_inactivity_days if c.is_strategic else settings.customer_inactivity_days
        warning_day = inactivity_limit - 7
        days_inactive = _days_since_activity(c, now)

        if days_inactive is None:
            status = "no_activity"
        elif days_inactive <= warning_day:
            status = "green"
        elif days_inactive <= inactivity_limit:
            status = "yellow"
        else:
            status = "red"

        results.append(
            {
                "company_id": c.id,
                "company_name": c.name,
                "days_inactive": days_inactive,
                "inactivity_limit": inactivity_limit,
                "status": status,
                "is_strategic": c.is_strategic or False,
                "last_activity_at": c.last_activity_at.isoformat() if c.last_activity_at else None,
            }
        )

    return results


def get_manager_digest(db: Session) -> dict:
    """Weekly roll-up for manager dashboard.

    Shows: accounts at risk, recently cleared, team activity summary.
    """
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    at_risk = get_accounts_at_risk(db)

    # Recently cleared (in the last 7 days)
    recently_cleared = (
        db.query(Company)
        .filter(
            Company.ownership_cleared_at.isnot(None),
            Company.ownership_cleared_at >= week_ago,
        )
        .all()
    )

    # Activity counts per user in the last 7 days
    user_activity = (
        db.query(User.id, User.name, func.count(ActivityLog.id).label("activity_count"))
        .outerjoin(
            ActivityLog,
            and_(
                ActivityLog.user_id == User.id,
                ActivityLog.created_at >= week_ago,
            ),
        )
        .filter(User.role.in_(["sales", "trader"]))
        .group_by(User.id, User.name)
        .all()
    )

    return {
        "at_risk_count": len(at_risk),
        "at_risk_accounts": at_risk[:10],  # Top 10 most urgent
        "recently_cleared": [
            {
                "company_id": c.id,
                "company_name": c.name,
                "cleared_at": c.ownership_cleared_at.isoformat() if c.ownership_cleared_at else None,
            }
            for c in recently_cleared
        ],
        "team_activity": [
            {"user_id": uid, "user_name": name, "activity_count": count} for uid, name, count in user_activity
        ],
        "generated_at": now.isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════
#  SITE-LEVEL OWNERSHIP — Prospecting Pool
# ═══════════════════════════════════════════════════════════════════════


def run_site_ownership_sweep(db: Session) -> dict:
    """Nightly sweep: clear stale site ownership, log warnings.

    Sites with owner_id set and no activity for 30 days lose ownership.
    Warning zone starts at day 23 (7 days before expiration).
    """
    now = datetime.now(timezone.utc)
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
            if site.created_at:
                created = (
                    site.created_at.replace(tzinfo=timezone.utc) if site.created_at.tzinfo is None else site.created_at
                )
                days_inactive = (now - created).days
            else:
                days_inactive = 999

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


def get_open_pool_sites(db: Session) -> list[dict]:
    """Get all unowned active sites with parent company info."""
    sites = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.owner_id.is_(None),
            CustomerSite.is_active.is_(True),
        )
        .order_by(CustomerSite.site_name)
        .all()
    )

    results = []
    company_cache = {}
    for s in sites:
        if s.company_id not in company_cache:
            company_cache[s.company_id] = db.get(Company, s.company_id)
        co = company_cache[s.company_id]
        results.append(
            {
                "site_id": s.id,
                "site_name": s.site_name,
                "company_id": s.company_id,
                "company_name": co.name if co else None,
                "contact_name": s.contact_name,
                "contact_email": s.contact_email,
                "city": s.city,
                "state": s.state,
                "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
                "ownership_cleared_at": s.ownership_cleared_at.isoformat() if s.ownership_cleared_at else None,
            }
        )
    return results


def claim_site(site_id: int, user_id: int, db: Session) -> bool:
    """Claim an unowned site. Sales/trader roles only. Concurrency-safe."""
    user = db.get(User, user_id)
    if not user or user.role not in ("sales", "trader"):
        return False

    site = (
        db.query(CustomerSite)
        .filter(CustomerSite.id == site_id, CustomerSite.owner_id.is_(None))
        .with_for_update()
        .first()
    )
    if not site:
        return False

    site.owner_id = user_id
    site.ownership_cleared_at = None
    db.flush()

    logger.info(f"Site claimed: '{site.site_name}' (ID {site.id}) by user {user.name} (ID {user_id})")
    return True


def get_my_sites(user_id: int, db: Session) -> list[dict]:
    """Get sites owned by user with health status (green/yellow/red)."""
    now = datetime.now(timezone.utc)
    inactivity_limit = settings.customer_inactivity_days
    warning_day = inactivity_limit - 7

    sites = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.owner_id == user_id,
            CustomerSite.is_active.is_(True),
        )
        .order_by(CustomerSite.site_name)
        .all()
    )

    results = []
    company_cache = {}
    for s in sites:
        if s.company_id not in company_cache:
            company_cache[s.company_id] = db.get(Company, s.company_id)
        co = company_cache[s.company_id]

        days_inactive = _site_days_since_activity(s, now)
        if days_inactive is None:
            status = "no_activity"
        elif days_inactive <= warning_day:
            status = "green"
        elif days_inactive <= inactivity_limit:
            status = "yellow"
        else:
            status = "red"

        results.append(
            {
                "site_id": s.id,
                "site_name": s.site_name,
                "company_id": s.company_id,
                "company_name": co.name if co else None,
                "contact_name": s.contact_name,
                "contact_email": s.contact_email,
                "city": s.city,
                "state": s.state,
                "days_inactive": days_inactive,
                "inactivity_limit": inactivity_limit,
                "status": status,
                "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
            }
        )
    return results


def get_sites_at_risk(db: Session) -> list[dict]:
    """Get owned sites approaching inactivity limit (in warning zone)."""
    now = datetime.now(timezone.utc)
    inactivity_limit = settings.customer_inactivity_days
    warning_day = inactivity_limit - 7

    owned = (
        db.query(CustomerSite, User)
        .outerjoin(User, CustomerSite.owner_id == User.id)
        .filter(
            CustomerSite.owner_id.isnot(None),
            CustomerSite.is_active.is_(True),
        )
        .all()
    )

    at_risk = []
    company_cache = {}
    for site, owner in owned:
        days_inactive = _site_days_since_activity(site, now)
        if days_inactive is None:
            days_inactive = 999

        if days_inactive >= warning_day:
            days_remaining = max(0, inactivity_limit - days_inactive)
            if site.company_id not in company_cache:
                company_cache[site.company_id] = db.get(Company, site.company_id)
            co = company_cache[site.company_id]
            at_risk.append(
                {
                    "site_id": site.id,
                    "site_name": site.site_name,
                    "company_id": site.company_id,
                    "company_name": co.name if co else None,
                    "owner_id": site.owner_id,
                    "owner_name": owner.name if owner else None,
                    "days_inactive": days_inactive,
                    "days_remaining": days_remaining,
                    "inactivity_limit": inactivity_limit,
                }
            )

    at_risk.sort(key=lambda x: x["days_remaining"])
    return at_risk


def _site_days_since_activity(site: CustomerSite, now: datetime) -> int | None:
    """Calculate days since last activity for a site."""
    if not site.last_activity_at:
        return None
    last = site.last_activity_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).days


# ═══════════════════════════════════════════════════════════════════════
#  INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _days_since_activity(company: Company, now: datetime) -> int | None:
    """Calculate days since last activity for a company.

    Uses company.last_activity_at (precomputed field updated on each activity log).
    """
    if not company.last_activity_at:
        return None
    last = company.last_activity_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).days


def _clear_ownership(company: Company, db: Session):
    """Clear ownership on a company, moving it to the open pool."""
    company.account_owner_id = None
    company.ownership_cleared_at = datetime.now(timezone.utc)
    db.flush()


def _was_warned_today(company_id: int, owner_id: int, db: Session) -> bool:
    """Check if we already sent a warning alert for this account today.

    Uses a simple activity_log check — warnings are logged as system activities.
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
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

    days_remaining = inactivity_limit - days_inactive

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

    # Teams channel alert (fire-and-forget)
    try:
        from app.services.teams import send_ownership_warning

        await send_ownership_warning(
            company_id=company.id,
            company_name=company.name,
            owner_name=owner.name or owner.email,
            days_remaining=days_remaining,
        )
    except Exception as e:
        logger.debug(f"Teams ownership warning skipped for {company.name}: {e}")


async def send_manager_digest_email(db: Session):
    """Send the weekly manager digest email to all admins."""
    digest = get_manager_digest(db)

    if not digest["at_risk_accounts"] and not digest["recently_cleared"]:
        logger.info("Manager digest: nothing to report")
        return

    # Build email body
    lines = ["<h2>Weekly Account Health Digest</h2>"]

    if digest["at_risk_accounts"]:
        lines.append(f"<h3 style='color: #d97706;'>⚠️ {digest['at_risk_count']} Account(s) At Risk</h3>")
        lines.append("<table border='1' cellpadding='8' cellspacing='0' style='border-collapse: collapse;'>")
        lines.append("<tr><th>Account</th><th>Owner</th><th>Days Inactive</th><th>Days Left</th></tr>")
        for acct in digest["at_risk_accounts"]:
            color = "#dc2626" if acct["days_remaining"] <= 2 else "#d97706"
            lines.append(
                f"<tr><td>{html.escape(str(acct['company_name']))}</td><td>{html.escape(str(acct['owner_name'] or 'N/A'))}</td>"
                f"<td>{acct['days_inactive']}</td>"
                f"<td style='color: {color}; font-weight: bold;'>{acct['days_remaining']}</td></tr>"
            )
        lines.append("</table>")

    if digest["recently_cleared"]:
        lines.append(
            f"<h3 style='color: #dc2626;'>🔴 {len(digest['recently_cleared'])} Account(s) Cleared This Week</h3>"
        )
        for acct in digest["recently_cleared"]:
            lines.append(
                f"<p>• {html.escape(str(acct['company_name']))} — cleared {html.escape(str(acct['cleared_at']))}</p>"
            )

    if digest["team_activity"]:
        lines.append("<h3>📊 Team Activity (Last 7 Days)</h3>")
        lines.append("<table border='1' cellpadding='8' cellspacing='0' style='border-collapse: collapse;'>")
        lines.append("<tr><th>Salesperson</th><th>Activities</th></tr>")
        for ta in sorted(digest["team_activity"], key=lambda x: x["activity_count"], reverse=True):
            lines.append(f"<tr><td>{html.escape(str(ta['user_name']))}</td><td>{ta['activity_count']}</td></tr>")
        lines.append("</table>")

    html_body = "\n".join(lines)

    # Send to all admins in parallel
    from app.scheduler import get_valid_token
    from app.utils.graph_client import GraphClient

    async def _send_digest_to_admin(admin_email):
        admin = db.query(User).filter(func.lower(User.email) == admin_email).first()
        if not admin:
            return
        try:
            token = await get_valid_token(admin, db)
            if not token:
                return

            gc = GraphClient(token)
            payload = {
                "message": {
                    "subject": f"[AVAIL] Weekly Account Health Digest — {digest['at_risk_count']} at risk",
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [{"emailAddress": {"address": admin_email}}],
                },
                "saveToSentItems": "false",
            }
            await gc.post_json("/me/sendMail", payload)
            logger.info(f"Manager digest sent to {admin_email}")
        except Exception as e:
            logger.error(f"Failed to send manager digest to {admin_email}: {e}")

    await asyncio.gather(*[_send_digest_to_admin(e) for e in settings.admin_emails])
