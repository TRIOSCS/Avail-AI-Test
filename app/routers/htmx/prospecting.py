"""
routers/htmx/prospecting.py — HTMX partials for Prospecting, Proactive Part Match,
and Strategic Vendors (My Vendors).

Handles prospect listing/detail/claim/dismiss/enrich, proactive match listing/dismiss,
and strategic vendor claim/drop workflows.

Called by: htmx router __init__.py (router is shared via _helpers)
Depends on: models (ProspectAccount, ProactiveMatch, User), services (prospect_claim,
    prospect_free_enrichment, prospect_warm_intros, proactive_service,
    strategic_vendor_service), _helpers (router, templates, _base_ctx)
"""

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session, joinedload

from ...database import get_db
from ...dependencies import require_user
from ...models import User
from ...models.prospect_account import ProspectAccount
from ...utils.sql_helpers import escape_like
from ._helpers import _base_ctx, router, templates

# ── Prospecting partials ──────────────────────────────────────────────


@router.get("/v2/partials/prospecting", response_class=HTMLResponse)
async def prospecting_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "buyer_ready_desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospecting list as HTML partial."""
    query = db.query(ProspectAccount)
    if status:
        query = query.filter(ProspectAccount.status == status)
    else:
        query = query.filter(ProspectAccount.status.in_(["suggested", "claimed", "dismissed"]))
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(ProspectAccount.name.ilike(f"%{safe}%") | ProspectAccount.domain.ilike(f"%{safe}%"))
    total = query.count()
    if sort == "fit_desc":
        query = query.order_by(ProspectAccount.fit_score.desc())
    elif sort == "recent_desc":
        query = query.order_by(ProspectAccount.created_at.desc())
    else:
        query = query.order_by(ProspectAccount.readiness_score.desc(), ProspectAccount.fit_score.desc())
    prospects = query.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = (total + per_page - 1) // per_page
    ctx = _base_ctx(request, user, "prospecting")
    ctx.update(
        {
            "prospects": prospects,
            "q": q,
            "status": status,
            "sort": sort,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }
    )
    return templates.TemplateResponse("htmx/partials/prospecting/list.html", ctx)


@router.get("/v2/partials/prospecting/{prospect_id}", response_class=HTMLResponse)
async def prospecting_detail_partial(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospect detail as HTML partial."""
    prospect = (
        db.query(ProspectAccount)
        .options(
            joinedload(ProspectAccount.claimed_by_user),
        )
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    return templates.TemplateResponse("htmx/partials/prospecting/detail.html", ctx)


@router.post("/v2/partials/prospecting/{prospect_id}/claim", response_class=HTMLResponse)
async def claim_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a prospect — returns updated card for OOB swap."""
    from ...services.prospect_claim import claim_prospect

    try:
        claim_prospect(prospect_id, user.id, db)
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e))
    prospect = (
        db.query(ProspectAccount)
        .options(
            joinedload(ProspectAccount.claimed_by_user),
        )
        .filter(ProspectAccount.id == prospect_id)
        .first()
    )
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    return templates.TemplateResponse("htmx/partials/prospecting/_card.html", ctx)


@router.post("/v2/partials/prospecting/{prospect_id}/dismiss", response_class=HTMLResponse)
async def dismiss_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss a prospect — returns updated card for OOB swap."""
    from datetime import datetime, timezone

    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    prospect.status = "dismissed"
    prospect.dismissed_by = user.id
    prospect.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    return templates.TemplateResponse("htmx/partials/prospecting/_card.html", ctx)


@router.post("/v2/partials/prospecting/{prospect_id}/enrich", response_class=HTMLResponse)
async def enrich_prospect_htmx(
    request: Request,
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Run free enrichment on a prospect — returns refreshed detail."""
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    try:
        from ...services.prospect_free_enrichment import run_free_enrichment

        await run_free_enrichment(prospect_id)
        db.refresh(prospect)
    except Exception as exc:
        logger.warning("Enrichment failed for prospect {}: {}", prospect_id, exc)
    try:
        from ...services.prospect_warm_intros import detect_warm_intros, generate_one_liner

        warm = detect_warm_intros(prospect, db)
        one_liner = generate_one_liner(prospect, warm)
        ed = dict(prospect.enrichment_data or {})
        ed["warm_intro"] = warm
        ed["one_liner"] = one_liner
        prospect.enrichment_data = ed
        db.commit()
    except Exception as exc:
        logger.warning("Warm intro detection failed for prospect {}: {}", prospect_id, exc)
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    return templates.TemplateResponse("htmx/partials/prospecting/detail.html", ctx)


