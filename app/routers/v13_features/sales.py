"""sales.py — Sales dashboard v1.3.0 routes.

Account ownership (my-accounts, at-risk, open-pool, claim, strategic),
manager digest, and notification endpoints.

Called by: v13_features package __init__.py
Depends on: services/ownership_service
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_user
from ...models import ActivityLog, Company, User
from ...schemas.v13_features import StrategicToggle

router = APIRouter(tags=["v13"])


# ═══════════════════════════════════════════════════════════════════════
#  SALES DASHBOARD: Account Ownership & Open Pool
# ═══════════════════════════════════════════════════════════════════════


@router.get("/api/sales/my-accounts")
async def my_accounts(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get all accounts owned by the current user with activity health."""
    from app.services.ownership_service import get_my_accounts

    return get_my_accounts(user.id, db)


@router.get("/api/sales/at-risk")
async def at_risk_accounts(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get all accounts approaching the inactivity warning zone."""
    from app.services.ownership_service import get_accounts_at_risk

    return get_accounts_at_risk(db)


@router.get("/api/sales/open-pool")
async def open_pool_accounts(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get all unowned accounts available for claiming."""
    from app.services.ownership_service import get_open_pool_accounts

    return get_open_pool_accounts(db)


@router.post("/api/sales/claim/{company_id}")
async def claim_account(company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
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

    company.is_strategic = payload.is_strategic if payload.is_strategic is not None else not company.is_strategic
    db.commit()

    inactivity_limit = settings.strategic_inactivity_days if company.is_strategic else settings.customer_inactivity_days
    return {
        "company_id": company_id,
        "is_strategic": company.is_strategic,
        "inactivity_limit": inactivity_limit,
    }


@router.get("/api/sales/manager-digest")
async def manager_digest(user: User = Depends(require_user), db: Session = Depends(get_db)):
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
    "competitive_quote",
    "proactive_match",
    "offer_pending_review",
    "quote_won",
    "quote_lost",
)


@router.get("/api/sales/notifications")
async def sales_notifications(user: User = Depends(require_user), db: Session = Depends(get_db)):
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
            "buy_plan_id": n.buy_plan_id,
            "quote_id": n.quote_id,
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


@router.get("/api/sales/notifications/count")
async def notification_count(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Lightweight unread notification count for badge display."""
    from sqlalchemy import func

    count = (
        db.query(func.count(ActivityLog.id))
        .filter(
            ActivityLog.user_id == user.id,
            ActivityLog.activity_type.in_(_NOTIFICATION_TYPES),
            ActivityLog.dismissed_at.is_(None),
            ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=14),
        )
        .scalar()
    )
    return {"count": count}
