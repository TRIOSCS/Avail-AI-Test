"""sightings.py — Buyer-facing sightings page HTMX endpoints.

Cross-requisition view of all open requirements with vendor status tracking,
batch inquiry workflow, and activity timeline.

Called by: main.py (router mount)
Depends on: models (Requirement, Requisition, Sighting, VendorSightingSummary,
            ActivityLog, VendorCard, Contact, Offer), sighting_status service,
            scoring.py, search_service, email_service, template_env
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..constants import OfferStatus, RequisitionStatus
from ..database import get_db
from ..dependencies import require_fresh_token, require_user
from ..models import User
from ..models.intelligence import ActivityLog
from ..models.offers import Offer
from ..models.sourcing import Requirement, Requisition, Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard, VendorContact
from ..schemas.sightings import SightingsListParams
from ..services.activity_service import log_rfq_activity
from ..services.sighting_status import compute_vendor_statuses
from ..template_env import templates
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["sightings"])

MAX_BATCH_SIZE = 50

_cache: dict[str, tuple[float, Any]] = {}


def _get_cached(key: str, ttl: float, factory):
    """Simple in-process TTL cache.

    For value tuples/dicts only (not ORM objects). Safe because cached results are
    detached column tuples, not session-bound objects.
    """
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    result = factory()
    _cache[key] = (now, result)
    return result


def _invalidate_cache(key: str):
    """Remove a cached entry (call after mutations that change the data)."""
    _cache.pop(key, None)


# Sort key → (column for asc, column for desc)
_SORT_COLUMNS = {
    "priority": (Requirement.priority_score.asc().nullslast(), Requirement.priority_score.desc().nullslast()),
    "mpn": (Requirement.primary_mpn.asc(), Requirement.primary_mpn.desc()),
    "created": (Requirement.created_at.asc(), Requirement.created_at.desc()),
    "status": (Requirement.sourcing_status.asc(), Requirement.sourcing_status.desc()),
}


def _oob_toast(msg: str, level: str = "success") -> HTMLResponse:
    """Return an OOB swap div that triggers a toast notification via Alpine."""
    safe_msg = msg.replace("'", "\\'").replace('"', "&quot;")
    return HTMLResponse(
        f'<div hx-swap-oob="true" id="toast-trigger" x-init="$store.toast.show(\'{safe_msg}\', \'{level}\')"></div>'
    )


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
    filters: SightingsListParams = Depends(),
):
    """Return the sightings table partial with filters and pagination."""
    query = (
        db.query(Requirement)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == RequisitionStatus.ACTIVE)
        .options(joinedload(Requirement.requisition).joinedload(Requisition.creator))
    )

    if filters.status:
        query = query.filter(Requirement.sourcing_status == filters.status)
    if filters.sales_person:
        query = query.join(User, Requisition.created_by == User.id).filter(User.name.ilike(f"%{filters.sales_person}%"))
    if filters.assigned == "mine":
        query = query.filter(Requirement.assigned_buyer_id == user.id)
    if filters.q:
        query = query.filter(
            Requirement.primary_mpn.ilike(f"%{filters.q}%") | Requisition.customer_name.ilike(f"%{filters.q}%")
        )

    total = query.count()

    asc_col, desc_col = _SORT_COLUMNS.get(filters.sort, _SORT_COLUMNS["priority"])
    order = asc_col if filters.dir == "asc" else desc_col
    query = query.order_by(order)

    offset = (filters.page - 1) * filters.limit
    requirements = query.offset(offset).limit(filters.limit).all()
    total_pages = max(1, (total + filters.limit - 1) // filters.limit)

    stat_counts = _get_cached(
        "sightings_stat_counts",
        30,
        lambda: dict(
            db.query(Requirement.sourcing_status, sqlfunc.count())
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(Requisition.status == RequisitionStatus.ACTIVE)
            .group_by(Requirement.sourcing_status)
            .all()
        ),
    )

    top_vendors = {}
    coverage_map = {}
    # Stale threshold uses naive datetime for SQLite compat
    stale_threshold = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=settings.sighting_stale_days)
    stale_req_ids: set[int] = set()

    if requirements:
        req_ids = [r.id for r in requirements]

        # Top vendor per requirement
        summaries = (
            db.query(
                VendorSightingSummary.requirement_id,
                VendorSightingSummary.vendor_name,
                VendorSightingSummary.score,
            )
            .filter(VendorSightingSummary.requirement_id.in_(req_ids))
            .order_by(VendorSightingSummary.requirement_id, VendorSightingSummary.score.desc())
            .all()
        )
        for s in summaries:
            if s.requirement_id not in top_vendors:
                top_vendors[s.requirement_id] = {"vendor_name": s.vendor_name, "score": s.score}

        # Stale detection
        last_activities = (
            db.query(ActivityLog.requirement_id, sqlfunc.max(ActivityLog.created_at).label("last_at"))
            .filter(ActivityLog.requirement_id.in_(req_ids))
            .group_by(ActivityLog.requirement_id)
            .all()
        )
        activity_map = {a.requirement_id: a.last_at for a in last_activities}
        for rid in req_ids:
            last = activity_map.get(rid)
            if last is None:
                stale_req_ids.add(rid)
            elif last.replace(tzinfo=None) < stale_threshold:
                stale_req_ids.add(rid)

        # Fulfillment coverage per requirement (Phase 2)
        coverage_rows = (
            db.query(
                VendorSightingSummary.requirement_id,
                sqlfunc.sum(VendorSightingSummary.estimated_qty).label("total_qty"),
            )
            .filter(VendorSightingSummary.requirement_id.in_(req_ids))
            .group_by(VendorSightingSummary.requirement_id)
            .all()
        )
        coverage_map = {c.requirement_id: c.total_qty or 0 for c in coverage_rows}

    # ── Dashboard Strip Counters (Phase 2) ──────────────────────────
    from datetime import date

    deadline_48h = date.today() + timedelta(days=2)

    # Active requirement IDs for dashboard (not just current page)
    active_req_select = (
        db.query(Requirement.id)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status == RequisitionStatus.ACTIVE)
    )

    # Urgent: priority >= 70 OR need_by_date within 48h
    urgent_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(
            Requirement.id.in_(active_req_select.subquery().select()),
            (Requirement.priority_score >= 70) | (Requirement.need_by_date <= deadline_48h),
        )
        .scalar()
    ) or 0

    # Stale: no ActivityLog within sighting_stale_days
    stale_select = (
        db.query(ActivityLog.requirement_id)
        .filter(ActivityLog.requirement_id.isnot(None))
        .group_by(ActivityLog.requirement_id)
        .having(sqlfunc.max(ActivityLog.created_at) >= stale_threshold)
    )
    stale_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(
            Requirement.id.in_(active_req_select.subquery().select()),
            ~Requirement.id.in_(stale_select.subquery().select()),
        )
        .scalar()
    ) or 0

    # Pending: has at least one offer with status pending_review
    pending_count = (
        db.query(sqlfunc.count(sqlfunc.distinct(Offer.requirement_id)))
        .filter(
            Offer.requirement_id.in_(active_req_select.subquery().select()),
            Offer.status == OfferStatus.PENDING_REVIEW,
        )
        .scalar()
    ) or 0

    # Unassigned: assigned_buyer_id IS NULL
    unassigned_count = (
        db.query(sqlfunc.count(Requirement.id))
        .filter(
            Requirement.id.in_(active_req_select.subquery().select()),
            Requirement.assigned_buyer_id.is_(None),
        )
        .scalar()
    ) or 0

    dashboard_counters = {
        "urgent": urgent_count,
        "stale": stale_count,
        "pending": pending_count,
        "unassigned": unassigned_count,
    }

    # ── Heatmap Row Set (Phase 2) ─────────────────────────────────
    # Rose tint for: near deadline (48h), high-priority stale, critical/hot urgency
    heatmap_req_ids: set[int] = set()
    if requirements:
        for r in requirements:
            # Near deadline
            if r.need_by_date and r.need_by_date <= deadline_48h:
                heatmap_req_ids.add(r.id)
                continue
            # Stale AND medium+ priority
            if r.id in stale_req_ids and (r.priority_score or 0) >= 40:
                heatmap_req_ids.add(r.id)
                continue
            # Critical/hot urgency (from requisition)
            urgency = getattr(r.requisition, "urgency", None) or ""
            if urgency in ("critical", "hot"):
                heatmap_req_ids.add(r.id)

    groups = None
    if filters.group_by in ("brand", "manufacturer"):
        groups: dict[str, list] = {}
        for r in requirements:
            key = getattr(r, filters.group_by, "") or "Unknown"
            groups.setdefault(key, []).append(r)

    ctx = {
        "request": request,
        "requirements": requirements,
        "total": total,
        "page": filters.page,
        "total_pages": total_pages,
        "limit": filters.limit,
        "status": filters.status,
        "sales_person": filters.sales_person,
        "assigned": filters.assigned,
        "q": filters.q,
        "group_by": filters.group_by,
        "sort": filters.sort,
        "dir": filters.dir,
        "stat_counts": stat_counts,
        "top_vendors": top_vendors,
        "stale_req_ids": stale_req_ids,
        "coverage_map": coverage_map,
        "dashboard_counters": dashboard_counters,
        "heatmap_req_ids": heatmap_req_ids,
        "groups": groups,
        "user": user,
    }
    return templates.TemplateResponse("htmx/partials/sightings/table.html", ctx)


@router.get("/v2/partials/sightings/{requirement_id}/detail", response_class=HTMLResponse)
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

    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        .order_by(VendorSightingSummary.score.desc())
        .all()
    )

    # Pass pre-fetched vendor names to avoid redundant VendorSightingSummary query
    vendor_statuses = compute_vendor_statuses(
        requirement_id, requirement.requisition_id, db, vendor_names=[s.vendor_name for s in summaries]
    )

    pending_offers = (
        db.query(Offer).filter(Offer.requirement_id == requirement_id, Offer.status == OfferStatus.PENDING_REVIEW).all()
    )

    # ── Vendor Intelligence (Phase 3) ─────────────────────────────
    from ..scoring import explain_lead

    normalized_names = [normalize_vendor_name(s.vendor_name) for s in summaries]

    # Single batch query for VendorCards — piggybacks phone + intelligence
    cards = (
        (db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all())
        if normalized_names
        else []
    )
    card_map = {c.normalized_name: c for c in cards}

    # Build vendor_phones (backward compat) + vendor_intel map
    vendor_phones = {s.vendor_name: s.vendor_phone for s in summaries if s.vendor_phone}
    vendor_intel: dict[str, dict] = {}

    for s in summaries:
        norm = normalize_vendor_name(s.vendor_name)
        card = card_map.get(norm)

        # Phone fallback from card
        if s.vendor_name not in vendor_phones and card and card.phones:
            phone = card.phones[0] if isinstance(card.phones, list) else card.phones
            if phone:
                vendor_phones[s.vendor_name] = phone

        # Intelligence fields
        age_days = None
        if s.newest_sighting_at:
            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - s.newest_sighting_at).days

        lead_explanation = explain_lead(
            vendor_name=s.vendor_name,
            is_authorized=False,
            vendor_score=card.vendor_score if card else None,
            unit_price=s.best_price,
            median_price=None,
            qty_available=s.estimated_qty,
            target_qty=requirement.target_qty,
            has_contact=s.has_contact_info or bool(vendor_phones.get(s.vendor_name)),
            evidence_tier=s.tier,
            source_type=(s.source_types[0] if s.source_types and isinstance(s.source_types, list) else None),
            age_days=age_days,
        )

        vendor_intel[s.vendor_name] = {
            "response_rate": card.response_rate if card else None,
            "ghost_rate": card.ghost_rate if card else None,
            "vendor_score": card.vendor_score if card else None,
            "engagement_score": card.engagement_score if card else None,
            "avg_response_hours": card.avg_response_hours if card else None,
            "explain_lead": lead_explanation,
            "listing_count": s.listing_count,
            "source_types": s.source_types or [],
            "tier": s.tier,
            "best_lead_time_days": s.best_lead_time_days,
            "min_moq": s.min_moq,
            "newest_sighting_at": s.newest_sighting_at,
            "age_days": age_days,
        }

    # ── OOO Contact Detection (Phase 3) ──────────────────────────
    ooo_map: dict[str, VendorContact] = {}
    if normalized_names:
        contacts_with_ooo = (
            db.query(VendorContact)
            .join(VendorCard, VendorContact.vendor_card_id == VendorCard.id)
            .filter(
                VendorCard.normalized_name.in_(normalized_names),
                VendorContact.is_ooo.is_(True),
            )
            .all()
        )
        # Build id-keyed map for contact->card resolution
        card_id_map = {c.id: c for c in cards} if cards else {}
        for c in contacts_with_ooo:
            # Map by normalized vendor name for template lookup
            vc = card_id_map.get(c.vendor_card_id)
            if vc:
                ooo_map[vc.normalized_name] = c

    # ── Suggested Next Action (Phase 3) ──────────────────────────
    status = requirement.sourcing_status or "open"
    vendor_count = len(summaries)
    pending_count_detail = len(pending_offers)

    if status == "open" and vendor_count > 0:
        suggested_action = f"{vendor_count} vendor{'s' if vendor_count != 1 else ''} available — send RFQs"
    elif status == "open" and vendor_count == 0:
        suggested_action = "No vendors found — run search"
    elif status == "sourcing":
        # Check days since last RFQ activity
        last_rfq = (
            db.query(sqlfunc.max(ActivityLog.created_at))
            .filter(
                ActivityLog.requirement_id == requirement_id,
                ActivityLog.activity_type == "rfq_sent",
            )
            .scalar()
        )
        if last_rfq:
            days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - last_rfq).days
            if days_since > 3:
                suggested_action = f"RFQs pending for {days_since} days — follow up"
            else:
                suggested_action = "RFQs sent — awaiting vendor responses"
        else:
            suggested_action = "Status is sourcing but no RFQs sent — send RFQs"
    elif status == "offered" and pending_count_detail > 0:
        suggested_action = f"{pending_count_detail} offer{'s' if pending_count_detail != 1 else ''} received — review and accept/reject"
    elif status == "offered":
        suggested_action = "Offers reviewed — advance to quoted when ready"
    elif status == "quoted":
        suggested_action = "Quote sent — awaiting customer response"
    elif status == "won":
        suggested_action = "Order won — proceed to fulfillment"
    else:
        suggested_action = None

    # ── MaterialCard Enrichment (Phase 3) ─────────────────────────
    from ..models.intelligence import MaterialCard

    material_card = None
    if requirement.material_card_id:
        material_card = db.get(MaterialCard, requirement.material_card_id)

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requirement_id == requirement_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    all_buyers = _get_cached(
        "all_buyers", 300, lambda: db.query(User.id, User.name).filter(User.is_active.is_(True)).all()
    )

    ctx = {
        "request": request,
        "requirement": requirement,
        "requisition": requisition,
        "summaries": summaries,
        "vendor_statuses": vendor_statuses,
        "pending_offers": pending_offers,
        "vendor_phones": vendor_phones,
        "vendor_intel": vendor_intel,
        "ooo_map": ooo_map,
        "suggested_action": suggested_action,
        "material_card": material_card,
        "activities": activities,
        "all_buyers": all_buyers,
        "user": user,
    }
    return templates.TemplateResponse("htmx/partials/sightings/detail.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/refresh", response_class=HTMLResponse)
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


@router.post("/v2/partials/sightings/batch-refresh", response_class=HTMLResponse)
async def sightings_batch_refresh(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Refresh sightings for multiple requirements."""
    from ..search_service import search_requirement

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    # Batch-fetch all requirements in one query
    reqs_by_id = {}
    if requirement_ids:
        reqs = db.query(Requirement).filter(Requirement.id.in_([int(rid) for rid in requirement_ids])).all()
        reqs_by_id = {r.id: r for r in reqs}

    success = 0
    failed = 0
    for rid in requirement_ids:
        req_obj = reqs_by_id.get(int(rid))
        if not req_obj:
            failed += 1
            continue
        try:
            await search_requirement(req_obj, db)
            success += 1
        except Exception:
            logger.warning("Batch refresh failed for requirement %s", rid, exc_info=True)
            failed += 1

    msg = f"Refreshed {success}/{success + failed} requirements."
    if failed:
        msg += f" {failed} failed."
    return _oob_toast(msg, "warning" if failed else "success")


