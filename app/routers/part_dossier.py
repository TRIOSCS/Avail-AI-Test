"""routers/part_dossier.py — the Search report's section routes (GET) + quick-source
actions.

Serves the one-page report at /v2/search?mpn=<PN>[&subs=A,B]: the part header, then
Posting now (live market), Posted before, Offered by email, and Who to call — every
section a server-rendered fragment that search/report.html lazy-loads into its own slot.
Not a SPA.

Section routes (all take ``mpn`` + optional ``subs``, the comma-separated substitutes):
  /dossier/hero          part header — instant DB read; bumps search_count /
                         last_searched_at on an EXISTING card only (a bare search never
                         creates a card)
  /dossier/market        Posting now — one run per part number: a fresh Redis pointer
                         (search:{key}:latest, written by search_service.stream_search_mpn)
                         renders that run's cached rows; otherwise the fragment fires the
                         existing POST /v2/partials/search/run SSE flow for it
  /dossier/posted-before Posted before — vendors who posted the part (part_report_service)
  /dossier/offers        Offered by email — Offer rows (part_report_service)
  /dossier/contacts      Who to call — vendor-card / posting contacts (part_report_service)
  /dossier/specs         specs & datasheet block (lazy, on the header toggle)
  /recent                recent searches for the landing

Called by: app/main.py (include_router); search/report.html + search/index.html lazy-loads.
Depends on: services.part_report_service, services.part_history_service
            .price_trend_for_card, services.fru_matrix_service, services
            .global_search_service._equivalence_expansion, search_service
            (_get_search_redis / get_market_source_health / compute_market_baseline),
            routers.htmx.search_views._get_cached_search_results, models.intelligence
            .MaterialCard, template_env.template_response. Shares base ctx via the
            lazy-imported htmx_views._base_ctx (same pattern as requisitions2.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import AccessKey
from ..database import get_db
from ..dependencies import require_user, user_has_access
from ..models import User
from ..models.intelligence import MaterialCard, MaterialCardDatasheet
from ..models.sourcing import Requisition
from ..services.datasheet_library import fetch_datasheet_bytes
from ..services.part_report_service import (
    ReportPart,
    distinct_vendor_count,
    offers_for_parts,
    parse_substitutes,
    posted_before,
    resolve_parts,
    who_to_call,
)
from ..services.quick_source_service import get_or_create_scratch_req, persist_rows_as_sightings
from ..template_env import template_response
from ..utils.async_helpers import safe_background_task
from ..utils.normalization import normalize_mpn_key

router = APIRouter(tags=["part-dossier"])

# Recent-searches landing cap.
_RECENT_LIMIT = 12


def _ctx(request: Request, user: User) -> dict:
    """Shared base context.

    Lazy import of htmx_views._base_ctx avoids an import cycle (the same lazy-import
    pattern used across the htmx partial routers).
    """
    from app.routers.htmx_views import _base_ctx

    return _base_ctx(request, user, "search")


def _resolve_card(db: Session, key: str) -> MaterialCard | None:
    """Look up a live MaterialCard by normalized key (never creates one)."""
    from ..services.material_card_service import get_live_card_by_key

    return get_live_card_by_key(db, key)


def report_query(mpn: str, subs: list[str]) -> str:
    """The encoded ``mpn``/``subs`` query every report section endpoint takes."""
    params = {"mpn": mpn}
    if subs:
        params["subs"] = ",".join(subs)
    return urlencode(params)


def _parts(db: Session, mpn: str, subs: str) -> list[ReportPart]:
    """Resolve the searched part + its substitutes (display form, cards attached)."""
    display = mpn.strip().upper()
    return resolve_parts(db, display, parse_substitutes(subs, display))


def _section_ctx(request: Request, user: User, parts: list[ReportPart]) -> dict:
    ctx = _ctx(request, user)
    subs = [p.display for p in parts[1:]]
    ctx.update(
        {
            "mpn": parts[0].display if parts else "",
            "subs": subs,
            "parts": parts,
            "qs": report_query(parts[0].display if parts else "", subs),
        }
    )
    return ctx


@router.get("/v2/partials/search/dossier/hero", response_class=HTMLResponse)
async def dossier_hero(
    request: Request,
    mpn: str = Query(""),
    subs: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Part header — instant DB read.

    Bumps search_count on an existing card only.
    """
    from ..services.part_history_service import price_trend_for_card

    parts = _parts(db, mpn, subs)
    display_mpn = parts[0].display if parts else ""
    key = normalize_mpn_key(display_mpn)
    card = parts[0].card if parts else None

    # Light-footprint write: a bare search only touches the existing card's search
    # telemetry. Unknown PNs stay "New to us" — no card is created.
    if card is not None:
        card.search_count = (card.search_count or 0) + 1
        card.last_searched_at = datetime.now(UTC)
        db.commit()

    price_trend = None
    if card is not None:
        try:
            price_trend = price_trend_for_card(db, card.id)
        except Exception:
            db.rollback()
            logger.exception("dossier_hero price trend failed mpn={} key={}", mpn, key)

    # FRU crosswalk context is additive — a failure just hides it.
    fru_view = None
    fru_reverse = None
    try:
        from ..services.fru_matrix_service import get_fru_view, get_reverse_context

        fru_view = get_fru_view(db, display_mpn)
        fru_reverse = get_reverse_context(db, display_mpn)
    except Exception:
        logger.exception("dossier_hero FRU context failed mpn={} key={}", mpn, key)
        fru_view = None
        fru_reverse = None

    # Equivalence class: "Also known as" chips — stored verdicts only, never an LLM in
    # the render path; failures just hide the chips.
    equivalence = None
    try:
        from ..services.global_search_service import _equivalence_expansion

        _eq_keys, equivalence = _equivalence_expansion(db, display_mpn)
    except Exception:
        logger.exception("dossier_hero equivalence expansion failed mpn={} key={}", mpn, key)

    ctx = _section_ctx(request, user, parts)
    ctx.update(
        {
            "card": card,
            "price_trend": price_trend,
            "fru_view": fru_view,
            "fru_reverse": fru_reverse,
            "equivalence": equivalence,
            # Same gate as the search banner: the demote button only renders for users
            # the PROACTIVE-gated verdict endpoint will actually accept.
            "can_verdict": user_has_access(user, AccessKey.PROACTIVE, db),
        }
    )

    # Auto-datasheet capture (background, never blocks the render).
    if display_mpn:
        from ..services.datasheet_capture import capture_datasheet

        await safe_background_task(
            capture_datasheet(display_mpn, user.id), task_name="datasheet_capture", suppress_in_testing=True
        )

    return template_response("htmx/partials/search/report_part.html", ctx)


