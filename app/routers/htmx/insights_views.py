"""routers/htmx/insights_views.py — AI insights and activity-digest partials (HTMX).

Covers the Phase 6 AI Insights panels (requisitions/vendors/customers) and the AI
activity-digest cards. Extracted verbatim from htmx_views.py (same routes, same
`htmx-views` tag). The dashboard stats partial, pipeline-insights panel, and the
Sprint 9 knowledge-base routes were deleted in the Wave 2 simplification sweep
(spec §5.4/§8 — Dashboard/Knowledge pages cut).

Called by: app/routers/htmx_views.py (aggregated into the single exported router).
Depends on: app.services.knowledge_service, app.services.activity_digest_service
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_user
from ...models import User
from ...template_env import template_response
from .._lookup_helpers import get_requisition_or_404
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


# ── AI Digest Endpoints ───────────────────────────────────────────────────────


@router.get("/v2/partials/requisitions/{req_id}/activity-digest", response_class=HTMLResponse)
async def requisition_activity_digest(
    request: Request,
    req_id: int,
    force: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI digest card for a requisition's activity timeline (HTMX lazy-load)."""
    from ...constants import DigestEntityType
    from ...services.activity_digest_service import get_or_build_digest

    get_requisition_or_404(db, req_id)
    digest = await get_or_build_digest(DigestEntityType.REQUISITION, req_id, db, force=bool(force))
    ctx = _base_ctx(request, user, "requisitions")
    ctx["digest"] = digest
    ctx["refresh_url"] = f"/v2/partials/requisitions/{req_id}/activity-digest"
    return template_response("htmx/partials/shared/activity_digest_card.html", ctx)


@router.get("/v2/partials/customers/{company_id}/activity-digest", response_class=HTMLResponse)
async def customer_activity_digest(
    request: Request,
    company_id: int,
    force: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """AI digest card for a company's activity timeline (HTMX lazy-load)."""
    from ...models import Company

    if not db.get(Company, company_id):
        raise HTTPException(404, "Company not found")

    from ...constants import DigestEntityType
    from ...services.activity_digest_service import get_or_build_digest

    digest = await get_or_build_digest(DigestEntityType.COMPANY, company_id, db, force=bool(force))
    ctx = _base_ctx(request, user, "customers")
    ctx["digest"] = digest
    ctx["refresh_url"] = f"/v2/partials/customers/{company_id}/activity-digest"
    return template_response("htmx/partials/shared/activity_digest_card.html", ctx)


# ── AI Insights HTMX routes (Phase 6) ─────────────────────────────────


def _render_insights(request, user, insights, entity_type, entity_id):
    """Render the shared insights panel partial."""
    ctx = _base_ctx(request, user, entity_type)
    ctx["insights"] = insights
    ctx["entity_type"] = entity_type
    ctx["entity_id"] = entity_id
    ctx["refresh_url"] = f"/v2/partials/{entity_type}/{entity_id}/insights/refresh"
    return template_response("htmx/partials/shared/insights_panel.html", ctx)


@router.get("/v2/partials/requisitions/{req_id}/insights", response_class=HTMLResponse)
async def requisition_insights_panel(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a requisition."""
    from ...services.knowledge_service import get_cached_insights

    insights = get_cached_insights(db, req_id)
    return _render_insights(request, user, insights, "requisitions", req_id)


@router.post("/v2/partials/requisitions/{req_id}/insights/refresh", response_class=HTMLResponse)
async def requisition_insights_refresh(
    request: Request,
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a requisition and return panel."""
    from ...services.knowledge_service import generate_insights, get_cached_insights

    entries = []
    try:
        entries = await generate_insights(db, req_id, interactive=True)
    except Exception as e:
        db.rollback()
        logger.warning(f"Insight generation failed for req {req_id}: {e}")
    insights = entries or get_cached_insights(db, req_id)
    return _render_insights(request, user, insights, "requisitions", req_id)


@router.get("/v2/partials/vendors/{vendor_id}/insights", response_class=HTMLResponse)
async def vendor_insights_panel(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a vendor."""
    from ...services.knowledge_service import get_cached_vendor_insights

    insights = get_cached_vendor_insights(db, vendor_id)
    return _render_insights(request, user, insights, "vendors", vendor_id)


@router.post("/v2/partials/vendors/{vendor_id}/insights/refresh", response_class=HTMLResponse)
async def vendor_insights_refresh(
    request: Request,
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a vendor and return panel."""
    from ...services.knowledge_service import generate_vendor_insights, get_cached_vendor_insights

    entries = []
    try:
        entries = await generate_vendor_insights(db, vendor_id, interactive=True)
    except Exception as e:
        db.rollback()
        logger.warning(f"Insight generation failed for vendor {vendor_id}: {e}")
    insights = entries or get_cached_vendor_insights(db, vendor_id)
    return _render_insights(request, user, insights, "vendors", vendor_id)


@router.get("/v2/partials/customers/{company_id}/insights", response_class=HTMLResponse)
async def company_insights_panel(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return cached AI insights panel for a company."""
    from ...services.knowledge_service import get_cached_company_insights

    insights = get_cached_company_insights(db, company_id)
    return _render_insights(request, user, insights, "customers", company_id)


@router.post("/v2/partials/customers/{company_id}/insights/refresh", response_class=HTMLResponse)
async def company_insights_refresh(
    request: Request,
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate fresh AI insights for a company and return panel."""
    from ...services.knowledge_service import generate_company_insights, get_cached_company_insights

    entries = []
    try:
        entries = await generate_company_insights(db, company_id, interactive=True)
    except Exception as e:
        db.rollback()
        logger.warning(f"Insight generation failed for company {company_id}: {e}")
    insights = entries or get_cached_company_insights(db, company_id)
    return _render_insights(request, user, insights, "customers", company_id)
