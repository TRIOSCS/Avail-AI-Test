"""Error Reports / Trouble Tickets API — simplified ticket CRUD + HTMX form.

Handles /api/error-reports, /api/trouble-tickets paths, and the floating
report button form/submit endpoints.

Called by: main.py (app.include_router), htmx/base.html (HTMX button)
Depends on: models/trouble_ticket.py
"""

import base64
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from markupsafe import escape
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User
from ..models.trouble_ticket import TroubleTicket

router = APIRouter(tags=["error-reports"])
templates = Jinja2Templates(directory="app/templates")

MAX_MESSAGE_LEN = 5000
UPLOAD_DIR = "/app/uploads/tickets"
MAX_SCREENSHOT_B64_SIZE = 2 * 1024 * 1024  # 2MB base64


class ErrorReportCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    current_url: Optional[str] = Field(None, max_length=500)
    screenshot: Optional[str] = Field(None, max_length=MAX_SCREENSHOT_B64_SIZE)


class TicketUpdate(BaseModel):
    status: Optional[str] = Field(None, pattern="^(submitted|in_progress|resolved|wont_fix)$")
    resolution_notes: Optional[str] = Field(None, max_length=5000)


def _save_screenshot(ticket_id: int, b64_data: str) -> str | None:
    """Decode base64 PNG and save to disk.

    Returns path or None on failure.
    """
    if not b64_data or len(b64_data) > MAX_SCREENSHOT_B64_SIZE:
        return None
    try:
        if "," in b64_data[:100]:
            b64_data = b64_data.split(",", 1)[1]
        png_bytes = base64.b64decode(b64_data)
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, f"TT-{ticket_id}.png")
        with open(path, "wb") as f:
            f.write(png_bytes)
        return path
    except Exception:
        logger.warning("Failed to save screenshot for ticket %d", ticket_id)
        return None


def _create_ticket(
    db: Session,
    user_id: int,
    message: str,
    current_url: Optional[str] = None,
    user_agent: Optional[str] = None,
    browser_info: Optional[str] = None,
    console_errors: Optional[str] = None,
    network_errors: Optional[str] = None,
) -> TroubleTicket:
    """Create and persist a trouble ticket.

    Commits the session.
    """
    ticket = TroubleTicket(
        ticket_number="PENDING",
        submitted_by=user_id,
        title=message[:120],
        description=message,
        current_page=current_url or None,
        user_agent=user_agent or None,
        browser_info=browser_info or None,
        console_errors=console_errors or None,
        network_errors=network_errors if network_errors else None,
        source="report_button",
        status="submitted",
        risk_tier="low",
        category="other",
        created_at=datetime.now(timezone.utc),
    )
    db.add(ticket)
    db.flush()
    ticket.ticket_number = f"TT-{ticket.id:04d}"
    db.commit()
    logger.info("Trouble ticket %s created by user %d", ticket.ticket_number, user_id)
    return ticket


async def _generate_ai_summary(ticket_id: int):
    """Generate a one-sentence AI summary for a trouble ticket.

    Runs as BackgroundTask.
    """
    from ..database import SessionLocal
    from ..utils.claude_client import claude_text

    db = SessionLocal()
    try:
        ticket = db.get(TroubleTicket, ticket_id)
        if not ticket or ticket.ai_summary:
            return

        prompt = (
            "Summarize this trouble report in one sentence. "
            f"Description: {ticket.description[:500]}. "
            f"Page: {ticket.current_page or 'unknown'}. "
            f"JS errors: {(ticket.console_errors or 'none')[:300]}. "
            f"Network errors: {str(ticket.network_errors or 'none')[:300]}"
        )

        summary = await claude_text(
            prompt=prompt,
            system="You are a bug report summarizer. Return exactly one sentence.",
            model_tier="fast",
        )

        if summary:
            ticket.ai_summary = summary.strip()[:500]
            ticket.updated_at = datetime.now(timezone.utc)
            db.commit()
            logger.debug("AI summary generated for ticket %s", ticket.ticket_number)
    except Exception:
        logger.warning("AI summary failed for ticket %d", ticket_id)
        db.rollback()
    finally:
        db.close()


# ── HTMX form endpoints (floating button) ────────────────────────


@router.get("/api/trouble-tickets/form", response_class=HTMLResponse)
async def trouble_ticket_form(request: Request, user: User = Depends(require_user)):
    """Return the trouble report form partial for the modal."""
    return templates.TemplateResponse(
        "htmx/partials/shared/trouble_report_form.html",
        {"request": request},
    )


