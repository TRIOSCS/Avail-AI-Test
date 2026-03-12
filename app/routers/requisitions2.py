"""HTMX-powered requisitions list page.

Serves the Requisitions 2 All page with server-rendered partials.
All DOM updates happen through HTMX swaps or Alpine.js local state.
No shared mutable globals — this page is a self-contained island.

Called by: app/main.py (include_router)
Depends on: app/services/requisition_list_service.py,
            app/services/requisition_state.py,
            app/schemas/requisitions2.py,
            app/templates/requisitions2/
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import Requisition, User
from ..schemas.requisitions2 import (
    BulkActionName,
    ReqListFilters,
    RowActionName,
)
from ..services.requisition_list_service import (
    get_requisition_detail,
    get_team_users,
    list_requisitions,
)

router = APIRouter(prefix="/requisitions2", tags=["requisitions2"])
templates = Jinja2Templates(directory="app/templates")


def _is_htmx(request: Request) -> bool:
    """Check if request is an HTMX partial request."""
    return request.headers.get("HX-Request") == "true"


def _parse_filters(request: Request) -> ReqListFilters:
    """Parse filter params from query string, tolerating missing/invalid values."""
    params = dict(request.query_params)
    # Strip empty strings so Pydantic uses defaults
    cleaned = {k: v for k, v in params.items() if v != ""}
    try:
        return ReqListFilters(**cleaned)
    except Exception:
        return ReqListFilters()


def _table_context(request: Request, filters: ReqListFilters, db: Session, user: User) -> dict:
    """Build the shared context dict for table rendering."""
    result = list_requisitions(
        db=db,
        filters=filters,
        user_id=user.id,
        user_role=getattr(user, "role", "sales"),
    )
    users = get_team_users(db)
    return {"request": request, **result, "user": user, "users": users}


# ── Full page ────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def requisitions_page(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Full page load — returns shell + filters + table.
    If HTMX request, returns only the table fragment."""
    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)

    if _is_htmx(request):
        return templates.TemplateResponse("requisitions2/_table.html", ctx)

    return templates.TemplateResponse("requisitions2/page.html", ctx)


# ── Table fragment ───────────────────────────────────────────────────


@router.get("/table", response_class=HTMLResponse)
async def requisitions_table(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Table fragment for HTMX swap after filter/sort/paginate."""
    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)
    return templates.TemplateResponse("requisitions2/_table.html", ctx)


# ── Rows-only fragment ───────────────────────────────────────────────


@router.get("/table/rows", response_class=HTMLResponse)
async def requisitions_table_rows(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Rows-only fragment for HTMX swap."""
    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)
    return templates.TemplateResponse("requisitions2/_table_rows.html", ctx)


# ── Detail modal ─────────────────────────────────────────────────────


@router.get("/{req_id}/modal", response_class=HTMLResponse)
async def requisition_modal(
    request: Request,
    req_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Detail modal content for HTMX swap into #rq2-modal."""
    detail = get_requisition_detail(
        db=db,
        req_id=req_id,
        user_id=user.id,
        user_role=getattr(user, "role", "sales"),
    )
    if detail is None:
        return HTMLResponse(
            '<div class="rq2-modal-error">Requisition not found.</div>',
            status_code=404,
        )
    return templates.TemplateResponse(
        "requisitions2/_modal.html",
        {"request": request, **detail, "user": user},
    )


# ── Row actions ──────────────────────────────────────────────────────


@router.post("/{req_id}/action/{action_name}", response_class=HTMLResponse)
async def row_action(
    request: Request,
    req_id: int,
    action_name: RowActionName,
    owner_id: int = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Execute a row-level action and return updated table."""
    req = db.query(Requisition).filter(Requisition.id == req_id).first()
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Action completed"

    if action_name == RowActionName.archive:
        from ..services.requisition_state import transition
        try:
            transition(req, "archived", user, db)
            msg = f"'{req.name}' archived"
        except ValueError as e:
            msg = str(e)

    elif action_name == RowActionName.activate:
        from ..services.requisition_state import transition
        try:
            transition(req, "active", user, db)
            msg = f"'{req.name}' activated"
        except ValueError as e:
            msg = str(e)

    elif action_name == RowActionName.claim:
        from ..services.requirement_status import claim_requisition
        try:
            claim_requisition(req, user, db)
            msg = f"Claimed '{req.name}'"
        except ValueError as e:
            msg = str(e)

    elif action_name == RowActionName.unclaim:
        from ..services.requirement_status import unclaim_requisition
        unclaim_requisition(req, db, actor=user)
        msg = f"Unclaimed '{req.name}'"

    elif action_name == RowActionName.won:
        from ..services.requisition_state import transition
        try:
            transition(req, "won", user, db)
            msg = f"'{req.name}' marked won"
        except ValueError as e:
            msg = str(e)

    elif action_name == RowActionName.lost:
        from ..services.requisition_state import transition
        try:
            transition(req, "lost", user, db)
            msg = f"'{req.name}' marked lost"
        except ValueError as e:
            msg = str(e)

    elif action_name == RowActionName.assign:
        if owner_id:
            req.created_by = owner_id
            msg = f"'{req.name}' reassigned"

    db.commit()

    # Return refreshed table
    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)
    response = templates.TemplateResponse("requisitions2/_table.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    return response


# ── Bulk actions ─────────────────────────────────────────────────────


@router.post("/bulk/{action_name}", response_class=HTMLResponse)
async def bulk_action(
    request: Request,
    action_name: BulkActionName,
    ids: str = Form(...),
    owner_id: int = Form(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Execute bulk action on selected requisitions."""
    # Parse IDs
    id_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    if not id_list:
        filters = _parse_filters(request)
        ctx = _table_context(request, filters, db, user)
        return templates.TemplateResponse("requisitions2/_table.html", ctx)

    reqs = db.query(Requisition).filter(Requisition.id.in_(id_list)).all()
    count = 0

    for req in reqs:
        if action_name == BulkActionName.archive:
            from ..services.requisition_state import transition
            try:
                transition(req, "archived", user, db)
                count += 1
            except ValueError:
                pass
        elif action_name == BulkActionName.activate:
            from ..services.requisition_state import transition
            try:
                transition(req, "active", user, db)
                count += 1
            except ValueError:
                pass
        elif action_name == BulkActionName.assign and owner_id:
            req.created_by = owner_id
            count += 1

    db.commit()

    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)
    response = templates.TemplateResponse("requisitions2/_table.html", ctx)
    word = action_name.value + ("d" if action_name.value.endswith("e") else "ed")
    msg = f"{count} requisition{'s' if count != 1 else ''} {word}"
    response.headers["HX-Trigger"] = json.dumps({
        "showToast": {"message": msg},
        "clearSelection": True,
    })
    return response
