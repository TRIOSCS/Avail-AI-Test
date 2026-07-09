"""routers/htmx/search_views.py — Global + part-dossier search partials (HTMX).

Covers global type-ahead search, the AI search box, the full search-results page,
the search form (Part Dossier entry point + history panel), the streaming
MPN search (search/run + SSE stream + filter + lead-detail), and the
requisition-picker "add shortlisted results to a requisition" flow. Extracted
verbatim from htmx_views.py (same `/v2/partials/search/...` paths, same
`htmx-views` tag).

Called by: app/routers/htmx_views.py (aggregated into the single exported router).
Depends on: app.search_service, app.services.global_search_service,
    app.services.part_history_service, app.services.fru_matrix_service,
    app.services.sse_broker, app.scoring, app.vendor_utils
"""

import html as html_mod
import json

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import AccessKey, ApiSourceStatus, SourcingStatus
from ...database import get_db
from ...dependencies import require_access, require_requisition_access, require_user
from ...models import ApiSource, Requirement, Requisition, Sighting, SourcingLead, User
from ...scoring import classify_lead, explain_lead, score_unified
from ...services.sighting_ingest import sighting_from_row
from ...services.vendor_unavailability import apply_to_fresh_sightings
from ...template_env import template_response, templates
from ...utils.sql_helpers import escape_like
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


# ── Global search ──────────────────────────────────────────────────────


@router.get("/v2/partials/search/global", response_class=HTMLResponse)
async def global_search(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Global search across all entity types (type-ahead)."""
    from ...services.global_search_service import fast_search

    results = fast_search(q, db, user)
    return template_response(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


@router.post("/v2/partials/search/ai", response_class=HTMLResponse)
async def ai_search_endpoint(
    request: Request,
    q: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI-powered search — triggered by Enter key."""
    from ...services.global_search_service import ai_search

    results = await ai_search(q, db, user)
    return template_response(
        "htmx/partials/shared/search_results.html",
        {**_base_ctx(request, user), "results": results, "query": q, "ai_search": True},
    )


@router.get("/v2/partials/search/results", response_class=HTMLResponse)
async def search_results_page(
    request: Request,
    q: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full search results page."""
    from ...services.global_search_service import fast_search

    results = fast_search(q, db, user) if q else {"best_match": None, "groups": {}, "total_count": 0}
    return template_response(
        "htmx/partials/search/full_results.html",
        {**_base_ctx(request, user), "results": results, "query": q},
    )


# ── Search partials ─────────────────────────────────────────────────────


@router.get("/v2/partials/search", response_class=HTMLResponse)
async def search_form_partial(
    request: Request,
    mpn: str = "",
    user: User = Depends(require_access(AccessKey.SEARCH)),
    db: Session = Depends(get_db),
):
    """Search surface entry point.

    With ``mpn`` → render the Part Dossier shell ("The Bench") whose sections lazy-load
    from part_dossier.py. Without ``mpn`` → the recent-searches landing (search box that
    deep-links the dossier + a lazy-loaded recent list). The new routes live in
    routers/part_dossier.py; this stays the single /v2/partials/search entry point.
    """
    ctx = _base_ctx(request, user, "search")
    if mpn.strip():
        ctx["mpn"] = mpn.strip().upper()
        return template_response("htmx/partials/search/dossier_shell.html", ctx)
    return template_response("htmx/partials/search/form.html", ctx)


@router.get("/v2/partials/search/history", response_class=HTMLResponse)
async def search_history_panel(
    request: Request,
    mpn: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the 'What we know' history panel for the searched MPN.

    Called by: results_shell.html right column (hx-get).
    Depends on: part_history_service.get_part_history, normalize_mpn_key,
                fru_matrix_service.get_fru_view/get_reverse_context (compact FRU
                crosswalk context — both are capped/cheap reads).
    """
    from ...services.fru_matrix_service import get_fru_view, get_reverse_context
    from ...services.part_history_service import PartHistory, get_part_history
    from ...utils.normalization import normalize_mpn_key

    key = normalize_mpn_key(mpn)  # pure/cheap; outside try so it can be logged on failure
    try:
        history = get_part_history(db, key)
        error = False
    except Exception:
        logger.exception("search_history_panel failed mpn={} key={} user={}", mpn, key, user.id)
        history = PartHistory(found=False)
        error = True

    # FRU crosswalk context, only for a concrete searched MPN: forward (the MPN is a
    # FRU) and reverse (the MPN appears under FRUs). The card is ADDITIVE, so its
    # lookups get their own scoped try/except — a crosswalk failure degrades to "no
    # crosswalk card" and must never discard a successfully loaded history or flip
    # the panel into the history-error state. (A history failure already suppresses
    # the card via the template's `not error` guard, so the lookups are skipped.)
    fru_view = None
    fru_reverse = None
    if key and not error:
        try:
            fru_view = get_fru_view(db, mpn)
            fru_reverse = get_reverse_context(db, mpn)
        except Exception:
            logger.exception("search_history_panel FRU context failed mpn={} key={} user={}", mpn, key, user.id)
            fru_view = None
            fru_reverse = None

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "history": history,
            "error": error,
            "fru_view": fru_view,
            "fru_reverse": fru_reverse,
            "fru_query": mpn,
        }
    )
    return template_response("htmx/partials/search/history_panel.html", ctx)


