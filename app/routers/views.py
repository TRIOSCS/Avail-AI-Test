"""
views.py — HTMX view router for server-rendered pages.
Serves full-page HTML views when USE_HTMX is enabled.
Called by: app/main.py (registered when use_htmx=True)
Depends on: app/dependencies.py, app/templates/
"""

from fastapi import APIRouter, Depends, Request
from fastapi.templating import Jinja2Templates

from app.dependencies import require_user, wants_html

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["views"])


@router.get("/app")
async def app_shell(request: Request, user=Depends(require_user)):
    """Serve the main app shell."""
    return templates.TemplateResponse("base.html", {"request": request, "user": user})
