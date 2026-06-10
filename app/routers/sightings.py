"""sightings.py — Buyer-facing sightings page HTMX endpoints.

Cross-requisition view of all open requirements with vendor status tracking,
batch inquiry workflow, and activity timeline.

Called by: main.py (router mount)
Depends on: models (Requirement, Requisition, Sighting, VendorSightingSummary,
            ActivityLog, VendorCard, Contact, Offer, VendorPartUnavailability via
            the vendor_unavailability service), sighting_status service,
            vendor_unavailability service (mark/clear/intel/RFQ exclusion/offer-hook
            release), scoring.py, search_service, email_service, template_env
"""

import asyncio
import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..constants import (
    ActivityType,
    OfferStatus,
    RequisitionStatus,
    SourcingStatus,
    UnavailabilityReason,
)
from ..database import get_db
from ..dependencies import require_buyer, require_fresh_token, require_user
from ..models import User
from ..models.intelligence import ActivityLog, MaterialCard
from ..models.offers import Offer
from ..models.sourcing import Requirement, Requisition, Sighting
from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard, VendorContact
from ..schemas.sightings import SightingsListParams
from ..services.activity_service import log_rfq_activity
from ..services.part_offers import part_offers_for
from ..services.sighting_status import compute_vendor_statuses
from ..services.sse_broker import broker
from ..services.status_machine import SOURCING_TRANSITIONS, require_valid_transition
from ..services.vendor_unavailability import (
    clear_unavailability,
    excluded_vendor_norms,
    record_unavailability,
    release_on_offer,
    sighting_vendor_norm,
    unavailability_for_requirement,
)
from ..template_env import template_response
from ..utils import safe_float, safe_int
from ..utils.sql_helpers import escape_like
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["sightings"])

MAX_BATCH_SIZE: Final[int] = 50
_EXCLUDED_REQ_STATUSES: Final = (RequisitionStatus.ARCHIVED, RequisitionStatus.CANCELLED)

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
        f'<div hx-swap-oob="true" id="toast-trigger"'
        f" x-init=\"$store.toast.message='{safe_msg}';"
        f"$store.toast.type='{level}';"
        f'$store.toast.show=true"></div>'
    )


# Result headers on POST /v2/partials/sightings/send-inquiry: the route returns HTTP 200
# even on a partial/total send failure, so the browser modal reads the true delivered
# count from these rather than inferring success from the status code.
RFQ_SENT_HEADER = "X-RFQ-Sent"
RFQ_TOTAL_HEADER = "X-RFQ-Total"
RFQ_SKIPPED_HEADER = "X-RFQ-Skipped"  # vendors with no contact email (not a delivery failure)
RFQ_UNAVAILABLE_HEADER = "X-RFQ-Unavailable"  # vendors dropped by the active-only unavailability re-check


def _render_offers_panel(request: Request, requirement: Requirement, db: Session) -> HTMLResponse:
    """Render the part-centric Offers panel for swap into #sightings-offers-panel."""
    ctx = {
        "request": request,
        "requirement": requirement,
        "part_offers": part_offers_for(requirement, db),
    }
    resp = template_response("htmx/partials/sightings/offers_panel.html", ctx)
    resp.headers["X-Rendered-Req-Id"] = str(requirement.id)
    return resp


def _with_toast(resp: HTMLResponse, msg: str, level: str = "success") -> HTMLResponse:
    """Attach the `showToast` HX-Trigger to an HTMX response (the same toast trigger
    this router already emits via HX-Trigger elsewhere)."""
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg, "type": level}})
    return resp


async def _publish_if_user_source(source: str, user_id: int, requirement_id: int) -> None:
    """Publish sighting-updated SSE only when the caller is a human user.

    Skips publish when source == 'sse' to prevent self-trigger loops.
    """
    if source != "sse":
        await broker.publish(
            f"user:{user_id}",
            "sighting-updated",
            json.dumps({"requirement_id": requirement_id}),
        )


def _toast_suppressed_for_sse(source: str) -> bool:
    """Return True when the caller is an SSE-triggered request."""
    return source == "sse"


