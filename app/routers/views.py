"""
views.py — HTMX view router for server-rendered pages.
Serves full-page HTML views when USE_HTMX is enabled.
Called by: app/main.py (registered when use_htmx=True)
Depends on: app/dependencies.py, app/templates/
"""

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates
from loguru import logger

from app.dependencies import require_user, wants_html

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["views"])


@router.get("/app")
async def app_shell(request: Request, user=Depends(require_user)):
    """Serve the main app shell."""
    return templates.TemplateResponse("base.html", {"request": request, "user": user})


@router.get("/search")
async def global_search(request: Request, q: str = "", user=Depends(require_user)):
    """Return search results partial for top bar global search.

    Accepts a query string and returns an HTML partial with matching results
    grouped by type (requisitions, companies, vendors). Used by the topbar
    search input via hx-get with debounce.
    """
    results = []  # TODO: aggregate search across requisitions, companies, vendors
    logger.debug("Global search query='{}' by user={}", q, user.email if user else "unknown")
    return templates.TemplateResponse(
        "partials/shared/search_results.html",
        {"request": request, "results": results, "query": q},
    )
