"""routers/excess.py — REST API for Excess Inventory lists and line items.

Thin routing layer that delegates to the excess_service for business logic.
Supports CRUD on ExcessList, single line-item add, and CSV/Excel bulk import.

Called by: main.py (router mount)
Depends on: services/excess_service, schemas/excess, file_utils, dependencies
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..file_utils import parse_tabular_file
from ..models import Company, User
from ..models.excess import ExcessLineItem
from ..schemas.excess import (
    ExcessLineItemCreate,
    ExcessLineItemResponse,
    ExcessListCreate,
    ExcessListResponse,
    ExcessListUpdate,
)
from ..services.excess_service import (
    create_excess_list,
    delete_excess_list,
    get_excess_list,
    import_line_items,
    list_excess_lists,
    update_excess_list,
)

router = APIRouter(tags=["excess"])
templates = Jinja2Templates(directory="app/templates")

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls"}


# ── HTMX Partials ────────────────────────────────────────────────────


@router.get("/v2/partials/excess", response_class=HTMLResponse)
async def partial_excess_list(
    request: Request,
    q: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render excess inventory list partial for HTMX."""
    result = list_excess_lists(db, q=q, status=status or None, limit=limit, offset=offset)
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse(
        "htmx/partials/excess/list.html",
        {
            "request": request,
            "user": user,
            "lists": result["items"],
            "total": result["total"],
            "limit": limit,
            "offset": offset,
            "companies": companies,
            "q": q,
            "status_filter": status or "",
        },
    )


@router.get("/v2/partials/excess/create-form", response_class=HTMLResponse)
async def partial_excess_create_form(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the create-excess-list modal form."""
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse(
        "htmx/partials/excess/create_modal.html",
        {
            "request": request,
            "companies": companies,
        },
    )


@router.get("/v2/partials/excess/{list_id}/add-line-item-form", response_class=HTMLResponse)
async def partial_add_line_item_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the add-line-item modal form."""
    get_excess_list(db, list_id)
    return templates.TemplateResponse(
        "htmx/partials/excess/add_line_item_modal.html",
        {"request": request, "list_id": list_id},
    )


@router.get("/v2/partials/excess/{list_id}", response_class=HTMLResponse)
async def partial_excess_detail(
    request: Request,
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render excess list detail partial for HTMX."""
    el = get_excess_list(db, list_id)
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    return templates.TemplateResponse(
        "htmx/partials/excess/detail.html",
        {
            "request": request,
            "user": user,
            "list": el,
            "line_items": items,
        },
    )


# ── List / Create ─────────────────────────────────────────────────────


@router.get("/api/excess-lists")
async def api_list_excess_lists(
    q: str = Query("", description="Search title"),
    status: str = Query("", description="Filter by status"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List excess lists with optional search, status filter, and pagination."""
    result = list_excess_lists(db, q=q, status=status or None, limit=limit, offset=offset)
    result["items"] = [ExcessListResponse.model_validate(el) for el in result["items"]]
    return result


@router.post("/api/excess-lists", status_code=201)
async def api_create_excess_list(
    payload: ExcessListCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create a new excess inventory list (owner set from current user)."""
    el = create_excess_list(
        db,
        title=payload.title,
        company_id=payload.company_id,
        owner_id=user.id,
        customer_site_id=payload.customer_site_id,
        notes=payload.notes,
    )
    return ExcessListResponse.model_validate(el)


# ── Detail / Update / Delete ──────────────────────────────────────────


@router.get("/api/excess-lists/{list_id}")
async def api_get_excess_list(
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get a single excess list by ID."""
    el = get_excess_list(db, list_id)
    return ExcessListResponse.model_validate(el)


@router.patch("/api/excess-lists/{list_id}")
async def api_update_excess_list(
    list_id: int,
    payload: ExcessListUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Partial update of an excess list."""
    updates = payload.model_dump(exclude_unset=True)
    el = update_excess_list(db, list_id, **updates)
    return ExcessListResponse.model_validate(el)


@router.delete("/api/excess-lists/{list_id}")
async def api_delete_excess_list(
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Hard-delete an excess list and its line items."""
    delete_excess_list(db, list_id)
    return {"ok": True}


# ── File Import ───────────────────────────────────────────────────────


@router.post("/api/excess-lists/{list_id}/import")
async def api_import_file(
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a CSV/TSV/Excel file to bulk-import line items."""
    filename = file.filename or ""
    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")

    rows = parse_tabular_file(content, filename)
    if not rows:
        raise HTTPException(400, "No data rows found in file")

    result = import_line_items(db, list_id, rows)
    return result


# ── Line Items ────────────────────────────────────────────────────────


@router.post("/api/excess-lists/{list_id}/line-items", status_code=201)
async def api_add_line_item(
    list_id: int,
    payload: ExcessLineItemCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Add a single line item to an excess list."""
    # Verify list exists
    excess_list = get_excess_list(db, list_id)

    item = ExcessLineItem(
        excess_list_id=list_id,
        part_number=payload.part_number,
        manufacturer=payload.manufacturer,
        quantity=payload.quantity,
        date_code=payload.date_code,
        condition=payload.condition or "New",
        asking_price=payload.asking_price,
        notes=payload.notes,
    )
    db.add(item)
    excess_list.total_line_items = (excess_list.total_line_items or 0) + 1
    db.commit()
    db.refresh(item)
    return ExcessLineItemResponse.model_validate(item)


@router.delete("/api/excess-lists/{list_id}/line-items/{item_id}")
async def api_delete_line_item(
    list_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a single line item from an excess list."""
    excess_list = get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, item_id)
    if not item or item.excess_list_id != list_id:
        raise HTTPException(404, f"Line item {item_id} not found in list {list_id}")
    db.delete(item)
    excess_list.total_line_items = max((excess_list.total_line_items or 1) - 1, 0)
    db.commit()
    return {"ok": True}


@router.get("/api/excess-lists/{list_id}/line-items")
async def api_list_line_items(
    list_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List line items for a given excess list."""
    # Verify list exists
    get_excess_list(db, list_id)

    query = db.query(ExcessLineItem).filter(ExcessLineItem.excess_list_id == list_id)
    total = query.count()
    items = query.order_by(ExcessLineItem.id).offset(offset).limit(limit).all()

    return {
        "items": [ExcessLineItemResponse.model_validate(li) for li in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
