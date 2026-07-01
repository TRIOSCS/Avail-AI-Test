"""Legacy /requisitions2 "opportunity table" surface — RETIRED.

This hidden parallel requisitions view (never in the nav, reachable only by typing the URL,
same underlying data as the canonical /v2/requisitions Sales Hub) was retired after the
2026-07 workflow review confirmed it duplicated the Sales Hub with no unique capability worth
maintaining. This module now only 302-redirects the old URLs to the canonical surface so any
stale bookmark keeps working.

Note: the shared list-filter/pagination schemas it used to own still live in
``app/schemas/requisitions2.py`` — they are imported by ``requisition_list_service`` (the main
Sales Hub list), so that module stays (worth renaming out of the requisitions2 namespace in a
follow-up).

Called by: app/main.py (router mount).
"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter(tags=["htmx-views"])


@router.get("/requisitions2")
@router.get("/requisitions2/{path:path}")
async def requisitions2_retired_redirect(path: str = "") -> RedirectResponse:
    """302 every retired /requisitions2 URL to the canonical /v2/requisitions Sales
    Hub."""
    return RedirectResponse("/v2/requisitions", status_code=302)
