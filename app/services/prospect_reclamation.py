"""SP4 account reclamation service — park, sweep, notify, reclaim.

Called by: app/jobs/prospecting_jobs.py (scheduler jobs),
           app/routers/htmx_views.py (HTMX reclaim action)
Depends on: app/services/activity_service.py, app/services/prospect_claim.py,
            app/utils/graph_client.py, app/models/prospect_account.py,
            app/models/crm.py, app/utils/token_manager.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..models.auth import User
from ..models.crm import Company
from ..models.prospect_account import ProspectAccount

# Phase 4 compliance: a former owner cannot reclaim a freshly-swept account for this many
# days (managers/admins bypass via reassign). Set on the ProspectAccount at sweep time.
RECLAIM_COOLDOWN_DAYS = 30

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
            days_dormant = (now - last_activity).days
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
                    pa.reclaim_blocked_until = now + timedelta(days=RECLAIM_COOLDOWN_DAYS)
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


def _sweep_notification_recipients(owner: User, db: Session) -> list[str]:
    """Resolve the deduped recipient list for a sweep notification.

    Includes the rep (former owner), every ACTIVE user with role MANAGER or ADMIN, and
    the configured settings.account_sweep_manager_email. Order is preserved with the rep
    first; comparison is case-insensitive so the same address is never sent twice.

    Called by: _send_sweep_notification
    """
    from ..constants import UserRole

    recipients: list[str] = []
    seen: set[str] = set()

    def _add(address: str | None) -> None:
        if not address:
            return
        key = address.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        recipients.append(address.strip())

    _add(owner.email)

    supervisors = (
        db.query(User)
        .filter(
            User.role.in_([UserRole.MANAGER, UserRole.ADMIN]),
            User.is_active.is_(True),
        )
        .all()
    )
    for sup in supervisors:
        _add(sup.email)

    _add(settings.account_sweep_manager_email)
    return recipients


async def _send_sweep_notification(
    owner: User,
    company: Company,
    last_activity_at: datetime | None,
    prospect_id: int,
    db: Session,
) -> None:
    """Send a Graph /me/sendMail loss-notification to the rep and every supervisor.

    Recipients: the former owner, all active MANAGER/ADMIN users, and the configured
    settings.account_sweep_manager_email (deduped via _sweep_notification_recipients).
    Sends one message per recipient so a single bad address can't suppress the rest —
    each send is wrapped in try/except so one failure never breaks the sweep. Uses the
    OWNER's token (get_valid_token); on a missing token: log a warning and return.

    Called by: job_account_sweep_with_db
    """
    from ..utils.graph_client import GraphClient
    from ..utils.token_manager import get_valid_token

    token = await get_valid_token(owner, db)
    if not token:
        logger.warning(
            "SP4 sweep notification: no valid token for {} ({}); skipping all emails "
            "(rep + managers/supervisors will NOT be notified)",
            owner.email,
            owner.id,
        )
        return

    last_active_str = last_activity_at.strftime("%Y-%m-%d") if last_activity_at else "never"
    body_html = (
        f"<p>The account <strong>{company.name}</strong> has been automatically moved to the "
        f"prospecting pool due to inactivity.</p>"
        f"<p><strong>Last activity:</strong> {last_active_str}</p>"
        f"<p><strong>Inactivity threshold:</strong> {settings.account_sweep_inactivity_days} days</p>"
        f"<p>It can be reclaimed from the Prospecting tab "
        f"(former owners after the {RECLAIM_COOLDOWN_DAYS}-day cooldown; "
        f"managers may reassign it at any time).</p>"
    )

    gc = GraphClient(token)
    recipients = _sweep_notification_recipients(owner, db)
    sent = 0
    for address in recipients:
        payload = {
            "message": {
                "subject": f"[AVAIL] Account swept to prospecting pool: {company.name}",
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": address}}],
            },
            "saveToSentItems": "false",
        }
        try:
            await gc.post_json("/me/sendMail", payload)
            sent += 1
        except Exception:
            logger.exception(
                "SP4 sweep notification failed for {} (company {})",
                address,
                company.name,
            )

    logger.info(
        "SP4 sweep notification: sent {}/{} for company {}",
        sent,
        len(recipients),
        company.name,
    )


# ── Auto-surface (Task 6) ─────────────────────────────────────────────────────


async def job_auto_surface_reactivation() -> None:
    """Daily 2AM — surface unassigned past-customer Companies as reactivation prospects.

    Opens its own database session. Delegates to job_auto_surface_with_db().
    Called by: app/jobs/prospecting_jobs.py
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        await job_auto_surface_with_db(db)
    finally:
        db.close()


