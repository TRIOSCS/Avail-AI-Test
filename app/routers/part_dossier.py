"""routers/part_dossier.py — the visible Part Dossier ("The Bench") GET routes.

Serves the one-PN sourcing dossier at /v2/search?mpn=<PN>: an instant-from-DB hero +
specs + history (reused), with the live market streaming in below. All routes are
server-rendered HTML fragments swapped by HTMX — NOT a SPA.

The four section endpoints are lazy-loaded by dossier_shell.html. Hero does the
light-footprint write (bump search_count / last_searched_at on an existing card only —
a bare search never creates a card). Market consults the search:{key}:latest Redis
pointer (written by search_service.stream_search_mpn) to render cached vendor rows on a
cache hit, else fires the existing /v2/partials/search/run SSE flow; a degraded-source
banner (get_market_source_health) renders above both branches.

Called by: app/main.py (include_router); dossier_shell.html lazy-load divs.
Depends on: services.part_history_service.get_part_history, services.fru_matrix_service
            .get_fru_view, search_service (_get_search_redis / _get_cached_search_results
            render path), models.intelligence.MaterialCard, utils.normalization
            .normalize_mpn_key, template_env.template_response. Shares base ctx via the
            lazy-imported htmx_views._base_ctx (same pattern as requisitions2.py).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..models.intelligence import MaterialCard, MaterialCardDatasheet
from ..models.sourcing import Requisition
from ..services.datasheet_library import fetch_datasheet_bytes
from ..services.quick_source_service import get_or_create_scratch_req, persist_rows_as_sightings
from ..template_env import template_response
from ..utils.async_helpers import safe_background_task
from ..utils.normalization import normalize_mpn_key

router = APIRouter(tags=["part-dossier"])

# Recent-searches landing cap (section 9 of the design spec).
_RECENT_LIMIT = 12


def _ctx(request: Request, user: User) -> dict:
    """Shared base context.

    Lazy import of htmx_views._base_ctx avoids an import cycle (same pattern as
    routers/requisitions2.py:67).
    """
    from app.routers.htmx_views import _base_ctx

    return _base_ctx(request, user, "search")


def _resolve_card(db: Session, key: str) -> MaterialCard | None:
    """Look up a live MaterialCard by normalized key (never creates one)."""
    if not key:
        return None
    return (
        db.query(MaterialCard)
        .filter(MaterialCard.normalized_mpn == key)
        .filter(MaterialCard.deleted_at.is_(None))
        .first()
    )


@router.get("/v2/partials/search/dossier/hero", response_class=HTMLResponse)
async def dossier_hero(
    request: Request,
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Identity hero — instant DB read.

    Bumps search_count on an existing card only.
    """
    from ..services.part_history_service import get_part_history

    display_mpn = mpn.strip().upper()
    key = normalize_mpn_key(mpn)
    card = _resolve_card(db, key)

    # Light-footprint write (decision #6): a bare search only touches the existing card's
    # search telemetry. Unknown PNs stay "New to us" — no card is created.
    if card is not None:
        card.search_count = (card.search_count or 0) + 1
        card.last_searched_at = datetime.now(timezone.utc)
        db.commit()

    history_error = False
    try:
        history = get_part_history(db, key)
    except Exception:
        # Roll back so the session is usable again — a DB error here otherwise leaves the
        # transaction aborted (InFailedSqlTransaction) and the FRU lookup below would be
        # guaranteed to fail, masking the real error. Mirrors search_history_panel's guard.
        db.rollback()
        logger.exception("dossier_hero get_part_history failed mpn={} key={}", mpn, key)
        from ..services.part_history_service import PartHistory

        history = PartHistory(found=False)
        history_error = True

    # FRU crosswalk is additive context only — skipped on a history failure (the card guard
    # already suppresses the crosswalk card, and we never run it on a degraded session).
    fru_view = None
    if not history_error:
        try:
            from ..services.fru_matrix_service import get_fru_view

            fru_view = get_fru_view(db, mpn)
        except Exception:
            logger.exception("dossier_hero get_fru_view failed mpn={} key={}", mpn, key)
            fru_view = None

    ctx = _ctx(request, user)
    ctx.update({"mpn": display_mpn, "card": card, "history": history, "fru_view": fru_view})

    # Auto-datasheet capture (background, never blocks the dossier render).
    if display_mpn:
        from ..services.datasheet_capture import capture_datasheet

        await safe_background_task(
            capture_datasheet(display_mpn, user.id), task_name="datasheet_capture", suppress_in_testing=True
        )

    return template_response("htmx/partials/search/dossier_hero.html", ctx)


