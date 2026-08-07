"""Sourcing board — workspace, list/filters/table, CSV export, batch actions.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Final

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload
from starlette.datastructures import FormData

from ...config import settings
from ...constants import (
    RESTRICTED_ROLES,
    AccessKey,
    ActivityType,
    Channel,
    OfferStatus,
    SourcingStatus,
    UserRole,
)
from ...database import get_db
from ...dependencies import (
    require_access,
    require_requisition_access_bulk,
    require_user,
)
from ...models import User
from ...models.intelligence import ActivityLog
from ...models.offers import Offer
from ...models.sourcing import Requirement, Requisition, Sighting
from ...models.vendor_sighting_summary import VendorSightingSummary
from ...schemas.sightings import SightingsListParams
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from ...utils.sql_helpers import escape_like
from .common import (  # noqa: F401
    _EXCLUDED_REQ_STATUSES,
    _EXCLUDED_SOURCING_STATUSES,
    _SEARCH_FANOUT_LIMIT,
    MAX_BATCH_SIZE,
    _active_sourcing_status_clause,
    _append_oob_toast,
    _best_contacts_by_card,
    _get_cached,
    _invalidate_cache,
    _mpn_link_map,
    _oob_toast,
    _oob_toast_html,
    _publish_if_user_source,
    _refresh_offers_panel,
    _render_offers_panel,
    _toast_suppressed_for_sse,
    _with_toast,
    router,
)

# Sort key → (column for asc, column for desc)
_SORT_COLUMNS = {
    "priority": (Requirement.priority_score.asc().nullslast(), Requirement.priority_score.desc().nullslast()),
    "mpn": (Requirement.primary_mpn.asc(), Requirement.primary_mpn.desc()),
    "created": (Requirement.created_at.asc(), Requirement.created_at.desc()),
    "status": (Requirement.sourcing_status.asc(), Requirement.sourcing_status.desc()),
}


@router.get("/v2/partials/sightings/workspace", response_class=HTMLResponse)
async def sightings_workspace(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_access(AccessKey.SIGHTINGS)),
):
    """Return the split-panel workspace layout.

    The table loads via hx-get inside. The follow-ups quick-link carries its pending
    count so a buyer can jump straight to the queue from their home screen; the count is
    computed once here (not on the hot table-refresh path).
    """
    from ..htmx.offers.follow_ups import follow_up_count

    ctx = {
        "request": request,
        "user": user,
        "follow_up_count": follow_up_count(db, user),
    }
    return template_response("htmx/partials/sightings/list.html", ctx)


def build_board_requirement_query(db: Session, user: User, filters: SightingsListParams):
    """Build the filtered Requirement query shared by the sightings board list and CSV
    export.

    Applies the identical active-board scoping (open requisitions, non-terminal sourcing
    status) plus EVERY board filter — status, urgent, stale, sales_person, assigned,
    search, manufacturer — and the restricted-role ownership boundary. It adds NO
    ordering, pagination, or eager-load options, so each caller layers those on. This is
    the single source of truth for "what the board matches" so the export never diverges
    from the list.
    """
    query = (
        db.query(Requirement)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status.notin_(_EXCLUDED_REQ_STATUSES))
        .filter(_active_sourcing_status_clause())
        # Exclude the resell mirror's system-owned "Customer Excess (list N)" scratch
        # requisition — supply advertising, not buyer demand a human should source
        # (finding #25, THEME F).
        .filter(Requisition.is_scratch.is_(False))
    )

    # Ownership boundary: restricted roles (SALES/TRADER) see only their own requisitions'
    # parts — mirrors the requisition list (requisitions/core.py). Without this a TRADER
    # could enumerate every customer's sightings/pricing via this list.
    if user.role in RESTRICTED_ROLES:
        query = query.filter(Requisition.created_by == user.id)

    stale_threshold = datetime.now(UTC) - timedelta(days=settings.sighting_stale_days)
    deadline_48h = date.today() + timedelta(days=2)

    if filters.status:
        query = query.filter(Requirement.sourcing_status == filters.status)
    # Urgent quick filter — same predicate as the "urgent" counter (priority >= 70 OR
    # need_by within 48h).
    if filters.urgent:
        query = query.filter((Requirement.priority_score >= 70) | (Requirement.need_by_date <= deadline_48h))
    # Stale quick filter — active requirements with no ActivityLog inside the stale
    # window (mirrors the "stale" counter's NOT-IN-recent-activity set).
    if filters.stale:
        recent_activity = (
            db.query(ActivityLog.requirement_id)
            .filter(ActivityLog.requirement_id.isnot(None))
            .group_by(ActivityLog.requirement_id)
            .having(sqlfunc.max(ActivityLog.created_at) >= stale_threshold)
        )
        query = query.filter(~Requirement.id.in_(recent_activity.subquery().select()))
    if filters.sales_person:
        safe = escape_like(filters.sales_person)
        query = query.join(User, Requisition.created_by == User.id).filter(User.name.ilike(f"%{safe}%", escape="\\"))
    if filters.assigned == "mine":
        query = query.filter(Requirement.assigned_buyer_id == user.id)
    if filters.q:
        safe_q = escape_like(filters.q)
        query = query.filter(
            Requirement.primary_mpn.ilike(f"%{safe_q}%", escape="\\")
            | Requisition.customer_name.ilike(f"%{safe_q}%", escape="\\")
            | Requirement.substitutes_text.ilike(f"%{safe_q}%", escape="\\")
        )
    if filters.manufacturer:
        safe_mfr = escape_like(filters.manufacturer)
        query = query.filter(Requirement.manufacturer.ilike(f"%{safe_mfr}%", escape="\\"))
    return query


@router.get("/v2/partials/sightings", response_class=HTMLResponse)
async def sightings_list(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    filters: SightingsListParams = Depends(),
):
    """Return the sightings table partial with filters and pagination."""
    return await _render_sightings_table(request, db, user, filters)


async def _render_sightings_table(
    request: Request,
    db: Session,
    user: User,
    filters: SightingsListParams,
    searching_req_ids: frozenset[int] | set[int] = frozenset(),
) -> HTMLResponse:
    """Build and render the sightings board table partial.

    Shared by the GET list route and the POST batch-refresh handler. batch-refresh passes
    ``searching_req_ids`` — the requirements whose background sourcing search it just
    scheduled — so their Coverage cell renders an immediate "Searching…" badge while the
    fan-out runs off the request thread.
    """
    # Board filters live in the shared builder so the CSV export applies the EXACT same
    # predicates — the download can never drift from what the board shows.
    query = build_board_requirement_query(db, user, filters).options(
        joinedload(Requirement.requisition).joinedload(Requisition.creator)
    )

    # Thresholds reused below by the dashboard-strip counters + heatmap (the builder applies
    # its own copies to the query). UTC-aware to line up with UTCDateTime columns.
    stale_threshold = datetime.now(UTC) - timedelta(days=settings.sighting_stale_days)
    deadline_48h = date.today() + timedelta(days=2)

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
            .filter(_active_sourcing_status_clause())
            .filter(Requisition.is_scratch.is_(False))
            .group_by(Requirement.sourcing_status)
            .all()
        ),
    )

    top_vendors = {}
    coverage_map = {}
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
            # nullslast(): a NULL score (e.g. a summary row never scored) must never
            # outrank a real scored vendor — Postgres' DESC default sorts NULLs FIRST
            # (finding #26, THEME F).
            .order_by(VendorSightingSummary.requirement_id, VendorSightingSummary.score.desc().nullslast())
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
    # Active requirement IDs for dashboard (not just current page)
    active_req_select = (
        db.query(Requirement.id)
        .join(Requisition, Requirement.requisition_id == Requisition.id)
        .filter(Requisition.status.notin_(_EXCLUDED_REQ_STATUSES))
        .filter(_active_sourcing_status_clause())
        # Mirror's virtual "Customer Excess" requirement never has an assigned buyer or
        # ActivityLog, which would otherwise inflate the Unassigned/Stale badges the board
        # shows directly above the now-filtered list (finding #25, THEME F).
        .filter(Requisition.is_scratch.is_(False))
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

    # ── Assignable buyers (action-bar "Assign to buyer" picker) ──
    # Active buyers + traders — the roles a requirement can be assigned to (mirrors the
    # buy-plan/leaderboard buyer pool). Feeds the multi-select action bar's Assign dropdown.
    assignable_buyers = (
        db.query(User)
        .filter(User.role.in_([UserRole.BUYER, UserRole.TRADER]), User.is_active.is_(True))
        .order_by(User.name)
        .all()
    )

    # ── MPN → MaterialCard link map ─────────────────────────────
    link_map = _mpn_link_map(db, requirements) if requirements else {}

    groups: dict[str, list] | None = None
    if filters.group_by in ("brand", "manufacturer"):
        groups = {}
        for r in requirements:
            # Group by the ENRICHED value from the linked material card (the manufacturer/
            # brand derived from the MPN) first; fall back to the requirement's own field.
            # The requirement's raw brand/manufacturer is blank on most rows (customer input),
            # so keying off it alone collapsed nearly every part into "Unknown" — making the
            # By-Brand / By-Manufacturer grouping look broken. material_card is lazy="joined"
            # (already loaded), so this adds no query.
            mc = r.material_card
            key = (
                (getattr(mc, filters.group_by, None) if mc else None) or getattr(r, filters.group_by, None) or "Unknown"
            )
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
        "searching_req_ids": searching_req_ids,
        "assignable_buyers": assignable_buyers,
    }
    return template_response("htmx/partials/sightings/table.html", ctx)


async def _rerender_board_with_toast(
    request: Request,
    db: Session,
    user: User,
    form: FormData,
    msg: str,
    level: str = "success",
    *,
    include_status: bool = True,
) -> HTMLResponse:
    """Re-render the sightings board table from the filters the action-bar form carried,
    then attach the toast via the HX-Trigger bridge.

    Shared by the multi-select batch handlers (assign/status/notes) so applying a bulk
    action refreshes the affected rows in place instead of leaving stale status/notes/buyer.
    Callers set ``hx-target="#sightings-table" hx-swap="innerHTML"``, mirroring batch-refresh.

    ``include_status=False`` is used by batch-status, whose ``status`` form field is the
    *target* status to apply (not a board filter) — so the refreshed board shows all
    statuses, keeping the just-moved rows visible with their fresh badges.
    """

    def _fs(key: str) -> str:
        val = form.get(key, "")
        return val if isinstance(val, str) else ""

    filters = SightingsListParams(
        status=_fs("status") if include_status else "",
        q=_fs("q"),
        group_by=_fs("group_by"),
        manufacturer=_fs("manufacturer"),
    )
    resp = await _render_sightings_table(request, db, user, filters)
    return _with_toast(resp, msg, level)


# CSV export column order — the REAL electronic-component sighting fields (vendor stock of a
# part), not the generic species/location columns. Header row written first, then one row per
# matching sighting.
_EXPORT_COLUMNS: Final = [
    "Requirement ID",
    "Customer",
    "Requirement MPN",
    "Sighting MPN",
    "Manufacturer",
    "Vendor",
    "Source",
    "Qty Available",
    "Unit Price",
    "Currency",
    "Condition",
    "MOQ",
    "Lead Time (Days)",
    "Score",
    "Evidence Tier",
    "Seen Date",
]


def _fmt_seen_date(dt: datetime | None) -> str:
    """Readable minute-precision timestamp for CSV (empty string when missing)."""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _sighting_export_rows(db: Session, user: User, filters: SightingsListParams):
    """Yield one raw CSV row per Sighting on a board-matching requirement (header via
    stream_csv).

    Reuses build_board_requirement_query so the exported set is exactly the board's
    filtered requirements expanded to their underlying vendor sightings — with NO
    pagination. Rows stream lazily via yield_per so memory stays flat regardless of row
    count.
    """
    matching_req_ids = build_board_requirement_query(db, user, filters).with_entities(Requirement.id).subquery()
    rows = (
        db.query(Sighting)
        .filter(Sighting.requirement_id.in_(matching_req_ids.select()))
        .options(joinedload(Sighting.requirement).joinedload(Requirement.requisition))
        .order_by(Sighting.requirement_id, Sighting.score.desc().nullslast())
        .yield_per(500)
    )
    for s in rows:
        req = s.requirement
        requisition = req.requisition if req else None
        yield (
            s.requirement_id,
            requisition.customer_name if requisition else None,
            req.primary_mpn if req else None,
            s.mpn_matched,
            s.manufacturer,
            s.vendor_name,
            s.source_type,
            s.qty_available,
            s.unit_price,
            s.currency,
            s.condition,
            s.moq,
            s.lead_time_days,
            s.score,
            s.evidence_tier,
            _fmt_seen_date(s.source_searched_at or s.created_at),
        )


@router.get("/v2/sightings/export")
async def sightings_export(
    db: Session = Depends(get_db),
    user: User = Depends(require_access(AccessKey.EXPORT_BULK_DATA)),
    filters: SightingsListParams = Depends(),
):
    """Stream every board-matching Sighting as a CSV download (attachment, no
    pagination).

    Gated on EXPORT_BULK_DATA (ISS-028 bulk dataset export lockdown — admin-by-default,
    per-user override possible). Same filters as the board list route — the export
    mirrors the board's active view (status/urgent/stale/search/manufacturer/
    assignment/ownership).
    """
    return stream_csv("sightings_export.csv", _EXPORT_COLUMNS, _sighting_export_rows(db, user, filters))


@router.post("/v2/partials/sightings/batch-assign", response_class=HTMLResponse)
async def sightings_batch_assign(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Batch-assign a buyer to multiple requirements."""
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    try:
        requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
        if not isinstance(requirement_ids, list):
            requirement_ids = []
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="Invalid requirement_ids format") from e
    buyer_id_str = form.get("buyer_id", "")
    buyer_id = int(buyer_id_str) if buyer_id_str else None

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    if not requirement_ids:
        return _oob_toast("No requirements selected", "warning")

    int_ids = [int(rid) for rid in requirement_ids]
    reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()
    require_requisition_access_bulk(db, (r.requisition_id for r in reqs), user, label="Requirement")

    buyer_name = "nobody"
    if buyer_id:
        buyer = db.get(User, buyer_id)
        buyer_name = buyer.name if buyer else f"user {buyer_id}"

    for r in reqs:
        r.assigned_buyer_id = buyer_id
    db.commit()

    _invalidate_cache("sightings_stat_counts")
    msg = f"Assigned {len(reqs)} requirement{'s' if len(reqs) != 1 else ''} to {buyer_name}"
    # Re-render the board so the assigned rows reflect their new buyer in place (the form
    # targets #sightings-table with hx-swap="innerHTML"). The toast rides HX-Trigger.
    return await _rerender_board_with_toast(request, db, user, form, msg)