@router.get("/v2/partials/search/dossier/specs", response_class=HTMLResponse)
async def dossier_specs(
    request: Request,
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Specs & datasheet block from MaterialCard enrichment fields (graceful when the
    card is None)."""
    from ..services.spec_format import format_specs_for_display

    card = _resolve_card(db, normalize_mpn_key(mpn))
    # Human-formatted specs (schema labels + units) — same formatter as the materials
    # list, so the report and the cards can never show a spec two ways.
    specs_display = format_specs_for_display(db, card.category, card.specs_structured) if card else []
    ctx = _ctx(request, user)
    ctx.update({"mpn": mpn.strip().upper(), "card": card, "specs_display": specs_display})
    return template_response("htmx/partials/search/report_specs.html", ctx)


@router.get("/v2/partials/search/dossier/datasheet-status", response_class=HTMLResponse)
async def dossier_datasheet_status(
    request: Request,
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll target for the 'fetching datasheet…' chip.

    Returns the datasheet block; stops polling (HTTP 286) once a copy is stored or a
    search has been recorded.
    """
    card = _resolve_card(db, normalize_mpn_key(mpn))
    ctx = _ctx(request, user)
    ctx.update({"mpn": mpn.strip().upper(), "card": card})
    resp = template_response("htmx/partials/search/_datasheet_block.html", ctx)
    if card is not None and (card.datasheet_captured_at or card.datasheet_searched_at):
        resp.status_code = 286
    return resp


def _cached_run(key: str) -> tuple[str, list[dict]] | tuple[None, None]:
    """(search_id, rows) for a part's freshest cached run, or (None, None)."""
    from ..search_service import _get_search_redis

    try:
        rc = _get_search_redis()
        if rc and key:
            pointer = rc.get(f"search:{key}:latest")
            if pointer:
                from ..routers.htmx_views import _get_cached_search_results

                rows = _get_cached_search_results(pointer)
                if rows:
                    return pointer, rows
    except Exception:
        logger.warning("dossier_market cache lookup failed key={}", key, exc_info=True)
    return None, None


@router.get("/v2/partials/search/dossier/market", response_class=HTMLResponse)
async def dossier_market(
    request: Request,
    mpn: str = Query(""),
    subs: str = Query(""),
    refresh: bool = Query(False),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Posting now — one run per part number.

    A run with a fresh Redis pointer renders its cached rows (the pointer key
    search:{key}:latest is written at the end of stream_search_mpn, TTL 900s); a run
    without — or every run when ``refresh=1`` — is a frame whose body fires the existing
    POST /v2/partials/search/run SSE flow. The SSE engine is reused UNCHANGED. A
    degraded-source banner (``market_health``) and the authorized baseline (cached rows
    only) render above the table.
    """
    from ..search_service import compute_market_baseline, get_market_source_health

    parts = _parts(db, mpn, subs)
    runs: list[dict] = []
    cached_ids: list[str] = []
    all_cached_rows: list[dict] = []
    for part in parts:
        search_id, rows = (None, None) if refresh else _cached_run(part.key)
        runs.append({"display": part.display, "cached_search_id": search_id, "cached_rows": rows})
        if search_id and rows:
            cached_ids.append(search_id)
            all_cached_rows.extend(rows)

    # Degraded-state banner: which live-market sources are down (auth/quota). Best-effort —
    # a health-check failure must never break the section itself.
    try:
        market_health = get_market_source_health(db)
    except Exception:
        logger.warning("dossier_market source-health lookup failed mpn={}", mpn, exc_info=True)
        market_health = None

    ctx = _section_ctx(request, user, parts)
    ctx.update(
        {
            "runs": runs,
            "live_runs": sum(1 for r in runs if r["cached_rows"] is None),
            "cached_ids": cached_ids,
            "market_health": market_health,
            "market_baseline": compute_market_baseline(all_cached_rows) if all_cached_rows else None,
        }
    )
    return template_response("htmx/partials/search/report_live.html", ctx)


@router.get("/v2/partials/search/dossier/posted-before", response_class=HTMLResponse)
async def dossier_posted_before(
    request: Request,
    mpn: str = Query(""),
    subs: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Posted before — vendors who posted the part (and its substitutes) before."""
    parts = _parts(db, mpn, subs)
    ctx = _section_ctx(request, user, parts)
    try:
        ctx["postings"] = posted_before(db, parts)
    except Exception:
        # Degrade to an in-section note, never a 500 that leaves the skeleton spinning.
        db.rollback()
        logger.exception("dossier_posted_before failed mpn={}", mpn)
        ctx.update({"postings": [], "error": True})
    return template_response("htmx/partials/search/report_posted_before.html", ctx)


@router.get("/v2/partials/search/dossier/offers", response_class=HTMLResponse)
async def dossier_offers(
    request: Request,
    mpn: str = Query(""),
    subs: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Offered by email — offers / quotes vendors sent us for the part."""
    parts = _parts(db, mpn, subs)
    ctx = _section_ctx(request, user, parts)
    try:
        offers = offers_for_parts(db, parts)
        ctx.update({"offers": offers, "vendor_count": distinct_vendor_count(offers)})
    except Exception:
        db.rollback()
        logger.exception("dossier_offers failed mpn={}", mpn)
        ctx.update({"offers": [], "vendor_count": 0, "error": True})
    return template_response("htmx/partials/search/report_offers.html", ctx)


@router.get("/v2/partials/search/dossier/contacts", response_class=HTMLResponse)
async def dossier_contacts(
    request: Request,
    mpn: str = Query(""),
    subs: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Who to call — contacts for every vendor in the part's posting + offer history."""
    parts = _parts(db, mpn, subs)
    ctx = _section_ctx(request, user, parts)
    try:
        postings = posted_before(db, parts)
        offers = offers_for_parts(db, parts)
        ctx["targets"] = who_to_call(db, postings, offers)
    except Exception:
        db.rollback()
        logger.exception("dossier_contacts failed mpn={}", mpn)
        ctx.update({"targets": [], "error": True})
    return template_response("htmx/partials/search/report_contacts.html", ctx)


@router.get("/v2/partials/search/recent", response_class=HTMLResponse)
async def search_recent(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Recent-searches list for the landing (each row opens that part's report)."""
    recent = db.scalars(
        select(MaterialCard)
        .where(MaterialCard.deleted_at.is_(None), MaterialCard.last_searched_at.isnot(None))
        .order_by(MaterialCard.last_searched_at.desc())
        .limit(_RECENT_LIMIT)
    ).all()
    ctx = _ctx(request, user)
    ctx.update({"recent": recent})
    return template_response("htmx/partials/search/recent.html", ctx)


# ── Quick-source actions — Send RFQ / Add Offer from the report ───────────────
#
# Both give a one-off Search action a home: get_or_create_scratch_req (idempotent per
# user+mpn) + persist the posted market rows as Sightings, then HX-Redirect to the scratch
# req's full workspace page. They are TWO distinct routes (the report has two distinct
# buttons) that deliberately share one flow and land on the SAME workspace — that is where
# the part + its captured sightings now live and where both Send RFQ (rfq-compose) and Add
# Offer are one click away. v1 does not deep-link a specific tab (the req page has no
# tab-by-URL support and partial URLs break on reload); the distinct completion happens in
# the workspace. Payload shapes: page-level posts {mpn, items=<JSON array>}; a per-row
# button posts {mpn, vendor_name} (single vendor). The scratch req is created ONLY here (an
# action), never on a bare search.


def _parse_rows(items: str, vendor_name: str, mpn: str) -> list[dict]:
    """Build the market-row list from either the JSON ``items`` payload (page-level) or
    a single ``vendor_name`` (per-row button)."""
    rows: list[dict] = []
    if items:
        try:
            parsed = json.loads(items)
            if isinstance(parsed, list):
                rows = [r for r in parsed if isinstance(r, dict)]
        except (ValueError, TypeError):
            logger.warning("quick-source: ignoring malformed items payload")
    if not rows and vendor_name.strip():
        rows = [{"vendor_name": vendor_name.strip(), "mpn_matched": mpn.strip().upper()}]
    return rows


def _start_quick_source(db: Session, user: User, mpn: str, items: str, vendor_name: str) -> Requisition | None:
    """Create-or-reuse the scratch req, persist the posted rows, commit.

    None if no mpn.
    """
    if not mpn.strip():
        return None
    req, requirement = get_or_create_scratch_req(db, user, mpn)
    rows = _parse_rows(items, vendor_name, mpn)
    if rows:
        persist_rows_as_sightings(db, requirement, rows)
    db.commit()
    return req


def _redirect_to_req(req: Requisition | None) -> HTMLResponse:
    """HX-Redirect to the scratch req's full workspace page (partials break on reload,
    so we send the canonical full-page route)."""
    if req is None:
        return HTMLResponse(
            '<div class="text-rose-600 text-sm p-2">Enter a part number first.</div>',
            status_code=400,
        )
    return HTMLResponse("", status_code=200, headers={"HX-Redirect": f"/v2/requisitions/{req.id}"})


async def _quick_source_action(db: Session, user: User, mpn: str, items: str, vendor_name: str) -> HTMLResponse:
    """Shared impl for both quick-source routes: start the scratch req, redirect, and
    fire the background datasheet capture."""
    response = _redirect_to_req(_start_quick_source(db, user, mpn, items, vendor_name))
    if mpn.strip():
        from ..services.datasheet_capture import capture_datasheet

        await safe_background_task(
            capture_datasheet(mpn.strip().upper(), user.id), task_name="datasheet_capture", suppress_in_testing=True
        )
    return response


@router.post("/v2/partials/search/quick-source/rfq", response_class=HTMLResponse)
async def quick_source_rfq(
    mpn: str = Form(""),
    items: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send RFQ from the report → scratch req + captured sightings → its workspace."""
    return await _quick_source_action(db, user, mpn, items, vendor_name)


@router.post("/v2/partials/search/quick-source/offer", response_class=HTMLResponse)
async def quick_source_offer(
    mpn: str = Form(""),
    items: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add Offer from the report → scratch req + captured sightings → its workspace."""
    return await _quick_source_action(db, user, mpn, items, vendor_name)


@router.get("/v2/partials/search/dossier/datasheet/{datasheet_id:int}/download")
async def dossier_datasheet_download(
    datasheet_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream our stored datasheet copy from the company library (app-only fetch)."""
    row = db.get(MaterialCardDatasheet, datasheet_id)
    if row is None or not row.library_item_id:
        raise HTTPException(404, "Datasheet not found")
    data = await fetch_datasheet_bytes(row.library_drive_id, row.library_item_id)
    if data is None:
        raise HTTPException(502, "Datasheet temporarily unavailable")
    # Allowlist the filename for the Content-Disposition header — file_name derives from
    # display_mpn (external-ish), so strip anything that could inject a header (CR/LF/quote).
    safe_name = "".join(c for c in (row.file_name or "") if c.isalnum() or c in "._- ") or "datasheet.pdf"
    return StreamingResponse(
        iter([data]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )
