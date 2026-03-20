"""routers/excess.py — REST API for Excess Inventory lists and line items.

Thin routing layer that delegates to the excess_service for business logic.
Supports CRUD on ExcessList, single line-item add, CSV/Excel bulk import,
stats, email solicitations, and proactive matching.

Called by: main.py (router mount)
Depends on: services/excess_service, schemas/excess, file_utils, dependencies
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..file_utils import parse_tabular_file
from ..models import Company, User
from ..models.excess import Bid, ExcessLineItem
from ..schemas.excess import (
    BidCreateRequest,
    BidResponse,
    BidSolicitationResponse,
    BidUpdate,
    ConfirmImportRequest,
    ExcessLineItemCreate,
    ExcessLineItemResponse,
    ExcessListCreate,
    ExcessListResponse,
    ExcessListUpdate,
    ExcessStatsResponse,
    ParseBidResponseRequest,
    SendBidSolicitationRequest,
)
from ..services.excess_service import (
    accept_bid,
    confirm_import,
    create_bid,
    create_excess_list,
    create_proactive_matches_for_excess,
    delete_excess_list,
    get_excess_list,
    get_excess_stats,
    import_line_items,
    list_bids,
    list_excess_lists,
    list_solicitations,
    match_excess_demand,
    parse_bid_response,
    preview_import,
    send_bid_solicitation,
    update_excess_list,
)
from ..utils.normalization import normalize_mpn_key

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
    stats = get_excess_stats(db)
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
            "stats": stats,
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


@router.post("/v2/partials/excess/{list_id}/import-preview", response_class=HTMLResponse)
async def partial_import_preview(
    request: Request,
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse uploaded file and render an import preview partial for HTMX."""
    get_excess_list(db, list_id)
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    rows = parse_tabular_file(content, filename)
    if not rows:
        raise HTTPException(400, "No data rows found")
    result = preview_import(rows)
    return templates.TemplateResponse(
        "htmx/partials/excess/import_preview.html",
        {
            "request": request,
            "list_id": list_id,
            "filename": filename,
            **result,
            "all_valid_rows_json": json.dumps(result["all_valid_rows"]),
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


# ── Import Preview / Confirm ──────────────────────────────────────────


@router.post("/api/excess-lists/{list_id}/preview-import")
async def api_preview_import(
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file and return a validation preview (no DB writes)."""
    get_excess_list(db, list_id)
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{ext}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    rows = parse_tabular_file(content, filename)
    if not rows:
        raise HTTPException(400, "No data rows found")
    return preview_import(rows)


@router.post("/api/excess-lists/{list_id}/confirm-import")
async def api_confirm_import(
    list_id: int,
    payload: ConfirmImportRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Confirm import of pre-validated rows, then run demand matching."""
    rows = [r.model_dump() for r in payload.rows]
    result = confirm_import(db, list_id, rows)
    match_result = match_excess_demand(db, list_id, user_id=user.id)
    return {
        "imported": result["imported"],
        "matches_created": match_result["matches_created"],
    }


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
        normalized_part_number=normalize_mpn_key(payload.part_number) or None,
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


# ── Bids ──────────────────────────────────────────────────────────────


@router.post("/api/excess-lists/{list_id}/line-items/{item_id}/bids", status_code=201)
async def api_create_bid(
    list_id: int,
    item_id: int,
    payload: BidCreateRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Record a bid on an excess line item."""
    bid = create_bid(
        db,
        line_item_id=item_id,
        list_id=list_id,
        unit_price=payload.unit_price,
        quantity_wanted=payload.quantity_wanted,
        user_id=user.id,
        bidder_company_id=payload.bidder_company_id,
        bidder_vendor_card_id=payload.bidder_vendor_card_id,
        lead_time_days=payload.lead_time_days,
        source=payload.source or "manual",
        notes=payload.notes,
    )
    return BidResponse.model_validate(bid)


@router.get("/api/excess-lists/{list_id}/line-items/{item_id}/bids")
async def api_list_bids(
    list_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all bids for a line item, sorted by price (best first)."""
    bids = list_bids(db, item_id, list_id)
    return {
        "items": [BidResponse.model_validate(b) for b in bids],
        "total": len(bids),
    }


@router.patch("/api/excess-lists/{list_id}/line-items/{item_id}/bids/{bid_id}")
async def api_update_bid(
    list_id: int,
    item_id: int,
    bid_id: int,
    payload: BidUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a bid.

    If status=='accepted', triggers accept_bid logic.
    """
    updates = payload.model_dump(exclude_unset=True)

    if updates.get("status") == "accepted":
        bid = accept_bid(db, bid_id, item_id, list_id)
        return BidResponse.model_validate(bid)

    # Verify ownership chain
    get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, item_id)
    if not item or item.excess_list_id != list_id:
        raise HTTPException(404, f"Line item {item_id} not found in list {list_id}")

    bid = db.get(Bid, bid_id)
    if not bid or bid.excess_line_item_id != item_id:
        raise HTTPException(404, f"Bid {bid_id} not found on line item {item_id}")

    for key, value in updates.items():
        if value is not None and hasattr(bid, key):
            setattr(bid, key, value)

    db.commit()
    db.refresh(bid)
    return BidResponse.model_validate(bid)


# ── Bid HTMX Partials ─────────────────────────────────────────────────


@router.get("/v2/partials/excess/{list_id}/line-items/{item_id}/bid-form", response_class=HTMLResponse)
async def partial_bid_form(
    request: Request,
    list_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the bid recording form modal."""
    get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, item_id)
    if not item or item.excess_list_id != list_id:
        raise HTTPException(404, f"Line item {item_id} not found in list {list_id}")
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse(
        "htmx/partials/excess/bid_form.html",
        {
            "request": request,
            "list_id": list_id,
            "item_id": item_id,
            "item": item,
            "companies": companies,
        },
    )


@router.get("/v2/partials/excess/{list_id}/line-items/{item_id}/bids", response_class=HTMLResponse)
async def partial_bid_list(
    request: Request,
    list_id: int,
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the bid list modal for a line item."""
    get_excess_list(db, list_id)
    item = db.get(ExcessLineItem, item_id)
    if not item or item.excess_list_id != list_id:
        raise HTTPException(404, f"Line item {item_id} not found in list {list_id}")
    bids = list_bids(db, item_id, list_id)
    companies = db.query(Company).order_by(Company.name).all()
    return templates.TemplateResponse(
        "htmx/partials/excess/bid_list.html",
        {
            "request": request,
            "list_id": list_id,
            "item_id": item_id,
            "item": item,
            "bids": bids,
            "companies": companies,
        },
    )


# ── Phase 4: Stats ────────────────────────────────────────────────────


@router.get("/api/excess-stats")
async def api_excess_stats(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get aggregate stats for the excess module."""
    stats = get_excess_stats(db)
    return ExcessStatsResponse(**stats)


# ── Phase 4: Email Solicitations ──────────────────────────────────────


@router.post("/api/excess-lists/{list_id}/solicitations", status_code=201)
async def api_send_solicitations(
    list_id: int,
    payload: SendBidSolicitationRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Send bid solicitation emails for selected line items."""
    solicitations = send_bid_solicitation(
        db,
        list_id=list_id,
        line_item_ids=payload.line_item_ids,
        recipient_email=payload.recipient_email,
        recipient_name=payload.recipient_name,
        contact_id=payload.contact_id,
        user_id=user.id,
        subject=payload.subject,
        message=payload.message,
    )
    return {
        "items": [BidSolicitationResponse.model_validate(s) for s in solicitations],
        "total": len(solicitations),
    }


@router.get("/api/excess-lists/{list_id}/solicitations")
async def api_list_solicitations(
    list_id: int,
    item_id: int | None = Query(None),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List solicitations for an excess list."""
    solicitations = list_solicitations(db, list_id, item_id)
    return {
        "items": [BidSolicitationResponse.model_validate(s) for s in solicitations],
        "total": len(solicitations),
    }


@router.post("/api/excess-solicitations/{solicitation_id}/parse-response")
async def api_parse_bid_response(
    solicitation_id: int,
    payload: ParseBidResponseRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Parse a bid response from an email solicitation and create a Bid."""
    bid = parse_bid_response(
        db,
        solicitation_id=solicitation_id,
        unit_price=payload.unit_price,
        quantity_wanted=payload.quantity_wanted,
        lead_time_days=payload.lead_time_days,
        notes=payload.notes,
    )
    return BidResponse.model_validate(bid)


# ── Phase 4: Proactive Matching on Archive ────────────────────────────


@router.post("/api/excess-lists/{list_id}/create-proactive-matches")
async def api_create_proactive_matches(
    list_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Create proactive matches when an excess list is archived."""
    result = create_proactive_matches_for_excess(db, list_id, user_id=user.id)
    return result
