"""
v13_features.py — v1.3.0 Feature Routes

Graph webhooks, activity logging, sales dashboard (account ownership & open pool).

Business Rules:
- Activity log feeds ownership expiration (30d standard, 90d strategic)

Called by: main.py (router mount)
Depends on: services/activity_service, services/ownership_service, services/webhook_service
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import is_admin as _is_admin
from ..dependencies import require_admin, require_user
from ..models import ActivityLog, Company, CustomerSite, User, VendorCard
from ..rate_limit import limiter
from ..schemas.v13_features import (
    ActivityAttributeRequest,
    CompanyCallLog,
    CompanyNoteLog,
    EmailClickLog,
    GraphWebhookPayload,
    PhoneCallLog,
    StrategicToggle,
    VendorCallLog,
    VendorNoteLog,
)

router = APIRouter(tags=["v13"])


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════


@router.post("/api/webhooks/graph")
@limiter.limit("60/minute")
async def graph_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Microsoft Graph webhook endpoint.

    Handles validation handshake and notification payloads.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return PlainTextResponse(content=validation_token, status_code=200)

    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON payload")

    payload = GraphWebhookPayload.model_validate(raw)

    from app.services.webhook_service import handle_notification, validate_notifications

    payload_dict = payload.model_dump()
    validated = validate_notifications(payload_dict, db)
    if not validated:
        raise HTTPException(403, "No valid notifications")

    try:
        await handle_notification(payload_dict, db, validated=validated)
    except Exception:
        logger.exception("Webhook notification processing failed")
        raise HTTPException(502, "Notification processing failed")
    return {"status": "accepted"}


# ═══════════════════════════════════════════════════════════════════════
#  ACTIVITY LOG
# ═══════════════════════════════════════════════════════════════════════


def _activity_to_dict(a) -> dict:
    """Serialize an ActivityLog record."""
    return {
        "id": a.id,
        "user_id": a.user_id,
        "user_name": a.user.name if a.user else None,
        "activity_type": a.activity_type,
        "channel": a.channel,
        "company_id": a.company_id,
        "vendor_card_id": a.vendor_card_id,
        "vendor_contact_id": getattr(a, "vendor_contact_id", None),
        "site_contact_id": getattr(a, "site_contact_id", None),
        "contact_email": a.contact_email,
        "contact_phone": a.contact_phone,
        "contact_name": a.contact_name,
        "subject": a.subject,
        "notes": getattr(a, "notes", None),
        "duration_seconds": a.duration_seconds,
        "requisition_id": getattr(a, "requisition_id", None),
        "dismissed_at": a.dismissed_at.isoformat() if getattr(a, "dismissed_at", None) else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@router.get("/api/companies/{company_id}/activities")
async def get_company_activities(
    company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get activity log for a company."""
    from app.services.activity_service import get_company_activities as _get

    activities = _get(company_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.post("/api/companies/{company_id}/activities/call")
async def log_company_phone_call(
    company_id: int,
    payload: CompanyCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual phone call against a company."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from app.services.activity_service import log_company_call

    record = log_company_call(
        user_id=user.id,
        company_id=company_id,
        direction=payload.direction,
        phone=payload.phone,
        duration_seconds=payload.duration_seconds,
        contact_name=payload.contact_name,
        notes=payload.notes,
        db=db,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.post("/api/companies/{company_id}/activities/note")
async def log_company_note_endpoint(
    company_id: int,
    payload: CompanyNoteLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a company."""
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    from app.services.activity_service import log_company_note

    record = log_company_note(
        user_id=user.id,
        company_id=company_id,
        contact_name=payload.contact_name,
        notes=payload.notes,
        db=db,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.get("/api/vendors/{vendor_id}/activities")
async def get_vendor_activities(
    vendor_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get activity log for a vendor."""
    from app.services.activity_service import get_vendor_activities as _get

    activities = _get(vendor_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.get("/api/users/{target_user_id}/activities")
async def get_user_activities(
    target_user_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get activity log for a specific user."""
    from app.services.activity_service import get_user_activities as _get

    activities = _get(target_user_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.post("/api/activities/email")
async def log_email_click(
    body: EmailClickLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Auto-log when a mailto: link is clicked anywhere in the app."""
    from app.services.activity_service import log_email_activity

    email = body.email.strip()
    if not email:
        return {"status": "skipped", "message": "No email provided"}
    record = log_email_activity(
        user_id=user.id,
        direction="sent",
        email_addr=email,
        subject=None,
        external_id=None,
        contact_name=body.contact_name,
        db=db,
    )
    db.commit()
    if record:
        return {"status": "logged", "activity_id": record.id}
    return {"status": "no_match", "message": "Email did not match any known contact"}


@router.post("/api/activities/call")
async def log_phone_call(
    payload: PhoneCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a phone call (click-to-call or softphone integration)."""
    from app.services.activity_service import log_call_activity

    record = log_call_activity(
        user_id=user.id,
        direction=payload.direction,
        phone=payload.phone,
        duration_seconds=payload.duration_seconds,
        external_id=payload.external_id,
        contact_name=payload.contact_name,
        db=db,
    )
    db.commit()
    if record:
        return {"status": "logged", "activity_id": record.id}
    return {
        "status": "no_match",
        "message": "Phone number did not match any known contact",
    }


@router.post("/api/vendors/{vendor_id}/activities/call")
async def log_vendor_phone_call(
    vendor_id: int,
    payload: VendorCallLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual phone call against a vendor card."""
    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    from app.services.activity_service import log_vendor_call

    record = log_vendor_call(
        user_id=user.id,
        vendor_card_id=vendor_id,
        vendor_contact_id=payload.vendor_contact_id,
        direction=payload.direction,
        phone=payload.phone,
        duration_seconds=payload.duration_seconds,
        contact_name=payload.contact_name,
        notes=payload.notes,
        db=db,
        requisition_id=payload.requisition_id,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


@router.post("/api/vendors/{vendor_id}/activities/note")
async def log_vendor_note_endpoint(
    vendor_id: int,
    payload: VendorNoteLog,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Log a manual note against a vendor card."""
    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    from app.services.activity_service import log_vendor_note

    record = log_vendor_note(
        user_id=user.id,
        vendor_card_id=vendor_id,
        vendor_contact_id=payload.vendor_contact_id,
        notes=payload.notes,
        contact_name=payload.contact_name,
        db=db,
        requisition_id=payload.requisition_id,
    )
    db.commit()
    return {"status": "logged", "activity_id": record.id}


# ═══════════════════════════════════════════════════════════════════════
#  UNMATCHED ACTIVITY QUEUE (Phase 2A)
# ═══════════════════════════════════════════════════════════════════════


@router.get("/api/activities/unmatched")
async def list_unmatched_activities(
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List activities with no company or vendor match (admin only)."""
    from app.services.activity_service import (
        count_unmatched_activities,
        get_unmatched_activities,
    )

    activities = get_unmatched_activities(db, limit=limit, offset=offset)
    total = count_unmatched_activities(db)
    return {
        "items": [_activity_to_dict(a) for a in activities],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/api/activities/{activity_id}/attribute")
async def attribute_activity_endpoint(
    activity_id: int,
    payload: ActivityAttributeRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Attribute an unmatched activity to a company or vendor (admin only)."""
    # Verify the target entity exists
    if payload.entity_type == "company":
        target = db.get(Company, payload.entity_id)
        if not target:
            raise HTTPException(404, "Company not found")
    elif payload.entity_type == "vendor":
        target = db.get(VendorCard, payload.entity_id)
        if not target:
            raise HTTPException(404, "Vendor not found")

    from app.services.activity_service import attribute_activity

    result = attribute_activity(
        activity_id=activity_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        db=db,
        user_id=user.id,
    )
    if not result:
        raise HTTPException(404, "Activity not found")

    db.commit()
    return {"status": "attributed", "activity": _activity_to_dict(result)}


@router.post("/api/activities/{activity_id}/dismiss")
async def dismiss_activity_endpoint(
    activity_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Dismiss an unmatched activity (admin only)."""
    from app.services.activity_service import dismiss_activity

    result = dismiss_activity(activity_id, db)
    if not result:
        raise HTTPException(404, "Activity not found")

    db.commit()
    return {"status": "dismissed", "activity_id": activity_id}


@router.get("/api/vendors/{vendor_id}/activity-status")
async def vendor_activity_status(
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get activity health status for a vendor (green/yellow/red indicator)."""
    from app.services.activity_service import days_since_last_vendor_activity

    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    days = days_since_last_vendor_activity(vendor_id, db)

    if days is None:
        status = "no_activity"
    elif days <= settings.customer_warning_days:
        status = "green"
    elif days <= settings.vendor_protection_warn_days:
        status = "yellow"
    else:
        status = "red"

    return {
        "vendor_card_id": vendor_id,
        "days_since_activity": days,
        "status": status,
    }


@router.get("/api/companies/{company_id}/activity-status")
async def company_activity_status(
    company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get activity health status for a company (for dashboard indicators)."""
    from app.config import settings as cfg
    from app.services.activity_service import days_since_last_activity

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    days = days_since_last_activity(company_id, db)
    inactivity_limit = (
        cfg.strategic_inactivity_days
        if company.is_strategic
        else cfg.customer_inactivity_days
    )

    if days is None:
        status = "no_activity"
    elif days <= cfg.customer_warning_days:
        status = "green"
    elif days <= inactivity_limit:
        status = "yellow"
    else:
        status = "red"

    return {
        "company_id": company_id,
        "days_since_activity": days,
        "inactivity_limit": inactivity_limit,
        "is_strategic": company.is_strategic or False,
        "status": status,
        "account_owner_id": company.account_owner_id,
    }


# ═══════════════════════════════════════════════════════════════════════
#  SALES DASHBOARD: Account Ownership & Open Pool
# ═══════════════════════════════════════════════════════════════════════


@router.get("/api/sales/my-accounts")
async def my_accounts(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get all accounts owned by the current user with activity health."""
    from app.services.ownership_service import get_my_accounts

    return get_my_accounts(user.id, db)


@router.get("/api/sales/at-risk")
async def at_risk_accounts(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get all accounts approaching the inactivity warning zone."""
    from app.services.ownership_service import get_accounts_at_risk

    return get_accounts_at_risk(db)


@router.get("/api/sales/open-pool")
async def open_pool_accounts(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get all unowned accounts available for claiming."""
    from app.services.ownership_service import get_open_pool_accounts

    return get_open_pool_accounts(db)


@router.post("/api/sales/claim/{company_id}")
async def claim_account(
    company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Manually claim an open pool account."""
    if user.role not in ("sales", "trader"):
        raise HTTPException(403, "Only sales users can claim accounts")

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    if company.account_owner_id is not None:
        owner = db.get(User, company.account_owner_id)
        owner_name = owner.name if owner else "Unknown"
        raise HTTPException(409, f"Account already owned by {owner_name}")

    company.account_owner_id = user.id
    company.ownership_cleared_at = None
    db.commit()
    return {"status": "claimed", "company_id": company_id, "company_name": company.name}


@router.put("/api/companies/{company_id}/strategic")
async def toggle_strategic(
    company_id: int,
    payload: StrategicToggle = StrategicToggle(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Toggle a company's strategic flag (admin/manager only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    company.is_strategic = (
        payload.is_strategic
        if payload.is_strategic is not None
        else not company.is_strategic
    )
    db.commit()

    inactivity_limit = (
        settings.strategic_inactivity_days
        if company.is_strategic
        else settings.customer_inactivity_days
    )
    return {
        "company_id": company_id,
        "is_strategic": company.is_strategic,
        "inactivity_limit": inactivity_limit,
    }


@router.get("/api/sales/manager-digest")
async def manager_digest(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get the manager digest data (admin only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    from app.services.ownership_service import get_manager_digest

    return get_manager_digest(db)


_NOTIFICATION_TYPES = (
    "ownership_warning",
    "buyplan_pending",
    "buyplan_approved",
    "buyplan_rejected",
    "buyplan_completed",
    "buyplan_cancelled",
    "vendor_reply_review",
    "competitive_quote",
    "proactive_match",
    "offer_pending_review",
)


@router.get("/api/sales/notifications")
async def sales_notifications(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get dashboard notifications for the current user."""
    notifications = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == user.id,
            ActivityLog.activity_type.in_(_NOTIFICATION_TYPES),
            ActivityLog.dismissed_at.is_(None),
            ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=14),
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(30)
        .all()
    )

    return [
        {
            "id": n.id,
            "type": n.activity_type,
            "company_id": n.company_id,
            "company_name": n.contact_name,
            "requisition_id": n.requisition_id,
            "vendor_card_id": n.vendor_card_id,
            "subject": n.subject,
            "notes": n.notes,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notifications
    ]


@router.post("/api/sales/notifications/{notif_id}/read")
async def mark_notification_read(
    notif_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read."""
    notif = db.get(ActivityLog, notif_id)
    if not notif or notif.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    notif.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


@router.post("/api/sales/notifications/read-all")
async def mark_all_notifications_read(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark all notifications as read for the current user."""
    db.query(ActivityLog).filter(
        ActivityLog.user_id == user.id,
        ActivityLog.activity_type.in_(_NOTIFICATION_TYPES),
        ActivityLog.dismissed_at.is_(None),
        ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=14),
    ).update(
        {"dismissed_at": datetime.now(timezone.utc)},
        synchronize_session="fetch",
    )
    db.commit()
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════
#  PROSPECTING POOL — Site-Level Ownership
# ═══════════════════════════════════════════════════════════════════════


@router.get("/api/prospecting/pool")
async def prospecting_pool(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get all unowned active sites available for claiming."""
    from app.services.ownership_service import get_open_pool_sites

    return get_open_pool_sites(db)


@router.post("/api/prospecting/claim/{site_id}")
async def prospecting_claim(
    site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Claim an unowned site for the current user.

    Enforces 200-site cap — users at cap must release sites first.
    """
    from sqlalchemy import func

    if user.role not in ("sales", "trader"):
        raise HTTPException(403, "Only sales/trader users can claim sites")

    # Enforce 200-site cap
    current_count = (
        db.query(func.count(CustomerSite.id))
        .filter(
            CustomerSite.owner_id == user.id,
            CustomerSite.is_active.is_(True),
        )
        .scalar()
        or 0
    )
    if current_count >= SITE_CAP_PER_USER:
        raise HTTPException(
            409,
            f"You have {current_count} sites (cap is {SITE_CAP_PER_USER}). "
            "Release inactive sites before claiming new ones.",
        )

    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    if site.owner_id is not None:
        owner = db.get(User, site.owner_id)
        owner_name = owner.name if owner else "Unknown"
        raise HTTPException(409, f"Site already owned by {owner_name}")

    from app.services.ownership_service import claim_site

    claimed = claim_site(site_id, user.id, db)
    if not claimed:
        raise HTTPException(409, "Site could not be claimed")

    db.commit()
    return {"status": "claimed", "site_id": site_id, "site_name": site.site_name}


@router.post("/api/prospecting/release/{site_id}")
async def prospecting_release(
    site_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Release ownership of a site back to the open pool."""
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")

    if site.owner_id != user.id and not _is_admin(user):
        raise HTTPException(403, "You can only release your own sites")

    site.owner_id = None
    site.ownership_cleared_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("User {} released site {} ({})", user.name, site_id, site.site_name)
    return {"status": "released", "site_id": site_id, "site_name": site.site_name}


@router.get("/api/prospecting/my-sites")
async def prospecting_my_sites(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get sites owned by the current user with health status."""
    from app.services.ownership_service import get_my_sites

    return get_my_sites(user.id, db)


@router.get("/api/prospecting/at-risk")
async def prospecting_at_risk(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get owned sites approaching inactivity limit."""
    from app.services.ownership_service import get_sites_at_risk

    return get_sites_at_risk(db)


@router.put("/api/prospecting/sites/{site_id}/owner")
async def prospecting_assign_owner(
    site_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only: assign/reassign a site to a specific user."""
    site = db.get(CustomerSite, site_id)
    if not site:
        raise HTTPException(404, "Site not found")

    body = await request.json()
    new_owner_id = body.get("owner_id")

    if new_owner_id is not None:
        target = db.get(User, new_owner_id)
        if not target:
            raise HTTPException(404, "User not found")

    site.owner_id = new_owner_id
    if new_owner_id is None:
        site.ownership_cleared_at = datetime.now(timezone.utc)
    else:
        site.ownership_cleared_at = None
    db.commit()

    return {
        "ok": True,
        "site_id": site_id,
        "owner_id": new_owner_id,
    }


# ═══════════════════════════════════════════════════════════════════════
#  PROSPECTING: Accounts-First Hierarchy + 200-Site Cap
# ═══════════════════════════════════════════════════════════════════════

SITE_CAP_PER_USER = 200


@router.get("/api/prospecting/my-accounts")
async def prospecting_my_accounts(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get accounts grouped from the user's owned sites.

    Returns accounts (companies) with site counts and health status.
    This is the accounts-first view: accounts -> drill into sites.
    """
    from sqlalchemy import case, func

    now = datetime.now(timezone.utc)
    thirty_days = now - timedelta(days=30)
    ninety_days = now - timedelta(days=90)

    # Get all companies that have at least one site owned by this user
    rows = (
        db.query(
            Company.id,
            Company.name,
            Company.domain,
            Company.industry,
            Company.hq_city,
            Company.hq_state,
            Company.employee_size,
            Company.is_strategic,
            func.count(CustomerSite.id).label("site_count"),
            func.count(
                case(
                    (CustomerSite.last_activity_at >= thirty_days, CustomerSite.id),
                    else_=None,
                )
            ).label("active_sites"),
            func.count(
                case(
                    (
                        (CustomerSite.last_activity_at < thirty_days)
                        | (CustomerSite.last_activity_at.is_(None)),
                        CustomerSite.id,
                    ),
                    else_=None,
                )
            ).label("inactive_sites"),
            func.max(CustomerSite.last_activity_at).label("last_activity"),
        )
        .join(CustomerSite, CustomerSite.company_id == Company.id)
        .filter(
            CustomerSite.owner_id == user.id,
            CustomerSite.is_active.is_(True),
        )
        .group_by(Company.id)
        .order_by(Company.name)
        .all()
    )

    accounts = []
    for r in rows:
        # Health: green=all active, yellow=some inactive, red=all inactive
        if r.site_count == 0:
            health = "grey"
        elif r.active_sites == r.site_count:
            health = "green"
        elif r.active_sites > 0:
            health = "yellow"
        else:
            health = "red"

        accounts.append({
            "company_id": r.id,
            "name": r.name,
            "domain": r.domain,
            "industry": r.industry,
            "location": ", ".join(filter(None, [r.hq_city, r.hq_state])) or None,
            "employee_size": r.employee_size,
            "is_strategic": r.is_strategic or False,
            "site_count": r.site_count,
            "active_sites": r.active_sites,
            "inactive_sites": r.inactive_sites,
            "health": health,
            "last_activity": r.last_activity.isoformat() if r.last_activity else None,
        })

    return accounts


@router.get("/api/prospecting/accounts/{company_id}/sites")
async def prospecting_account_sites(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get all sites for a specific account owned by the current user.

    Returns site list with contact info, status, and last activity.
    """
    now = datetime.now(timezone.utc)
    thirty_days = now - timedelta(days=30)

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    sites = (
        db.query(CustomerSite)
        .filter(
            CustomerSite.company_id == company_id,
            CustomerSite.owner_id == user.id,
            CustomerSite.is_active.is_(True),
        )
        .order_by(CustomerSite.site_name)
        .all()
    )

    result = []
    for s in sites:
        last = s.last_activity_at
        if last and last >= thirty_days:
            status = "green"
        elif last:
            status = "yellow" if (now - last).days <= 60 else "red"
        else:
            status = "grey"

        result.append({
            "site_id": s.id,
            "site_name": s.site_name,
            "site_type": s.site_type,
            "contact_name": s.contact_name,
            "contact_email": s.contact_email,
            "city": s.city,
            "state": s.state,
            "status": status,
            "last_activity_at": last.isoformat() if last else None,
            "days_inactive": (now - last).days if last else None,
        })

    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "domain": company.domain,
            "industry": company.industry,
        },
        "sites": result,
    }


@router.get("/api/prospecting/capacity")
async def prospecting_capacity(
    user_id: int | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get site capacity for a user (current user by default).

    Returns used/cap counts and list of stalest accounts for nudges.
    """
    from sqlalchemy import func

    target_id = user_id or user.id

    # Count active sites owned by user
    used = (
        db.query(func.count(CustomerSite.id))
        .filter(
            CustomerSite.owner_id == target_id,
            CustomerSite.is_active.is_(True),
        )
        .scalar()
        or 0
    )

    # Find stalest accounts (no activity in 90+ days) for nudge
    ninety_days = datetime.now(timezone.utc) - timedelta(days=90)
    stale_sites = (
        db.query(
            CustomerSite.id,
            CustomerSite.site_name,
            Company.name.label("company_name"),
            CustomerSite.last_activity_at,
        )
        .join(Company, Company.id == CustomerSite.company_id)
        .filter(
            CustomerSite.owner_id == target_id,
            CustomerSite.is_active.is_(True),
            (CustomerSite.last_activity_at < ninety_days)
            | (CustomerSite.last_activity_at.is_(None)),
        )
        .order_by(CustomerSite.last_activity_at.asc().nullsfirst())
        .limit(10)
        .all()
    )

    stale = [
        {
            "site_id": s.id,
            "site_name": s.site_name,
            "company_name": s.company_name,
            "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
        }
        for s in stale_sites
    ]

    return {
        "used": used,
        "cap": SITE_CAP_PER_USER,
        "remaining": max(0, SITE_CAP_PER_USER - used),
        "at_cap": used >= SITE_CAP_PER_USER,
        "stale_accounts": stale,
    }


