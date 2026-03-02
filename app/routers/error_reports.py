"""Error Reports API — compatibility shim that delegates to the unified trouble ticket system.

All error report operations now create/read TroubleTickets with source='report_button'.
This shim preserves backward compatibility for any clients still using /api/error-reports.

Called by: main.py (app.include_router)
Depends on: services/trouble_ticket_service.py, services/ai_trouble_prompt.py
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import User
from ..models.trouble_ticket import TroubleTicket
from ..services import trouble_ticket_service as svc
from ..services.ai_trouble_prompt import generate_trouble_prompt

router = APIRouter(tags=["error-reports"])

MAX_SCREENSHOT_SIZE = 2 * 1024 * 1024  # 2 MB base64


# ── Schemas ──────────────────────────────────────────────────────────


class ErrorReportCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    screenshot_b64: Optional[str] = None
    current_url: Optional[str] = None
    current_view: Optional[str] = None
    browser_info: Optional[str] = None
    screen_size: Optional[str] = None
    console_errors: Optional[str] = None
    page_state: Optional[str] = None


class StatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(open|in_progress|resolved|closed)$")
    admin_notes: Optional[str] = None


# ── Status mapping ───────────────────────────────────────────────────

# ErrorReport statuses → TroubleTicket statuses
_ER_TO_TT_STATUS = {
    "open": "submitted",
    "in_progress": "diagnosed",
    "resolved": "resolved",
    "closed": "rejected",
}

_TT_TO_ER_STATUS = {v: k for k, v in _ER_TO_TT_STATUS.items()}
_TT_TO_ER_STATUS["submitted"] = "open"
_TT_TO_ER_STATUS["diagnosed"] = "in_progress"
_TT_TO_ER_STATUS["rejected"] = "closed"


def _tt_to_er_status(tt_status: str) -> str:
    """Map TroubleTicket status back to ErrorReport status for compat."""
    return _TT_TO_ER_STATUS.get(tt_status, tt_status)


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/api/error-reports")
async def create_error_report(
    body: ErrorReportCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a trouble report (any authenticated user). Creates a TroubleTicket."""
    if body.screenshot_b64 and len(body.screenshot_b64) > MAX_SCREENSHOT_SIZE:
        raise HTTPException(400, "Screenshot too large (max 2 MB)")

    message = body.message.strip()
    initial_title = body.title.strip() if body.title else message[:200]

    ticket = svc.create_ticket(
        db=db,
        user_id=user.id,
        title=initial_title,
        description=message,
        current_page=body.current_url,
        source="report_button",
        screenshot_b64=body.screenshot_b64 or None,
        browser_info=body.browser_info,
        screen_size=body.screen_size,
        console_errors=body.console_errors,
        page_state=body.page_state,
        current_view=body.current_view,
    )
    logger.info("Trouble report #{} created by {} (via compat shim)", ticket.id, user.email)

    # Generate AI prompt — failure doesn't break submission
    try:
        result = await generate_trouble_prompt(
            user_message=message,
            current_url=body.current_url,
            current_view=body.current_view,
            browser_info=body.browser_info,
            screen_size=body.screen_size,
            console_errors=body.console_errors,
            page_state=body.page_state,
            has_screenshot=bool(body.screenshot_b64),
            reporter_name=user.name or user.email,
        )
        if result:
            svc.update_ticket(
                db=db, ticket_id=ticket.id,
                title=result["title"],
                ai_prompt=result["prompt"],
            )
            logger.info("AI prompt generated for ticket #{}", ticket.id)
    except Exception as e:
        logger.warning("AI prompt generation failed for ticket #{}: {}", ticket.id, e)

    # Fire-and-forget: auto-diagnose in background
    import asyncio
    asyncio.create_task(svc.auto_process_ticket(ticket.id))

    return {"id": ticket.id, "status": "created"}


