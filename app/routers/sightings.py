"""sightings.py — Buyer-facing sightings page HTMX endpoints.

Cross-requisition view of all open requirements with vendor status tracking,
batch inquiry workflow, and activity timeline.

Called by: main.py (router mount)
Depends on: models (Requirement, Requisition, Sighting, VendorSightingSummary,
            ActivityLog, VendorCard, Contact, Offer), sighting_status service,
            scoring.py, search_service, email_service, template_env
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..database import get_db
from ..dependencies import require_fresh_token, require_user
from ..models import User
from ..models.intelligence import ActivityLog
from ..models.offers import Offer
from ..models.sourcing import Requirement, Requisition, Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard
from ..services.sighting_status import compute_vendor_statuses
from ..template_env import templates

router = APIRouter(tags=["sightings"])


@router.get("/v2/partials/sightings/workspace", response_class=HTMLResponse)
async def sightings_workspace(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return the split-panel workspace layout.

    The table loads via hx-get inside.
    """
    ctx = {"request": request, "user": user}
    return templates.TemplateResponse("htmx/partials/sightings/list.html", ctx)


@router.get("/v2/partials/sightings", response_class=HTMLResponse)
async def sightings_list(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    status: str = "",
    sales_person: str = "",
    assigned: str = "",  # "mine" or "" for all
    q: str = "",
    group_by: str = "",  # "" (flat), "brand", "manufacturer"
    sort: str = "priority",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Return the sightings table partial with filters and pagination."""
    query = (
        db.query(Requirement)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == "active")
        .options(joinedload(Requirement.requisition))
    )

    # Filters
    if status:
        query = query.filter(Requirement.sourcing_status == status)
    if sales_person:
        query = query.join(User, Requisition.created_by == User.id).filter(User.name.ilike(f"%{sales_person}%"))
    if assigned == "mine":
        query = query.filter(Requirement.assigned_buyer_id == user.id)
    if q:
        query = query.filter(Requirement.primary_mpn.ilike(f"%{q}%") | Requisition.customer_name.ilike(f"%{q}%"))

    # Count before pagination
    total = query.count()

    # Sorting
    sort_map = {
        "priority": Requirement.priority_score.desc().nullslast(),
        "mpn": Requirement.primary_mpn.asc(),
        "created": Requirement.created_at.desc(),
        "status": Requirement.sourcing_status.asc(),
    }
    order = sort_map.get(sort, Requirement.priority_score.desc().nullslast())
    if dir == "asc" and sort in sort_map:
        order = getattr(Requirement, sort if sort != "priority" else "priority_score").asc()
    query = query.order_by(order)

    # Pagination
    offset = (page - 1) * limit
    requirements = query.offset(offset).limit(limit).all()
    total_pages = max(1, (total + limit - 1) // limit)

    # Stat pill counts — lifecycle status counts across ALL active requirements
    stat_counts = dict(
        db.query(Requirement.sourcing_status, sqlfunc.count())
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == "active")
        .group_by(Requirement.sourcing_status)
        .all()
    )

    # Top vendor per requirement (best VendorSightingSummary score)
    top_vendors = {}
    if requirements:
        req_ids = [r.id for r in requirements]
        summaries = (
            db.query(
                VendorSightingSummary.requirement_id,
                VendorSightingSummary.vendor_name,
                VendorSightingSummary.score,
            )
            .filter(VendorSightingSummary.requirement_id.in_(req_ids))
            .order_by(
                VendorSightingSummary.requirement_id,
                VendorSightingSummary.score.desc(),
            )
            .all()
        )
        for s in summaries:
            if s.requirement_id not in top_vendors:
                top_vendors[s.requirement_id] = {
                    "vendor_name": s.vendor_name,
                    "score": s.score,
                }

    # Stale detection — last activity per requirement
    stale_threshold = datetime.now(timezone.utc) - timedelta(days=settings.sighting_stale_days)
    stale_req_ids: set[int] = set()
    if requirements:
        req_ids = [r.id for r in requirements]
        last_activities = (
            db.query(
                ActivityLog.requirement_id,
                sqlfunc.max(ActivityLog.created_at).label("last_at"),
            )
            .filter(ActivityLog.requirement_id.in_(req_ids))
            .group_by(ActivityLog.requirement_id)
            .all()
        )
        activity_map = {a.requirement_id: a.last_at for a in last_activities}
        for rid in req_ids:
            last = activity_map.get(rid)
            if last is None or last < stale_threshold:
                stale_req_ids.add(rid)

    # Group-by logic
    groups = None
    if group_by in ("brand", "manufacturer"):
        from collections import OrderedDict

        groups = OrderedDict()
        for r in requirements:
            key = getattr(r, group_by, "") or "Unknown"
            groups.setdefault(key, []).append(r)

    ctx = {
        "request": request,
        "requirements": requirements,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "limit": limit,
        "status": status,
        "sales_person": sales_person,
        "assigned": assigned,
        "q": q,
        "group_by": group_by,
        "sort": sort,
        "dir": dir,
        "stat_counts": stat_counts,
        "top_vendors": top_vendors,
        "stale_req_ids": stale_req_ids,
        "groups": groups,
        "user": user,
    }
    return templates.TemplateResponse("htmx/partials/sightings/table.html", ctx)


@router.get(
    "/v2/partials/sightings/{requirement_id}/detail",
    response_class=HTMLResponse,
)
async def sightings_detail(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return the detail panel for a single requirement."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    requisition = db.get(Requisition, requirement.requisition_id)

    # Vendor summaries for this requirement
    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc())
        .all()
    )

    # Vendor statuses
    vendor_statuses = compute_vendor_statuses(requirement_id, requirement.requisition_id, db)

    # Pending-review offers
    pending_offers = (
        db.query(Offer)
        .filter(
            Offer.requirement_id == requirement_id,
            Offer.status == "pending_review",
        )
        .all()
    )

    # Vendor phone lookup
    vendor_phones = {}
    for s in summaries:
        if s.vendor_phone:
            vendor_phones[s.vendor_name] = s.vendor_phone
            continue
        card = db.query(VendorCard).filter(VendorCard.normalized_name == s.vendor_name.strip().lower()).first()
        if card and card.phones:
            vendor_phones[s.vendor_name] = card.phones[0] if isinstance(card.phones, list) else card.phones

    # Activity timeline
    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requirement_id == requirement_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    # All users for buyer assignment dropdown
    all_buyers = db.query(User).filter(User.is_active.is_(True)).all()

    ctx = {
        "request": request,
        "requirement": requirement,
        "requisition": requisition,
        "summaries": summaries,
        "vendor_statuses": vendor_statuses,
        "pending_offers": pending_offers,
        "vendor_phones": vendor_phones,
        "activities": activities,
        "all_buyers": all_buyers,
        "user": user,
    }
    return templates.TemplateResponse("htmx/partials/sightings/detail.html", ctx)


@router.post(
    "/v2/partials/sightings/{requirement_id}/refresh",
    response_class=HTMLResponse,
)
async def sightings_refresh(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Re-run search pipeline for a requirement.

    Returns updated detail panel.
    """
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    try:
        from ..search_service import search_requirement

        await search_requirement(requirement, db)
    except Exception:
        logger.warning("Search refresh failed for requirement %s", requirement_id, exc_info=True)

    return await sightings_detail(request, requirement_id, db, user)


@router.post(
    "/v2/partials/sightings/batch-refresh",
    response_class=HTMLResponse,
)
async def sightings_batch_refresh(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Refresh sightings for multiple requirements."""
    import json

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []

    success = 0
    failed = 0
    for rid in requirement_ids:
        req_obj = db.get(Requirement, int(rid))
        if not req_obj:
            failed += 1
            continue
        try:
            from ..search_service import search_requirement

            await search_requirement(req_obj, db)
            success += 1
        except Exception:
            logger.warning("Batch refresh failed for requirement %s", rid, exc_info=True)
            failed += 1

    msg = f"Refreshed {success}/{success + failed} requirements."
    if failed:
        msg += f" {failed} failed."
    return HTMLResponse(
        f'<div hx-swap-oob="true" id="toast-trigger" '
        f"x-init=\"$store.toast.show('{msg}', '{'warning' if failed else 'success'}')\">"
        f"</div>"
    )


@router.post(
    "/v2/partials/sightings/{requirement_id}/mark-unavailable",
    response_class=HTMLResponse,
)
async def sightings_mark_unavailable(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Mark all sightings for a vendor on this requirement as unavailable."""
    form = await request.form()
    vendor_name = form.get("vendor_name", "")
    if not vendor_name:
        raise HTTPException(status_code=400, detail="vendor_name required")

    from ..vendor_utils import normalize_vendor_name

    normalized = normalize_vendor_name(vendor_name)
    sightings = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == requirement_id,
            sqlfunc.lower(sqlfunc.trim(Sighting.vendor_name)) == normalized,
        )
        .all()
    )
    for s in sightings:
        s.is_unavailable = True
    db.commit()

    return await sightings_detail(request, requirement_id, db, user)


