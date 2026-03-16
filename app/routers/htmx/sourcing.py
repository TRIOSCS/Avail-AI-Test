"""
routers/htmx/sourcing.py — Search and sourcing HTMX partials.

Handles search form/results partials (quick MPN search) and sourcing
workflow partials (multi-source search with SSE streaming, lead cards,
lead detail, status updates, and buyer feedback).

Called by: main.py via htmx router mount
Depends on: models (Requirement, Sighting, SourcingLead, User),
            search_service, scoring, services.sourcing_leads,
            services.sse_broker
"""

import time

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_user, require_user
from ...models import Requirement, Sighting, SourcingLead, User
from ...scoring import classify_lead, explain_lead, score_unified
from ._helpers import _base_ctx, router, templates

# ── Search partials ─────────────────────────────────────────────────────


@router.get("/partials/search", response_class=HTMLResponse)
async def search_form_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the search form partial."""
    ctx = _base_ctx(request, user, "search")
    return templates.TemplateResponse("partials/search/form.html", ctx)


@router.post("/partials/search/run", response_class=HTMLResponse)
async def search_run(
    request: Request,
    mpn: str = Form(default=""),
    requirement_id: int = Query(default=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run a part search and return results HTML.

    If requirement_id is provided, searches for that requirement's MPN.
    Otherwise uses the mpn form field.
    """
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

    start = time.time()
    results = []
    error = None
    source_errors: list[str] = []

    try:
        # Use the search service to find parts across all connectors
        from ...search_service import quick_search_mpn

        raw_results = await quick_search_mpn(search_mpn, db)
        if isinstance(raw_results, list):
            results = raw_results
        else:
            results = raw_results.get("sightings", [])
            source_errors = raw_results.get("source_errors", [])
    except Exception as exc:
        logger.error("Search failed for {}: {}", search_mpn, exc)
        error = f"Search error: {exc}"

    elapsed = time.time() - start

    # Enrich each result with confidence, lead quality, and reason summary
    # using scoring functions that already exist in scoring.py.
    for r in results:
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

    ctx = _base_ctx(request, user, "search")
    ctx.update(
        {
            "results": results,
            "mpn": search_mpn,
            "elapsed_seconds": elapsed,
            "error": error,
            "source_errors": source_errors,
        }
    )
    return templates.TemplateResponse("partials/search/results.html", ctx)


@router.get("/partials/search/lead-detail", response_class=HTMLResponse)
async def search_lead_detail(
    request: Request,
    idx: int = Query(0, ge=0),
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return the lead detail drawer content for a single search result.

    Re-runs the search (cached in search_service) and returns the enriched
    result at the given index.
    """
    if not mpn.strip():
        return HTMLResponse('<p class="p-4 text-sm text-gray-500">No part number specified.</p>')

    try:
        from ...search_service import quick_search_mpn

        raw_results = await quick_search_mpn(mpn.strip(), db)
        results = raw_results if isinstance(raw_results, list) else raw_results.get("sightings", [])
    except Exception as exc:
        logger.error("Lead detail search failed for {}: {}", mpn, exc)
        return HTMLResponse(f'<p class="p-4 text-sm text-red-600">Search error: {exc}</p>')

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
            .filter(SourcingLead.vendor_name.ilike(vendor_name))
            .order_by(SourcingLead.created_at.desc())
            .first()
        )
        if lead_row and lead_row.vendor_safety_band:
            safety_band = lead_row.vendor_safety_band
            safety_score = lead_row.vendor_safety_score
            safety_summary = lead_row.vendor_safety_summary or safety_summary
            safety_flags = lead_row.vendor_safety_flags or []
            safety_available = True

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
        }
    )
    return templates.TemplateResponse("partials/search/lead_detail.html", ctx)


# ── Sourcing partials ──────────────────────────────────────────────────


@router.get("/sourcing/{requirement_id}", response_class=HTMLResponse)
async def v2_sourcing_page(request: Request, requirement_id: int, db: Session = Depends(get_db)):
    """Full page load for sourcing results."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/partials/sourcing/{requirement_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)


@router.get("/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def v2_lead_detail_page(request: Request, lead_id: int, db: Session = Depends(get_db)):
    """Full page load for lead detail."""
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request})
    ctx = _base_ctx(request, user, "requisitions")
    ctx["partial_url"] = f"/partials/sourcing/leads/{lead_id}"
    return templates.TemplateResponse("htmx/base_page.html", ctx)


