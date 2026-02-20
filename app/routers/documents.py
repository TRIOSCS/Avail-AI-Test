"""Documents API — PDF generation for requisitions and quotes."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..rate_limit import limiter

router = APIRouter(tags=["documents"])


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

    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(
            None, generate_rfq_summary_pdf, requisition_id, db
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("PDF generation failed for requisition %d: %s", requisition_id, e)
        raise HTTPException(500, "PDF generation failed")

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

    try:
        loop = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(
            None, generate_quote_report_pdf, quote_id, db
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error("PDF generation failed for quote %d: %s", quote_id, e)
        raise HTTPException(500, "PDF generation failed")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=quote-{quote_id}.pdf"},
    )