@router.post("/v2/partials/search/run", response_class=HTMLResponse)
async def search_run(
    request: Request,
    mpn: str = Form(default=""),
    requirement_id: int = Query(default=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Launch a streaming part search and return the results shell HTML.

    Generates a search_id, launches stream_search_mpn as a background task, and returns
    the results_shell.html template with SSE connection details.

    If requirement_id is provided, searches for that requirement's MPN. Otherwise uses
    the mpn form field.
    """
    from uuid import uuid4

    from ...utils.async_helpers import safe_background_task as _safe_bg

    search_mpn = mpn.strip()

    # If searching from a requirement row, get the MPN from query params
    if not search_mpn and requirement_id:
        req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
        if req:
            search_mpn = req.primary_mpn or ""

    # Also check query params for mpn (when called from requirement detail)
    if not search_mpn:
        search_mpn = request.query_params.get("mpn", "").strip()

    if not search_mpn:
        return HTMLResponse('<div class="p-4 text-sm text-red-600">Please enter a part number.</div>')

    # Generate a unique search ID and launch streaming search in background
    search_id = str(uuid4())
    enabled_sources = _get_enabled_sources(db)

    from ...search_service import stream_search_mpn

    await _safe_bg(stream_search_mpn(search_id, search_mpn), task_name="stream_search_mpn")

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "search_id": search_id,
            "mpn": search_mpn,
            "enabled_sources": enabled_sources,
        }
    )
    return template_response("htmx/partials/search/results_shell.html", ctx)


@router.get("/v2/partials/search/stream")
async def search_stream(
    request: Request,
    search_id: str = Query(...),
    user: User = Depends(require_user),
):
    """SSE stream endpoint for search results.

    Subscribes to the SSE broker channel for the given search_id and yields events until
    the 'done' event is received or the client disconnects.
    """
    from sse_starlette.sse import EventSourceResponse

    from ...services.sse_broker import broker

    async def event_generator():
        async for msg in broker.listen(f"search:{search_id}"):
            if await request.is_disconnected():
                break
            yield {"event": msg["event"], "data": msg["data"]}
            if msg["event"] == "done":
                break

    return EventSourceResponse(event_generator())


def _get_enabled_sources(db: Session) -> list[dict]:
    """Return list of enabled API sources for the source progress chips.

    Called by: search_run
    Depends on: ApiSource model
    """

    sources = db.query(ApiSource).filter(ApiSource.status != ApiSourceStatus.DISABLED).all()
    return [{"name": s.name, "status": s.status} for s in sources]


def _get_cached_search_results(search_id: str) -> list[dict] | None:
    """Read cached search results from Redis.

    Called by: search_filter
    Depends on: search_service._get_search_redis
    """
    try:
        from ...search_service import _get_search_redis

        rc = _get_search_redis()
        if rc:
            data = rc.get(f"search:{search_id}:results")
            if data:
                return json.loads(data)
    except Exception:
        logger.warning("Redis cache lookup failed for search", exc_info=True)
    return None


@router.get("/v2/partials/search/filter", response_class=HTMLResponse)
async def search_filter(
    request: Request,
    search_id: str = Query(...),
    confidence: str = Query("all"),
    source: str = Query("all"),
    sort: str = Query("best"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Re-render search results with filters applied, reading from Redis cache.

    Called by: search filter bar (HTMX)
    Depends on: _get_cached_search_results, vendor_card.html template
    """
    results = _get_cached_search_results(search_id)
    if results is None:
        return HTMLResponse('<div class="text-sm text-gray-500 p-4">Search results expired. Please search again.</div>')

    # Apply filters
    if confidence != "all":
        color_map = {"high": "green", "medium": "amber", "low": "red"}
        results = [r for r in results if r.get("confidence_color") == color_map.get(confidence)]

    if source != "all":
        results = [r for r in results if source in (r.get("sources_found") or [])]

    # Apply sort
    if sort == "cheapest":
        results.sort(key=lambda r: r.get("unit_price") or float("inf"))
    elif sort == "stock":
        results.sort(key=lambda r: r.get("qty_available") or 0, reverse=True)
    else:
        results.sort(key=lambda r: (r.get("score", 0), r.get("confidence_pct", 0)), reverse=True)

    # Re-render cards using vendor_card.html for each result
    cards_html = ""
    for i, card in enumerate(results):
        cards_html += templates.get_template("htmx/partials/search/vendor_card.html").render(
            card=card, card_index=i, search_id=search_id
        )
    return HTMLResponse(cards_html)


@router.get("/v2/partials/search/lead-detail", response_class=HTMLResponse)
async def search_lead_detail(
    request: Request,
    idx: int = Query(0, ge=0),
    mpn: str = Query(""),
    search_id: str = Query(""),
    vendor_key: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the lead detail drawer content for a single search result.

    When search_id + vendor_key are provided, reads from Redis cache (new flow).
    Otherwise falls back to re-running the search via quick_search_mpn (legacy).

    Called by: lead detail drawer (HTMX)
    Depends on: _get_cached_search_results, vendor_utils.normalize_vendor_name
    """
    # ── New path: read from Redis cache by vendor_key ──
    if search_id and vendor_key:
        results = _get_cached_search_results(search_id)
        if results:
            from ...vendor_utils import normalize_vendor_name

            # Normalize BOTH sides: the template sends the raw vendor name (url-encoded),
            # so applying the same normalizer here makes the key survive suffix stripping
            # (", Inc." / "LLC" / "Corp.") that used to make the row's Details → miss.
            vendor_key_norm = normalize_vendor_name(vendor_key)
            lead = next(
                (r for r in results if normalize_vendor_name(r.get("vendor_name", "")) == vendor_key_norm),
                None,
            )
            if lead:
                ctx = _base_ctx(request, user, "search")
                ctx.update({"lead": lead, "mpn": lead.get("mpn_matched", "")})
                return template_response("htmx/partials/search/lead_detail.html", ctx)
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">Lead not found in cache. Please search again.</p>')

    # ── Legacy path: re-run search by MPN + index ──
    if not mpn.strip():
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">No part number specified.</p>')

    try:
        from ...search_service import quick_search_mpn

        raw_results = await quick_search_mpn(mpn.strip(), db)
        results = raw_results if isinstance(raw_results, list) else raw_results.get("sightings", [])
    except Exception as exc:
        logger.error("Lead detail search failed for {}: {}", mpn, exc)
        return HTMLResponse(f'<p class="p-4 text-sm text-red-600">Search error: {html_mod.escape(str(exc))}</p>')

    if idx >= len(results):
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">Lead not found.</p>')

    r = results[idx]

    # Enrich the single result with scoring data
    unified = score_unified(
        source_type=r.get("source_type", ""),
        vendor_score=r.get("vendor_score"),
        is_authorized=r.get("is_authorized", False),
        unit_price=r.get("unit_price"),
        qty_available=r.get("qty_available"),
        age_hours=r.get("age_hours"),
        has_price=bool(r.get("unit_price")),
        has_qty=bool(r.get("qty_available")),
        has_lead_time=bool(r.get("lead_time")),
        has_condition=bool(r.get("condition")),
    )
    r["confidence_pct"] = unified["confidence_pct"]
    r["confidence_color"] = unified["confidence_color"]
    r["source_badge"] = unified["source_badge"]
    r["score_components"] = unified.get("components", {})
    r["lead_quality"] = classify_lead(
        score=unified["score"],
        is_authorized=r.get("is_authorized", False),
        has_price=bool(r.get("unit_price")),
        has_qty=bool(r.get("qty_available")),
        has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
        evidence_tier=r.get("evidence_tier"),
    )
    r["reason"] = explain_lead(
        vendor_name=r.get("vendor_name"),
        is_authorized=r.get("is_authorized", False),
        vendor_score=r.get("vendor_score"),
        unit_price=r.get("unit_price"),
        qty_available=r.get("qty_available"),
        has_contact=bool(r.get("vendor_email") or r.get("vendor_phone")),
        evidence_tier=r.get("evidence_tier"),
        source_type=r.get("source_type"),
    )

    # Look up vendor safety data from SourcingLead records if available
    safety_band = "unknown"
    safety_score = None
    safety_summary = "Safety is assessed when leads are sourced through requisitions."
    safety_flags = []
    safety_available = False

    vendor_name = r.get("vendor_name", "")
    if vendor_name:
        lead_row = (
            db.query(SourcingLead)
            .filter(SourcingLead.vendor_name.ilike(escape_like(vendor_name), escape="\\"))
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead_row and lead_row.vendor_safety_band:
            safety_band = lead_row.vendor_safety_band
            safety_score = lead_row.vendor_safety_score
            safety_summary = lead_row.vendor_safety_summary or safety_summary
            safety_flags = lead_row.vendor_safety_flags or []
            safety_available = True

    # Look up material card for this MPN
    from ...models.intelligence import MaterialCard

    material_card_id = None
    mpn_clean = mpn.strip().lower()
    if mpn_clean:
        mc = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn == mpn_clean).first()
        if mc:
            material_card_id = mc.id

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "lead": r,
            "mpn": mpn.strip(),
            "idx": idx,
            "safety_band": safety_band,
            "safety_score": safety_score,
            "safety_summary": safety_summary,
            "safety_flags": safety_flags,
            "safety_available": safety_available,
            "material_card_id": material_card_id,
        }
    )
    return template_response("htmx/partials/search/lead_detail.html", ctx)


@router.get("/v2/partials/search/requisition-picker", response_class=HTMLResponse)
async def requisition_picker(
    request: Request,
    mpn: str = Query(""),
    items: str = Query("[]"),
    action: str = Query("add"),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render requisition picker modal for adding shortlisted results.

    Called by: shortlist_bar.html "Add to Requisition" button
    Depends on: models.sourcing (Requisition)
    """
    recent_reqs = (
        db.query(Requisition)
        .filter(Requisition.is_scratch.is_(False))
        .order_by(Requisition.created_at.desc())
        .limit(20)
        .all()
    )

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "requisitions": recent_reqs,
            "mpn": mpn,
            "items_json": items,
            "action": action,
        }
    )
    return template_response("htmx/partials/search/requisition_picker_modal.html", ctx)


@router.post("/v2/partials/search/add-to-requisition", response_class=HTMLResponse)
async def add_to_requisition(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add shortlisted search results to a requisition as Sighting rows.

    Creates a Requirement for the MPN if one doesn't exist on the requisition.
    Persists each selected result as a Sighting row.

    Called by: requisition_picker_modal.html action button
    Depends on: models.sourcing (Requisition, Requirement, Sighting)
    """
    body = await request.json()
    requisition_id = body.get("requisition_id")
    mpn = body.get("mpn", "").strip()
    items = body.get("items", [])

    # The MPN + requisition are the only required inputs: from the part dossier the
    # meaningful action is "add THIS part to a requisition", which creates the
    # Requirement row. Shortlisted market rows (items) are optional supporting
    # sightings, so an empty shortlist must still succeed (it added the part).
    if not requisition_id or not mpn:
        return HTMLResponse(
            '<div class="text-red-600 text-sm p-2">Missing required fields.</div>',
            status_code=400,
        )

    req = db.get(Requisition, requisition_id)
    if not req:
        return HTMLResponse(
            '<div class="text-red-600 text-sm p-2">Requisition not found.</div>',
            status_code=404,
        )
    require_requisition_access(db, int(requisition_id), user)

    # Find or create Requirement for this MPN
    requirement = (
        db.query(Requirement)
        .filter_by(
            requisition_id=requisition_id,
            primary_mpn=mpn,
        )
        .first()
    )

    if not requirement:
        # Mirror update_requirement: store the canonical key-form normalized_mpn
        # (lowercase, separators stripped) so part-history / material-card joins
        # line up, and resolve the MaterialCard up front.
        from ...search_service import resolve_material_card
        from ...utils.normalization import normalize_mpn_key

        card = resolve_material_card(mpn, db)
        requirement = Requirement(
            requisition_id=requisition_id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            material_card_id=card.id if card else None,
            target_qty=None,
            sourcing_status=SourcingStatus.OPEN,
        )
        db.add(requirement)
        db.flush()

    # Create Sighting rows (shared mapping — see services.sighting_ingest)
    created_rows: list[Sighting] = []
    for item in items:
        sighting = sighting_from_row(requirement.id, item)
        db.add(sighting)
        created_rows.append(sighting)

    # Re-apply durable vendor+part unavailability knowledge — a manually added
    # sighting for a known-dead vendor+part renders flagged with its reason; the
    # user can Mark available to override.
    apply_to_fresh_sightings(db, requirement, created_rows)

    db.commit()

    count = len(items)
    req_label = html_mod.escape(req.name or "")
    if count:
        what = f"{count} result{'s' if count != 1 else ''}"
    else:
        what = html_mod.escape(mpn)
    return HTMLResponse(
        f'<div class="text-sm text-emerald-600 p-2">Added {what} to requisition &ldquo;{req_label}&rdquo;</div>'
    )