@router.post("/v2/partials/sightings/batch-status", response_class=HTMLResponse)
async def sightings_batch_status(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Batch-update sourcing status on multiple requirements with transition
    validation."""
    from ...services.status_machine import validate_transition

    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    try:
        requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
        if not isinstance(requirement_ids, list):
            requirement_ids = []
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="Invalid requirement_ids format") from e
    new_status = form.get("status", "")

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    if not requirement_ids:
        return _with_toast(HTMLResponse(""), "No requirements selected", "warning")

    try:
        SourcingStatus(new_status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid status: {new_status}") from e

    int_ids = [int(rid) for rid in requirement_ids]
    reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()
    require_requisition_access_bulk(db, (r.requisition_id for r in reqs), user, label="Requirement")

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
    # Re-render the board so the updated rows show their fresh status in place (the form
    # targets #sightings-table with hx-swap="innerHTML"). include_status=False: the
    # ``status`` field here is the applied TARGET, not a board filter, so the refresh shows
    # all statuses and the just-moved rows stay visible with their new badges.
    return await _rerender_board_with_toast(request, db, user, form, msg, level, include_status=False)


@router.post("/v2/partials/sightings/batch-notes", response_class=HTMLResponse)
async def sightings_batch_notes(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Add a note to multiple requirements."""
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    try:
        requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
        if not isinstance(requirement_ids, list):
            requirement_ids = []
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="Invalid requirement_ids format") from e
    notes = form.get("notes", "").strip()

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    if not requirement_ids:
        return _with_toast(HTMLResponse(""), "No requirements selected", "warning")

    if not notes:
        return _with_toast(HTMLResponse(""), "Note text is required", "warning")

    int_ids = [int(rid) for rid in requirement_ids]
    reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()
    require_requisition_access_bulk(db, (r.requisition_id for r in reqs), user, label="Requirement")

    for r in reqs:
        activity = ActivityLog(
            user_id=user.id,
            activity_type=ActivityType.NOTE,
            channel=Channel.MANUAL,
            requirement_id=r.id,
            requisition_id=r.requisition_id,
            notes=notes,
        )
        db.add(activity)

    db.commit()

    count = len(reqs)
    msg = f"Added note to {count} requirement{'s' if count != 1 else ''}"
    # Re-render the board so the noted rows reflect the activity in place (the form targets
    # #sightings-table with hx-swap="innerHTML"). The toast rides HX-Trigger.
    return await _rerender_board_with_toast(request, db, user, form, msg)