def _annotated_unavailability(
    db: Session, requirement: Requirement, vendor_names: list[str]
) -> dict[str, dict[str, Any]]:
    """Template-facing unavailability intel: vendor display name → plain dict.

    Wraps unavailability_for_requirement() with the small render-only enrichments the
    three-state vendor-row UI needs (reason label, marker name, has_unstamped_row for
    the rows-win state selection) so the template stays dumb and never re-derives
    policy. Vendors with no matching record are absent.
    """
    raw = unavailability_for_requirement(db, requirement, vendor_names)
    if not raw:
        return {}
    rows = db.query(Sighting).filter(Sighting.requirement_id == requirement.id).all()
    unstamped_norms = {sighting_vendor_norm(s) for s in rows if not s.is_unavailable}
    creator_ids = {i.record.created_by_id for i in raw.values() if i.record.created_by_id is not None}
    creator_names: dict[int, str] = (
        dict(db.query(User.id, User.name).filter(User.id.in_(creator_ids)).all()) if creator_ids else {}
    )
    annotated: dict[str, dict[str, Any]] = {}
    for vendor_name, item in raw.items():
        rec = item.record
        annotated[vendor_name] = {
            "is_active": item.is_active,
            "age_days": item.age_days,
            "release_trigger": item.release_trigger,
            "reason": rec.reason,
            "reason_label": UnavailabilityReason(rec.reason).label,
            "note": rec.note,
            "qty_at_mark": rec.qty_at_mark,
            "marked_by": creator_names.get(rec.created_by_id),
            "has_unstamped_row": normalize_vendor_name(vendor_name) in unstamped_norms,
        }
    return annotated


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
    return template_response("htmx/partials/sightings/list.html", ctx)


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
        .filter(Requisition.status.notin_(_EXCLUDED_REQ_STATUSES))
        .options(joinedload(Requirement.requisition).joinedload(Requisition.creator))
    )

    if filters.status:
        query = query.filter(Requirement.sourcing_status == filters.status)
    if filters.sales_person:
        safe = escape_like(filters.sales_person)
        query = query.join(User, Requisition.created_by == User.id).filter(User.name.ilike(f"%{safe}%"))
    if filters.assigned == "mine":
        query = query.filter(Requirement.assigned_buyer_id == user.id)
    if filters.q:
        safe_q = escape_like(filters.q)
        query = query.filter(
            Requirement.primary_mpn.ilike(f"%{safe_q}%")
            | Requisition.customer_name.ilike(f"%{safe_q}%")
            | Requirement.substitutes_text.ilike(f"%{safe_q}%")
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
            .filter(Requisition.status.notin_(_EXCLUDED_REQ_STATUSES))
            .group_by(Requirement.sourcing_status)
            .all()
        ),
    )

    top_vendors = {}
    coverage_map = {}
    # Stale threshold is UTC-aware; UTCDateTime columns read back aware too
    stale_threshold = datetime.now(timezone.utc) - timedelta(days=settings.sighting_stale_days)
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
            elif last < stale_threshold:
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
        .filter(Requisition.status.notin_(_EXCLUDED_REQ_STATUSES))
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

    # ── MPN → MaterialCard link map ─────────────────────────────
    link_map: dict[str, int] = {}
    if requirements:
        from ..utils.normalization import normalize_mpn_key

        all_mpns: set[str] = set()
        for r in requirements:
            if r.primary_mpn:
                all_mpns.add(r.primary_mpn.upper())
            for sub in r.substitutes or []:
                mpn = sub.get("mpn") if isinstance(sub, dict) else sub
                if mpn:
                    all_mpns.add(str(mpn).upper())
        if all_mpns:
            norm_to_display: dict[str, str] = {}
            for mpn in all_mpns:
                n = normalize_mpn_key(mpn)
                if n:
                    norm_to_display[n] = mpn
            cards = (
                db.query(MaterialCard.id, MaterialCard.normalized_mpn)
                .filter(
                    MaterialCard.normalized_mpn.in_(list(norm_to_display.keys())),
                    MaterialCard.deleted_at.is_(None),
                )
                .all()
            )
            for card_id, norm in cards:
                display = norm_to_display.get(norm)
                if display:
                    link_map[display] = card_id

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
        "link_map": link_map,
        "user": user,
    }
    return template_response("htmx/partials/sightings/table.html", ctx)


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
            age_days = (datetime.now(timezone.utc) - s.newest_sighting_at).days

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
    status = requirement.sourcing_status or SourcingStatus.OPEN
    vendor_count = len(summaries)
    pending_count_detail = len(pending_offers)

    if status == SourcingStatus.OPEN and vendor_count > 0:
        suggested_action = f"{vendor_count} vendor{'s' if vendor_count != 1 else ''} available — send RFQs"
    elif status == SourcingStatus.OPEN and vendor_count == 0:
        suggested_action = "No vendors found — run search"
    elif status == SourcingStatus.SOURCING:
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
            days_since = (datetime.now(timezone.utc) - last_rfq).days
            if days_since > 3:
                suggested_action = f"RFQs pending for {days_since} days — follow up"
            else:
                suggested_action = "RFQs sent — awaiting vendor responses"
        else:
            suggested_action = "Status is sourcing but no RFQs sent — send RFQs"
    elif status == SourcingStatus.OFFERED and pending_count_detail > 0:
        suggested_action = f"{pending_count_detail} offer{'s' if pending_count_detail != 1 else ''} received — review and accept/reject"
    elif status == SourcingStatus.OFFERED:
        suggested_action = "Offers reviewed — advance to quoted when ready"
    elif status == SourcingStatus.QUOTED:
        suggested_action = "Quote sent — awaiting customer response"
    elif status == SourcingStatus.WON:
        suggested_action = "Order won — proceed to fulfillment"
    else:
        suggested_action = None

    # ── MaterialCard Enrichment (Phase 3) ─────────────────────────
    material_card = None
    if requirement.material_card_id:
        material_card = db.get(MaterialCard, requirement.material_card_id)

    # ── MPN → MaterialCard link map for detail header ────────────
    from ..utils.normalization import normalize_mpn_key as _norm_key

    detail_link_map: dict[str, int] = {}
    detail_mpns: set[str] = set()
    if requirement.primary_mpn:
        detail_mpns.add(requirement.primary_mpn.upper())
    for sub in requirement.substitutes or []:
        mpn = sub.get("mpn") if isinstance(sub, dict) else sub
        if mpn:
            detail_mpns.add(str(mpn).upper())
    if detail_mpns:
        norm_to_display: dict[str, str] = {}
        for mpn in detail_mpns:
            n = _norm_key(mpn)
            if n:
                norm_to_display[n] = mpn
        detail_cards = (
            db.query(MaterialCard.id, MaterialCard.normalized_mpn)
            .filter(
                MaterialCard.normalized_mpn.in_(list(norm_to_display.keys())),
                MaterialCard.deleted_at.is_(None),
            )
            .all()
        )
        for card_id, norm in detail_cards:
            display = norm_to_display.get(norm)
            if display:
                detail_link_map[display] = card_id

    # ── Cross-Requirement Vendor Overlap (Phase 4.7) ────────────
    overlap_counts: dict[str, int] = dict(
        db.query(
            VendorSightingSummary.vendor_name,
            sqlfunc.count(sqlfunc.distinct(VendorSightingSummary.requirement_id)),
        )
        .join(Requirement, VendorSightingSummary.requirement_id == Requirement.id)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status.notin_(_EXCLUDED_REQ_STATUSES))
        .group_by(VendorSightingSummary.vendor_name)
        .having(sqlfunc.count(sqlfunc.distinct(VendorSightingSummary.requirement_id)) > 1)
        .all()
    )

    # ── Vendor Matched MPNs (substitute visibility) ──────────────
    matched_rows = (
        db.query(Sighting.vendor_name, Sighting.mpn_matched)
        .filter(
            Sighting.requirement_id == requirement_id,
            Sighting.mpn_matched.isnot(None),
        )
        .distinct()
        .all()
    )
    vendor_matched_mpns: dict[str, list[str]] = {}
    for vendor_name, mpn in matched_rows:
        vendor_matched_mpns.setdefault(vendor_name, []).append(mpn)

    # ── Durable vendor+part unavailability intel (three-state row UI) ───
    unavailable_intel = _annotated_unavailability(db, requirement, [s.vendor_name for s in summaries])

    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requirement_id == requirement_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    # ── Available Status Transitions (Phase 4.3) ────────────────
    available_statuses = sorted(SOURCING_TRANSITIONS.get(status, set()))

    # Part-centric offers for the Offers tab (primary + substitute MPNs, any req).
    part_offers = part_offers_for(requirement, db)

    ctx = {
        "request": request,
        "requirement": requirement,
        "requisition": requisition,
        "summaries": summaries,
        "vendor_statuses": vendor_statuses,
        "pending_offers": pending_offers,
        "part_offers": part_offers,
        "vendor_phones": vendor_phones,
        "vendor_intel": vendor_intel,
        "ooo_map": ooo_map,
        "overlap_counts": overlap_counts,
        "vendor_matched_mpns": vendor_matched_mpns,
        "unavailable_intel": unavailable_intel,
        "suggested_action": suggested_action,
        "available_statuses": available_statuses,
        "material_card": material_card,
        "link_map": detail_link_map,
        "activities": activities,
        "user": user,
    }
    resp = template_response("htmx/partials/sightings/detail.html", ctx)
    resp.headers["X-Rendered-Req-Id"] = str(requirement_id)
    return resp


