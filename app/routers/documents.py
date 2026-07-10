"""Documents API — PDF generation for requisitions and quotes."""

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_quote_for_user, get_req_for_user, require_user
from ..models import User
from ..rate_limit import limiter

router = APIRouter(tags=["documents"])


async def _render_pdf(generator: Callable[[int, Session], bytes], obj_id: int, db: Session, log_label: str) -> bytes:
    """Run a blocking PDF generator off the event loop, mapping failures to HTTP errors.

    ValueError → 404 (caller passed a known not-found message); anything else → 500.
    """
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, generator, obj_id, db)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.error("PDF generation failed for {} {}: {}", log_label, obj_id, e)
        raise HTTPException(500, "PDF generation failed") from e


@router.get("/api/requisitions/{requisition_id}/pdf")
@limiter.limit("10/minute")
async def download_rfq_pdf(
    requisition_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate and download a PDF summary of a requisition."""
    from ..services.document_service import generate_rfq_summary_pdf

    req = get_req_for_user(db, user, requisition_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    pdf_bytes = await _render_pdf(generate_rfq_summary_pdf, req.id, db, "requisition")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=rfq-{requisition_id}.pdf"},
    )


@router.get("/api/quotes/{quote_id}/pdf")
@limiter.limit("10/minute")
async def download_quote_pdf(
    quote_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Generate and download a PDF report for a quote."""
    from ..services.document_service import generate_quote_report_pdf

    quote = get_quote_for_user(db, user, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")

    pdf_bytes = await _render_pdf(generate_quote_report_pdf, quote.id, db, "quote")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=quote-{quote_id}.pdf"},
    )