@router.get("/partials/sourcing/{requirement_id}/stream")
async def sourcing_stream(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """SSE endpoint for sourcing search progress.

    Streams per-source completion events as connectors finish searching.
    Client connects via hx-ext="sse" sse-connect attribute.
    Channel: sourcing:{requirement_id}
    """
    from sse_starlette.sse import EventSourceResponse

    from ...services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    async def event_generator():
        async for msg in broker.listen(f"sourcing:{requirement_id}"):
            if await request.is_disconnected():
                break
            yield {
                "event": msg["event"],
                "data": msg["data"],
            }

    return EventSourceResponse(event_generator())


@router.post("/partials/sourcing/{requirement_id}/search", response_class=HTMLResponse)
async def sourcing_search_trigger(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger multi-source search for a requirement.

    Runs connectors in parallel, publishes SSE events per source completion,
    syncs leads on completion, returns redirect to sourcing results.
    """
    import asyncio
    import json

    from ...services.sse_broker import broker

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    mpn = req.primary_mpn or ""
    sources = ["brokerbin", "nexar", "digikey", "mouser", "oemsecrets", "element14"]
    channel = f"sourcing:{requirement_id}"
    all_sightings = []

    async def search_source(source_name):
        start_t = time.time()
        try:
            from ...search_service import quick_search_mpn

            raw = await quick_search_mpn(mpn, db)
            results = raw if isinstance(raw, list) else raw.get("sightings", [])
            elapsed = int((time.time() - start_t) * 1000)
            count = len(results) if results else 0
            await broker.publish(
                channel,
                "source-complete",
                json.dumps({"source": source_name, "count": count, "elapsed_ms": elapsed, "status": "done"}),
            )
            return results or []
        except Exception as exc:
            elapsed = int((time.time() - start_t) * 1000)
            logger.error("Sourcing search failed for {} on {}: {}", mpn, source_name, exc)
            await broker.publish(
                channel,
                "source-complete",
                json.dumps(
                    {"source": source_name, "count": 0, "elapsed_ms": elapsed, "status": "failed", "error": str(exc)}
                ),
            )
            return []

    results_by_source = await asyncio.gather(*[search_source(s) for s in sources], return_exceptions=True)

    for source_results in results_by_source:
        if isinstance(source_results, list):
            all_sightings.extend(source_results)

    await broker.publish(
        channel, "search-complete", json.dumps({"total": len(all_sightings), "requirement_id": requirement_id})
    )

    return HTMLResponse(status_code=200, headers={"HX-Redirect": f"/sourcing/{requirement_id}"})


@router.get("/partials/sourcing/{requirement_id}", response_class=HTMLResponse)
async def sourcing_results_partial(
    request: Request,
    requirement_id: int,
    confidence: str = "",
    safety: str = "",
    freshness: str = "",
    source: str = "",
    status: str = "",
    contactability: str = "",
    corroborated: str = "",
    sort: str = "best",
    page: int = Query(1, ge=1),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return sourcing results with lead cards for a requirement.

    Supports filtering by confidence band, safety band, freshness window,
    source type, buyer status, contactability, and corroboration. Sorts by
    best overall (default), freshest, safest, easiest to contact, or most proven.
    """
    from ...models.sourcing_lead import SourcingLead

    req = db.query(Requirement).filter(Requirement.id == requirement_id).first()
    if not req:
        raise HTTPException(404, "Requirement not found")

    query = db.query(SourcingLead).filter(SourcingLead.requirement_id == requirement_id)

    if confidence:
        bands = [b.strip() for b in confidence.split(",")]
        query = query.filter(SourcingLead.confidence_band.in_(bands))
    if safety:
        bands = [b.strip() for b in safety.split(",")]
        query = query.filter(SourcingLead.vendor_safety_band.in_(bands))
    if freshness and freshness != "all":
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoffs = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
        if freshness in cutoffs:
            query = query.filter(SourcingLead.source_last_seen_at >= now - cutoffs[freshness])
    if source:
        sources_list = [s.strip() for s in source.split(",")]
        query = query.filter(SourcingLead.primary_source_type.in_(sources_list))
    if status and status != "all":
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(SourcingLead.buyer_status.in_(statuses))
    if contactability == "has_email":
        query = query.filter(SourcingLead.contact_email.isnot(None))
    elif contactability == "has_phone":
        query = query.filter(SourcingLead.contact_phone.isnot(None))
    if corroborated == "yes":
        query = query.filter(SourcingLead.corroborated.is_(True))
    elif corroborated == "no":
        query = query.filter(SourcingLead.corroborated.is_(False))

    sort_map = {
        "best": [SourcingLead.confidence_score.desc()],
        "freshest": [SourcingLead.source_last_seen_at.desc().nullslast()],
        "safest": [SourcingLead.vendor_safety_score.desc().nullslast()],
        "contact": [SourcingLead.contactability_score.desc().nullslast()],
        "proven": [SourcingLead.historical_success_score.desc().nullslast()],
    }
    for col in sort_map.get(sort, sort_map["best"]):
        query = query.order_by(col)

    total = query.count()
    per_page = 24
    leads = query.offset((page - 1) * per_page).limit(per_page).all()

    lead_sighting_data = {}
    if leads:
        vendor_names = [lead.vendor_name_normalized for lead in leads]
        sightings = (
            db.query(Sighting)
            .filter(
                Sighting.requirement_id == requirement_id,
                Sighting.vendor_name_normalized.in_(vendor_names),
            )
            .order_by(Sighting.created_at.desc().nullslast())
            .all()
        )
        # Group by vendor, keep only the most recent (already sorted desc)
        best_by_vendor: dict[str, Sighting] = {}
        for s in sightings:
            if s.vendor_name_normalized not in best_by_vendor:
                best_by_vendor[s.vendor_name_normalized] = s
        for lead in leads:
            best = best_by_vendor.get(lead.vendor_name_normalized)
            if best:
                lead_sighting_data[lead.id] = {
                    "qty_available": best.qty_available,
                    "unit_price": best.unit_price,
                }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "requirement": req,
            "leads": leads,
            "lead_sighting_data": lead_sighting_data,
            "total": total,
            "page": page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "per_page": per_page,
            "f_confidence": confidence,
            "f_safety": safety,
            "f_freshness": freshness,
            "f_source": source,
            "f_status": status,
            "f_contactability": contactability,
            "f_corroborated": corroborated,
            "f_sort": sort,
        }
    )
    return templates.TemplateResponse("partials/sourcing/results.html", ctx)