@router.patch(
    "/v2/partials/sightings/{requirement_id}/assign",
    response_class=HTMLResponse,
)
async def sightings_assign_buyer(
    request: Request,
    requirement_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Update the assigned buyer for a requirement."""
    form = await request.form()
    buyer_id_str = form.get("assigned_buyer_id", "")
    buyer_id = int(buyer_id_str) if buyer_id_str else None

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    requirement.assigned_buyer_id = buyer_id
    db.commit()

    return await sightings_detail(request, requirement_id, db, user)


@router.get(
    "/v2/partials/sightings/vendor-modal",
    response_class=HTMLResponse,
)
async def sightings_vendor_modal(
    request: Request,
    requirement_ids: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return vendor selection + email compose modal content."""
    req_id_list = [int(x) for x in requirement_ids.split(",") if x.strip().isdigit()]

    requirements = (db.query(Requirement).filter(Requirement.id.in_(req_id_list)).all()) if req_id_list else []

    parts = [
        {
            "mpn": r.primary_mpn,
            "qty": r.target_qty,
            "target_price": float(r.target_price) if r.target_price else None,
        }
        for r in requirements
    ]

    # Suggest vendors: those with sightings for these requirements, ranked by score
    suggested_vendors = (
        (
            db.query(VendorCard)
            .join(
                VendorSightingSummary,
                sqlfunc.lower(sqlfunc.trim(VendorSightingSummary.vendor_name)) == VendorCard.normalized_name,
            )
            .filter(
                VendorSightingSummary.requirement_id.in_(req_id_list),
                VendorCard.is_blacklisted.is_(False),
            )
            .order_by(VendorCard.engagement_score.desc().nullslast())
            .distinct()
            .limit(20)
            .all()
        )
        if req_id_list
        else []
    )

    ctx = {
        "request": request,
        "suggested_vendors": suggested_vendors,
        "requirement_ids": req_id_list,
        "parts": parts,
    }
    return templates.TemplateResponse("htmx/partials/sightings/vendor_modal.html", ctx)


@router.post(
    "/v2/partials/sightings/send-inquiry",
    response_class=HTMLResponse,
)
async def sightings_send_inquiry(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    token: str = Depends(require_fresh_token),
):
    """Send batch RFQ to selected vendors for selected requirements.

    Uses require_fresh_token to get a valid Graph API token for email sending.
    """
    form = await request.form()
    requirement_ids = [int(x) for x in form.getlist("requirement_ids") if x.isdigit()]
    vendor_names = form.getlist("vendor_names")
    email_body = form.get("email_body", "")

    if not requirement_ids or not vendor_names or not email_body:
        raise HTTPException(
            status_code=400,
            detail="requirement_ids, vendor_names, and email_body required",
        )

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()

    # Get requisition for context
    req_ids = {r.requisition_id for r in requirements}
    requisition_id = next(iter(req_ids)) if req_ids else None

    # Build vendor_groups in the format send_batch_rfq expects:
    # [{vendor_name, vendor_email, parts, subject, body}]
    vendor_groups = []
    for vn in vendor_names:
        # Look up vendor email from VendorCard → VendorContact
        card = db.query(VendorCard).filter(VendorCard.normalized_name == vn.strip().lower()).first()
        vendor_email = ""
        if card:
            from ..models.vendors import VendorContact

            contact = db.query(VendorContact).filter(VendorContact.vendor_card_id == card.id).first()
            if contact and contact.email:
                vendor_email = contact.email

        vendor_groups.append(
            {
                "vendor_name": vn,
                "vendor_email": vendor_email,
                "parts": [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements],
                "subject": f"RFQ — {len(requirements)} part{'s' if len(requirements) != 1 else ''}",
                "body": email_body,
            }
        )

    sent_count = 0
    failed_vendors = []
    try:
        from ..email_service import send_batch_rfq

        results = await send_batch_rfq(
            token=token,
            db=db,
            user_id=user.id,
            requisition_id=requisition_id,
            vendor_groups=vendor_groups,
        )
        sent_count = len(results)

        # Log activity per requirement per vendor
        for r in requirements:
            for vn in vendor_names:
                log = ActivityLog(
                    user_id=user.id,
                    activity_type="rfq_sent",
                    channel="email",
                    requisition_id=r.requisition_id,
                    requirement_id=r.id,
                    notes=f"RFQ sent to {vn}",
                )
                db.add(log)
    except Exception:
        logger.warning("RFQ send failed", exc_info=True)
        failed_vendors = vendor_names

    db.commit()

    total = len(vendor_names)
    if failed_vendors:
        msg = f"Sent to {sent_count}/{total} vendors. Failed: {', '.join(failed_vendors)}."
    else:
        msg = f"RFQ sent to {sent_count} vendor{'s' if sent_count != 1 else ''}."

    return HTMLResponse(
        f'<div hx-swap-oob="true" id="toast-trigger" '
        f"x-init=\"$store.toast.show('{msg}', '{'warning' if failed_vendors else 'success'}')\">"
        f"</div>"
    )
