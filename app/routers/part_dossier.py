"""routers/part_dossier.py — the visible Part Dossier ("The Bench") GET routes.

Serves the one-PN sourcing dossier at /v2/search?mpn=<PN>: an instant-from-DB hero +
specs + history (reused), with the live market terminal streaming in below. All routes
are server-rendered HTML fragments swapped by HTMX — NOT a SPA.

The four section endpoints are lazy-loaded by dossier_shell.html. Hero does the
light-footprint write (bump search_count / last_searched_at on an existing card only —
a bare search never creates a card). Market consults the search:{key}:latest Redis
pointer (written by search_service.stream_search_mpn) to render cached vendor rows on a
cache hit, else fires the existing /v2/partials/search/run SSE flow inside the dark
terminal frame.

Called by: app/main.py (include_router); dossier_shell.html lazy-load divs.
Depends on: services.part_history_service.get_part_history, services.fru_matrix_service
            .get_fru_view, search_service (_get_search_redis / _get_cached_search_results
            render path), models.intelligence.MaterialCard, utils.normalization
            .normalize_mpn_key, template_env.template_response. Shares base ctx via the
            lazy-imported htmx_views._base_ctx (same pattern as requisitions2.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..models.intelligence import MaterialCard
from ..template_env import template_response
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

    try:
        history = get_part_history(db, key)
    except Exception:
        logger.exception("dossier_hero get_part_history failed mpn={} key={}", mpn, key)
        from ..services.part_history_service import PartHistory

        history = PartHistory(found=False)

    # FRU crosswalk is additive context only — a failure must never break the hero.
    fru_view = None
    try:
        from ..services.fru_matrix_service import get_fru_view

        fru_view = get_fru_view(db, mpn)
    except Exception:
        logger.exception("dossier_hero get_fru_view failed mpn={} key={}", mpn, key)
        fru_view = None

    ctx = _ctx(request, user)
    ctx.update({"mpn": display_mpn, "card": card, "history": history, "fru_view": fru_view})
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


@router.get("/v2/partials/search/dossier/market", response_class=HTMLResponse)
async def dossier_market(
    request: Request,
    mpn: str = Query(""),
    refresh: bool = Query(False),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Live-market terminal. Cache hit → cached vendor rows + freshness stamp; cache
    miss (or ``refresh=1``) → terminal frame whose body auto-fires the existing
    /v2/partials/search/run SSE flow.

    The SSE engine is reused UNCHANGED — the cache-miss branch just embeds it in the dark
    terminal frame. The pointer key search:{key}:latest is written at the end of
    stream_search_mpn (TTL 900s). ``refresh=1`` (the "↻ Refresh market" button) skips the
    cache so the connector sweep re-runs.
    """
    from ..search_service import _get_search_redis

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

    ctx = _ctx(request, user)
    ctx.update(
        {
            "mpn": display_mpn,
            "cached_search_id": cached_search_id,
            "cached_rows": cached_rows,
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
