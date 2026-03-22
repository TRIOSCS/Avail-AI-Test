"""routers/quote_builder.py — Quote Builder modal, save, and export endpoints.

Serves the full-screen two-panel quote builder modal, handles save (with
revision support), and streams Excel/PDF exports.

Called by: Parts tab "Build Quote" button (HTMX), Alpine.js fetch (save)
Depends on: app.services.quote_builder_service, app.schemas.quote_builder
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..schemas.quote_builder import QuoteBuilderSaveRequest

router = APIRouter(tags=["quote-builder"])


@router.get("/v2/partials/quote-builder/{req_id}")
async def quote_builder_modal(
    req_id: int,
    request: Request,
    requirement_ids: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Open the quote builder modal shell (lightweight HTML, no line data).

    Line data is fetched separately via the /data endpoint by the Alpine component on
    init, keeping the initial HTML payload small even for requisitions with 200+
    requirements.
    """
    from ..dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    customer_name = ""
    has_customer_site = bool(req.customer_site_id)
    if has_customer_site:
        from ..models import CustomerSite

        site = db.get(CustomerSite, req.customer_site_id)
        if site and site.company:
            customer_name = site.company.name or ""

    from ..main import templates

    return templates.TemplateResponse(
        "htmx/partials/quote_builder/modal.html",
        {
            "request": request,
            "req": req,
            "customer_name": customer_name,
            "has_customer_site": has_customer_site,
            "requirement_ids": requirement_ids or "",
        },
    )


@router.get("/v2/partials/quote-builder/{req_id}/data")
async def quote_builder_data(
    req_id: int,
    requirement_ids: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return line data as JSON for the quote builder Alpine component.

    Called by the Alpine component on init via fetch(). Keeps the modal HTML small and
    allows the browser to parse the JSON efficiently as a separate network request
    rather than inline in an HTML attribute.
    """
    from ..dependencies import get_req_for_user
    from ..services.quote_builder_service import apply_smart_defaults, get_builder_data

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    req_ids = None
    if requirement_ids:
        try:
            req_ids = [int(x.strip()) for x in requirement_ids.split(",") if x.strip()]
        except ValueError:
            req_ids = None

    lines = get_builder_data(req_id, db, requirement_ids=req_ids)
    apply_smart_defaults(lines)

    return {"lines": lines}


@router.post("/v2/partials/quote-builder/{req_id}/save")
async def quote_builder_save(
    req_id: int,
    payload: QuoteBuilderSaveRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the quote from the builder modal."""
    from ..dependencies import get_req_for_user
    from ..services.quote_builder_service import save_quote_from_builder

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    if not req.customer_site_id:
        raise HTTPException(400, "Requisition must be linked to a customer site before quoting")

    try:
        result = save_quote_from_builder(db, req_id=req_id, payload=payload, user=user)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("Quote builder save failed for req {}: {}", req_id, e)
        raise HTTPException(500, "Failed to save quote. Please try again.")

    return result


@router.get("/v2/partials/quote-builder/{req_id}/export/excel")
async def quote_builder_export_excel(
    req_id: int,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream an Excel export of a saved quote."""
    from ..models import Quote

    quote = db.get(Quote, quote_id)
    if not quote or quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")

    from ..services.quote_builder_service import build_excel_export

    customer_name = ""
    if quote.customer_site and quote.customer_site.company:
        customer_name = quote.customer_site.company.name or ""

    try:
        xlsx_bytes = build_excel_export(
            line_items=quote.line_items or [],
            quote_number=quote.quote_number,
            customer_name=customer_name,
        )
    except Exception as e:
        logger.error("Excel export failed for quote {}: {}", quote_id, e)
        raise HTTPException(500, "Excel export failed. Please try again.")

    filename = f"{quote.quote_number}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/v2/partials/quote-builder/{req_id}/export/pdf")
async def quote_builder_export_pdf(
    req_id: int,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream a PDF export of a saved quote (reuses existing PDF generator)."""
    import asyncio

    from fastapi.responses import Response

    from ..models import Quote

    quote = db.get(Quote, quote_id)
    if not quote or quote.requisition_id != req_id:
        raise HTTPException(404, "Quote not found")

    from ..services.document_service import generate_quote_report_pdf

    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(None, generate_quote_report_pdf, quote.id, db)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("PDF generation failed for quote {}: {}", quote_id, e)
        raise HTTPException(500, "PDF generation failed")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{quote.quote_number}.pdf"'},
    )
