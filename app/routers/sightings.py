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
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Final, Literal, NamedTuple, TypedDict
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import and_, or_
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload

from ..config import settings
from ..constants import (
    ActivityType,
    OfferStatus,
    ReleaseTrigger,
    RequisitionStatus,
    SightingsSkipReason,
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
from ..services.offer_qualification import prefill_from_vendor
from ..services.part_offers import part_offers_for
from ..services.sighting_status import compute_vendor_statuses
from ..services.sse_broker import broker
from ..services.status_machine import SOURCING_TRANSITIONS, require_valid_transition
from ..services.vendor_duplicates import check_vendor_duplicate
from ..services.vendor_unavailability import (
    clear_unavailability,
    excluded_vendor_norms,
    record_unavailability,
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


def _oob_toast_html(msg: str, level: str = "success") -> str:
    """The OOB toast fragment — swaps into #toast-trigger and fires $store.toast."""
    safe_msg = msg.replace("'", "\\'").replace('"', "&quot;")
    return (
        f'<div hx-swap-oob="true" id="toast-trigger"'
        f" x-init=\"$store.toast.message='{safe_msg}';"
        f"$store.toast.type='{level}';"
        f'$store.toast.show=true"></div>'
    )


def _oob_toast(msg: str, level: str = "success") -> HTMLResponse:
    """Return an OOB swap div that triggers a toast notification via Alpine."""
    return HTMLResponse(_oob_toast_html(msg, level))


def _append_oob_toast(resp: Response, msg: str, level: str = "success") -> HTMLResponse:
    """Append the OOB toast fragment to an already-rendered HTMX response (mark/clear
    feedback on detail re-renders), preserving the original custom headers."""
    out = HTMLResponse(resp.body.decode("utf-8") + _oob_toast_html(msg, level), status_code=resp.status_code)
    for key, value in resp.headers.items():
        if key.lower() not in ("content-length", "content-type"):
            out.headers[key] = value
    return out


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
            # Precomputed display fragment ("offer" / "vendor email") via the
            # ReleaseTrigger enum's .label — templates never compare raw trigger
            # strings (reason_label precedent).
            "released_by": ReleaseTrigger(rec.release_trigger).label if rec.release_trigger else None,
            "reason": rec.reason,
            "reason_label": UnavailabilityReason(rec.reason).label,
            "note": rec.note,
            "qty_at_mark": rec.qty_at_mark,
            "marked_by": creator_names.get(rec.created_by_id),
            "has_unstamped_row": normalize_vendor_name(vendor_name) in unstamped_norms,
        }
    return annotated


def _partition_by_unavailability(vendor_names: list[str], excluded_norms: set[str]) -> tuple[list[str], list[str]]:
    """Split vendor display names into (unavailable, sendable) against the active-only
    excluded-norm set, preserving order.

    The RFQ preview/send re-check shares this partition: vendors with an ACTIVE
    unavailability record on the selected parts are dropped from the send and reported
    visibly, never silently.
    """
    unavailable = [vn for vn in vendor_names if normalize_vendor_name(vn) in excluded_norms]
    sendable = [vn for vn in vendor_names if normalize_vendor_name(vn) not in excluded_norms]
    return unavailable, sendable


def _mpn_link_map(db: Session, requirements) -> dict[str, int]:
    """Build a display-MPN → MaterialCard.id map for the given requirements.

    Collects each requirement's primary_mpn plus substitute MPNs (string or dict form),
    normalizes them, and resolves live (non-deleted) MaterialCards in one query. Shared
    by sightings_list (table) and sightings_detail (header). Empty input → empty map.
    """
    from ..utils.normalization import normalize_mpn_key

    all_mpns: set[str] = set()
    for r in requirements:
        if r.primary_mpn:
            all_mpns.add(r.primary_mpn.upper())
        for sub in r.substitutes or []:
            mpn = sub.get("mpn") if isinstance(sub, dict) else sub
            if mpn:
                all_mpns.add(str(mpn).upper())
    if not all_mpns:
        return {}

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
    link_map: dict[str, int] = {}
    for card_id, norm in cards:
        display = norm_to_display.get(norm)
        if display:
            link_map[display] = card_id
    return link_map


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
    if filters.manufacturer:
        safe_mfr = escape_like(filters.manufacturer)
        query = query.filter(Requirement.manufacturer.ilike(f"%{safe_mfr}%"))

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
    link_map = _mpn_link_map(db, requirements) if requirements else {}

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
        "manufacturer": filters.manufacturer,
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
    detail_link_map = _mpn_link_map(db, [requirement])

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

    total = success + failed
    msg = f"Searched {success}/{total} requirements."
    if failed:
        msg += f" {failed} failed."
    level = "warning" if failed else "success"
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


async def _mark_error_response(
    request: Request,
    requirement_id: int,
    db: Session,
    user: User,
    msg: str,
) -> HTMLResponse:
    """400-path feedback for the mark/clear routes.

    htmx callers get the re-rendered detail plus the ACTIONABLE message as an error
    toast (the global htmx:responseError handler only shows a generic "Request failed"
    line); non-htmx/API callers keep the 400 JSON contract.
    """
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(status_code=400, detail=msg)
    detail = await sightings_detail(request, requirement_id, db, user)
    return _append_oob_toast(detail, msg, "error")


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
    service's ValueErrors (zero derivable keys / empty vendor norm) surface their
    actionable message as an error toast to htmx callers and as a 400 JSON error to API
    callers — nothing written either way. Success re-renders the detail panel with a
    confirmation toast appended.
    """
    form = await request.form()
    vendor_name = str(form.get("vendor_name") or "").strip()
    if not vendor_name:
        return await _mark_error_response(request, requirement_id, db, user, "vendor_name required")
    try:
        reason = UnavailabilityReason(str(form.get("reason") or ""))
    except ValueError:
        valid = ", ".join(m.value for m in UnavailabilityReason)
        return await _mark_error_response(
            request, requirement_id, db, user, f"reason is required and must be one of: {valid}"
        )
    note = str(form.get("note") or "") or None

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    try:
        record_unavailability(db, requirement, vendor_name, reason, note, user)
    except ValueError as exc:
        db.rollback()
        return await _mark_error_response(request, requirement_id, db, user, str(exc))
    db.commit()

    await _publish_if_user_source(source, user.id, requirement_id)

    detail = await sightings_detail(request, requirement_id, db, user)
    if _toast_suppressed_for_sse(source):
        return detail
    return _append_oob_toast(detail, f"Marked {vendor_name} unavailable — {reason.label}")


@router.post("/v2/partials/sightings/{requirement_id}/mark-available", response_class=HTMLResponse)
async def sightings_mark_available(
    request: Request,
    requirement_id: int,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Undo a mark: delete the vendor's unavailability records and unflag its
    sightings (clear_unavailability), then re-render the detail panel with a
    confirmation toast appended (errors surface per _mark_error_response)."""
    form = await request.form()
    vendor_name = str(form.get("vendor_name") or "").strip()
    if not vendor_name:
        return await _mark_error_response(request, requirement_id, db, user, "vendor_name required")

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")

    try:
        clear_unavailability(db, requirement, vendor_name, user)
    except ValueError as exc:
        db.rollback()
        return await _mark_error_response(request, requirement_id, db, user, str(exc))
    db.commit()

    await _publish_if_user_source(source, user.id, requirement_id)

    detail = await sightings_detail(request, requirement_id, db, user)
    if _toast_suppressed_for_sse(source):
        return detail
    return _append_oob_toast(detail, f"{vendor_name} marked available again")


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


def _vss_vendor_card_join():
    """Coalesce join VendorSightingSummary → VendorCard (F10).

    The vendor_card_id FK (indexed ix_vss_vendor_card) is the PRIMARY branch; VSS
    rows with a NULL FK (e.g. summaries rebuilt before the FK backfill ran) fall
    back to the legacy lower(trim(vendor_name)) == normalized_name match so a
    known vendor's coverage never silently disappears. The NULL-FK guard on the
    fallback prevents double-matching FK rows by name. Plain functions only —
    SQLite + PG safe.

    Called by: _coverage_ranked_vendor_rows, sightings_vendor_modal (MPN titles).
    """
    return or_(
        VendorSightingSummary.vendor_card_id == VendorCard.id,
        and_(
            VendorSightingSummary.vendor_card_id.is_(None),
            sqlfunc.lower(sqlfunc.trim(VendorSightingSummary.vendor_name)) == VendorCard.normalized_name,
        ),
    )


class RankedVendor(NamedTuple):
    """One coverage-ranked vendor row (see _coverage_ranked_vendor_rows).

    Coverage-discovery (2026-06-15): a row may be CARDLESS — ``card is None`` for a
    vendor that has sightings on the selected parts but no matching VendorCard. Such a
    vendor is surfaced for discovery ("who has my parts?") but is NOT RFQ-able, so
    ``has_contact`` is False and the send path would skip it.

    Fields:
    - ``card``: the representative VendorCard for the group, or None when cardless.
    - ``vendor_name``: the deterministic display name — ``card.display_name`` when
      carded, else the lexicographically-min raw VSS ``vendor_name`` in the group.
    - ``covered_count``: distinct selected requirements this vendor has sightings on.
    - ``avg_score``: mean of the non-null VSS scores in the group (None if all null).
    - ``has_contact``: True iff the send path (``sightings_send_inquiry`` /
      ``sightings_preview_inquiry``) would resolve a non-empty email for this vendor —
      i.e. ``card is not None`` AND some VendorContact for that card has a non-empty
      ``email``. Mirrors the send-skip logic EXACTLY so the "no contact" badge never
      lies. (``card.emails`` is deliberately NOT consulted: the send path resolves the
      address only from VendorContact rows.)
    """

    card: VendorCard | None
    vendor_name: str
    covered_count: int
    avg_score: float | None
    has_contact: bool


class CoverageEntry(TypedDict):
    """Per-vendor coverage shape rendered in the vendor modal row, keyed by the
    suggested vendor's ``key`` (card id for carded, normalized vendor_name for
    cardless).

    ``mpns`` is populated lazily by a second query and stays ``""`` for vendors with no
    MPN rows — ``""`` is a valid terminal value, NOT "not yet computed".
    """

    count: int
    avg_score: float | None
    mpns: str


class SuggestedVendor(NamedTuple):
    """Template-facing view of one coverage-ranked suggested vendor (carded OR
    cardless).

    Built from a RankedVendor in sightings_vendor_modal. Field names mirror the
    VendorCard attributes the modal template already renders (``id``, ``normalized_name``,
    ``display_name``, ``response_rate``, ``engagement_score``) so the carded path renders
    byte-equivalent to before; cardless rows synthesize those (id = normalized name as the
    coverage key + Alpine selection key, no card-derived badge fields). New fields
    (``vendor_name``, ``has_contact``, ``card``) drive the Task-2 cardless chrome.

    ``id`` is the grouping/coverage key (card id for carded, normalized vendor_name for
    cardless); ``coverage`` in the modal context is keyed by this same value.
    """

    id: object
    card: VendorCard | None
    normalized_name: str
    display_name: str
    vendor_name: str
    has_contact: bool
    response_rate: float | None
    engagement_score: float | None


def _cards_with_resolvable_email(db: Session, card_ids: list[int]) -> set[int]:
    """Card ids for which the send path would resolve a non-empty contact email.

    MIRRORS the send-path contact resolution in sightings_send_inquiry /
    sightings_preview_inquiry EXACTLY: a vendor is reachable iff a VendorContact for its
    card has a non-empty ``email`` (the send path reads ``contact.email`` from
    _best_contacts_by_card; it never consults ``card.emails``). One batched query over
    all representative card ids — no N+1 over groups. Empty input → empty set.
    """
    if not card_ids:
        return set()
    rows = (
        db.query(VendorContact.vendor_card_id)
        .filter(
            VendorContact.vendor_card_id.in_(card_ids),
            VendorContact.email.isnot(None),
            VendorContact.email != "",
        )
        .distinct()
        .all()
    )
    return {cid for (cid,) in rows}


def _dnc_emails_for_cards(db: Session, card_ids: list[int]) -> set[str]:
    """Return the lowercased email addresses (from VendorContact) that will be DNC-
    skipped by send_batch_rfq for the given vendor card ids.

    Mirrors the send-time DNC check in email_service.send_batch_rfq (line ~181):
    join VendorContact → SiteContact by func.lower(email), filtered on
    SiteContact.do_not_contact.is_(True). Uses func.lower on BOTH sides so the
    advisory set is consistent with the case-insensitive send-time check.

    Returns a set of lowercased emails — the caller compares contact.email.lower()
    against this set. Advisory only; the authoritative skip stays in send_batch_rfq
    (TOCTOU guard — a SiteContact can be flagged after the modal opens).

    Called by: sightings_vendor_modal, sightings_preview_inquiry.
    """
    if not card_ids:
        return set()

    from ..models.crm import SiteContact

    rows = (
        db.query(VendorContact.email)
        .join(
            SiteContact,
            sqlfunc.lower(VendorContact.email) == sqlfunc.lower(SiteContact.email),
        )
        .filter(
            VendorContact.vendor_card_id.in_(card_ids),
            VendorContact.email.isnot(None),
            VendorContact.email != "",
            SiteContact.do_not_contact.is_(True),
        )
        .distinct()
        .all()
    )
    return {email.lower() for (email,) in rows}


def _coverage_ranked_vendor_rows(db: Session, req_id_list: list[int], excluded: set[str]) -> list[RankedVendor]:
    """Coverage-ranked suggested vendors — the single source shared by the vendor modal
    and the affinity endpoint (which must drop already-suggested vendors computed the
    SAME way, so it stays self-contained).

    Coverage-discovery (2026-06-15): an OUTER join over VendorSightingSummary →
    VendorCard (via _vss_vendor_card_join) plus Python grouping, so a vendor with
    sightings but NO matching card (card=None, "cardless") is surfaced for discovery
    instead of silently dropped. Python grouping sidesteps the GROUP-BY-entity
    SQLite/PG portability seam; VSS is a few hundred rows — trivial.

    Grouping key: ``card.id`` when carded, else ``normalize_vendor_name(vendor_name)``
    (the canonical normalizer — two name variants of one cardless vendor merge, and the
    key matches the exclusion set). Per group: distinct requirement count
    (covered_count), mean of non-null scores (avg_score), a representative card (None if
    all cardless), and a deterministic display name (card.display_name if carded, else
    the lexicographically-min raw vendor_name).

    Drops: blacklisted only when carded; excluded (unavailability) by normalized name
    (cardless = its group key; carded = normalize_vendor_name(display/normalized_name) —
    belt-and-braces re-check kept for legacy suffixed cards). has_contact mirrors the
    send-skip logic (see _cards_with_resolvable_email).

    Rank: covered_count desc, has_contact desc, engagement_score desc nullslast, then a
    stable group-key tiebreak. Capped at 20.
    """
    raw_rows = (
        db.query(VendorSightingSummary, VendorCard)
        .outerjoin(VendorCard, _vss_vendor_card_join())
        .filter(VendorSightingSummary.requirement_id.in_(req_id_list))
        .all()
    )

    # Group in Python by card.id (carded) or normalize_vendor_name(vendor_name) (cardless).
    groups: dict[object, dict] = {}
    for vss, card in raw_rows:
        # Blacklist applies only to carded vendors (cardless rows have no flag).
        if card is not None and card.is_blacklisted:
            continue
        if card is not None:
            key: object = card.id
        else:
            key = normalize_vendor_name(vss.vendor_name or "")
            if not key:
                continue  # un-normalizable cardless name — nothing to suggest
        g = groups.get(key)
        if g is None:
            g = {"card": None, "req_ids": set(), "scores": [], "raw_names": []}
            groups[key] = g
        g["req_ids"].add(vss.requirement_id)
        if vss.score is not None:
            g["scores"].append(vss.score)
        if vss.vendor_name:
            g["raw_names"].append(vss.vendor_name)
        if g["card"] is None and card is not None:
            g["card"] = card

    # Fold suffix-mismatch duplicates (F-H1): the SQL fallback join matches NULL-FK rows by
    # raw lower(trim(vendor_name)) == normalized_name, but cardless grouping keys on
    # normalize_vendor_name(vendor_name). A NULL-FK row "Acme Inc" thus does NOT join to
    # card "acme" (the ' inc' suffix survives raw lower(trim)) yet normalizes to "acme" — it
    # would emit a SECOND, cardless "acme" row with split coverage. Merge any cardless group
    # whose key equals a CARDED group's normalize_vendor_name(display) into that carded
    # group BEFORE ranking, so coverage counts union and no duplicate row is emitted; the
    # carded card / has_contact / display win. Two carded groups never collide on this key
    # (each carded group keys on a distinct card.id).
    carded_by_norm: dict[str, object] = {}
    for ck, cg in groups.items():
        c = cg["card"]
        if c is not None:
            carded_by_norm[normalize_vendor_name(c.display_name or c.normalized_name or "")] = ck
    for cardless_key in [k for k, g in groups.items() if g["card"] is None and k in carded_by_norm]:
        src = groups.pop(cardless_key)
        dst = groups[carded_by_norm[cardless_key]]
        dst["req_ids"].update(src["req_ids"])
        dst["scores"].extend(src["scores"])
        dst["raw_names"].extend(src["raw_names"])

    # has_contact: one batched VendorContact lookup over all representative card ids.
    contactable_card_ids = _cards_with_resolvable_email(db, [g["card"].id for g in groups.values() if g["card"]])

    ranked: list[tuple[int, bool, float, tuple[int, object], RankedVendor]] = []
    for key, g in groups.items():
        card = g["card"]
        excl_key: object
        if card is not None:
            display = card.display_name or card.normalized_name or ""
            excl_key = normalize_vendor_name(display)
        else:
            display = min(g["raw_names"]) if g["raw_names"] else str(key)
            excl_key = key  # cardless group key IS its normalized name
        # Exclusion (unavailability) drop — cardless by group key, carded belt-and-braces.
        if excluded and excl_key in excluded:
            continue
        covered = len(g["req_ids"])
        scores = g["scores"]
        avg_score = (sum(scores) / len(scores)) if scores else None
        has_contact = card is not None and card.id in contactable_card_ids
        rv = RankedVendor(
            card=card,
            vendor_name=display,
            covered_count=covered,
            avg_score=avg_score,
            has_contact=has_contact,
        )
        engagement = card.engagement_score if (card is not None and card.engagement_score is not None) else None
        # Stable, deterministic tiebreak (F-L1): carded ties keep NUMERIC card.id order
        # (bucket 0), cardless after (bucket 1, keyed by group-key string). str(key) alone
        # was lexicographic ("10" < "2"), drifting which equally-ranked vendor fell off the
        # cap-20 vs main's numeric id order.
        tiebreak: tuple[int, object] = (0, card.id) if card is not None else (1, str(key))
        # Sort tuple: covered desc, has_contact desc, engagement desc nullslast, then tiebreak.
        ranked.append(
            (
                -covered,
                not has_contact,  # False(0) sorts before True(1) → contactable first
                -(engagement if engagement is not None else float("-inf")),
                tiebreak,
                rv,
            )
        )

    ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    return [t[4] for t in ranked[:20]]


def _find_affinity_in_thread(mpn: str) -> list[dict]:
    """Run the SYNC find_vendor_affinity on a worker thread with its OWN session.

    SQLAlchemy sessions are not thread-safe, so the request session never crosses the
    to_thread boundary — each call opens and closes a fresh SessionLocal (the
    established thread-work pattern: description_service._collect_db_descriptions,
    jobs/tagging_jobs). find_vendor_affinity is imported lazily so tests mock it at
    the source module (app.services.vendor_affinity_service), never the import site.
    """
    from ..database import SessionLocal
    from ..services.vendor_affinity_service import find_vendor_affinity

    thread_db = SessionLocal()
    try:
        return find_vendor_affinity(mpn, thread_db)
    finally:
        thread_db.close()


@router.get("/v2/partials/sightings/vendor-modal", response_class=HTMLResponse)
async def sightings_vendor_modal(
    request: Request,
    requirement_ids: str = "",
    preselect: str = "",
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

    suggested_vendors: list[SuggestedVendor] = []
    coverage: dict[object, CoverageEntry] = {}
    if req_id_list:
        rows = _coverage_ranked_vendor_rows(db, req_id_list, excluded)
        # Group key: card id for carded, normalized vendor_name for cardless. coverage is
        # keyed by this same key so both carded and cardless rows resolve their chip. The
        # Alpine selection / send-path key (normalized_name) is the card's stored
        # normalized_name for carded rows (unchanged) and the normalized vendor_name for
        # cardless rows.
        for r in rows:
            norm = normalize_vendor_name(r.vendor_name)
            key = r.card.id if r.card is not None else norm
            suggested_vendors.append(
                SuggestedVendor(
                    id=key,
                    card=r.card,
                    normalized_name=(r.card.normalized_name if r.card is not None else norm),
                    display_name=r.vendor_name,
                    vendor_name=r.vendor_name,
                    has_contact=r.has_contact,
                    response_rate=(r.card.response_rate if r.card is not None else None),
                    engagement_score=(r.card.engagement_score if r.card is not None else None),
                )
            )
            coverage[key] = CoverageEntry(
                count=r.covered_count,
                avg_score=float(r.avg_score) if r.avg_score is not None else None,
                mpns="",
            )
        if coverage:
            # Covered-MPN list per vendor (rendered in the row's `title`) — a second
            # plain query; no string_agg/group_concat (SQLite vs PG divergence). LEFT
            # join so cardless rows (no card) contribute their MPNs too; the per-row key
            # mirrors the ranking grouping (card id when joined, else normalized name).
            mpn_rows = (
                db.query(
                    VendorCard.id,
                    VendorSightingSummary.vendor_name,
                    Requirement.primary_mpn,
                )
                .select_from(VendorSightingSummary)
                .join(Requirement, VendorSightingSummary.requirement_id == Requirement.id)
                .outerjoin(VendorCard, _vss_vendor_card_join())
                .filter(VendorSightingSummary.requirement_id.in_(req_id_list))
                .distinct()
                .all()
            )
            mpns_by_key: dict[object, set[str]] = {}
            for card_id, vendor_name, mpn in mpn_rows:
                if not mpn:
                    continue
                row_key: object = card_id if card_id is not None else normalize_vendor_name(vendor_name or "")
                if row_key in coverage:
                    mpns_by_key.setdefault(row_key, set()).add(mpn)
            for cov_key, mpns in mpns_by_key.items():
                coverage[cov_key]["mpns"] = ", ".join(sorted(mpns))

    # ── Preselect union: append any named vendor not already in coverage ────────
    # Split on comma, normalize each name, skip blanks, dedup against the
    # already-suggested set (keyed by normalized_name to match the Alpine selection
    # key). `has_contact` is resolved via the same _cards_with_resolvable_email
    # helper used for coverage rows — so the Alpine seed stays consistent.
    if preselect.strip():
        existing_norms = {sv.normalized_name for sv in suggested_vendors}
        preselect_names = [n.strip() for n in preselect.split(",") if n.strip()]
        for raw_name in preselect_names:
            norm = normalize_vendor_name(raw_name)
            if not norm or norm in existing_norms:
                continue
            # Resolve against VendorCard by normalized_name
            card = db.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
            if card is not None:
                contactable = _cards_with_resolvable_email(db, [card.id])
                has_contact = card.id in contactable
                sv = SuggestedVendor(
                    id=card.id,
                    card=card,
                    normalized_name=card.normalized_name,
                    display_name=card.display_name or raw_name,
                    vendor_name=card.display_name or raw_name,
                    has_contact=has_contact,
                    response_rate=card.response_rate,
                    engagement_score=card.engagement_score,
                )
            else:
                # No matching card — cardless synthetic row, no contact resolvable
                sv = SuggestedVendor(
                    id=norm,
                    card=None,
                    normalized_name=norm,
                    display_name=raw_name,
                    vendor_name=raw_name,
                    has_contact=False,
                    response_rate=None,
                    engagement_score=None,
                )
            suggested_vendors.append(sv)
            existing_norms.add(norm)

    # Compute how many distinct requisitions the basket spans (for the "Spanning N
    # requisitions" note in the Parts panel). Only shown when >1 to keep the modal quiet
    # for the single-requirement case.
    requisition_count = len({r.requisition_id for r in requirements})

    # Advisory DNC computation — determine which suggested vendors have a DNC-flagged
    # contact email. Two steps:
    # 1. Collect the card ids of all carded suggested vendors.
    # 2. _dnc_emails_for_cards returns the lowercased DNC emails for those cards.
    # 3. Fetch each carded vendor's best contact (same ordering as the send path), then
    #    cross-reference: if the resolved email is in dnc_emails → vendor is advisory DNC.
    # Result is `dnc_norms`: set of normalized_names passed to the template so it can
    # disable the checkbox and render the rose chip WITHOUT lazy-loading relationships.
    carded_ids = [sv.card.id for sv in suggested_vendors if sv.card is not None]
    dnc_emails: set[str] = _dnc_emails_for_cards(db, carded_ids) if carded_ids else set()

    dnc_norms: set[str] = set()
    if dnc_emails and carded_ids:
        # Best-contact-per-card (same ordering as send path) to determine which vendor's
        # resolved email is in dnc_emails.
        best_contacts = _best_contacts_by_card(db, carded_ids)
        card_best_contact: dict[int, VendorContact] = {c.vendor_card_id: c for c in best_contacts}
        for sv in suggested_vendors:
            if sv.card is not None:
                contact = card_best_contact.get(sv.card.id)
                if contact and contact.email and contact.email.lower() in dnc_emails:
                    dnc_norms.add(sv.normalized_name)

    # Contactable, non-DNC normalized names for the Alpine selectedVendors seed.
    # Passed explicitly so the template doesn't need Jinja2 set-member filtering.
    contactable_non_dnc = [
        sv.normalized_name for sv in suggested_vendors if sv.has_contact and sv.normalized_name not in dnc_norms
    ]

    ctx = {
        "request": request,
        "suggested_vendors": suggested_vendors,
        "coverage": coverage,
        "requirement_ids": req_id_list,
        "parts": parts,
        "requisition_count": requisition_count,
        "dnc_norms": dnc_norms,
        "contactable_non_dnc": contactable_non_dnc,
    }
    return template_response("htmx/partials/sightings/vendor_modal.html", ctx)


@router.get("/v2/partials/sightings/vendor-affinity", response_class=HTMLResponse)
async def sightings_vendor_affinity(
    request: Request,
    requirement_ids: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """On-demand affinity vendor suggestions for the RFQ vendor modal.

    Called by: vendor_modal.html "Suggest more vendors" button (hx-get swaps the
    rows into #rfq-affinity-section, replacing the button — second click impossible).
    Runs find_vendor_affinity per selected primary MPN, merges/dedupes by vendor
    keeping the highest confidence, drops vendors already coverage-suggested (same
    query as the modal — self-contained) or unavailability-excluded, caps at 10.

    THREADING: find_vendor_affinity is SYNC with a blocking Anthropic L3 call inside
    (3-12s for 6 parts) — each per-MPN call runs via asyncio.to_thread with its own
    short-lived session (_find_affinity_in_thread), gathered under a Semaphore(3) so
    a wide selection can't exhaust the thread pool. Never call it bare from this
    async route: it would block the uvicorn worker.
    """
    req_id_list = [int(x) for x in requirement_ids.split(",") if x.strip().isdigit()]
    requirements = (db.query(Requirement).filter(Requirement.id.in_(req_id_list)).all()) if req_id_list else []

    affinity_vendors: list[dict] = []
    affinity_partial = False
    if requirements:
        excluded = excluded_vendor_norms(db, requirements)
        suggested_norms: set[str] = set()
        for r in _coverage_ranked_vendor_rows(db, req_id_list, excluded):
            # Canonical normalization of the row's display name — covers carded AND
            # cardless rows uniformly (affinity matches are compared canonically). For
            # carded rows also add the stored normalized_name, which may be legacy-
            # suffixed and so differ from the canonical re-normalization.
            suggested_norms.add(normalize_vendor_name(r.vendor_name))
            if r.card is not None:
                suggested_norms.add(r.card.normalized_name or "")

        # One affinity call per UNIQUE primary MPN (order-preserving dedupe — no
        # double L3 spend when requirements share an MPN).
        mpns = list(dict.fromkeys(r.primary_mpn for r in requirements if r.primary_mpn))
        sem = asyncio.Semaphore(3)

        async def _bounded(mpn: str) -> list[dict]:
            async with sem:
                return await asyncio.to_thread(_find_affinity_in_thread, mpn)

        # F6: one MPN's failure must not blank the whole panel (or 500 the swap).
        # Failed MPNs are logged with the MPN in context; survivors render, and
        # the template shows a quiet "suggestions incomplete" notice.
        per_mpn_results = await asyncio.gather(*(_bounded(m) for m in mpns), return_exceptions=True)
        per_mpn_matches: list[list[dict]] = []
        for mpn, matches in zip(mpns, per_mpn_results):
            if isinstance(matches, BaseException):
                affinity_partial = True
                logger.error("Vendor affinity lookup failed for MPN {}: {}", mpn, matches)
                continue
            per_mpn_matches.append(matches)

        best: dict[str, dict] = {}
        for matches in per_mpn_matches:
            for match in matches:
                norm = normalize_vendor_name(match.get("vendor_name") or "")
                if not norm or norm in suggested_norms or norm in excluded:
                    continue
                if norm not in best or match["confidence"] > best[norm]["confidence"]:
                    best[norm] = {**match, "normalized_name": norm}
        affinity_vendors = sorted(best.values(), key=lambda m: m["confidence"], reverse=True)[:10]

    ctx = {"request": request, "affinity_vendors": affinity_vendors, "affinity_partial": affinity_partial}
    return template_response("htmx/partials/sightings/vendor_affinity_rows.html", ctx)


def _parse_website_domain(website: str) -> str:
    """Extract a usable domain from user-typed website input (F12).

    urlsplit-based (scheme optional), lowercased host, strips ONE leading
    "www." — never a blanket str.replace that mangles hosts containing the
    substring. Returns "" when no plausible domain can be extracted; the caller
    turns that into a visible 400 instead of silently saving a junk domain.

    Called by: sightings_composer_vendor.
    """
    raw = website.strip()
    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
        host = (parsed.hostname or "").strip().lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    return host


@router.post("/v2/partials/sightings/composer-vendor", response_class=HTMLResponse)
async def sightings_composer_vendor(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Resolve-or-create a vendor for the RFQ composer's any-vendor picker.

    Called by: vendor_modal.html — both the "Find any vendor" autocomplete pick and
    the "Add new vendor" inline mini-form POST here (form fields: vendor_name
    required; website, email, requirement_ids optional); the returned row is
    appended into the stable-id #rfq-added-vendors sub-container.

    Flow: check_vendor_duplicate (the extracted service — direct call, never
    loopback HTTP). A confident duplicate is an EXACT normalized-name match (the
    service's own classification: exact short-circuits at score 100; fuzzy >= 80
    are suggestions, not dupes) → return the EXISTING vendor as a selected row with
    a "matched existing vendor" notice, no new DB row. Otherwise create the minimal
    VendorCard (normalized_name, display_name, optional domain parsed from the
    website — the crm/offers manual-entry pattern) plus a VendorContact when an
    email was given, commit, then fire _background_enrich_vendor post-commit
    (identical to the materials.py / vendor_contacts.py patterns; imports are lazy
    so tests mock both gates at their source modules).

    If the resolved vendor is unavailability-excluded for the selected
    requirement_ids, the row renders the rose "marked unavailable" chip with a
    DISABLED checkbox — send-time re-validation stays the backstop.
    """
    form = await request.form()
    vendor_name = str(form.get("vendor_name") or "").strip()
    norm = normalize_vendor_name(vendor_name)
    if not vendor_name or not norm:
        raise HTTPException(status_code=400, detail="vendor_name required")
    website = str(form.get("website") or "").strip()
    email = str(form.get("email") or "").strip()
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="invalid contact email")
    domain = ""
    if website:
        domain = _parse_website_domain(website)
        if not domain:
            raise HTTPException(status_code=400, detail="invalid website — could not extract a domain")
    req_id_list = [int(x) for x in form.getlist("requirement_ids") if str(x).strip().isdigit()]

    matches = check_vendor_duplicate(vendor_name, db)
    matched_existing = bool(matches) and matches[0]["match"] == "exact"
    contact_added = False

    # TOCTOU: check_vendor_duplicate ran an earlier, separate query, so the matched
    # card can be deleted between that check and this fetch. A None card here would
    # AttributeError → generic 500; instead treat it as "no confident duplicate" and
    # fall through to the create branch (re-resolving the name the user typed).
    matched_card = db.get(VendorCard, matches[0]["id"]) if matched_existing else None
    if matched_existing and matched_card is None:
        matched_existing = False

    if matched_existing:
        # Confident duplicate: hand back the existing card — but a typed email /
        # website must NOT be silently discarded (F4): attach the email as a
        # VendorContact (deduped case-insensitively against the card's existing
        # contacts) and backfill a missing domain. The row's notice reports the
        # email attach explicitly.
        assert matched_card is not None  # narrowed by the TOCTOU guard above
        card = matched_card
        updated = False
        if email:
            existing_emails = {
                (vc.email or "").lower()
                for vc in db.query(VendorContact).filter(VendorContact.vendor_card_id == card.id).all()
            }
            if email.lower() not in existing_emails:
                db.add(
                    VendorContact(
                        vendor_card_id=card.id,
                        email=email,
                        contact_type="company",
                        source="rfq_manual",
                        confidence=80,
                        is_verified=False,
                    )
                )
                contact_added = True
                updated = True
        if domain and not card.domain:
            card.domain = domain
            updated = True
        if updated:
            db.commit()
    else:
        card = VendorCard(
            normalized_name=norm,
            display_name=vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(card)
        db.flush()
        if email:
            db.add(
                VendorContact(
                    vendor_card_id=card.id,
                    email=email,
                    contact_type="company",
                    source="rfq_manual",
                    confidence=80,
                    is_verified=False,
                )
            )
        db.commit()

        # Post-commit background enrichment when a usable domain came with the
        # website. Lazy imports: tests mock _background_enrich_vendor and
        # get_credential_cached at their SOURCE modules (CLAUDE.md), and the
        # coroutine opens its own session (never the request session). F7: the
        # card is already committed — a failure ANYWHERE in this block is logged
        # and the created row is still returned (enrichment is best-effort;
        # turning it into a 500 would misreport a successful create).
        try:
            if card.domain and not card.last_enriched_at:
                from ..services.credential_service import get_credential_cached

                if get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or get_credential_cached(
                    "anthropic_ai", "ANTHROPIC_API_KEY"
                ):
                    from ..utils.async_helpers import safe_background_task
                    from ..utils.vendor_helpers import _background_enrich_vendor

                    await safe_background_task(
                        _background_enrich_vendor(card.id, card.domain, card.display_name),
                        task_name="enrich_vendor_from_composer",
                    )
        except Exception:
            logger.error(
                "Post-create enrichment kickoff failed for vendor card {} — card committed and returned",
                card.id,
                exc_info=True,
            )

    # Active-only unavailability re-check for the selected parts: an excluded
    # vendor renders the rose chip with a DISABLED checkbox and never joins the
    # selection. Both norm spellings are checked (stored normalized_name may be
    # legacy-suffixed), same belt-and-braces as _coverage_ranked_vendor_rows.
    is_excluded = False
    if req_id_list:
        requirements = db.query(Requirement).filter(Requirement.id.in_(req_id_list)).all()
        if requirements:
            excluded = excluded_vendor_norms(db, requirements)
            canonical = normalize_vendor_name(card.display_name or card.normalized_name or "")
            is_excluded = canonical in excluded or (card.normalized_name or "") in excluded

    ctx = {
        "request": request,
        "vendor": card,
        "matched_existing": matched_existing,
        "contact_added": contact_added,
        "is_excluded": is_excluded,
    }
    return template_response("htmx/partials/sightings/composer_vendor_row.html", ctx)


def _best_contacts_by_card(db: Session, card_ids: list[int]) -> list[VendorContact]:
    """Vendor contacts ordered worst-first so a last-wins ``{card_id: c}`` dict keeps
    the BEST contact per vendor.

    VendorContact has no is_primary flag, and a vendor can hold several contacts (an
    rfq_manual row added inline via the composer alongside an Apollo-enriched row). An
    unordered ``{c.vendor_card_id: c for c in contacts}`` lets an arbitrary (possibly
    EMPTY-email) row win, which would silently skip the vendor as "had no email". Ordering
    a usable email LAST (then verified, then higher confidence) makes the real email win.

    An EMPTY-OR-NULL email row sorts FIRST (loses last-wins) — both ``NULL`` and ``''`` are
    unusable (the send path only resolves a non-empty ``contact.email``), and
    ``_cards_with_resolvable_email`` (the has_contact badge) filters ``email != ''`` too. If
    only ``is_(None)`` were checked, a higher-confidence ``''``-email row would win last-wins
    and resolve ``vendor_email=''`` → skip, while the badge promised contactable. Treating
    ``''`` like NULL here keeps the send path consistent with the badge.

    Called by: sightings_preview_inquiry, sightings_send_inquiry.
    """
    if not card_ids:
        return []
    return (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id.in_(card_ids))
        .order_by(
            # Empty-or-NULL email rows first (lose last-wins) — '' is as unusable as NULL.
            or_(VendorContact.email.is_(None), VendorContact.email == "").desc(),
            VendorContact.is_verified.asc().nullsfirst(),  # verified rows last
            VendorContact.confidence.asc().nullsfirst(),  # higher confidence last
            VendorContact.id.asc(),  # deterministic tiebreak (newest row wins ties)
        )
        .all()
    )


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
    unavailable_vendors, vendor_names = _partition_by_unavailability(vendor_names, excluded)

    # LOCKSTEP with send-inquiry: one [ref:{id}] token per involved requisition,
    # ascending requisition id — exactly what send_batch_rfq will append.
    requisition_ids = sorted({r.requisition_id for r in requirements})

    # Batch-fetch vendor cards + contacts (same logic as send-inquiry)
    normalized_names = [normalize_vendor_name(vn) for vn in vendor_names]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    # Order worst-first so the dict's last-wins keeps the BEST contact per vendor: a
    # vendor with multiple contacts (e.g. an rfq_manual row added via the composer plus
    # an Apollo-enriched row) must not pick a NULL-email contact over one that has the
    # real email — that would silently skip the vendor as "had no email".
    contacts = _best_contacts_by_card(db, card_ids)
    contact_map = {c.vendor_card_id: c for c in contacts}

    from ..email_service import _build_html_body

    avail_token = " ".join(f"[ref:{rid}]" for rid in requisition_ids)
    parts_list = [{"mpn": r.primary_mpn, "qty": r.target_qty} for r in requirements]

    # Advisory DNC set — look up which resolved emails are flagged do_not_contact.
    # This mirrors the send-time check in email_service.py so preview ≈ what sends.
    preview_dnc_emails: set[str] = _dnc_emails_for_cards(db, card_ids) if card_ids else set()

    previews = []
    for vn in vendor_names:
        card = card_map.get(normalize_vendor_name(vn))
        vendor_email = ""
        if card:
            contact = contact_map.get(card.id)
            if contact and contact.email:
                vendor_email = contact.email

        # Compute advisory skip reason for the badge in the preview template.
        if not vendor_email:
            skip_reason = SightingsSkipReason.NO_EMAIL
        elif vendor_email.lower() in preview_dnc_emails:
            skip_reason = SightingsSkipReason.DO_NOT_CONTACT
        else:
            skip_reason = SightingsSkipReason.READY

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
                "skip_reason": skip_reason,
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
    if not requirements:
        # Every posted requirement id is stale (rows deleted under an open modal).
        # Without this guard the send would proceed with NO requisition at all —
        # emails out, zero Contact tracking — instead of telling the user.
        raise HTTPException(status_code=400, detail="selected requirements no longer exist — refresh and retry")

    # Send-time re-validation (closes the TOCTOU the modal filter alone leaves open):
    # vendors with an ACTIVE unavailability record on the selected parts are dropped
    # from the send and reported visibly below — never a silent drop.
    excluded = excluded_vendor_norms(db, requirements)
    unavailable_vendors, sendable_vendors = _partition_by_unavailability(vendor_names, excluded)

    # Per-requisition parts map: NO collapse to one arbitrary requisition. Each
    # involved requisition gets its own Contact rows (scoped to its parts) and
    # its own [ref:{id}] subject token inside send_batch_rfq.
    requisition_parts_map: dict[int, list] = {}
    for r in requirements:
        requisition_parts_map.setdefault(r.requisition_id, []).append({"mpn": r.primary_mpn, "qty": r.target_qty})

    # Batch-fetch vendor cards + contacts in two queries instead of N+1
    normalized_names = [normalize_vendor_name(vn) for vn in sendable_vendors]
    cards = db.query(VendorCard).filter(VendorCard.normalized_name.in_(normalized_names)).all()
    card_map = {c.normalized_name: c for c in cards}

    card_ids = [c.id for c in cards]
    # Best-contact-per-vendor (see _best_contacts_by_card): last-wins dict over a
    # worst-first ordering so a non-NULL email always beats a NULL-email row.
    contacts = _best_contacts_by_card(db, card_ids)
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
                vendor_groups=vendor_groups,
                requisition_parts_map=requisition_parts_map,
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
        # A mid-batch crash can leave partial Contact/ActivityLog rows pending on
        # the session — roll them back BEFORE the commit below, or the commit
        # would persist tracking for sends in an unknown state.
        db.rollback()
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


def _qual_dict(
    usage: str,
    refurbished_by: str,
    refurb_process: str,
    cert_doc: str,
    part_condition: str,
    provenance_story: str,
    terms: str,
    lead_time_reason: str,
) -> "dict | None":
    """Build the qualification JSON blob from submitted form values.

    Returns None when all fields are blank (no qualification data to store).
    """
    q = {
        "usage": usage or None,
        "refurbished_by": refurbished_by or None,
        "refurb_process": refurb_process or None,
        "cert_doc": cert_doc or None,
        "part_condition": part_condition or None,
        "provenance_story": provenance_story or None,
        "terms": terms or None,
        "lead_time_reason": lead_time_reason or None,
        "requests": [],
        "schema": 1,  # forward-version the qualification blob (spec §3.1)
    }
    _qual_keys = (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    )
    return q if any(q[k] for k in _qual_keys) else None


def _echo_prefill(
    vendor_name: str,
    mpn: str,
    manufacturer: str,
    qty_available: str,
    unit_price: str,
    lead_time: str,
    date_code: str,
    condition: str,
    packaging: str,
    firmware: str,
    hardware_code: str,
    moq: str,
    spq: str,
    warranty: str,
    country_of_origin: str,
    valid_until: str,
    notes: str,
    usage: str,
    refurbished_by: str,
    refurb_process: str,
    cert_doc: str,
    part_condition: str,
    provenance_story: str,
    terms: str,
    lead_time_reason: str,
) -> dict:
    """Re-build a prefill dict from submitted form values so the modal preserves what
    the buyer typed on a validation error re-render.

    Keys match the input name= attributes in _offer_form_fields.html.
    """
    return {
        "vendor_name": vendor_name,
        "mpn": mpn,
        "manufacturer": manufacturer,
        "qty_available": qty_available,
        "unit_price": unit_price,
        "lead_time": lead_time,
        "date_code": date_code,
        "condition": condition,
        "packaging": packaging,
        "firmware": firmware,
        "hardware_code": hardware_code,
        "moq": moq,
        "spq": spq,
        "warranty": warranty,
        "country_of_origin": country_of_origin,
        "valid_until": valid_until,
        "notes": notes,
        "usage": usage,
        "refurbished_by": refurbished_by,
        "refurb_process": refurb_process,
        "cert_doc": cert_doc,
        "part_condition": part_condition,
        "provenance_story": provenance_story,
        "terms": terms,
        "lead_time_reason": lead_time_reason,
    }


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
        remembered = prefill_from_vendor(db, normalize_vendor_name(vendor_name))
        for k, v in remembered.items():
            prefill.setdefault(k, v)  # only fill empty keys; buyer overrides
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
    usage: str = Form(""),
    refurbished_by: str = Form(""),
    refurb_process: str = Form(""),
    cert_doc: str = Form(""),
    part_condition: str = Form(""),
    provenance_story: str = Form(""),
    terms: str = Form(""),
    lead_time_reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
) -> HTMLResponse:
    """Create an offer for this part via the canonical create_offer, then re-render the
    offers panel.

    Reused for both Convert-to-offer and Enter-offer.
    """
    from ..routers.crm.offers import create_offer
    from ..schemas.crm import OfferCreate
    from ..services.offer_qualification import essentials_data, normalize_offer_condition, validate_essentials

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    # Build (and structurally validate) the payload FIRST so a bad numeric/date is
    # reported as a 422 (not masked by the essentials gate below or crashed as a 500).
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
            qualification=_qual_dict(
                usage,
                refurbished_by,
                refurb_process,
                cert_doc,
                part_condition,
                provenance_story,
                terms,
                lead_time_reason,
            ),
        )
    except ValidationError as e:
        # Surface as a 422 (not a 500) so a bad numeric/date is reported, not crashed.
        raise RequestValidationError(e.errors()) from e

    # Gate: validate the buyer's submitted essentials BEFORE delegating to the
    # canonical builder (which no longer blocks). On a missing essential, re-render the
    # modal with inline errors and do not persist. Uses the schema-normalized condition.
    gate_errors = validate_essentials(
        normalize_offer_condition(payload.condition) or "new",
        essentials_data(
            manufacturer=manufacturer,
            packaging=packaging,
            usage=usage,
            refurbished_by=refurbished_by,
            refurb_process=refurb_process,
            cert_doc=cert_doc,
            part_condition=part_condition,
        ),
    )
    if gate_errors:
        prefill = _echo_prefill(
            vendor_name,
            mpn,
            manufacturer,
            qty_available,
            unit_price,
            lead_time,
            date_code,
            condition,
            packaging,
            firmware,
            hardware_code,
            moq,
            spq,
            warranty,
            country_of_origin,
            valid_until,
            notes,
            usage,
            refurbished_by,
            refurb_process,
            cert_doc,
            part_condition,
            provenance_story,
            terms,
            lead_time_reason,
        )
        ctx = {
            "request": request,
            "requirement": requirement,
            "offer": None,
            "prefill": prefill,
            "errors": gate_errors,
        }
        return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)

    # The canonical create_offer fires the offer-hook release itself
    # (maybe_release_on_offer) — no route-level call needed here. Essentials were already
    # gated above, so create_offer no longer rejects on qualification grounds.
    await create_offer(requirement.requisition_id, payload, user=user, db=db)
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
    # Scope the offer to the path requirement (IDOR guard — prevents a guessed offer_id
    # from a different requirement from being approved/rejected).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
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
    # Scope the offer to the path requirement (IDOR guard).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
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
    # Scope the offer to the path requirement (IDOR guard).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
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
    # Scope the offer to the path requirement (IDOR guard).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
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
    # Scope the offer to the path requirement (prevents cross-requirement IDOR via a
    # guessed offer_id); 404 if the offer belongs to another requirement.
    if offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
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

    def _json_safe(v: object) -> object:
        """Coerce DB field values to JSON-serializable types for |tojson in Alpine
        x-data."""
        if v is None:
            return ""
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    prefill = {f: _json_safe(getattr(offer, f)) for f in fields}
    # Repopulate the qualification chips/inputs from the stored JSON so the Alpine panel
    # reflects current state on edit (otherwise the chips render empty and a re-save would
    # appear to clear them). Keys match the offerQualification x-data names.
    _q = offer.qualification or {}
    for _qk in (
        "usage",
        "refurbished_by",
        "refurb_process",
        "cert_doc",
        "part_condition",
        "provenance_story",
        "terms",
        "lead_time_reason",
    ):
        prefill[_qk] = _json_safe(_q.get(_qk))
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
    usage: str = Form(""),
    refurbished_by: str = Form(""),
    refurb_process: str = Form(""),
    cert_doc: str = Form(""),
    part_condition: str = Form(""),
    provenance_story: str = Form(""),
    terms: str = Form(""),
    lead_time_reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
) -> HTMLResponse:
    """Update an offer via the canonical update_offer, then re-render the panel."""
    from ..routers.crm.offers import update_offer
    from ..schemas.crm import OfferUpdate
    from ..services.offer_qualification import essentials_data, normalize_offer_condition, validate_essentials

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")

    # Load the offer FIRST and scope it to the path requirement (prevents cross-requirement
    # IDOR via a guessed offer_id; 404 if missing or owned by another requirement).
    offer = db.get(Offer, offer_id)
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})

    # MERGE-not-overwrite: start from the stored qualification JSON and overlay only the
    # SUBMITTED non-empty qual fields. This preserves the logged #7 `requests` array and any
    # optional keys (provenance_story / terms / lead_time_reason) that aren't on this form
    # submission — the previous always-rebuild path wiped them on every edit.
    merged_qual = dict(offer.qualification or {})
    _submitted_qual = {
        "usage": usage or None,
        "refurbished_by": refurbished_by or None,
        "refurb_process": refurb_process or None,
        "cert_doc": cert_doc or None,
        "part_condition": part_condition or None,
        "provenance_story": provenance_story or None,
        "terms": terms or None,
        "lead_time_reason": lead_time_reason or None,
    }
    for _k, _v in _submitted_qual.items():
        if _v:
            merged_qual[_k] = _v
    # Always preserve the existing requests list (never reset it to []); copy it last so it
    # can't be clobbered by anything overlaid above.
    merged_qual["requests"] = list((offer.qualification or {}).get("requests") or [])
    # Forward-version the blob (spec §3.1).
    merged_qual["schema"] = 1
    # If nothing meaningful is stored (only the structural keys), persist None.
    _qual_value_keys = (*_submitted_qual.keys(),)
    qualification_to_store = (
        merged_qual if (any(merged_qual.get(k) for k in _qual_value_keys) or merged_qual["requests"]) else None
    )

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
            qualification=qualification_to_store,
        )
    except ValidationError as e:
        raise RequestValidationError(e.errors()) from e

    # Gate: validate the MERGED essentials BEFORE delegating to the canonical builder
    # (payload already structurally validated above, so a bad numeric is a 422 either way).
    # Using merged data means editing an unrelated field on a pulls/refurb offer whose stored
    # usage/process is intact is NOT falsely blocked. On a missing essential, re-render the
    # modal with inline errors and do not persist.
    norm_condition = normalize_offer_condition(payload.condition)
    if norm_condition:
        gate_errors = validate_essentials(
            norm_condition,
            essentials_data(
                manufacturer=manufacturer,
                packaging=packaging,
                usage=merged_qual.get("usage"),
                refurbished_by=merged_qual.get("refurbished_by"),
                refurb_process=merged_qual.get("refurb_process"),
                cert_doc=merged_qual.get("cert_doc"),
                part_condition=merged_qual.get("part_condition"),
            ),
        )
        if gate_errors:
            prefill = _echo_prefill(
                vendor_name,
                mpn,
                manufacturer,
                qty_available,
                unit_price,
                lead_time,
                date_code,
                condition,
                packaging,
                firmware,
                hardware_code,
                moq,
                spq,
                warranty,
                country_of_origin,
                valid_until,
                notes,
                usage,
                refurbished_by,
                refurb_process,
                cert_doc,
                part_condition,
                provenance_story,
                terms,
                lead_time_reason,
            )
            ctx = {
                "request": request,
                "requirement": requirement,
                "offer": offer,
                "prefill": prefill,
                "errors": gate_errors,
            }
            return template_response("htmx/partials/sightings/offer_form_modal.html", ctx)

    await update_offer(offer_id, payload, user=user, db=db)
    db.expire_all()
    return _with_toast(_refresh_offers_panel(request, requirement_id, db), "Offer updated")


@router.post("/v2/partials/sightings/{requirement_id}/offers/{offer_id}/request", response_class=HTMLResponse)
async def sightings_offer_request(
    request: Request,
    requirement_id: int,
    offer_id: int,
    kind: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
):
    """Log a pending vendor request (images / FPQ / cert / pkg qty) on an offer.

    Logs the request to offer.qualification['requests'] (status="pending") and returns
    the drafted RFQ-back line as a toast. This route is require_buyer-only (NO Graph
    token) so logging never 401s on an expired token. Sending a logged PENDING request
    as a real email is now available on demand via the separate token-bearing route
    `.../offers/{offer_id}/request/{index}/send` (sightings_offer_request_send) — the
    buyer no longer has to copy the draft into the solicit modal by hand.
    """
    from datetime import datetime, timezone

    from ..services.offer_qualification import REQUEST_KINDS, request_template

    if kind not in REQUEST_KINDS:
        raise HTTPException(status_code=400, detail={"error": "invalid request kind"})
    offer = db.get(Offer, offer_id)
    # Scope the offer to the path requirement (prevents cross-requirement IDOR via a
    # guessed offer_id); 404 if the offer is missing or belongs to another requirement.
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})
    draft = request_template(kind, offer.mpn)
    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    reqs.append(
        {
            "kind": kind,
            "status": "pending",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "contact_id": None,
        }
    )
    q["requests"] = reqs
    offer.qualification = q
    db.commit()
    db.expire_all()
    return _append_oob_toast(_refresh_offers_panel(request, requirement_id, db), f"Logged request: {draft}")


@router.post(
    "/v2/partials/sightings/{requirement_id}/offers/{offer_id}/request/{index}/send",
    response_class=HTMLResponse,
)
async def sightings_offer_request_send(
    request: Request,
    requirement_id: int,
    offer_id: int,
    index: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_buyer),
    token: str = Depends(require_fresh_token),
):
    """Send a previously-logged PENDING #7 vendor request as a real RFQ-back email.

    Distinct from the logging route (sightings_offer_request) on purpose: this route
    also requires a fresh Graph token (require_fresh_token) so the actual send fails
    loudly on an expired token, while LOGGING a request never 401s. `index` addresses
    the entry in offer.qualification['requests'] (append-only, so the index is stable).

    Flow: resolve the vendor's best contact email (_best_contacts_by_card, mirroring the
    batch send-inquiry path), draft the request body via request_template, and hand a
    single vendor group to send_batch_rfq with the SCALAR requisition_id (single-req
    mode; passing both the scalar and a parts-map raises ValueError). send_batch_rfq
    commits internally and can expire the session, so the entry-status update is applied
    AFTER it returns against a freshly re-fetched offer. Idempotent on an already-"sent"
    entry; a single request is logged as an outreach activity but does NOT auto-progress
    the sourcing status (one clarification is not a full RFQ round).
    """
    from datetime import datetime, timezone

    from ..email_service import send_batch_rfq
    from ..services.offer_qualification import request_template

    offer = db.get(Offer, offer_id)
    # Scope the offer to the path requirement (prevents cross-requirement IDOR via a
    # guessed offer_id); 404 if the offer is missing or belongs to another requirement.
    if offer is None or offer.requirement_id != requirement_id:
        raise HTTPException(status_code=404, detail={"error": "offer not found for this requirement"})

    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    if index < 0 or index >= len(reqs):
        raise HTTPException(status_code=404, detail={"error": "request not found"})
    # Copy the entry into a fresh nested dict (the dict()/list() above are SHALLOW, so
    # reqs[index] is still the committed-JSON baseline object — see the post-send block).
    entry = dict(reqs[index])
    reqs[index] = entry

    # Idempotency: a request already sent is never re-sent (the entry is the durable
    # record of the outreach). Surface an info toast, leave state untouched.
    if entry.get("status") == "sent":
        return _append_oob_toast(
            _refresh_offers_panel(request, requirement_id, db),
            "Request already sent",
            "info",
        )

    # Requisition guard: Contact.requisition_id is NOT NULL, so send_batch_rfq can write
    # no tracking row without a requisition. An offer with no requisition (unsolicited
    # inbound) is marked "skipped" rather than firing an untracked email.
    if offer.requisition_id is None:
        entry["status"] = "skipped"
        q["requests"] = reqs
        offer.qualification = q
        db.commit()
        return _append_oob_toast(
            _refresh_offers_panel(request, requirement_id, db),
            "No requisition on this offer — not sent",
            "warning",
        )

    # Resolve the vendor's BEST contact email exactly as the batch send path does:
    # worst-first ordering + last-wins dict so a real email always beats a NULL/'' row.
    contact_map = (
        {c.vendor_card_id: c for c in _best_contacts_by_card(db, [offer.vendor_card_id])}
        if offer.vendor_card_id
        else {}
    )
    contact = contact_map.get(offer.vendor_card_id)
    vendor_email = contact.email if contact and contact.email else ""

    draft = request_template(entry["kind"], offer.mpn)
    requirement = db.get(Requirement, requirement_id)
    parts = [{"mpn": offer.mpn, "qty": requirement.target_qty if requirement else None}]

    # Single-requisition mode: pass the SCALAR requisition_id (one Contact row, parts
    # from the vendor group). Passing requisition_parts_map AS WELL would raise ValueError.
    results = await send_batch_rfq(
        token=token,
        db=db,
        user_id=user.id,
        requisition_id=offer.requisition_id,
        vendor_groups=[
            {
                "vendor_name": offer.vendor_name,
                "vendor_email": vendor_email,
                "parts": parts,
                "subject": f"Request: {entry['kind']} — {offer.mpn}",
                "body": draft,
            }
        ],
    )

    # CRITICAL: send_batch_rfq does its own db.commit() and can expire the session, so
    # re-fetch the offer and re-read the requests list before mutating the entry status.
    offer = db.get(Offer, offer_id)
    q = dict(offer.qualification or {})
    reqs = list(q.get("requests") or [])
    # COPY the target entry into a fresh nested dict before mutating: dict(q)/list(reqs)
    # are SHALLOW, so reqs[index] is still the SAME object SQLAlchemy holds as the
    # committed JSON baseline. Mutating it in place would make the new value equal the
    # (already-mutated) old value and the JSON flush would write nothing. Re-slotting a
    # fresh dict keeps the change detectable.
    entry = dict(reqs[index])
    reqs[index] = entry

    r = results[0]
    if r["status"] == "sent":
        entry["status"] = "sent"
        entry["contact_id"] = r.get("id")
        entry["sent_at"] = datetime.now(timezone.utc).isoformat()
        toast_msg, toast_level = (f"Request sent to {offer.vendor_name}", "success")
        # Log the outreach (mirrors sightings_send_inquiry's rfq_sent), but deliberately
        # NO auto_progress: one clarification request is not a full RFQ round.
        log_rfq_activity(
            db=db,
            rfq_id=offer.requisition_id,
            activity_type="rfq_sent",
            description=f"Requested {entry['kind']} from {offer.vendor_name}",
            user_id=user.id,
            requirement_id=requirement_id,
        )
    elif r["status"] == "skipped":
        entry["status"] = "skipped"
        toast_msg, toast_level = ("Not sent — no contact email on file", "warning")
    else:
        entry["status"] = "failed"
        entry["error"] = r.get("error")
        toast_msg, toast_level = (f"Send failed for {offer.vendor_name}", "error")

    # Reassign a NEW dict so SQLAlchemy's JSON change detection persists the mutation.
    q["requests"] = reqs
    offer.qualification = q
    db.commit()

    return _append_oob_toast(
        _refresh_offers_panel(request, requirement_id, db),
        toast_msg,
        toast_level,
    )
