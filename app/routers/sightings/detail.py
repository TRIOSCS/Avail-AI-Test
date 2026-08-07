"""Requirement detail panel — render, background refresh, row actions.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...constants import (
    CONDITION_SPECIFIC_REASONS,
    ActivityType,
    OfferStatus,
    ReleaseTrigger,
    SourcingStatus,
    UnavailabilityReason,
)
from ...database import get_db
from ...dependencies import (
    require_requisition_access,
    require_requisition_access_bulk,
    require_user,
)
from ...models import User
from ...models.intelligence import ActivityLog, MaterialCard
from ...models.offers import Offer
from ...models.sourcing import Requirement, Requisition, Sighting
from ...models.vendor_sighting_summary import VendorSightingSummary
from ...models.vendors import VendorCard, VendorContact
from ...schemas.sightings import SightingsListParams
from ...services.activity_service import log_rfq_activity
from ...services.part_offers import part_offers_for
from ...services.sighting_status import compute_vendor_statuses
from ...services.status_machine import SOURCING_TRANSITIONS, require_valid_transition
from ...services.vendor_unavailability import (
    clear_unavailability,
    condition_matches,
    record_unavailability,
    sighting_vendor_norm,
    unavailability_for_requirement,
)
from ...template_env import template_response
from ...utils.normalization import normalize_condition
from ...vendor_utils import normalize_vendor_name
from .board import _render_sightings_table
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


def _annotated_unavailability(
    db: Session, requirement: Requirement, vendor_names: list[str]
) -> dict[str, dict[str, Any]]:
    """Template-facing unavailability intel: vendor display name → plain dict.

    Wraps unavailability_for_requirement() with the small render-only enrichments the
    three-state vendor-row UI needs (reason label, marker name, has_unstamped_row for
    the rows-win state selection) so the template stays dumb and never re-derives
    policy. Vendors with no matching record are absent.
    """
    # PERF-8: load the requirement's sightings once and share the single scan between the
    # unavailability lookup and the has_unstamped_row grouping below — both operate on the
    # identical set (Sighting.requirement_id == requirement.id). Skip the scan entirely
    # when there are no vendors to annotate (unavailability_for_requirement returns {}
    # without touching sightings in that case, so the pre-fix query count is unchanged).
    rows = db.query(Sighting).filter(Sighting.requirement_id == requirement.id).all() if vendor_names else []
    raw = unavailability_for_requirement(db, requirement, vendor_names, sightings=rows)
    if not raw:
        return {}
    # Group sightings by vendor norm for condition-scoped has_unstamped_row computation.
    sightings_by_vendor: dict[str, list[Sighting]] = {}
    for s in rows:
        sightings_by_vendor.setdefault(sighting_vendor_norm(s), []).append(s)
    creator_ids = {i.record.created_by_id for i in raw.values() if i.record.created_by_id is not None}
    creator_names: dict[int, str] = (
        dict(db.query(User.id, User.name).filter(User.id.in_(creator_ids)).all()) if creator_ids else {}
    )
    annotated: dict[str, dict[str, Any]] = {}
    for vendor_name, item in raw.items():
        rec = item.record
        vendor_norm = normalize_vendor_name(vendor_name)
        # An unstamped sighting only counts when it matches the record's condition:
        # NULL record (all-conditions) → any unstamped sighting counts.
        # Specific-condition record → only same-condition unstamped sightings count.
        rec_cond = rec.condition
        has_unstamped_row = any(
            not s.is_unavailable and condition_matches(rec_cond, normalize_condition(s.condition))
            for s in sightings_by_vendor.get(vendor_norm, [])
        )
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
            "condition": rec_cond,
            "has_unstamped_row": has_unstamped_row,
        }
    return annotated


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

    # Read-IDOR gate: this panel exposes vendor sightings, pricing, and contacts — restrict
    # to users who may access the owning requisition (matches the other sightings routes).
    require_requisition_access(db, requirement.requisition_id, user)

    requisition = db.get(Requisition, requirement.requisition_id)

    summaries = (
        db.query(VendorSightingSummary)
        .filter(VendorSightingSummary.requirement_id == requirement_id)
        # nullslast(): a NULL score must never outrank a real scored vendor — Postgres'
        # DESC default sorts NULLs FIRST (finding #26, THEME F).
        .order_by(VendorSightingSummary.score.desc().nullslast())
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
    from ...scoring import explain_lead, score_sighting_v2_breakdown

    # Deterministic score-hover feed: for each vendor, the persisted score_components of
    # its highest-scoring sighting (which defines the summary score) → the exact weighted
    # drivers behind the displayed Score. Keyed on the same normalized-lowercase vendor
    # name the summary rows carry (see sighting_aggregation grouping). Best-effort — a
    # vendor without stored components simply gets no breakdown popover.
    winning_components: dict[str, tuple[float, dict]] = {}
    for vn_raw, sc, comp in (
        db.query(Sighting.vendor_name, Sighting.score, Sighting.score_components)
        .filter(Sighting.requirement_id == requirement_id, Sighting.is_unavailable.isnot(True))
        .all()
    ):
        if not comp:
            continue
        key = (vn_raw or "unknown").lower().strip()
        if key not in winning_components or (sc or 0.0) > winning_components[key][0]:
            winning_components[key] = (sc or 0.0, comp)

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
            age_days = (datetime.now(UTC) - s.newest_sighting_at).days

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

        _wc = winning_components.get(s.vendor_name)
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
            "score_breakdown": score_sighting_v2_breakdown(_wc[1]) if _wc else None,
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
            days_since = (datetime.now(UTC) - last_rfq).days
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
        .filter(_active_sourcing_status_clause())
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


async def _run_search_and_publish(
    requirement_ids: list[int],
    user_id: int,
    source: str = "user",
) -> None:
    """Background job: run ``search_requirement`` for each requirement (bounded
    concurrency) then publish a ``sighting-updated`` SSE per requirement.

    Runs AFTER the HTTP response is sent (FastAPI ``BackgroundTasks``), so the POST that
    scheduled it already returned an immediate "Searching…" state and the board never froze
    on the multi-supplier + AI fan-out. When each search finishes, the SSE publish tells the
    board's EventSource listener to pull the fresh detail panel in.

    Uses its own DB session per requirement (the request session is closed once the response
    is sent) and caps concurrency at ``_SEARCH_FANOUT_LIMIT`` so a bulk refresh of up to
    ``MAX_BATCH_SIZE`` requirements does not stampede the supplier APIs. Publishes even when
    the search fails or the requirement vanished, so the board always clears "Searching…".
    """
    from ...database import SessionLocal
    from ...search_service import search_requirement

    sem = asyncio.Semaphore(_SEARCH_FANOUT_LIMIT)

    async def _one(rid: int) -> None:
        async with sem:
            db = SessionLocal()
            try:
                req = db.get(Requirement, rid)
                if req is not None:
                    await search_requirement(req, db)
            except Exception:
                logger.warning("Background search refresh failed for requirement {}", rid, exc_info=True)
            finally:
                db.close()
        # Announce completion regardless of outcome. source="sse" is suppressed inside the
        # helper (never reached — the SSE render path schedules no background job).
        await _publish_if_user_source(source, user_id, rid)

    await asyncio.gather(*(_one(rid) for rid in requirement_ids), return_exceptions=True)


@router.post("/v2/partials/sightings/{requirement_id}/refresh", response_class=HTMLResponse)
async def sightings_refresh(
    request: Request,
    requirement_id: int,
    background_tasks: BackgroundTasks,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Kick off a background sourcing refresh and immediately return a "Searching…"
    panel.

    ``search_requirement`` (multi-supplier + AI web search) is slow, so a user click
    schedules it as a FastAPI background job and the POST returns at once — the board never
    freezes. When the search completes the job publishes a ``sighting-updated`` SSE; the
    board's EventSource listener then POSTs ``/refresh?source=sse``, which re-renders the
    fresh detail panel (the search already ran, so the sse path only renders — it schedules
    nothing and publishes nothing, breaking the self-trigger loop per docs/htmx-conventions).
    """
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)

    if source == "sse":
        # SSE-triggered render: the background search already committed via its own write
        # session; drop the caller session cache and paint the fresh detail panel.
        db.expire(requirement)
        return await sightings_detail(request, requirement_id, db, user)

    # User click: run the slow search off the request thread, acknowledge immediately.
    background_tasks.add_task(_run_search_and_publish, [requirement_id], user.id)
    resp = template_response(
        "htmx/partials/sightings/searching_panel.html",
        {"request": request, "requirement": requirement},
    )
    resp.headers["X-Rendered-Req-Id"] = str(requirement_id)
    return resp