@router.get("/v2/partials/search/dossier/specs", response_class=HTMLResponse)
async def dossier_specs(
    request: Request,
    mpn: str = Query(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Spec grid from MaterialCard enrichment fields (graceful when card is None)."""
    card = _resolve_card(db, normalize_mpn_key(mpn))
    ctx = _ctx(request, user)
    ctx.update({"mpn": mpn.strip().upper(), "card": card})
    return template_response("htmx/partials/search/dossier_specs.html", ctx)


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
    resp = template_response("htmx/partials/search/dossier_datasheet_block.html", ctx)
    if card is not None and (card.datasheet_captured_at or card.datasheet_searched_at):
        resp.status_code = 286
    return resp


@router.get("/v2/partials/search/dossier/market", response_class=HTMLResponse)
async def dossier_market(
    request: Request,
    mpn: str = Query(""),
    refresh: bool = Query(False),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Live-market section (light brand card). Cache hit → cached vendor rows +
    freshness stamp; cache miss (or ``refresh=1``) → a frame whose body auto-fires the
    existing /v2/partials/search/run SSE flow.

    The SSE engine is reused UNCHANGED — the cache-miss branch just embeds it. A
    degraded-source banner (``market_health``) is rendered above both branches so a
    sparse market reads as "N sources unavailable" rather than looking empty. The
    pointer key search:{key}:latest is written at the end of stream_search_mpn (TTL
    900s). ``refresh=1`` (the "↻ Refresh market" button) skips the cache so the
    connector sweep re-runs.
    """
    from ..search_service import _get_search_redis, get_market_source_health

    display_mpn = mpn.strip().upper()
    key = normalize_mpn_key(mpn)

    cached_search_id: str | None = None
    cached_rows: list[dict] | None = None
    if not refresh:
        try:
            rc = _get_search_redis()
            if rc and key:
                pointer = rc.get(f"search:{key}:latest")
                if pointer:
                    from ..routers.htmx_views import _get_cached_search_results

                    rows = _get_cached_search_results(pointer)
                    if rows:
                        cached_search_id = pointer
                        cached_rows = rows
        except Exception:
            logger.warning("dossier_market cache lookup failed mpn={} key={}", mpn, key, exc_info=True)

    # Degraded-state banner: which live-market sources are down (auth/quota). Rendered
    # above both the cache-hit and cache-miss branches so a sparse market reads as
    # "N sources unavailable" instead of looking mysteriously empty. Best-effort — a
    # health-check failure must never break the market section itself.
    try:
        market_health = get_market_source_health(db)
    except Exception:
        logger.warning("dossier_market source-health lookup failed mpn={}", mpn, exc_info=True)
        market_health = None

    ctx = _ctx(request, user)
    ctx.update(
        {
            "mpn": display_mpn,
            "cached_search_id": cached_search_id,
            "cached_rows": cached_rows,
            "market_health": market_health,
        }
    )
    return template_response("htmx/partials/search/dossier_market.html", ctx)


@router.get("/v2/partials/search/recent", response_class=HTMLResponse)
async def search_recent(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Recent-searches list for the landing (deep links to each PN's dossier)."""
    recent = (
        db.query(MaterialCard)
        .filter(MaterialCard.deleted_at.is_(None))
        .filter(MaterialCard.last_searched_at.isnot(None))
        .order_by(MaterialCard.last_searched_at.desc())
        .limit(_RECENT_LIMIT)
        .all()
    )
    ctx = _ctx(request, user)
    ctx.update({"recent": recent})
    return template_response("htmx/partials/search/dossier_recent.html", ctx)


# ── Quick-source actions — Send RFQ / Add Offer from the dossier ──────────────
#
# Both give a one-off Search action a home: get_or_create_scratch_req (idempotent per
# user+mpn) + persist the posted market rows as Sightings, then HX-Redirect to the scratch
# req's full workspace page. They are TWO distinct routes (the dossier has two distinct
# buttons) that deliberately share one flow and land on the SAME workspace — that is where
# the part + its captured sightings now live and where both Send RFQ (rfq-compose) and Add
# Offer are one click away. v1 does not deep-link a specific tab (the req page has no
# tab-by-URL support and partial URLs break on reload); the distinct completion happens in
# the workspace. Payload shapes: page-level posts {mpn, items=<JSON array>}; a per-row
# button posts {mpn, vendor_name} (single vendor). The scratch req is created ONLY here (an
# action), never on a bare search (design decision #4).


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


@router.post("/v2/partials/search/quick-source/rfq", response_class=HTMLResponse)
async def quick_source_rfq(
    mpn: str = Form(""),
    items: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send RFQ from the dossier → scratch req + captured sightings → its workspace."""
    response = _redirect_to_req(_start_quick_source(db, user, mpn, items, vendor_name))
    if mpn.strip():
        from ..services.datasheet_capture import capture_datasheet

        await safe_background_task(
            capture_datasheet(mpn.strip().upper(), user.id), task_name="datasheet_capture", suppress_in_testing=True
        )
    return response


@router.post("/v2/partials/search/quick-source/offer", response_class=HTMLResponse)
async def quick_source_offer(
    mpn: str = Form(""),
    items: str = Form(""),
    vendor_name: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add Offer from the dossier → scratch req + captured sightings → its workspace."""
    response = _redirect_to_req(_start_quick_source(db, user, mpn, items, vendor_name))
    if mpn.strip():
        from ..services.datasheet_capture import capture_datasheet

        await safe_background_task(
            capture_datasheet(mpn.strip().upper(), user.id), task_name="datasheet_capture", suppress_in_testing=True
        )
    return response


@router.get("/v2/partials/search/dossier/datasheet/{datasheet_id:int}/download")
async def dossier_datasheet_download(
    datasheet_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream our stored datasheet copy from the company library (app-only fetch)."""
    row = db.query(MaterialCardDatasheet).filter(MaterialCardDatasheet.id == datasheet_id).first()
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