@router.post("/v2/partials/sightings/{requirement_id}/refresh", response_class=HTMLResponse)
async def sightings_refresh(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Re-run sourcing pipeline for a requirement, gated by 48h per-MPN cooldown.

    Returns the rendered detail panel + HX-Trigger toast describing per-MPN result.
    """
    from ..search_service import search_requirement

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    is_sse = source == "sse"
    refresh_failed = False
    mpn_results: dict[str, str] = {}
    try:
        result = await search_requirement(requirement, db)
        mpn_results = result.get("mpn_results", {})
    except Exception:
        logger.warning("Search refresh failed for requirement {}", requirement_id, exc_info=True)
        refresh_failed = True

    # Force a fresh read; search_requirement uses a separate write session.
    db.expire(requirement)

    await _publish_if_user_source(source, user.id, requirement_id)

    response = await sightings_detail(request, requirement_id, db, user)

    if not is_sse:
        toast_msg = _build_mpn_toast(mpn_results, refresh_failed)
        toast_type = (
            "warning" if refresh_failed else ("info" if all(v == "cached" for v in mpn_results.values()) else "success")
        )
        if toast_msg:
            response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": toast_msg, "type": toast_type}})
    return response


def _build_mpn_toast(mpn_results: dict[str, str], refresh_failed: bool) -> str:
    """Build the per-MPN toast message from search_requirement's result map."""
    if refresh_failed:
        return "Search refresh failed - showing cached results"
    if not mpn_results:
        return ""
    searched = sum(1 for v in mpn_results.values() if v == "searched")
    cached = sum(1 for v in mpn_results.values() if v == "cached")
    if searched and cached:
        return f"Searched {searched} MPN{'s' if searched != 1 else ''}, {cached} cached"
    if searched:
        return f"Searched {searched} MPN{'s' if searched != 1 else ''}"
    return "All MPNs searched within 48h - showing cached"


@router.post("/v2/partials/sightings/batch-refresh", response_class=HTMLResponse)
async def sightings_batch_refresh(
    request: Request,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Refresh sightings for multiple requirements.

    Per-MPN 48h cooldown is enforced inside search_requirement
    (MaterialCard.last_searched_at). source="sse" suppresses the OOB toast and
    broker.publish to prevent self-trigger loops.
    """
    from ..search_service import search_requirement

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    try:
        requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
        if not isinstance(requirement_ids, list):
            requirement_ids = []
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid requirement_ids format")

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    # Batch-fetch all requirements in one query
    reqs_by_id = {}
    if requirement_ids:
        reqs = db.query(Requirement).filter(Requirement.id.in_([int(rid) for rid in requirement_ids])).all()
        reqs_by_id = {r.id: r for r in reqs}

    success = 0
    failed = 0

    # Build the list of requirements that exist. The 48h per-MPN cooldown
    # is enforced inside search_requirement (MaterialCard.last_searched_at)
    # so we no longer pre-filter by requirement-level last_searched_at.
    to_search: list[tuple[int, Requirement]] = []
    for rid in requirement_ids:
        req_obj = reqs_by_id.get(int(rid))
        if not req_obj:
            failed += 1
            continue
        to_search.append((int(rid), req_obj))

    # Fan out. search_requirement() uses its own write session for the
    # sightings / material-card / last_searched_at writes (commit
    # 55093bf1); the caller's db is still touched by _fetch_fresh for
    # ApiSource stats, which tolerates occasional concurrent-session
    # errors via rollback. Same concurrency model as the existing
    # caller in routers/requisitions/requirements.py. return_exceptions
    # ensures one failing search does not cancel the rest.
    if to_search:
        results = await asyncio.gather(
            *(search_requirement(req_obj, db) for _, req_obj in to_search),
            return_exceptions=True,
        )
        for (rid, _), outcome in zip(to_search, results):
            if isinstance(outcome, Exception):
                logger.warning("Batch refresh failed for requirement {}", rid, exc_info=outcome)
                failed += 1
            else:
                success += 1

    # Notify all connected clients that these requirements changed
    for rid in requirement_ids:
        await _publish_if_user_source(source, user.id, int(rid))

    parts = []
    total = success + failed
    parts.append(f"Searched {success}/{total} requirements.")
    if failed:
        parts.append(f"{failed} failed.")
    msg = " ".join(parts)
    if failed:
        level = "warning"
    else:
        level = "success"
    if _toast_suppressed_for_sse(source):
        return HTMLResponse("")
    return _oob_toast(msg, level)


@router.post("/v2/partials/sightings/batch-assign", response_class=HTMLResponse)
async def sightings_batch_assign(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Batch-assign a buyer to multiple requirements."""
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
    buyer_id_str = form.get("buyer_id", "")
    buyer_id = int(buyer_id_str) if buyer_id_str else None

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    if not requirement_ids:
        return _oob_toast("No requirements selected", "warning")

    int_ids = [int(rid) for rid in requirement_ids]
    reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()

    buyer_name = "nobody"
    if buyer_id:
        buyer = db.get(User, buyer_id)
        buyer_name = buyer.name if buyer else f"user {buyer_id}"

    for r in reqs:
        r.assigned_buyer_id = buyer_id
    db.commit()

    _invalidate_cache("sightings_stat_counts")
    msg = f"Assigned {len(reqs)} requirement{'s' if len(reqs) != 1 else ''} to {buyer_name}"
    return _oob_toast(msg)


@router.post("/v2/partials/sightings/batch-status", response_class=HTMLResponse)
async def sightings_batch_status(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Batch-update sourcing status on multiple requirements with transition
    validation."""
    from ..services.status_machine import validate_transition

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
    new_status = form.get("status", "")

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    if not requirement_ids:
        return _oob_toast("No requirements selected", "warning")

    try:
        SourcingStatus(new_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}")

    int_ids = [int(rid) for rid in requirement_ids]
    reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()

    updated = 0
    skipped = 0
    for r in reqs:
        current = r.sourcing_status or "open"
        if validate_transition("requirement", current, new_status, raise_on_invalid=False):
            old_status = r.sourcing_status
            r.sourcing_status = new_status
            activity = ActivityLog(
                user_id=user.id,
                activity_type=ActivityType.STATUS_CHANGED,
                channel="system",
                requirement_id=r.id,
                requisition_id=r.requisition_id,
                notes=f"Status changed from {old_status} to {new_status} (batch)",
            )
            db.add(activity)
            updated += 1
        else:
            skipped += 1

    db.commit()
    _invalidate_cache("sightings_stat_counts")

    total = updated + skipped
    msg = f"Updated {updated} of {total} requirement{'s' if total != 1 else ''}."
    if skipped:
        msg += f" {skipped} skipped (invalid transition)."
    level = "success" if skipped == 0 else "warning"
    return _oob_toast(msg, level)


@router.post("/v2/partials/sightings/batch-notes", response_class=HTMLResponse)
async def sightings_batch_notes(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Add a note to multiple requirements."""
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
    notes = form.get("notes", "").strip()

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    if not requirement_ids:
        return _oob_toast("No requirements selected", "warning")

    if not notes:
        return _oob_toast("Note text is required", "warning")

    int_ids = [int(rid) for rid in requirement_ids]
    reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()

    for r in reqs:
        activity = ActivityLog(
            user_id=user.id,
            activity_type="note",
            channel="manual",
            requirement_id=r.id,
            requisition_id=r.requisition_id,
            notes=notes,
        )
        db.add(activity)

    db.commit()

    count = len(reqs)
    msg = f"Added note to {count} requirement{'s' if count != 1 else ''}"
    return _oob_toast(msg)


@router.get("/v2/partials/sightings/{requirement_id}/unavailable-form", response_class=HTMLResponse)
async def sightings_unavailable_form(
    request: Request,
    requirement_id: int,
    vendor_name: str = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Reason modal for mark-unavailable, served through the open-modal dispatch.

    Also the verify/re-arm affordance for the advisory + restock row states: when a
    record already exists it shows "Currently marked" and carries BOTH actions — submit
    re-arms (upsert refresh), "It's back" POSTs mark-available. There is NO separate
    verify endpoint.
    """
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")
    current = _annotated_unavailability(db, requirement, [vendor_name]).get(vendor_name)
    ctx = {
        "request": request,
        "requirement": requirement,
        "vendor_name": vendor_name,
        "reasons": list(UnavailabilityReason),
        "current": current,
    }
    return template_response("htmx/partials/sightings/unavailable_form.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/mark-unavailable", response_class=HTMLResponse)
async def sightings_mark_unavailable(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Durably mark a vendor's stock of this requirement's part(s) as unavailable.

    Requires a validated reason (+ optional note) and delegates entirely to
    record_unavailability (upsert per MPN key, normalized sighting re-stamp,
    ActivityLog). Re-POSTing for an already-marked vendor is the re-arm path. The
    service's ValueErrors (zero derivable keys / empty vendor norm) map to a 400 JSON
    error with nothing written.
    """
    form = await request.form()
    vendor_name = str(form.get("vendor_name") or "").strip()
    if not vendor_name:
        raise HTTPException(status_code=400, detail="vendor_name required")
    try:
        reason = UnavailabilityReason(str(form.get("reason") or ""))
    except ValueError:
        valid = ", ".join(m.value for m in UnavailabilityReason)
        raise HTTPException(status_code=400, detail=f"reason is required and must be one of: {valid}")
    note = str(form.get("note") or "") or None

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    try:
        record_unavailability(db, requirement, vendor_name, reason, note, user)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()

    await _publish_if_user_source(source, user.id, requirement_id)

    return await sightings_detail(request, requirement_id, db, user)


@router.post("/v2/partials/sightings/{requirement_id}/mark-available", response_class=HTMLResponse)
async def sightings_mark_available(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Undo a mark: delete the vendor's unavailability records and unflag its
    sightings (clear_unavailability), then re-render the detail panel."""
    form = await request.form()
    vendor_name = str(form.get("vendor_name") or "").strip()
    if not vendor_name:
        raise HTTPException(status_code=400, detail="vendor_name required")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    try:
        clear_unavailability(db, requirement, vendor_name, user)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()

    await _publish_if_user_source(source, user.id, requirement_id)

    return await sightings_detail(request, requirement_id, db, user)


@router.patch("/v2/partials/sightings/{requirement_id}/assign", response_class=HTMLResponse)
async def sightings_assign_buyer(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
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

    await _publish_if_user_source(source, user.id, requirement_id)

    return await sightings_detail(request, requirement_id, db, user)


@router.patch("/v2/partials/sightings/{requirement_id}/advance-status", response_class=HTMLResponse)
async def sightings_advance_status(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Advance the sourcing status for a requirement via state machine validation."""
    form = await request.form()
    target_status = form.get("status", "")
    if not target_status:
        raise HTTPException(status_code=400, detail="status is required")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    current = requirement.sourcing_status or SourcingStatus.OPEN

    # Validates transition; raises HTTPException 409 on invalid
    require_valid_transition("requirement", current, target_status)

    old_status = current
    requirement.sourcing_status = target_status
    _invalidate_cache("sightings_stat_counts")

    log_rfq_activity(
        db=db,
        rfq_id=requirement.requisition_id,
        activity_type=ActivityType.STATUS_CHANGED,
        description=f"Status changed from {old_status} to {target_status}",
        user_id=user.id,
        requirement_id=requirement_id,
    )
    db.commit()

    await _publish_if_user_source(source, user.id, requirement_id)

    return await sightings_detail(request, requirement_id, db, user)


@router.post("/v2/partials/sightings/{requirement_id}/log-activity", response_class=HTMLResponse)
async def sightings_log_activity(
    request: Request,
    requirement_id: int,
    notes: str = Form(...),
    channel: str = Form("note"),
    vendor_name: str = Form(""),
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Log a manual note/call/email activity against a requirement.

    Returns the updated activity timeline section so the new entry appears inline.
    """
    if not notes or not notes.strip():
        raise HTTPException(status_code=400, detail="Notes cannot be empty")

    if channel not in ("note", "call", "email"):
        raise HTTPException(status_code=400, detail="Channel must be note, call, or email")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    activity_type_map = {
        "note": "note",
        "call": ActivityType.CALL_LOGGED,
        "email": "email_sent",
    }

    record = ActivityLog(
        user_id=user.id,
        activity_type=activity_type_map[channel],
        channel=channel if channel != "note" else "manual",
        # Manual call/email logs from the sighting timeline are outbound.
        direction="outbound" if channel in ("call", "email") else None,
        requirement_id=requirement_id,
        requisition_id=requirement.requisition_id,
        notes=notes.strip(),
        contact_name=vendor_name.strip() if vendor_name and vendor_name.strip() else None,
    )
    db.add(record)
    db.commit()

    logger.info(
        "Sighting activity logged: {} on requirement {} by user {}",
        channel,
        requirement_id,
        user.id,
    )

    await _publish_if_user_source(source, user.id, requirement_id)

    # Re-fetch activities for the timeline
    activities = (
        db.query(ActivityLog)
        .filter(ActivityLog.requirement_id == requirement_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
        .all()
    )

    ctx = {
        "request": request,
        "activities": activities,
        "requirement": requirement,
    }
    resp = template_response("htmx/partials/sightings/activity_section.html", ctx)
    resp.headers["X-Rendered-Req-Id"] = str(requirement_id)
    return resp


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

    # Active-only unavailability exclusion (alongside the blacklist filter): a vendor
    # durably marked unavailable for ANY selected part is not suggested — deliberately
    # conservative multi-requirement semantics. Expired/released/cleared records do
    # not exclude (active-only is enforced inside the service).
    excluded = excluded_vendor_norms(db, requirements) if requirements else set()

    suggested_vendors: list[VendorCard] = []
    if req_id_list:
        vendor_query = (
            db.query(VendorCard)
            .join(
                VendorSightingSummary,
                sqlfunc.lower(sqlfunc.trim(VendorSightingSummary.vendor_name)) == VendorCard.normalized_name,
            )
            .filter(
                VendorSightingSummary.requirement_id.in_(req_id_list),
                VendorCard.is_blacklisted.is_(False),
            )
        )
        if excluded:
            vendor_query = vendor_query.filter(VendorCard.normalized_name.notin_(sorted(excluded)))
        suggested_vendors = (
            vendor_query.order_by(VendorCard.engagement_score.desc().nullslast()).distinct().limit(20).all()
        )
        if excluded:
            # Belt and braces: some legacy cards carry a normalized_name that predates
            # normalize_vendor_name (suffix kept) — re-check through the canonical
            # normalizer so "X, Inc." cards can't slip past the column filter.
            suggested_vendors = [
                v
                for v in suggested_vendors
                if normalize_vendor_name(v.display_name or v.normalized_name or "") not in excluded
            ]

    ctx = {
        "request": request,
        "suggested_vendors": suggested_vendors,
        "requirement_ids": req_id_list,
        "parts": parts,
    }
    return template_response("htmx/partials/sightings/vendor_modal.html", ctx)


@router.post("/v2/partials/sightings/preview-inquiry", response_class=HTMLResponse)
async def sightings_preview_inquiry(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Preview rendered RFQ emails per vendor without sending.

    Called by: vendor_modal.html Preview button
    Depends on: email_service._build_html_body, VendorCard, VendorContact
    """
    form = await request.form()
    requirement_ids = [int(x) for x in form.getlist("requirement_ids") if x.isdigit()]
    vendor_names = form.getlist("vendor_names")
    email_body = form.get("email_body", "")

    if not requirement_ids or not vendor_names:
        raise HTTPException(status_code=400, detail="requirement_ids and vendor_names required")

    requirements = db.query(Requirement).filter(Requirement.id.in_(requirement_ids)).all()

    # Request-time re-validation against ACTIVE unavailability records (the modal
    # filter alone leaves a TOCTOU hole): excluded vendors are dropped from the
    # preview and reported visibly — never a silent drop.
    excluded = excluded_vendor_norms(db, requirements)
    unavailable_vendors = [vn for vn in vendor_names if normalize_vendor_name(vn) in excluded]
    if unavailable_vendors:
        vendor_names = [vn for vn in vendor_names if normalize_vendor_name(vn) not in excluded]

    requisition_ids = {r.requisition_id for r in requirements}
    requisition_id = next(iter(requisition_ids)) if requisition_ids else None

    # Batch-fetch vendor cards + contacts (same logic as send-inquiry)
    normalized_names = [normalize_vendor_name(vn) for vn in vendor_names]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id.in_(card_ids)).all() if card_ids else []
    contact_map = {c.vendor_card_id: c for c in contacts}

    from ..email_service import _build_html_body

    avail_token = f"[ref:{requisition_id}]" if requisition_id else ""
    parts_list = [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements]

    previews = []
    for vn in vendor_names:
        card = card_map.get(normalize_vendor_name(vn))
        vendor_email = ""
        if card:
            contact = contact_map.get(card.id)
            if contact and contact.email:
                vendor_email = contact.email

        raw_subject = f"RFQ — {len(requirements)} part{'s' if len(requirements) != 1 else ''}"
        tagged_subject = f"{raw_subject} {avail_token}" if avail_token else raw_subject
        html_body = _build_html_body(email_body)

        previews.append(
            {
                "vendor_name": vn,
                "vendor_email": vendor_email,
                "subject": tagged_subject,
                "html_body": html_body,
                "parts": parts_list,
            }
        )

    ctx = {
        "request": request,
        "previews": previews,
        "requirement_ids": requirement_ids,
        "vendor_names": vendor_names,
        "email_body": email_body,
        "unavailable_vendors": unavailable_vendors,
    }
    return template_response("htmx/partials/sightings/preview_inquiry.html", ctx)


@router.post("/v2/partials/sightings/send-inquiry", response_class=HTMLResponse)
async def sightings_send_inquiry(
    request: Request,
    source: Literal["user", "sse"] = Query(default="user"),
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

    # Send-time re-validation (closes the TOCTOU the modal filter alone leaves open):
    # vendors with an ACTIVE unavailability record on the selected parts are dropped
    # from the send and reported visibly below — never a silent drop.
    excluded = excluded_vendor_norms(db, requirements)
    unavailable_vendors = [vn for vn in vendor_names if normalize_vendor_name(vn) in excluded]
    sendable_vendors = [vn for vn in vendor_names if normalize_vendor_name(vn) not in excluded]

    requisition_ids = {r.requisition_id for r in requirements}
    requisition_id = next(iter(requisition_ids)) if requisition_ids else None

    # Batch-fetch vendor cards + contacts in two queries instead of N+1
    normalized_names = [normalize_vendor_name(vn) for vn in sendable_vendors]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    contacts = db.query(VendorContact).filter(VendorContact.vendor_card_id.in_(card_ids)).all() if card_ids else []
    contact_map = {c.vendor_card_id: c for c in contacts}

    vendor_groups = []
    for vn in sendable_vendors:
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
    progressed_count = 0
    failed_vendors: list[str] = []
    no_email_vendors: list[str] = []
    try:
        if vendor_groups:
            from ..email_service import send_batch_rfq

            results = await send_batch_rfq(
                token=token,
                db=db,
                user_id=user.id,
                requisition_id=requisition_id,
                vendor_groups=vendor_groups,
            )
        else:
            results = []  # every requested vendor was dropped by the unavailability re-check
        # send_batch_rfq returns one record per requested vendor tagged "sent" / "failed"
        # / "skipped" (no contact email). A vendor is delivered only when status=="sent"
        # — len(results) would over-count, and a "skipped" vendor is not a delivery
        # failure (the user just needs to add an email), so surface the three distinctly.
        sent_count = sum(1 for r in results if r.get("status") == "sent")
        sent_vendors = {r.get("vendor_name") for r in results if r.get("status") == "sent"}
        failed_vendors = [r.get("vendor_name", "") for r in results if r.get("status") == "failed"]
        no_email_vendors = [r.get("vendor_name", "") for r in results if r.get("status") == "skipped"]

        # Log "RFQ sent" activity only for vendors actually reached.
        for r in requirements:
            for vn in sendable_vendors:
                if vn in sent_vendors:
                    log_rfq_activity(
                        db=db,
                        rfq_id=r.requisition_id,
                        activity_type="rfq_sent",
                        description=f"RFQ sent to {vn}",
                        user_id=user.id,
                        requirement_id=r.id,
                    )

        # Auto-progress sourcing status OPEN → SOURCING only once at least one RFQ went out.
        if sent_vendors:
            from ..services.sourcing_auto_progress import auto_progress_status

            for r in requirements:
                if auto_progress_status(r, SourcingStatus.SOURCING, db, user.id):
                    progressed_count += 1
    except Exception:
        logger.warning("RFQ send failed", exc_info=True)
        failed_vendors = list(sendable_vendors)
        sent_count = 0

    db.commit()

    # Notify SSE listeners for each affected requirement
    for r in requirements:
        await _publish_if_user_source(source, user.id, r.id)

    total = len(vendor_names)
    if sent_count >= total:
        msg = f"RFQ sent to {sent_count} vendor{'s' if sent_count != 1 else ''}."
        if progressed_count:
            msg += f" {progressed_count} requirement{'s' if progressed_count != 1 else ''} advanced to sourcing."
        level = "success"
    else:
        bits = [f"Sent to {sent_count}/{total} vendors."]
        if failed_vendors:
            bits.append(f"Failed: {', '.join(v for v in failed_vendors if v)}.")
        if no_email_vendors:
            bits.append(f"No email on file: {', '.join(v for v in no_email_vendors if v)}.")
        if unavailable_vendors:
            bits.append(f"Skipped (marked unavailable): {', '.join(unavailable_vendors)}.")
        msg = " ".join(bits)
        level = "warning"

    # Machine-readable result so the browser caller (rfqVendorModal.confirmSend) can
    # report the TRUE outcome: this route intentionally returns 200 even on a partial /
    # total failure (failures are captured above, not raised), so the client must not
    # infer success from the HTTP status. X-RFQ-Skipped counts no-email vendors so the
    # client can distinguish "had no email" from "send failed".
    resp = _oob_toast(msg, level)
    resp.headers[RFQ_SENT_HEADER] = str(sent_count)
    resp.headers[RFQ_TOTAL_HEADER] = str(total)
    resp.headers[RFQ_SKIPPED_HEADER] = str(len(no_email_vendors))
    resp.headers[RFQ_UNAVAILABLE_HEADER] = str(len(unavailable_vendors))
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Offers tab (part-centric) — Convert-to-offer, Enter-offer, and mutations.
# Creation/mutation logic is reused from app.routers.crm.offers (no duplication);
# these endpoints just adapt form input and re-render #sightings-offers-panel.
# ─────────────────────────────────────────────────────────────────────────────


def _refresh_offers_panel(request: Request, requirement_id: int, db: Session) -> HTMLResponse:
    """Re-fetch the requirement (post-mutation) and render the offers panel, or 404."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    return _render_offers_panel(request, requirement, db)


def _parse_iso_date(v: str | None) -> date | None:
    """Parse an optional ISO-date form field ('' or unparseable → None)."""
    s = (v or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


@router.get("/v2/partials/sightings/{requirement_id}/offer-form", response_class=HTMLResponse)
async def sightings_offer_form(
    request: Request,
    requirement_id: int,
    vendor_name: str = Query(""),
    unit_price: str = Query(""),
    qty: str = Query(""),
    moq: str = Query(""),
    lead_days: str = Query(""),
    manufacturer: str = Query(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Modal offer form — pre-filled from a sighting (Convert) or blank (Enter)."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    prefill = None
    if vendor_name:
        prefill = {
            "vendor_name": vendor_name,
            "mpn": requirement.primary_mpn,
            "manufacturer": manufacturer or requirement.manufacturer or "",
            "unit_price": unit_price,
            "qty_available": qty,
            "moq": moq,
            "lead_time": f"{lead_days} days" if lead_days else "",
        }
    ctx = {"request": request, "requirement": requirement, "prefill": prefill, "offer": None}
    return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/offers", response_class=HTMLResponse)
async def sightings_create_offer(
    request: Request,
    requirement_id: int,
    vendor_name: str = Form(...),
    mpn: str = Form(...),
    manufacturer: str = Form(""),
    qty_available: str = Form(""),
    unit_price: str = Form(""),
    lead_time: str = Form(""),
    date_code: str = Form(""),
    condition: str = Form("new"),
    packaging: str = Form(""),
    firmware: str = Form(""),
    hardware_code: str = Form(""),
    moq: str = Form(""),
    spq: str = Form(""),
    warranty: str = Form(""),
    country_of_origin: str = Form(""),
    valid_until: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
) -> HTMLResponse:
    """Create an offer for this part via the canonical create_offer, then re-render the
    offers panel.

    Reused for both Convert-to-offer and Enter-offer.
    """
    from ..routers.crm.offers import create_offer
    from ..schemas.crm import OfferCreate

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    try:
        payload = OfferCreate(
            mpn=mpn,
            vendor_name=vendor_name,
            requirement_id=requirement_id,
            manufacturer=manufacturer or None,
            qty_available=safe_int(qty_available),
            unit_price=safe_float(unit_price),
            lead_time=lead_time or None,
            date_code=date_code or None,
            condition=condition or "new",
            packaging=packaging or None,
            firmware=firmware or None,
            hardware_code=hardware_code or None,
            moq=safe_int(moq),
            spq=safe_int(spq),
            warranty=warranty or None,
            country_of_origin=country_of_origin or None,
            valid_until=_parse_iso_date(valid_until),
            notes=notes or None,
            source="manual",
        )
    except ValidationError as e:
        # Surface as a 422 (not a 500) so a bad numeric/date is reported, not crashed.
        raise RequestValidationError(e.errors()) from e

    await create_offer(requirement.requisition_id, payload, user=user, db=db)
    # Offer hook: an incoming offer is proof of availability — release the vendor's
    # matching ACTIVE unavailability records ('offer_received'; never different_part).
    release_on_offer(db, requirement, vendor_name, user)
    db.commit()
    db.expire_all()
    return _with_toast(_refresh_offers_panel(request, requirement_id, db), "Offer saved")


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/review", response_class=HTMLResponse)
async def sightings_review_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    action: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Approve or reject a pending_review offer, then re-render the offers panel."""
    from ..routers.crm.offers import approve_offer, reject_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    if action == "approve":
        await approve_offer(offer_id, user=user, db=db)
    else:
        await reject_offer(offer_id, user=user, db=db)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/reconfirm", response_class=HTMLResponse)
async def sightings_reconfirm_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Reconfirm an offer, then re-render the offers panel."""
    from ..routers.crm.offers import reconfirm_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    await reconfirm_offer(offer_id, user=user, db=db)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/mark-sold", response_class=HTMLResponse)
async def sightings_mark_offer_sold(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    """Mark an offer sold, then re-render the offers panel."""
    from ..routers.crm.offers import mark_offer_sold

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    await mark_offer_sold(offer_id, user=user, db=db)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.delete("/v2/partials/sightings/{requirement_id}/offers/{offer_id}", response_class=HTMLResponse)
async def sightings_delete_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    """Delete an offer, then re-render the offers panel."""
    from ..routers.crm.offers import delete_offer

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    await delete_offer(offer_id, user=user, db=db)
    db.expire_all()
    return _refresh_offers_panel(request, requirement_id, db)


@router.get("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/edit-form", response_class=HTMLResponse)
async def sightings_offer_edit_form(
    request: Request,
    requirement_id: int,
    offer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Modal offer form pre-filled from an existing offer (edit mode)."""
    requirement = db.get(Requirement, requirement_id)
    offer = db.get(Offer, offer_id)
    if not requirement or not offer:
        raise HTTPException(404, "Not found")
    fields = [
        "vendor_name",
        "mpn",
        "manufacturer",
        "qty_available",
        "unit_price",
        "lead_time",
        "date_code",
        "condition",
        "packaging",
        "firmware",
        "hardware_code",
        "moq",
        "spq",
        "warranty",
        "country_of_origin",
        "notes",
    ]
    prefill = {f: ("" if getattr(offer, f) is None else getattr(offer, f)) for f in fields}
    ctx = {"request": request, "requirement": requirement, "prefill": prefill, "offer": offer}
    return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}", response_class=HTMLResponse)
async def sightings_update_offer(
    request: Request,
    requirement_id: int,
    offer_id: int,
    vendor_name: str = Form(""),
    mpn: str = Form(""),
    manufacturer: str = Form(""),
    qty_available: str = Form(""),
    unit_price: str = Form(""),
    lead_time: str = Form(""),
    date_code: str = Form(""),
    condition: str = Form(""),
    packaging: str = Form(""),
    firmware: str = Form(""),
    hardware_code: str = Form(""),
    moq: str = Form(""),
    spq: str = Form(""),
    warranty: str = Form(""),
    country_of_origin: str = Form(""),
    valid_until: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
) -> HTMLResponse:
    """Update an offer via the canonical update_offer, then re-render the panel."""
    from ..routers.crm.offers import update_offer
    from ..schemas.crm import OfferUpdate

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    try:
        payload = OfferUpdate(
            vendor_name=vendor_name or None,
            mpn=mpn or None,
            manufacturer=manufacturer or None,
            qty_available=safe_int(qty_available),
            unit_price=safe_float(unit_price),
            lead_time=lead_time or None,
            date_code=date_code or None,
            condition=condition or None,
            packaging=packaging or None,
            firmware=firmware or None,
            hardware_code=hardware_code or None,
            moq=safe_int(moq),
            spq=safe_int(spq),
            warranty=warranty or None,
            country_of_origin=country_of_origin or None,
            valid_until=_parse_iso_date(valid_until),
            notes=notes or None,
        )
    except ValidationError as e:
        raise RequestValidationError(e.errors()) from e

    await update_offer(offer_id, payload, user=user, db=db)
    db.expire_all()
    return _with_toast(_refresh_offers_panel(request, requirement_id, db), "Offer updated")