@router.post("/v2/partials/sightings/batch-refresh", response_class=HTMLResponse)
async def sightings_batch_refresh(
    request: Request,
    background_tasks: BackgroundTasks,
    source: Literal["user", "sse"] = Query(default="user"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Schedule a background sourcing refresh for many requirements; return immediately.

    The selected requirements' searches run off the request thread (bounded concurrency, so
    up to ``MAX_BATCH_SIZE`` reqs do not stampede the supplier APIs). Each completed search
    publishes a ``sighting-updated`` SSE. The immediate response re-renders the board table
    with the scheduled rows flagged "Searching…" so the click is acknowledged without waiting
    for the fan-out. The per-MPN 48h cooldown is still enforced inside search_requirement.
    ``source="sse"`` is inert (no schedule, no publish) to honour the self-trigger gate.
    """
    form = await request.form()
    req_ids_raw = form.get("requirement_ids", "[]")
    try:
        requirement_ids = json.loads(req_ids_raw) if isinstance(req_ids_raw, str) else []
        if not isinstance(requirement_ids, list):
            requirement_ids = []
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail="Invalid requirement_ids format") from e

    if len(requirement_ids) > MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail=f"Maximum {MAX_BATCH_SIZE} requirements per batch")

    # Resolve + access-check the requirements that exist. Missing IDs are simply dropped
    # (nothing to search); caller order is preserved.
    valid_ids: list[int] = []
    if requirement_ids:
        int_ids = [int(rid) for rid in requirement_ids]
        reqs = db.query(Requirement).filter(Requirement.id.in_(int_ids)).all()
        reqs_by_id = {r.id: r for r in reqs}
        require_requisition_access_bulk(db, (r.requisition_id for r in reqs), user, label="Requirement")
        valid_ids = [rid for rid in int_ids if rid in reqs_by_id]

    is_sse = source == "sse"
    # Schedule the slow fan-out off the request thread. Skip on the SSE path so an
    # SSE-triggered call can never re-publish and loop.
    if valid_ids and not is_sse:
        background_tasks.add_task(_run_search_and_publish, valid_ids, user.id, source)

    if is_sse:
        return HTMLResponse("")

    searching_set = set(valid_ids)
    n = len(searching_set)
    msg = f"Searching {n} requirement{'s' if n != 1 else ''}…" if n else "No requirements to search."

    # Re-render the sightings table for the split-panel caller so the scheduled rows show
    # their "Searching…" badge immediately. The requisition parts-tab caller posts
    # hx-swap="none" and only needs the toast, which fires via the HX-Trigger bridge.
    hx_target = request.headers.get("HX-Target", "")
    if hx_target == "sightings-table":

        def _fs(key: str) -> str:
            val = form.get(key, "")
            return val if isinstance(val, str) else ""

        filters = SightingsListParams(
            status=_fs("status"),
            q=_fs("q"),
            group_by=_fs("group_by"),
            manufacturer=_fs("manufacturer"),
        )
        resp: HTMLResponse = await _render_sightings_table(request, db, user, filters, searching_req_ids=searching_set)
    else:
        resp = HTMLResponse("")
    return _with_toast(resp, msg, "info")


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
        "conditions": ["new", "refurb", "used"],
        "specific_reason_values": [r.value for r in CONDITION_SPECIFIC_REASONS],
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
    condition = str(form.get("condition") or "").strip() or None

    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Requirement not found")
    require_requisition_access(db, requirement.requisition_id, user)

    try:
        record_unavailability(db, requirement, vendor_name, reason, note, user, condition=condition)
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
    require_requisition_access(db, requirement.requisition_id, user)

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
    require_requisition_access(db, requirement.requisition_id, user)

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
    require_requisition_access(db, requirement.requisition_id, user)

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
