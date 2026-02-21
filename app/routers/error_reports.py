"""Error Reports API — trouble ticket submission and management."""

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import User
from ..models.error_report import ErrorReport
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


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/api/error-reports")
async def create_error_report(
    body: ErrorReportCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a trouble report (any authenticated user)."""
    if body.screenshot_b64 and len(body.screenshot_b64) > MAX_SCREENSHOT_SIZE:
        raise HTTPException(400, "Screenshot too large (max 2 MB)")

    # Use message as description; truncate for initial title
    message = body.message.strip()
    initial_title = body.title.strip() if body.title else message[:255]

    report = ErrorReport(
        user_id=user.id,
        title=initial_title,
        description=message,
        screenshot_b64=body.screenshot_b64 or None,
        current_url=body.current_url,
        current_view=body.current_view,
        browser_info=body.browser_info,
        screen_size=body.screen_size,
        console_errors=body.console_errors,
        page_state=body.page_state,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    logger.info("Trouble report #{} created by {}", report.id, user.email)

    # Generate AI prompt in background — failure doesn't break submission
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
            report.title = result["title"]
            report.ai_prompt = result["prompt"]
            db.commit()
            logger.info("AI prompt generated for report #{}", report.id)
    except Exception as e:
        logger.warning("AI prompt generation failed for report #{}: {}", report.id, e)

    return {"id": report.id, "status": "created"}


@router.get("/api/error-reports")
def list_error_reports(
    status: Optional[str] = Query(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List trouble reports (admin). Omits screenshot_b64 for performance."""
    q = db.query(ErrorReport)
    if status:
        q = q.filter(ErrorReport.status == status)
    q = q.order_by(desc(ErrorReport.created_at))
    reports = q.limit(500).all()
    return [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status,
            "reporter_email": r.reporter.email if r.reporter else None,
            "reporter_name": r.reporter.name if r.reporter else None,
            "has_screenshot": bool(r.screenshot_b64),
            "has_ai_prompt": bool(r.ai_prompt),
            "current_url": r.current_url,
            "current_view": r.current_view,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        }
        for r in reports
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

    q = db.query(ErrorReport)
    if status:
        q = q.filter(ErrorReport.status == status)
    q = q.order_by(desc(ErrorReport.created_at))
    reports = q.limit(2000).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Trouble Tickets"
    headers = [
        "ID", "Title", "Description", "Status", "Reporter",
        "URL", "View", "Browser", "Screen", "Console Errors",
        "Admin Notes", "Created", "Resolved",
    ]
    ws.append(headers)
    for r in reports:
        ws.append([
            r.id,
            r.title,
            r.description or "",
            r.status,
            r.reporter.email if r.reporter else "",
            r.current_url or "",
            r.current_view or "",
            r.browser_info or "",
            r.screen_size or "",
            r.console_errors or "",
            r.admin_notes or "",
            r.created_at.isoformat() if r.created_at else "",
            r.resolved_at.isoformat() if r.resolved_at else "",
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
    report = db.get(ErrorReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    return {
        "id": report.id,
        "title": report.title,
        "description": report.description,
        "screenshot_b64": report.screenshot_b64,
        "current_url": report.current_url,
        "current_view": report.current_view,
        "browser_info": report.browser_info,
        "screen_size": report.screen_size,
        "console_errors": report.console_errors,
        "page_state": report.page_state,
        "status": report.status,
        "admin_notes": report.admin_notes,
        "ai_prompt": report.ai_prompt,
        "reporter_email": report.reporter.email if report.reporter else None,
        "reporter_name": report.reporter.name if report.reporter else None,
        "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
        "resolved_by_email": report.resolved_by.email if report.resolved_by else None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }


@router.post("/api/error-reports/{report_id}/regenerate-prompt")
async def regenerate_prompt(
    report_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Re-generate the AI prompt for an existing trouble report (admin)."""
    report = db.get(ErrorReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    result = await generate_trouble_prompt(
        user_message=report.description or report.title,
        current_url=report.current_url,
        current_view=report.current_view,
        browser_info=report.browser_info,
        screen_size=report.screen_size,
        console_errors=report.console_errors,
        page_state=report.page_state,
        has_screenshot=bool(report.screenshot_b64),
        reporter_name=report.reporter.name if report.reporter else None,
    )
    if not result:
        raise HTTPException(502, "AI prompt generation failed — try again later")

    report.title = result["title"]
    report.ai_prompt = result["prompt"]
    db.commit()
    logger.info("AI prompt regenerated for report #{} by {}", report_id, user.email)
    return {"id": report.id, "ai_prompt": report.ai_prompt, "title": report.title}


@router.put("/api/error-reports/{report_id}/status")
def update_error_report_status(
    report_id: int,
    body: StatusUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update bug report status and admin notes (admin)."""
    report = db.get(ErrorReport, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    report.status = body.status
    if body.admin_notes is not None:
        report.admin_notes = body.admin_notes

    if body.status in ("resolved", "closed"):
        report.resolved_at = datetime.now(timezone.utc)
        report.resolved_by_id = user.id
    elif body.status == "open":
        report.resolved_at = None
        report.resolved_by_id = None

    db.commit()
    logger.info("Bug report #{} → {} by {}", report_id, body.status, user.email)
    return {"id": report.id, "status": report.status}