@router.post("/api/trouble-tickets/submit", response_class=HTMLResponse)
async def submit_trouble_ticket(
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Handle submission from trouble ticket form — accepts JSON or form data."""
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            return HTMLResponse(
                '<div class="p-4 text-rose-600 text-sm">Invalid request.</div>',
                status_code=422,
            )
        description = (body.get("description") or "").strip()
        page_url = body.get("page_url")
        screenshot_b64 = body.get("screenshot")
        ua = body.get("user_agent")
        viewport = body.get("viewport")
        error_log = body.get("error_log")
        network_log_raw = body.get("network_log")
    else:
        # Legacy form-encoded fallback
        form = await request.form()
        description = (form.get("message") or "").strip()
        page_url = form.get("current_url")
        screenshot_b64 = None
        ua = None
        viewport = None
        error_log = None
        network_log_raw = None

    if not description:
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Please describe the problem.</div>',
            status_code=422,
        )
    if len(description) > MAX_MESSAGE_LEN:
        return HTMLResponse(
            f'<div class="p-4 text-rose-600 text-sm">Message too long (max {MAX_MESSAGE_LEN} characters).</div>',
            status_code=422,
        )

    import json as _json

    browser_info = None
    if ua or viewport:
        browser_info = _json.dumps({"user_agent": ua, "viewport": viewport})

    network_errors = None
    if network_log_raw:
        try:
            network_errors = _json.loads(network_log_raw) if isinstance(network_log_raw, str) else network_log_raw
        except (ValueError, TypeError):
            network_errors = None

    try:
        ticket = _create_ticket(
            db,
            user.id,
            description,
            current_url=page_url,
            user_agent=ua,
            browser_info=browser_info,
            console_errors=error_log,
            network_errors=network_errors,
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to create trouble ticket for user %d", user.id)
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Something went wrong saving your report. Please try again.</div>',
            status_code=500,
        )

    if screenshot_b64:
        path = _save_screenshot(ticket.id, screenshot_b64)
        if path:
            ticket.screenshot_path = path
            db.commit()

    background_tasks.add_task(_generate_ai_summary, ticket.id)

    return HTMLResponse(
        '<div class="p-4 text-center">'
        '<div class="text-emerald-600 font-medium mb-2">Report submitted!</div>'
        f'<div class="text-sm text-gray-500 mb-3">Ticket {escape(ticket.ticket_number)}</div>'
        '<button type="button" @click="$dispatch(\'close-modal\')" '
        'class="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Close</button>'
        "</div>"
    )


# ── Screenshot serving ────────────────────────────────────────────


@router.get("/api/trouble-tickets/{ticket_id}/screenshot")
async def get_ticket_screenshot(
    ticket_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Serve screenshot PNG from disk; fall back to legacy screenshot_b64."""
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.screenshot_path and os.path.isfile(ticket.screenshot_path):
        return FileResponse(ticket.screenshot_path, media_type="image/png")
    if ticket.screenshot_b64:
        png_bytes = base64.b64decode(ticket.screenshot_b64)
        return Response(content=png_bytes, media_type="image/png")
    raise HTTPException(404, "No screenshot available")


# ── JSON API endpoints ────────────────────────────────────────────


@router.post("/api/error-reports")
@router.post("/api/trouble-tickets")
async def create_error_report(
    body: ErrorReportCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a trouble report (any authenticated user)."""
    ticket = _create_ticket(db, user.id, body.message, body.current_url)
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
    """List trouble reports (source='report_button' only)."""
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


@router.post("/api/trouble-tickets/analyze", response_class=HTMLResponse)
async def analyze_tickets(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Batch AI analysis — group open tickets by root cause."""
    from ..models.root_cause_group import RootCauseGroup
    from ..utils.claude_client import claude_structured

    tickets = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status.in_(["submitted", "in_progress"]))
        .filter(TroubleTicket.source == "report_button")
        .order_by(desc(TroubleTicket.created_at))
        .limit(50)
        .all()
    )

    if not tickets:
        return HTMLResponse('<div class="text-center py-4 text-sm text-gray-500">No open tickets to analyze.</div>')

    ticket_data = []
    for t in tickets:
        ticket_data.append(
            {
                "id": t.id,
                "description": (t.description or "")[:300],
                "page": t.current_page or "",
                "js_errors": (t.console_errors or "")[:200],
                "network": str(t.network_errors or "")[:200],
            }
        )

    tool_schema = {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "ticket_ids": {"type": "array", "items": {"type": "integer"}},
                    },
                    "required": ["title", "ticket_ids"],
                },
            }
        },
        "required": ["groups"],
    }

    import json as _json

    result = await claude_structured(
        prompt=(
            "Group these trouble tickets by root cause. For each group, provide a short title "
            "and a suggested fix. Return JSON with a 'groups' array.\n\n"
            f"Tickets:\n{_json.dumps(ticket_data, indent=2)}"
        ),
        system="You are a bug triage assistant. Group related bug reports by their likely root cause.",
        output_schema=tool_schema,
        model_tier="fast",
    )

    if not result or "groups" not in result:
        return HTMLResponse(
            '<div class="text-center py-4 text-sm text-amber-600">AI analysis returned no results. Try again later.</div>'
        )

    ticket_map = {t.id: t for t in tickets}
    for group_data in result["groups"]:
        title = (group_data.get("title") or "Unknown")[:200]
        fix = group_data.get("suggested_fix")
        ticket_ids = group_data.get("ticket_ids", [])

        group = db.query(RootCauseGroup).filter(RootCauseGroup.title == title).first()
        if not group:
            group = RootCauseGroup(title=title, suggested_fix=fix)
            db.add(group)
            db.flush()
        elif fix and not group.suggested_fix:
            group.suggested_fix = fix

        for tid in ticket_ids:
            if tid in ticket_map:
                ticket_map[tid].root_cause_group_id = group.id

    db.commit()
    logger.info("AI analysis grouped %d tickets into %d groups", len(tickets), len(result["groups"]))

    # Return empty response with HX-Trigger to reload list
    resp = HTMLResponse("")
    resp.headers["HX-Trigger"] = "ticketsUpdated"
    return resp


@router.patch("/api/error-reports/{report_id}")
@router.patch("/api/trouble-tickets/{report_id}")
async def update_ticket(
    report_id: int,
    body: TicketUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Update a trouble ticket status or resolution notes."""
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
