"""Admin router — pending manufacturer-alias approval queue (survey idea #11).

Routes (all require require_settings_access):
  GET  /v2/partials/admin/manufacturer-aliases                  — pending queue partial
  POST /v2/partials/admin/manufacturer-aliases/{id}/approve     — append alias / create canonical
  POST /v2/partials/admin/manufacturer-aliases/{id}/reject      — dismiss

Thin router: the promote/dismiss logic lives in services.manufacturer_alias_harvester.
Called by: app.routers.admin (mount).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...constants import ManufacturerAliasStatus
from ...database import get_db
from ...dependencies import require_settings_access
from ...models import User
from ...models.sourcing import ManufacturerAliasPending
from ...services.manufacturer_alias_harvester import approve_manufacturer_alias, reject_manufacturer_alias
from ...template_env import template_response
from ..htmx._shared import full_page_shell

router = APIRouter()


def _render_queue(request: Request, user: User, db: Session) -> HTMLResponse:
    rows = (
        db.query(ManufacturerAliasPending)
        .filter(
            ManufacturerAliasPending.status == ManufacturerAliasStatus.PENDING.value
        )  # rejected rows retained, not shown
        .order_by(ManufacturerAliasPending.created_at.desc())
        .all()
    )
    return template_response(
        "htmx/partials/admin/manufacturer_aliases_pending.html",
        {"request": request, "rows": rows, "user": user},
    )


@router.get("/v2/partials/admin/manufacturer-aliases", response_class=HTMLResponse)
async def list_pending(
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """The pending manufacturer-alias queue (HTMX partial; app shell on a raw
    reload)."""
    if request.headers.get("HX-Request") != "true":
        return full_page_shell(request, user, request.url.path, "settings")
    return _render_queue(request, user, db)


@router.post("/v2/partials/admin/manufacturer-aliases/{pending_id}/approve", response_class=HTMLResponse)
async def approve(
    pending_id: int,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Approve: append the variant to its canonical's aliases (or create the canonical)."""
    approve_manufacturer_alias(db, pending_id, user)
    return _render_queue(request, user, db)


@router.post("/v2/partials/admin/manufacturer-aliases/{pending_id}/reject", response_class=HTMLResponse)
async def reject(
    pending_id: int,
    request: Request,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Reject: dismiss the pending proposal."""
    reject_manufacturer_alias(db, pending_id)
    return _render_queue(request, user, db)