async def job_auto_surface_with_db(db: Session) -> None:
    """Core auto-surface logic — injectable session for testability.

    Criteria: Company.account_owner_id IS NULL AND (has Requisition OR has Quote via
    CustomerSite). Skip if ProspectAccount already linked (company_id set, non-dismissed).
    Sets discovery_source="reactivation".

    Called by: job_auto_surface_reactivation(), tests
    """
    from sqlalchemy import exists

    from ..models.crm import CustomerSite
    from ..models.quotes import Quote
    from ..models.sourcing import Requisition

    req_subq = exists().where(Requisition.company_id == Company.id)
    quote_subq = exists().where(Quote.customer_site_id == CustomerSite.id).where(CustomerSite.company_id == Company.id)

    candidates = (
        db.query(Company)
        .filter(
            Company.account_owner_id.is_(None),
            req_subq | quote_subq,
        )
        .all()
    )

    from ..constants import ProspectAccountStatus

    surfaced = 0
    skipped = 0

    for co in candidates:
        # Skip if already in pool (any non-dismissed ProspectAccount by company_id)
        existing = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.company_id == co.id,
                ProspectAccount.status != ProspectAccountStatus.DISMISSED,
            )
            .first()
        )
        if existing:
            skipped += 1
            continue

        domain = (co.domain or "").strip().lower()
        if not domain:
            logger.warning(
                "SP4 reactivation: company {} ({}) has no domain; skipping surfacing",
                co.name,
                co.id,
            )
            skipped += 1
            continue

        # Handle domain collision (ProspectAccount.domain is UNIQUE)
        domain_existing = db.query(ProspectAccount).filter(ProspectAccount.domain == domain).first()
        if domain_existing:
            if domain_existing.company_id is None:
                domain_existing.company_id = co.id
                db.commit()
            skipped += 1
            continue

        try:
            pa = ProspectAccount(
                name=co.name,
                domain=domain,
                discovery_source="reactivation",
                status=ProspectAccountStatus.SUGGESTED,
                fit_score=0,
                readiness_score=0,
                company_id=co.id,
            )
            db.add(pa)
            db.commit()
            surfaced += 1
            logger.info("SP4 reactivation: surfaced company {} ({})", co.name, co.id)
        except Exception:
            logger.exception("SP4 reactivation: failed to surface company {} ({})", co.name, co.id)
            db.rollback()

    logger.info("SP4 reactivation complete: surfaced={}, skipped={}", surfaced, skipped)


# ── Reclaim (Task 7) ──────────────────────────────────────────────────────────


def reclaim_prospect_account(
    prospect_id: int,
    user_id: int,
    db: Session,
    *,
    is_admin: bool = False,
    justification: str | None = None,
) -> dict:
    """Reclaim a swept prospect: re-assign Company owner, remove from pool, reset clock.

    Permission: swept_from_owner_id == user_id OR is_admin OR is_manager_or_admin(user)
                OR user.email == settings.account_sweep_manager_email.

    Compliance (Phase 4): a former owner cannot reclaim while reclaim_blocked_until is in
    the future (a 30-day post-sweep cooldown). Managers/admins bypass the cooldown — they
    should use reassign_account, but a direct reclaim is also permitted for them.

    Actions:
    - Set Company.account_owner_id = user_id; Company.ownership_cleared_at = None
    - Set ProspectAccount.status = DISMISSED (removes from pool)
    - Log a "reclaim" ActivityLog entry (resets activity clock)

    Returns: {prospect_id, company_id, company_name, status: "reclaimed"}
    Raises: LookupError (not found), ValueError (permission denied / wrong status / cooldown)

    Called by: app/routers/htmx_views.py
    """
    from ..constants import ProspectAccountStatus
    from ..dependencies import is_manager_or_admin
    from ..services.activity_service import log_activity

    pa = db.get(ProspectAccount, prospect_id)
    if pa is None:
        raise LookupError(f"ProspectAccount {prospect_id} not found")

    if pa.status not in (ProspectAccountStatus.SUGGESTED, ProspectAccountStatus.CLAIMED):
        raise ValueError(f"Cannot reclaim a prospect with status '{pa.status}'")

    user = db.get(User, user_id)
    if user is None:
        raise RuntimeError(f"User {user_id} not found")

    manager_email = settings.account_sweep_manager_email
    is_former_owner = pa.swept_from_owner_id == user_id
    is_email_manager = bool(manager_email and user.email == manager_email)
    is_supervisor = is_admin or is_manager_or_admin(user) or is_email_manager

    if not (is_former_owner or is_supervisor):
        raise ValueError("Reclaim permission denied: must be former owner, admin, or sweep manager")

    # Phase 4 cooldown: the former owner is blocked until reclaim_blocked_until passes.
    # Supervisors (manager/admin/configured manager email) bypass it.
    if is_former_owner and not is_supervisor and pa.reclaim_blocked_until is not None:
        blocked_until = pa.reclaim_blocked_until
        if blocked_until.tzinfo is None:
            blocked_until = blocked_until.replace(tzinfo=timezone.utc)
        if blocked_until > datetime.now(timezone.utc):
            raise ValueError("This account is in a 30-day cooldown; ask a manager to reassign it.")

    pa.status = ProspectAccountStatus.DISMISSED
    pa.dismissed_at = datetime.now(timezone.utc)
    pa.dismissed_by = user_id
    pa.dismiss_reason = "reclaimed"

    company_id = pa.company_id
    company_name = pa.name

    if company_id:
        co = db.get(Company, company_id)
        if co:
            co.account_owner_id = user_id
            co.ownership_cleared_at = None
            company_name = co.name

    log_activity(
        db,
        activity_type="reclaim",
        channel="system",
        user_id=user_id,
        company_id=company_id,
        summary="Account reclaimed from prospecting pool",
        details={"prospect_id": prospect_id, "justification": justification},
    )

    db.commit()

    logger.info(
        "SP4 reclaim: user {} reclaimed prospect {} (company {})",
        user_id,
        prospect_id,
        company_id,
    )

    return {
        "prospect_id": prospect_id,
        "company_id": company_id,
        "company_name": company_name,
        "status": "reclaimed",
    }