@router.post("/v2/partials/sightings/{requirement_id}/mark-unavailable", response_class=HTMLResponse)
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


@router.patch("/v2/partials/sightings/{requirement_id}/assign", response_class=HTMLResponse)
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


@router.get("/v2/partials/sightings/vendor-modal", response_class=HTMLResponse)
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


@router.post("/v2/partials/sightings/send-inquiry", response_class=HTMLResponse)
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

    requisition_ids = {r.requisition_id for r in requirements}
    requisition_id = next(iter(requisition_ids)) if requisition_ids else None

    # Batch-fetch vendor cards + contacts in two queries instead of N+1
    normalized_names = [normalize_vendor_name(vn) for vn in vendor_names]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id.in_(card_ids)).all() if card_ids else []
    contact_map = {c.vendor_card_id: c for c in contacts}

    vendor_groups = []
    for vn in vendor_names:
        card = card_map.get(normalize_vendor_name(vn))
        vendor_email = ""
        if card:
            contact = contact_map.get(card.id)
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

        for r in requirements:
            for vn in vendor_names:
                log_rfq_activity(
                    db=db,
                    rfq_id=r.requisition_id,
                    activity_type="rfq_sent",
                    description=f"RFQ sent to {vn}",
                    user_id=user.id,
                    requirement_id=r.id,
                )
    except Exception:
        logger.warning("RFQ send failed", exc_info=True)
        failed_vendors = vendor_names

    db.commit()

    total = len(vendor_names)
    if failed_vendors:
        msg = f"Sent to {sent_count}/{total} vendors. Failed: {', '.join(failed_vendors)}."
    else:
        msg = f"RFQ sent to {sent_count} vendor{'s' if sent_count != 1 else ''}."

    return _oob_toast(msg, "warning" if failed_vendors else "success")