@router.get("/api/error-reports")
def list_error_reports(
    status: Optional[str] = Query(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List trouble reports (admin). Filters TroubleTickets where source='report_button'."""
    q = db.query(TroubleTicket).filter(TroubleTicket.source == "report_button")
    if status:
        tt_status = _ER_TO_TT_STATUS.get(status, status)
        q = q.filter(TroubleTicket.status == tt_status)
    q = q.order_by(desc(TroubleTicket.created_at))
    tickets = q.limit(500).all()
    return [
        {
            "id": t.id,
            "title": t.title,
            "status": _tt_to_er_status(t.status),
            "reporter_email": t.submitter.email if t.submitter else None,
            "reporter_name": t.submitter.name if t.submitter else None,
            "has_screenshot": bool(t.screenshot_b64),
            "has_ai_prompt": bool(t.ai_prompt),
            "current_url": t.current_page,
            "current_view": t.current_view,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
        }
        for t in tickets
    ]


@router.get("/api/error-reports/export/xlsx")
def export_error_reports_xlsx(
    status: Optional[str] = Query(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Export bug reports to Excel (admin)."""
    import io

    from openpyxl import Workbook

    q = db.query(TroubleTicket).filter(TroubleTicket.source == "report_button")
    if status:
        tt_status = _ER_TO_TT_STATUS.get(status, status)
        q = q.filter(TroubleTicket.status == tt_status)
    q = q.order_by(desc(TroubleTicket.created_at))
    tickets = q.limit(2000).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Trouble Tickets"
    headers = [
        "ID", "Title", "Description", "Status", "Reporter",
        "URL", "View", "Browser", "Screen", "Console Errors",
        "Admin Notes", "Created", "Resolved",
    ]
    ws.append(headers)
    for t in tickets:
        ws.append([
            t.id,
            t.title,
            t.description or "",
            _tt_to_er_status(t.status),
            t.submitter.email if t.submitter else "",
            t.current_page or "",
            t.current_view or "",
            t.browser_info or "",
            t.screen_size or "",
            t.console_errors or "",
            t.admin_notes or "",
            t.created_at.isoformat() if t.created_at else "",
            t.resolved_at.isoformat() if t.resolved_at else "",
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trouble_tickets.xlsx"},
    )


@router.get("/api/error-reports/{report_id}")
def get_error_report(
    report_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get full trouble report detail including screenshot (admin)."""
    ticket = db.get(TroubleTicket, report_id)
    if not ticket:
        raise HTTPException(404, "Report not found")
    resolved_by = db.get(User, ticket.resolved_by_id) if ticket.resolved_by_id else None
    return {
        "id": ticket.id,
        "title": ticket.title,
        "description": ticket.description,
        "screenshot_b64": ticket.screenshot_b64,
        "current_url": ticket.current_page,
        "current_view": ticket.current_view,
        "browser_info": ticket.browser_info,
        "screen_size": ticket.screen_size,
        "console_errors": ticket.console_errors,
        "page_state": ticket.page_state,
        "status": _tt_to_er_status(ticket.status),
        "admin_notes": ticket.admin_notes,
        "ai_prompt": ticket.ai_prompt,
        "reporter_email": ticket.submitter.email if ticket.submitter else None,
        "reporter_name": ticket.submitter.name if ticket.submitter else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "resolved_by_email": resolved_by.email if resolved_by else None,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
    }


@router.post("/api/error-reports/{report_id}/regenerate-prompt")
async def regenerate_prompt(
    report_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Re-generate the AI prompt for an existing trouble report (admin)."""
    ticket = db.get(TroubleTicket, report_id)
    if not ticket:
        raise HTTPException(404, "Report not found")

    result = await generate_trouble_prompt(
        user_message=ticket.description or ticket.title,
        current_url=ticket.current_page,
        current_view=ticket.current_view,
        browser_info=ticket.browser_info,
        screen_size=ticket.screen_size,
        console_errors=ticket.console_errors,
        page_state=ticket.page_state,
        has_screenshot=bool(ticket.screenshot_b64),
        reporter_name=ticket.submitter.name if ticket.submitter else None,
    )
    if not result:
        raise HTTPException(502, "AI prompt generation failed — try again later")

    svc.update_ticket(
        db=db, ticket_id=ticket.id,
        title=result["title"],
        ai_prompt=result["prompt"],
    )
    logger.info("AI prompt regenerated for ticket #{} by {}", report_id, user.email)
    return {"id": ticket.id, "ai_prompt": result["prompt"], "title": result["title"]}


@router.put("/api/error-reports/{report_id}/status")
def update_error_report_status(
    report_id: int,
    body: StatusUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update bug report status and admin notes (admin)."""
    tt_status = _ER_TO_TT_STATUS.get(body.status, body.status)
    ticket = svc.update_admin_status(
        db=db,
        ticket_id=report_id,
        status=tt_status,
        admin_notes=body.admin_notes,
        admin_user_id=user.id,
    )
    if not ticket:
        raise HTTPException(404, "Report not found")

    logger.info("Bug report #{} → {} by {}", report_id, body.status, user.email)
    return {"id": ticket.id, "status": _tt_to_er_status(ticket.status)}
