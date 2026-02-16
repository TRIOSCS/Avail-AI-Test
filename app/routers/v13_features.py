"""
v13_features.py — v1.3.0 Feature Routes

Graph webhooks, buyer profiles, activity logging, sales dashboard
(account ownership & open pool), and buyer routing assignments.

Business Rules:
- Activity log feeds ownership expiration (30d standard, 90d strategic)
- Routing: top 3 buyers scored, first to offer within 48h claims
- Offer attribution has 14-day TTL with reconfirmation

Called by: main.py (router mount)
Depends on: services/activity_service, services/ownership_service,
            services/buyer_service, services/routing_service, services/webhook_service
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from loguru import logger as log
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import is_admin as _is_admin, require_user
from ..models import ActivityLog, Company, Offer, User
from ..schemas.v13_features import (
    BuyerProfileUpsert, PhoneCallLog, RoutingPairRequest, StrategicToggle,
)

router = APIRouter(tags=["v13"])


# ═══════════════════════════════════════════════════════════════════════
#  GRAPH WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════

@router.post("/api/webhooks/graph")
async def graph_webhook(request: Request, db: Session = Depends(get_db)):
    """Microsoft Graph webhook endpoint.

    Handles validation handshake and notification payloads.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        return PlainTextResponse(content=validation_token, status_code=200)

    try:
        payload = await request.json()
    except Exception:
        return {"status": "invalid payload"}

    from app.services.webhook_service import handle_notification
    try:
        await handle_notification(payload, db)
    except Exception as e:
        log.error(f"Webhook notification error: {e}")
    return {"status": "accepted"}


