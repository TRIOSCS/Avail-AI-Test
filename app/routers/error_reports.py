"""Error Reports / Trouble Tickets API — simplified ticket CRUD + HTMX form.

Handles /api/error-reports, /api/trouble-tickets paths, and the floating
report button form/submit endpoints.

Called by: main.py (app.include_router), base.html (HTMX button)
Depends on: models/trouble_ticket.py
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..models.trouble_ticket import TroubleTicket

router = APIRouter(tags=["error-reports"])
templates = Jinja2Templates(directory="app/templates")


class ErrorReportCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    current_url: Optional[str] = None


class TicketUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(submitted|in_progress|resolved|wont_fix)$")
    resolution_notes: Optional[str] = Field(None, max_length=5000)


def _next_ticket_number(db: Session) -> str:
    last = db.query(func.max(TroubleTicket.id)).scalar() or 0
    return f"TT-{last + 1:04d}"


# ── HTMX form endpoints (floating button) ────────────────────────


@router.get("/api/trouble-tickets/form", response_class=HTMLResponse)
async def trouble_ticket_form(request: Request, user: User = Depends(require_user)):
    """Return the trouble report form partial for the modal."""
    return templates.TemplateResponse(
        "htmx/partials/shared/trouble_report_form.html",
        {"request": request},
    )


@router.post("/api/trouble-tickets/submit", response_class=HTMLResponse)
async def submit_trouble_ticket_form(
    request: Request,
    message: str = Form(...),
    current_url: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Handle form submission from the floating report button."""
    msg = message.strip()
    if not msg:
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Please describe the problem.</div>',
            status_code=422,
        )

    ticket = TroubleTicket(
        ticket_number=_next_ticket_number(db),
        submitted_by=user.id,
        title=msg[:120],
        description=msg,
        current_page=current_url or None,
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

    return HTMLResponse(
        '<div class="p-4 text-center">'
        '<div class="text-emerald-600 font-medium mb-2">Report submitted!</div>'
        f'<div class="text-sm text-gray-500 mb-3">Ticket {ticket.ticket_number}</div>'
        '<button type="button" @click="$dispatch(\'close-modal\')" '
        'class="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Close</button>'
        "</div>"
    )


# ── JSON API endpoints ────────────────────────────────────────────


@router.post("/api/error-reports")
@router.post("/api/trouble-tickets")
async def create_error_report(
    body: ErrorReportCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a trouble report (any authenticated user)."""
    ticket = TroubleTicket(
        ticket_number=_next_ticket_number(db),
        submitted_by=user.id,
        title=body.message[:120],
        description=body.message,
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
        "resolution_notes": ticket.resolution_notes,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
    }


@router.patch("/api/error-reports/{report_id}")
@router.patch("/api/trouble-tickets/{report_id}")
async def update_ticket(
    report_id: int,
    body: TicketUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a trouble ticket status or add resolution notes."""
    ticket = db.get(TroubleTicket, report_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if body.status:
        ticket.status = body.status
        if body.status == "resolved":
            ticket.resolved_at = datetime.now(timezone.utc)
            ticket.resolved_by_id = user.id
    if body.resolution_notes is not None:
        ticket.resolution_notes = body.resolution_notes

    ticket.updated_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("Ticket %s updated to %s by user %d", ticket.ticket_number, ticket.status, user.id)
    return {"id": ticket.id, "status": ticket.status}
