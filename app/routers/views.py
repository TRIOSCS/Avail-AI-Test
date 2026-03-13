"""
views.py — HTMX view router for server-rendered pages.
Serves full-page HTML views when USE_HTMX is enabled.
Called by: app/main.py (registered when use_htmx=True)
Depends on: app/dependencies.py, app/templates/, app/database.py
"""

import math

from fastapi import APIRouter, Depends, Query, Request
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy import or_
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user, wants_html
from app.models import Requirement, Requisition, User
from app.utils.sql_helpers import escape_like

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(tags=["views"])

PER_PAGE = 25


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


# ── Requisitions views ──────────────────────────────────────────────


def _query_requisitions(db: Session, user: User, q: str, status: str, sort: str, dir: str, page: int):
    """Build a filtered, sorted, paginated requisition query.

    Returns (requisitions_list, page, total_pages) where each item is a
    lightweight dict suitable for the list template.
    """
    req_count_sq = (
        sqlfunc.count(Requirement.id)
    )
    base = (
        db.query(Requisition, req_count_sq)
        .outerjoin(Requirement, Requirement.requisition_id == Requisition.id)
        .group_by(Requisition.id)
    )

    # Sales sees own reqs only
    if user.role == "sales":
        base = base.filter(Requisition.created_by == user.id)

    # Search filter
    if q.strip():
        safe_q = escape_like(q.strip())
        base = base.filter(
            or_(
                Requisition.name.ilike(f"%{safe_q}%"),
                Requisition.customer_name.ilike(f"%{safe_q}%"),
            )
        )

    # Status filter
    if status == "archived":
        base = base.filter(Requisition.status.in_(["archived", "won", "lost", "closed"]))
    elif status:
        base = base.filter(Requisition.status == status)
    else:
        base = base.filter(Requisition.status.notin_(["archived", "won", "lost", "closed"]))

    # Sort
    allowed_sorts = {
        "created_at": Requisition.created_at,
        "name": Requisition.name,
        "status": Requisition.status,
        "customer_name": Requisition.customer_name,
    }
    sort_col = allowed_sorts.get(sort, Requisition.created_at)
    sort_expr = sort_col.asc() if dir == "asc" else sort_col.desc()

    total = base.count()
    total_pages = max(1, math.ceil(total / PER_PAGE))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = base.order_by(sort_expr).offset(offset).limit(PER_PAGE).all()

    requisitions = [
        type("Req", (), {
            "id": r.id,
            "name": r.name,
            "customer_name": r.customer_name or "",
            "status": r.status,
            "requirement_count": cnt or 0,
            "created_at": r.created_at,
        })
        for r, cnt in rows
    ]
    return requisitions, page, total_pages


@router.get("/views/requisitions")
async def requisitions_page(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full requisitions list page."""
    logger.debug("Requisitions page view by user={}", user.email if user else "unknown")
    requisitions, page, total_pages = _query_requisitions(db, user, q, status, sort, dir, page)
    return templates.TemplateResponse(
        "partials/requisitions/list.html",
        {
            "request": request,
            "user": user,
            "requisitions": requisitions,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "status": status,
            "sort": sort,
            "dir": dir,
            "url": "/views/requisitions/rows",
            "target_id": "req-table-body",
            "message": "No requisitions found.",
            "action_url": "/views/requisitions/create-form",
            "action_label": "Create one",
        },
    )


@router.get("/views/requisitions/rows")
async def requisitions_rows(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "created_at",
    dir: str = "desc",
    page: int = Query(1, ge=1),
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return requisition table rows partial (HTMX swap target)."""
    logger.debug("Requisitions rows partial q='{}' status='{}' sort={} dir={}", q, status, sort, dir)
    requisitions, page, total_pages = _query_requisitions(db, user, q, status, sort, dir, page)

    # Build rows HTML by rendering each row partial
    from fastapi.responses import HTMLResponse

    row_html_parts = []
    for req in requisitions:
        rendered = templates.get_template("partials/requisitions/req_row.html").render(req=req)
        row_html_parts.append(rendered)

    if not row_html_parts:
        empty = templates.get_template("partials/shared/empty_state.html").render(
            message="No requisitions found.",
            action_url="/views/requisitions/create-form",
            action_label="Create one",
        )
        row_html_parts.append(f'<tr><td colspan="5">{empty}</td></tr>')

    # Append pagination if needed
    if total_pages > 1:
        pagination = templates.get_template("partials/shared/pagination.html").render(
            page=page, total_pages=total_pages, url="/views/requisitions/rows", target_id="req-table-body",
        )
        row_html_parts.append(pagination)

    return HTMLResponse("\n".join(row_html_parts))


@router.get("/views/requisitions/create-form")
async def requisitions_create_form(request: Request, user=Depends(require_user)):
    """Return the create-requisition modal form partial."""
    logger.debug("Requisition create form requested by user={}", user.email if user else "unknown")
    return templates.TemplateResponse(
        "partials/requisitions/create_modal.html",
        {"request": request},
    )