# ── Proactive Part Match ─────────────────────────────────────────────


@router.get("/v2/partials/proactive", response_class=HTMLResponse)
async def proactive_list_partial(
    request: Request,
    tab: str = "matches",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Proactive matches list partial — shows matches and sent offers."""
    from ...services.proactive_service import get_matches_for_user, get_sent_offers

    matches = get_matches_for_user(db, user.id, status="new")
    sent = get_sent_offers(db, user.id) if tab == "sent" else []

    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = matches
    ctx["sent"] = sent
    ctx["tab"] = tab
    return templates.TemplateResponse("htmx/partials/proactive/list.html", ctx)


@router.post("/v2/partials/proactive/{match_id}/dismiss", response_class=HTMLResponse)
async def proactive_dismiss(
    match_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss a proactive match and reload the list."""
    from ...models import ProactiveMatch

    db.query(ProactiveMatch).filter(
        ProactiveMatch.id == match_id,
        ProactiveMatch.salesperson_id == user.id,
        ProactiveMatch.status == "new",
    ).update({"status": "dismissed"}, synchronize_session=False)
    db.commit()

    # Re-render list
    from ...services.proactive_service import get_matches_for_user

    matches = get_matches_for_user(db, user.id, status="new")
    ctx = _base_ctx(request, user, "proactive")
    ctx["matches"] = matches
    ctx["sent"] = []
    ctx["tab"] = "matches"
    return templates.TemplateResponse("htmx/partials/proactive/list.html", ctx)


# ── Strategic Vendors (My Vendors) ───────────────────────────────────


@router.get("/v2/partials/strategic", response_class=HTMLResponse)
async def strategic_list_partial(
    request: Request,
    search: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """My Vendors list partial — claimed vendors + open pool."""
    from ...services import strategic_vendor_service as svc

    my_vendors = svc.get_my_strategic(db, user.id)
    open_vendors, open_total = svc.get_open_pool(db, limit=20, offset=0, search=search or None)

    ctx = _base_ctx(request, user, "strategic")
    ctx["my_vendors"] = my_vendors
    ctx["slot_count"] = len(my_vendors)
    ctx["max_slots"] = svc.MAX_STRATEGIC_VENDORS
    ctx["open_vendors"] = open_vendors
    ctx["open_total"] = open_total
    ctx["search"] = search
    return templates.TemplateResponse("htmx/partials/strategic/list.html", ctx)


@router.post("/v2/partials/strategic/claim/{vendor_card_id}", response_class=HTMLResponse)
async def strategic_claim(
    vendor_card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a vendor as strategic and reload the list."""
    from ...services import strategic_vendor_service as svc

    svc.claim_vendor(db, user.id, vendor_card_id)

    my_vendors = svc.get_my_strategic(db, user.id)
    open_vendors, open_total = svc.get_open_pool(db, limit=20, offset=0)

    ctx = _base_ctx(request, user, "strategic")
    ctx["my_vendors"] = my_vendors
    ctx["slot_count"] = len(my_vendors)
    ctx["max_slots"] = svc.MAX_STRATEGIC_VENDORS
    ctx["open_vendors"] = open_vendors
    ctx["open_total"] = open_total
    ctx["search"] = ""
    return templates.TemplateResponse("htmx/partials/strategic/list.html", ctx)


@router.delete("/v2/partials/strategic/{vendor_card_id}/drop", response_class=HTMLResponse)
async def strategic_drop(
    vendor_card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Drop a strategic vendor and reload the list."""
    from ...services import strategic_vendor_service as svc

    svc.drop_vendor(db, user.id, vendor_card_id)

    my_vendors = svc.get_my_strategic(db, user.id)
    open_vendors, open_total = svc.get_open_pool(db, limit=20, offset=0)

    ctx = _base_ctx(request, user, "strategic")
    ctx["my_vendors"] = my_vendors
    ctx["slot_count"] = len(my_vendors)
    ctx["max_slots"] = svc.MAX_STRATEGIC_VENDORS
    ctx["open_vendors"] = open_vendors
    ctx["open_total"] = open_total
    ctx["search"] = ""
    return templates.TemplateResponse("htmx/partials/strategic/list.html", ctx)
