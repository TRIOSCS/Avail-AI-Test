"""htmx/page.py — Full page entry points for the HTMX frontend.

Called by: __init__.py (router mount)
Depends on: _shared helpers, template_env
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_user
from ...template_env import templates
from ._shared import _base_ctx, _vite_assets

router = APIRouter(tags=["htmx-views"])


# ── Full page entry points ──────────────────────────────────────────────


@router.get("/v2", response_class=HTMLResponse)
@router.get("/v2/requisitions", response_class=HTMLResponse)
@router.get("/v2/requisitions/{req_id:int}", response_class=HTMLResponse)
@router.get("/v2/search", response_class=HTMLResponse)
@router.get("/v2/vendors", response_class=HTMLResponse)
@router.get("/v2/vendors/{vendor_id:int}", response_class=HTMLResponse)
@router.get("/v2/customers", response_class=HTMLResponse)
@router.get("/v2/customers/{company_id:int}", response_class=HTMLResponse)
@router.get("/v2/buy-plans", response_class=HTMLResponse)
@router.get("/v2/buy-plans/{bp_id:int}", response_class=HTMLResponse)
@router.get("/v2/excess", response_class=HTMLResponse)
@router.get("/v2/excess/{list_id:int}", response_class=HTMLResponse)
@router.get("/v2/quotes", response_class=HTMLResponse)
@router.get("/v2/quotes/{quote_id:int}", response_class=HTMLResponse)
@router.get("/v2/settings", response_class=HTMLResponse)
@router.get("/v2/prospecting", response_class=HTMLResponse)
@router.get("/v2/prospecting/{prospect_id:int}", response_class=HTMLResponse)
@router.get("/v2/proactive", response_class=HTMLResponse)
@router.get("/v2/materials", response_class=HTMLResponse)
@router.get("/v2/materials/{card_id:int}", response_class=HTMLResponse)
@router.get("/v2/follow-ups", response_class=HTMLResponse)
@router.get("/v2/trouble-tickets", response_class=HTMLResponse)
@router.get("/v2/trouble-tickets/{ticket_id:int}", response_class=HTMLResponse)
async def v2_page(request: Request, db: Session = Depends(get_db)):
    """Full page load — serves base.html with initial content via HTMX."""

    path = request.url.path
    user = get_user(request, db)
    if not user:
        return templates.TemplateResponse("htmx/login.html", {"request": request, **_vite_assets()})
    if "/buy-plans" in path:
        current_view = "buy-plans"
    elif "/excess" in path:
        current_view = "excess"
    elif "/quotes" in path:
        current_view = "quotes"
    elif "/prospecting" in path:
        current_view = "prospecting"
    elif "/proactive" in path:
        current_view = "proactive"
    elif "/settings" in path:
        current_view = "settings"
    elif "/materials" in path:
        current_view = "materials"
    elif "/follow-ups" in path:
        current_view = "follow-ups"
    elif "/trouble-tickets" in path:
        current_view = "trouble-tickets"
    elif "/vendors" in path:
        current_view = "vendors"
    elif "/customers" in path:
        current_view = "customers"
    elif "/search" in path:
        current_view = "search"
    elif "/requisitions" in path:
        current_view = "requisitions"
    else:
        current_view = "requisitions"

    # Determine the correct partial URL for initial content load
    if current_view == "requisitions":
        # Split-panel workspace is the new default for requisitions
        partial_url = "/v2/partials/parts/workspace"
    elif current_view == "trouble-tickets":
        partial_url = "/v2/partials/trouble-tickets/workspace"
    else:
        partial_url = f"/v2/partials/{current_view}"
    # Pass path params for detail views
    if current_view == "requisitions" and "/requisitions/" in path:
        parts = path.split("/requisitions/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/requisitions/{parts[1]}"
    elif current_view == "vendors" and "/vendors/" in path:
        parts = path.split("/vendors/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/vendors/{parts[1]}"
    elif current_view == "customers" and "/customers/" in path:
        parts = path.split("/customers/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/customers/{parts[1]}"
    elif current_view == "buy-plans" and "/buy-plans/" in path:
        parts = path.split("/buy-plans/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/buy-plans/{parts[1]}"
    elif current_view == "excess" and "/excess/" in path:
        parts = path.split("/excess/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/excess/{parts[1]}"
    elif current_view == "quotes" and "/quotes/" in path:
        parts = path.split("/quotes/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/quotes/{parts[1]}"
    elif current_view == "prospecting" and "/prospecting/" in path:
        parts = path.split("/prospecting/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/prospecting/{parts[1]}"
    elif current_view == "trouble-tickets" and "/trouble-tickets/" in path:
        parts = path.split("/trouble-tickets/")
        if len(parts) > 1 and parts[1].isdigit():
            partial_url = f"/v2/partials/trouble-tickets/{parts[1]}"

    ctx = _base_ctx(request, user, current_view)
    ctx["partial_url"] = partial_url
    return templates.TemplateResponse("htmx/base_page.html", ctx)
