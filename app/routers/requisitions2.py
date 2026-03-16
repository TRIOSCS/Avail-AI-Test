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

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_req_for_user, require_user
from ..models import Requisition, User
from ..schemas.requisitions2 import (
    BulkActionName,
    InlineEditField,
    ReqListFilters,
    RowActionName,
)
from ..services.requisition_list_service import (
    get_requisition_detail,
    get_row_context,
    get_team_users,
    list_requisitions,
)
from ..services.sse_broker import broker

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

    If HTMX request, returns only the table fragment.
    """
    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)

    if _is_htmx(request):
        return templates.TemplateResponse("requisitions2/_table.html", ctx)

    return templates.TemplateResponse("requisitions2/page.html", ctx)


# ── SSE stream ───────────────────────────────────────────────────────


@router.get("/stream")
async def requisitions_stream(request: Request, _user: User = Depends(require_user)):
    """SSE endpoint — pushes table-refresh events when data changes."""

    async def event_generator():
        queue = broker.subscribe("requisitions")
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {msg['event']}\ndata: {msg.get('data', '')}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"
        finally:
            broker.unsubscribe("requisitions", queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


# ── Inline editing ───────────────────────────────────────────────────


@router.get("/{req_id}/edit/{field}", response_class=HTMLResponse)
async def inline_edit_cell(
    request: Request,
    req_id: int,
    field: InlineEditField,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return an inline edit form for a single cell."""
    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)
    users = get_team_users(db) if field == InlineEditField.owner else []
    return templates.TemplateResponse(
        "requisitions2/_inline_cell.html",
        {"request": request, "req": req, "field": field.value, "users": users},
    )


@router.patch("/{req_id}/inline", response_class=HTMLResponse)
async def inline_save(
    request: Request,
    req_id: int,
    field: str = Form(...),
    value: str = Form(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Save an inline edit and return the updated full row."""
    req = get_req_for_user(db, user, req_id, options=[])
    if not req:
        return HTMLResponse("Not found", status_code=404)

    msg = "Updated"

    if field == "name":
        clean = value.strip()
        if clean:
            req.name = clean
            msg = f"Renamed to '{clean}'"

    elif field == "status":
        from ..services.requisition_state import transition

        try:
            transition(req, value, user, db)
            msg = f"Status changed to {value}"
        except ValueError as e:
            msg = str(e)

    elif field == "urgency":
        if value in ("normal", "hot", "critical"):
            req.urgency = value
            msg = f"Urgency set to {value}"

    elif field == "deadline":
        req.deadline = value if value else None
        msg = f"Deadline {'set to ' + value if value else 'cleared'}"

    elif field == "owner":
        if value and value.isdigit():
            req.created_by = int(value)
            msg = "Owner reassigned"

    req.updated_at = datetime.now(timezone.utc)
    req.updated_by_id = user.id
    db.commit()
    db.refresh(req)

    # Build single-row context and return full <tr>

    row_ctx = get_row_context(db, req, user)
    row_ctx["request"] = request
    response = templates.TemplateResponse("requisitions2/_single_row.html", row_ctx)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    await broker.publish("requisitions", "table-refresh", msg)
    return response


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
    req = get_req_for_user(db, user, req_id, options=[])
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

    elif action_name == RowActionName.clone:
        from ..services.requisition_service import clone_requisition

        new_req = clone_requisition(db, req, user.id)
        msg = f"Cloned '{req.name}' → REQ-{new_req.id:03d}"

    elif action_name == RowActionName.assign:
        if owner_id:
            req.created_by = owner_id
            msg = f"'{req.name}' reassigned"

    if action_name != RowActionName.clone:
        db.commit()

    # Return refreshed table
    filters = _parse_filters(request)
    ctx = _table_context(request, filters, db, user)
    response = templates.TemplateResponse("requisitions2/_table.html", ctx)
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg}})
    await broker.publish("requisitions", "table-refresh", msg)
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

    reqs_q = db.query(Requisition).filter(Requisition.id.in_(id_list))
    if user.role == "sales":
        reqs_q = reqs_q.filter(Requisition.created_by == user.id)
    reqs = reqs_q.all()
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
    response.headers["HX-Trigger"] = json.dumps(
        {
            "showToast": {"message": msg},
            "clearSelection": True,
        }
    )
    await broker.publish("requisitions", "table-refresh", msg)
    return response