# ═══════════════════════════════════════════════════════════════════════
#  BUYER PROFILES
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/buyer-profiles")
async def list_buyer_profiles(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """List all buyer profiles (visible to all users for transparency)."""
    from app.services.buyer_service import list_profiles
    return list_profiles(db)


@router.get("/api/buyer-profiles/{user_id}")
async def get_buyer_profile(user_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get a specific buyer profile."""
    from app.services.buyer_service import get_profile
    profile = get_profile(user_id, db)
    if not profile:
        raise HTTPException(404, "Buyer profile not found")
    return {
        "user_id": profile.user_id,
        "primary_commodity": profile.primary_commodity,
        "secondary_commodity": profile.secondary_commodity,
        "primary_geography": profile.primary_geography,
        "brand_specialties": profile.brand_specialties or [],
        "brand_material_types": profile.brand_material_types or [],
        "brand_usage_types": profile.brand_usage_types or [],
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


@router.put("/api/buyer-profiles/{user_id}")
async def upsert_buyer_profile(
    user_id: int, payload: BuyerProfileUpsert,
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Create or update a buyer profile. Admin or self only."""
    if not _is_admin(user) and user.id != user_id:
        raise HTTPException(403, "Only admins can edit other buyers' profiles")

    target = db.get(User, user_id)
    if not target or target.role != "buyer":
        raise HTTPException(400, "Target user must be a buyer")

    from app.services.buyer_service import upsert_profile
    profile = upsert_profile(user_id, payload.model_dump(exclude_unset=True), db)
    db.commit()
    return {
        "user_id": profile.user_id,
        "primary_commodity": profile.primary_commodity,
        "secondary_commodity": profile.secondary_commodity,
        "primary_geography": profile.primary_geography,
        "brand_specialties": profile.brand_specialties or [],
        "brand_material_types": profile.brand_material_types or [],
        "brand_usage_types": profile.brand_usage_types or [],
    }


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
        "contact_email": a.contact_email,
        "contact_phone": a.contact_phone,
        "contact_name": a.contact_name,
        "subject": a.subject,
        "duration_seconds": a.duration_seconds,
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
    target_user_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get activity log for a specific user."""
    from app.services.activity_service import get_user_activities as _get
    activities = _get(target_user_id, db)
    return [_activity_to_dict(a) for a in activities]


@router.post("/api/activities/call")
async def log_phone_call(
    payload: PhoneCallLog, user: User = Depends(require_user), db: Session = Depends(get_db)
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
    return {"status": "no_match", "message": "Phone number did not match any known contact"}


@router.get("/api/companies/{company_id}/activity-status")
async def company_activity_status(
    company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get activity health status for a company (for dashboard indicators)."""
    from app.services.activity_service import days_since_last_activity
    from app.config import settings as cfg

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    days = days_since_last_activity(company_id, db)
    inactivity_limit = cfg.strategic_inactivity_days if company.is_strategic else cfg.customer_inactivity_days

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
async def claim_account(
    company_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Manually claim an open pool account."""
    if user.role != "sales":
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
    company_id: int, payload: StrategicToggle = StrategicToggle(),
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Toggle a company's strategic flag (admin/manager only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    company.is_strategic = (
        payload.is_strategic if payload.is_strategic is not None
        else not company.is_strategic
    )
    db.commit()

    inactivity_limit = (
        settings.strategic_inactivity_days if company.is_strategic
        else settings.customer_inactivity_days
    )
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


@router.get("/api/sales/notifications")
async def sales_notifications(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get dashboard notifications for the current user."""
    notifications = db.query(ActivityLog).filter(
        ActivityLog.user_id == user.id,
        ActivityLog.activity_type == "ownership_warning",
        ActivityLog.created_at >= datetime.now(timezone.utc) - timedelta(days=7),
    ).order_by(ActivityLog.created_at.desc()).limit(20).all()

    return [
        {
            "id": n.id,
            "type": "ownership_warning",
            "company_id": n.company_id,
            "company_name": n.contact_name,
            "subject": n.subject,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notifications
    ]


# ═══════════════════════════════════════════════════════════════════════
#  BUYER ROUTING: Assignments, Scoring, Claims
# ═══════════════════════════════════════════════════════════════════════

@router.get("/api/routing/my-assignments")
async def my_routing_assignments(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get all active routing assignments where the current user is in the top-3."""
    from app.services.routing_service import get_active_assignments_for_buyer
    return get_active_assignments_for_buyer(user.id, db)


@router.get("/api/routing/assignments/{assignment_id}")
async def routing_assignment_detail(
    assignment_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get full details of a routing assignment."""
    from app.services.routing_service import get_assignment_details
    result = get_assignment_details(assignment_id, db)
    if not result:
        raise HTTPException(404, "Assignment not found")
    return result


@router.post("/api/routing/assignments/{assignment_id}/claim")
async def claim_routing_assignment(
    assignment_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Claim a routing assignment by entering an offer."""
    from app.services.routing_service import claim_routing
    result = claim_routing(assignment_id, user.id, db)
    if not result["success"]:
        raise HTTPException(409, result["message"])
    db.commit()
    return result


@router.post("/api/routing/score")
async def score_routing(
    payload: RoutingPairRequest, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Preview routing scores for a requirement+vendor pair."""
    from app.services.routing_service import rank_buyers_for_assignment
    return rank_buyers_for_assignment(payload.requirement_id, payload.vendor_card_id, db)


@router.post("/api/routing/create")
async def create_routing(
    payload: RoutingPairRequest, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Manually trigger routing assignment for a requirement+vendor pair."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")

    from app.services.routing_service import create_routing_assignment
    assignment = create_routing_assignment(payload.requirement_id, payload.vendor_card_id, db)
    if not assignment:
        raise HTTPException(404, "No buyers available for routing")
    db.commit()

    try:
        from app.services.routing_service import notify_routing_assignment
        await notify_routing_assignment(assignment, db)
    except Exception as e:
        log.error(f"Routing notification error: {e}")

    from app.services.routing_service import get_assignment_details
    return get_assignment_details(assignment.id, db)


@router.post("/api/offers/{offer_id}/reconfirm")
async def reconfirm_offer_endpoint(
    offer_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Reconfirm an offer to extend its TTL by another 14 days."""
    from app.services.routing_service import reconfirm_offer
    result = reconfirm_offer(offer_id, db)
    if not result["success"]:
        raise HTTPException(400, result["message"])
    db.commit()
    return result


@router.post("/api/admin/reload-routing-maps")
async def reload_routing_maps_endpoint(user: User = Depends(require_user)):
    """Reload brand/commodity and country/region maps from config JSON (admin only)."""
    if not _is_admin(user):
        raise HTTPException(403, "Admin only")
    from app.routing_maps import load_routing_maps, get_brand_commodity_map, get_country_region_map
    try:
        load_routing_maps()
    except Exception as exc:
        log.error(f"Failed to reload routing maps: {exc}")
        raise HTTPException(500, f"Reload failed: {exc}")
    return {
        "status": "reloaded",
        "brands": len(get_brand_commodity_map()),
        "countries": len(get_country_region_map()),
    }
