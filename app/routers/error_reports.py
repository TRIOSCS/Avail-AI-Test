"""Error Reports / Trouble Tickets API — simplified ticket CRUD.

Handles both /api/error-reports and /api/trouble-tickets paths.
Basic submit/list/view — no AI diagnosis or automation.

Called by: main.py (app.include_router)
Depends on: models/trouble_ticket.py
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..models.trouble_ticket import TroubleTicket

router = APIRouter(tags=["error-reports"])


class ErrorReportCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    current_url: Optional[str] = None
    screenshot: Optional[str] = None


def _next_ticket_number(db: Session) -> str:
    last = db.query(func.max(TroubleTicket.id)).scalar() or 0
    return f"TT-{last + 1:04d}"


@router.post("/api/error-reports")
@router.post("/api/trouble-tickets")
async def create_error_report(
    body: ErrorReportCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a trouble report (any authenticated user)."""
    title = body.message[:120]
    message = body.message

    ticket = TroubleTicket(
        ticket_number=_next_ticket_number(db),
        submitted_by=user.id,
        title=title,
        description=message,
        current_page=body.current_url,
        source="report_button",
        status="submitted",
        risk_tier="low",
        category="other",
        created_at=datetime.now(timezone.utc),
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    logger.info("Trouble ticket %s created by user %d", ticket.ticket_number, user.id)
    return {"id": ticket.id, "status": "created"}


@router.get("/api/error-reports")
@router.get("/api/trouble-tickets")
async def list_error_reports(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List trouble reports."""
    q = db.query(TroubleTicket).filter(TroubleTicket.source == "report_button")
    if status:
        q = q.filter(TroubleTicket.status == status)
    q = q.order_by(desc(TroubleTicket.created_at))
    total = q.count()
    items = q.offset(offset).limit(limit).all()

    return {
        "items": [
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "title": t.title,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in items
        ],
        "total": total,
    }


@router.get("/api/error-reports/{report_id}")
@router.get("/api/trouble-tickets/{report_id}")
async def get_error_report(
    report_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get a single trouble report."""
    ticket = db.get(TroubleTicket, report_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "title": ticket.title,
        "description": ticket.description,
        "status": ticket.status,
        "risk_tier": ticket.risk_tier,
        "category": ticket.category,
        "current_page": ticket.current_page,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
    }