@router.get("/partials/sourcing/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail_partial(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return lead detail as HTML partial.

    Loads the SourcingLead, its evidence (sorted by confidence_impact desc),
    groups evidence by source category, fetches vendor card and best sighting.
    """
    from ...models.sourcing_lead import LeadEvidence, SourcingLead
    from ...services.sourcing_leads import _source_category

    lead = db.query(SourcingLead).filter(SourcingLead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    evidence = (
        db.query(LeadEvidence)
        .filter(LeadEvidence.lead_id == lead.id)
        .order_by(LeadEvidence.confidence_impact.desc().nullslast())
        .all()
    )

    evidence_by_category = {}
    for ev in evidence:
        cat = _source_category(ev.source_type)
        evidence_by_category.setdefault(cat, []).append(ev)

    category_labels = {
        "api": "API",
        "marketplace": "Marketplace",
        "salesforce_history": "Salesforce History",
        "avail_history": "Avail History",
        "web_ai": "Web / AI",
        "safety_review": "Safety Review",
        "buyer_feedback": "Buyer Feedback",
    }

    requirement = db.query(Requirement).filter(Requirement.id == lead.requirement_id).first()

    vendor_card = lead.vendor_card

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update(
        {
            "lead": lead,
            "evidence": evidence,
            "evidence_by_category": evidence_by_category,
            "category_labels": category_labels,
            "requirement": requirement,
            "vendor_card": vendor_card,
            "best_sighting": best_sighting,
        }
    )
    return templates.TemplateResponse("partials/sourcing/lead_detail.html", ctx)


@router.post("/partials/sourcing/leads/{lead_id}/status", response_class=HTMLResponse)
async def lead_status_update(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update lead buyer status.

    Returns updated lead card when called from results view (for OOB swap),
    or updated lead detail when called from lead detail page.
    """
    from ...services.sourcing_leads import update_lead_status

    form = await request.form()
    status_val = form.get("status", "").strip()
    note = form.get("note", "").strip() or None

    try:
        lead = update_lead_status(
            db,
            lead_id,
            status_val,
            note=note,
            actor_user_id=user.id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not lead:
        raise HTTPException(404, "Lead not found")

    referer = request.headers.get("HX-Current-URL", "")
    if "/leads/" in referer:
        return await lead_detail_partial(request, lead_id, user, db)

    best_sighting = (
        db.query(Sighting)
        .filter(
            Sighting.requirement_id == lead.requirement_id,
            Sighting.vendor_name_normalized == lead.vendor_name_normalized,
        )
        .order_by(Sighting.created_at.desc().nullslast())
        .first()
    )
    lead_sighting_data = {}
    if best_sighting:
        lead_sighting_data[lead.id] = {
            "qty_available": best_sighting.qty_available,
            "unit_price": best_sighting.unit_price,
        }

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"lead": lead, "lead_sighting_data": lead_sighting_data})
    return templates.TemplateResponse("partials/sourcing/lead_card.html", ctx)


@router.post("/partials/sourcing/leads/{lead_id}/feedback", response_class=HTMLResponse)
async def lead_feedback(
    request: Request,
    lead_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add feedback event to a lead without changing status. Returns updated lead detail."""
    from ...services.sourcing_leads import append_lead_feedback

    form = await request.form()
    note = form.get("note", "").strip() or None
    reason_code = form.get("reason_code", "").strip() or None
    contact_method = form.get("contact_method", "").strip() or None

    lead = append_lead_feedback(
        db,
        lead_id,
        note=note,
        reason_code=reason_code,
        contact_method=contact_method,
        actor_user_id=user.id,
    )
    if not lead:
        raise HTTPException(404, "Lead not found")

    return await lead_detail_partial(request, lead_id, user, db)