# ── Manager reassign (Phase 4) ────────────────────────────────────────────────


def reassign_account(company_id: int, to_user_id: int, by_user: User, db: Session) -> dict:
    """Manager/admin reassigns a Company to another owner, overriding the reclaim
    cooldown.

    This is the supervisor escape hatch for the Phase 4 30-day cooldown: a former owner who
    is still blocked asks a manager to hand the account to someone (often back to them).

    Gate: is_manager_or_admin(by_user) — else HTTPException(403). Actions:
    - Set Company.account_owner_id = to_user_id; clear ownership_cleared_at.
    - If a swept ProspectAccount exists for this company, dismiss it and clear
      reclaim_blocked_until (the cooldown no longer applies once a manager has acted).
    - Log a "reassign" ActivityLog entry on the company.

    Returns: {company_id, company_name, to_user_id, prospect_id|None, status: "reassigned"}
    Raises: HTTPException(403) (not a supervisor), LookupError (company missing),
            ValueError (target user missing).

    Called by: app/routers/htmx_views.py (POST /v2/partials/prospects/{id}/reassign)
    """
    from ..constants import ProspectAccountStatus
    from ..dependencies import is_manager_or_admin
    from ..services.activity_service import log_activity

    if not is_manager_or_admin(by_user):
        raise PermissionError("Only a manager or admin can reassign an account")

    co = db.get(Company, company_id)
    if co is None:
        raise LookupError(f"Company {company_id} not found")

    target = db.get(User, to_user_id)
    if target is None:
        raise ValueError(f"Target user {to_user_id} not found")
    if not target.is_active:
        raise ValueError(f"Target user {to_user_id} is inactive")

    co.account_owner_id = to_user_id
    co.ownership_cleared_at = None

    prospect_id: int | None = None
    swept_pa = (
        db.query(ProspectAccount)
        .filter(
            ProspectAccount.company_id == company_id,
            ProspectAccount.status != ProspectAccountStatus.DISMISSED,
            (ProspectAccount.swept_at.is_not(None)) | (ProspectAccount.swept_from_owner_id.is_not(None)),
        )
        .first()
    )
    if swept_pa is not None:
        swept_pa.status = ProspectAccountStatus.DISMISSED
        swept_pa.dismissed_at = datetime.now(timezone.utc)
        swept_pa.dismissed_by = by_user.id
        swept_pa.dismiss_reason = "reassigned"
        swept_pa.reclaim_blocked_until = None
        prospect_id = swept_pa.id

    log_activity(
        db,
        activity_type="reassign",
        channel="system",
        user_id=by_user.id,
        company_id=company_id,
        summary=f"Account reassigned to user {to_user_id}",
        details={"to_user_id": to_user_id, "by_user_id": by_user.id, "prospect_id": prospect_id},
    )

    db.commit()

    logger.info(
        "SP4 reassign: manager {} reassigned company {} to user {} (prospect {})",
        by_user.id,
        company_id,
        to_user_id,
        prospect_id,
    )

    return {
        "company_id": company_id,
        "company_name": co.name,
        "to_user_id": to_user_id,
        "prospect_id": prospect_id,
        "status": "reassigned",
    }
