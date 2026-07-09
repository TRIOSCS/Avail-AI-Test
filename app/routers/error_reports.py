"""Error Reports / Trouble Tickets API — simplified ticket CRUD + HTMX form.

Handles /api/error-reports, /api/trouble-tickets paths, and the floating
report button form/submit endpoints.

Called by: main.py (app.include_router), htmx/base.html (HTMX button)
Depends on: models/trouble_ticket.py
"""

import asyncio
import base64
import json
import os
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from markupsafe import escape
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..constants import TicketSource, TicketStatus, TicketType
from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import User
from ..models.trouble_ticket import TroubleTicket
from ..template_env import template_response

router = APIRouter(tags=["error-reports"])

MAX_MESSAGE_LEN = 5000
UPLOAD_DIR = "/app/uploads/tickets"
MAX_SCREENSHOT_B64_SIZE = 2 * 1024 * 1024  # 2MB base64

_upload_dir_ready = False


class ErrorReportCreate(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    current_url: str | None = Field(None, max_length=500)
    screenshot: str | None = Field(None, max_length=MAX_SCREENSHOT_B64_SIZE)


class TicketUpdate(BaseModel):
    status: TicketStatus | None = None
    resolution_notes: str | None = Field(None, max_length=5000)
    admin_notes: str | None = Field(None, max_length=5000)


class DiagnoseBulkBody(BaseModel):
    ticket_ids: list[int] = Field(..., min_length=1, max_length=50)


class BulkStatusBody(BaseModel):
    ticket_ids: list[int] = Field(..., min_length=1, max_length=200)
    status: TicketStatus


def _ensure_upload_dir() -> None:
    global _upload_dir_ready
    if not _upload_dir_ready:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        _upload_dir_ready = True


def _save_screenshot(ticket_id: int, b64_data: str) -> str | None:
    """Decode base64 PNG and save to disk.

    Returns the saved path, or None for bad input (empty/oversized/undecodable base64).
    Storage faults — a non-writable dir (PermissionError) or a full disk (OSError) — are
    re-raised so the caller can surface a clear 500 instead of silently dropping the
    screenshot (TT-0002).
    """
    if not b64_data or len(b64_data) > MAX_SCREENSHOT_B64_SIZE:
        return None
    try:
        if "," in b64_data[:100]:
            b64_data = b64_data.split(",", 1)[1]
        png_bytes = base64.b64decode(b64_data)
    except Exception as e:
        logger.warning("Undecodable screenshot for ticket {}: {}", ticket_id, e)
        return None
    _ensure_upload_dir()
    path = os.path.join(UPLOAD_DIR, f"TT-{ticket_id}.png")
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def _coerce_ticket_type(value: str | None) -> TicketType:
    """Map an inbound ticket_type string to a TicketType, defaulting to BUG.

    Anything that is not exactly 'feature' (missing, unknown, or the legacy Report-a-
    Problem path that sends nothing) reads as a bug, so the existing bug-report flow is
    unchanged.
    """
    return TicketType.FEATURE if value == TicketType.FEATURE else TicketType.BUG


def _create_ticket(
    db: Session,
    user_id: int,
    message: str,
    current_url: str | None = None,
    context: dict | None = None,
    ticket_type: TicketType = TicketType.BUG,
) -> TroubleTicket:
    """Create and persist a trouble ticket.

    Args:
        context: optional dict with keys user_agent, browser_info,
                 console_errors, network_errors, auto_captured_context, current_view.
        ticket_type: BUG (default, unchanged Report-a-Problem path) or FEATURE.
    """
    ctx = context or {}
    ticket = TroubleTicket(
        ticket_number="PENDING",
        submitted_by=user_id,
        title=message[:120],
        description=message,
        ticket_type=ticket_type,
        current_page=current_url or None,
        user_agent=ctx.get("user_agent") or None,
        browser_info=ctx.get("browser_info") or None,
        console_errors=ctx.get("console_errors") or None,
        network_errors=ctx.get("network_errors") or None,
        auto_captured_context=ctx.get("auto_captured_context") or None,
        current_view=ctx.get("current_view") or None,
        source=TicketSource.REPORT_BUTTON,
        status=TicketStatus.SUBMITTED,
        risk_tier="low",
        category="other",
        created_at=datetime.now(UTC),
    )
    db.add(ticket)
    db.flush()
    ticket.ticket_number = f"TT-{ticket.id:04d}"
    db.commit()
    logger.info("Trouble ticket {} created by user {}", ticket.ticket_number, user_id)
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
            ticket.updated_at = datetime.now(UTC)
            db.commit()
            logger.debug("AI summary generated for ticket {}", ticket.ticket_number)
    except Exception:
        logger.warning("AI summary failed for ticket {}", ticket_id)
        db.rollback()
    finally:
        db.close()


# ── HTMX form endpoints (floating button) ────────────────────────


@router.get("/api/trouble-tickets/form", response_class=HTMLResponse)
async def trouble_ticket_form(
    request: Request,
    type: str = Query("bug"),
    user: User = Depends(require_user),
):
    """Return the report form partial for the modal.

    ``type`` selects the kind: 'feature' renders the Request-a-Feature copy, anything
    else (default) renders the Report-a-Problem copy. Both share the same partial and
    the same client-side context capture.
    """
    return template_response(
        "htmx/partials/shared/trouble_report_form.html",
        {"request": request, "ticket_type": _coerce_ticket_type(type)},
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
    screenshot_b64 = ua = viewport = error_log = network_log_raw = auto_ctx_raw = None
    ticket_type_raw: str | None = None

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
        auto_ctx_raw = body.get("auto_captured_context")
        ticket_type_raw = body.get("ticket_type")
    else:
        # Legacy form-encoded fallback
        form = await request.form()
        description = (form.get("message") or "").strip()
        page_url = form.get("current_url")
        ticket_type_raw = form.get("ticket_type")

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

    browser_info = None
    if ua or viewport:
        browser_info = json.dumps({"user_agent": ua, "viewport": viewport})

    network_errors = None
    if network_log_raw:
        try:
            network_errors = json.loads(network_log_raw) if isinstance(network_log_raw, str) else network_log_raw
        except (ValueError, TypeError):
            network_errors = None

    auto_ctx = None
    if auto_ctx_raw:
        try:
            auto_ctx = json.loads(auto_ctx_raw) if isinstance(auto_ctx_raw, str) else auto_ctx_raw
        except (ValueError, TypeError):
            auto_ctx = None

    try:
        ticket = _create_ticket(
            db,
            user.id,
            description,
            current_url=page_url,
            context={
                "user_agent": ua,
                "browser_info": browser_info,
                "console_errors": error_log,
                "network_errors": network_errors,
                "auto_captured_context": auto_ctx,
                "current_view": (auto_ctx or {}).get("current_view") if isinstance(auto_ctx, dict) else None,
            },
            ticket_type=_coerce_ticket_type(ticket_type_raw),
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to create trouble ticket for user {}", user.id)
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Something went wrong saving your report. Please try again.</div>',
            status_code=500,
        )

    # Screenshot persistence is best-effort relative to the ticket (the ticket is
    # already committed above), but a non-writable storage dir is an infra fault
    # we must surface clearly rather than swallow (TT-0002).
    if screenshot_b64:
        try:
            # P2.6: _save_screenshot does a blocking disk write; dispatch it via
            # asyncio.to_thread so a slow/contended disk doesn't stall the event loop.
            path = await asyncio.to_thread(_save_screenshot, ticket.id, screenshot_b64)
            if path:
                ticket.screenshot_path = path
                db.commit()
        except (PermissionError, OSError) as exc:
            db.rollback()
            logger.exception("Screenshot storage not writable for ticket {}: {}", ticket.id, exc)
            return JSONResponse(
                status_code=500,
                content={"error": "Screenshot storage is not writable — contact an administrator."},
            )

    background_tasks.add_task(_generate_ai_summary, ticket.id)

    headline = "Feature request submitted!" if ticket.ticket_type == TicketType.FEATURE else "Report submitted!"
    return HTMLResponse(
        '<div class="p-4 text-center">'
        f'<div class="text-emerald-600 font-medium mb-2">{headline}</div>'
        f'<div class="text-sm text-gray-500 mb-3">Ticket {escape(ticket.ticket_number)}</div>'
        '<button type="button" @click="$dispatch(\'close-modal\')" '
        'class="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Close</button>'
        "</div>"
    )


# ── Screenshot serving ────────────────────────────────────────────


def _resolve_screenshot_file(path: str) -> str | None:
    """Resolve + traversal-check a stored ticket screenshot path.

    Sync (isfile/realpath hit the filesystem) — call via ``asyncio.to_thread``
    so the event loop never blocks. Returns the resolved path, or None when
    the file does not exist (caller falls back to legacy ``screenshot_b64``).
    Raises HTTPException(403) on path traversal.
    """
    if not os.path.isfile(path):
        return None
    real_path = os.path.realpath(path)
    if not real_path.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
        logger.warning(f"Path traversal blocked: {path} resolves outside UPLOAD_DIR")
        raise HTTPException(403, "Invalid screenshot path")
    return real_path


@router.get("/api/trouble-tickets/{ticket_id}/screenshot")
async def get_ticket_screenshot(
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Serve screenshot PNG from disk; fall back to legacy screenshot_b64.

    Admin-only: screenshots can contain sensitive on-screen data (customer
    names, pricing, contacts), so retrieval is gated to the ticket console.
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.screenshot_path:
        real_path = await asyncio.to_thread(_resolve_screenshot_file, ticket.screenshot_path)
        if real_path:
            return FileResponse(real_path, media_type="image/png")
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
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List trouble reports (source='report_button' only)."""
    q = db.query(TroubleTicket).filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
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
    user: User = Depends(require_admin),
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
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Batch AI analysis — group open tickets by root cause."""
    from ..models.root_cause_group import RootCauseGroup
    from ..utils.claude_client import claude_structured
    from ..utils.claude_errors import ClaudeError, ClaudeUnavailableError

    tickets = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status.in_([TicketStatus.SUBMITTED, TicketStatus.IN_PROGRESS]))
        .filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
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

    try:
        result = await claude_structured(
            prompt=(
                "Group these trouble tickets by root cause. For each group, provide a short title "
                "and a suggested fix. Return JSON with a 'groups' array.\n\n"
                f"Tickets:\n{json.dumps(ticket_data, indent=2)}"
            ),
            schema=tool_schema,
            system="You are a bug triage assistant. Group related bug reports by their likely root cause.",
            model_tier="fast",
        )
    except (ClaudeUnavailableError, ClaudeError) as e:
        logger.warning("AI root cause analysis failed: {}", e)
        result = None

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
    logger.info("AI analysis grouped {} tickets into {} groups", len(tickets), len(result["groups"]))

    # Render and return the freshly-grouped list partial so the innerHTML swap into
    # #ticket-list shows the new groupings. The "open" logical filter mirrors the
    # workspace's default Open view (submitted + in_progress).
    from .htmx.archive import _build_ticket_list_context

    return template_response(
        "htmx/partials/tickets/list.html",
        {"request": request, **_build_ticket_list_context(db, "open")},
    )


@router.patch("/api/error-reports/{report_id}")
@router.patch("/api/trouble-tickets/{report_id}")
async def update_ticket(
    report_id: int,
    body: TicketUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a trouble ticket status or resolution notes."""
    ticket = db.get(TroubleTicket, report_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if body.status:
        ticket.status = body.status
        if body.status == TicketStatus.RESOLVED:
            ticket.resolved_at = datetime.now(UTC)
            ticket.resolved_by_id = user.id
    if body.resolution_notes is not None:
        ticket.resolution_notes = body.resolution_notes
    if body.admin_notes is not None:
        ticket.admin_notes = body.admin_notes

    ticket.updated_at = datetime.now(UTC)
    db.commit()

    logger.info("Ticket {} updated to {} by user {}", ticket.ticket_number, ticket.status, user.id)
    return {"id": ticket.id, "status": ticket.status}


# ── AI diagnosis (admin) ──────────────────────────────────────────


@router.post("/api/trouble-tickets/{ticket_id}/diagnose", response_class=HTMLResponse)
async def diagnose_ticket_endpoint(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: AI-diagnose one ticket. Returns the diagnosis partial for HTMX swap."""
    from ..services.ticket_diagnosis_service import diagnose_ticket
    from ..utils.claude_errors import ClaudeError, ClaudeUnavailableError

    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    try:
        await diagnose_ticket(db, ticket)
    except (ClaudeUnavailableError, ClaudeError) as e:
        logger.warning("Diagnose failed for ticket {}: {}", ticket.ticket_number, e)
        return HTMLResponse(
            '<div class="p-3 text-sm text-amber-600 bg-amber-50 border border-amber-200 rounded-lg">'
            "AI diagnosis is unavailable right now. Please try again later.</div>"
        )

    # Diagnosis swaps #diagnosis-container; the fix prompt it produced rides along as
    # an out-of-band swap of the shared #ticket-prompt box (single home for the prompt).
    diagnosis_html = template_response(
        "htmx/partials/tickets/_diagnosis.html",
        {"request": request, "ticket": ticket},
    ).body.decode()
    prompt_html = template_response(
        "htmx/partials/tickets/_generated_prompt.html",
        {"request": request, "ticket": ticket, "oob": True},
    ).body.decode()
    resp = HTMLResponse(diagnosis_html + prompt_html)
    resp.headers["HX-Trigger"] = "ticketsUpdated"
    return resp


@router.post("/api/trouble-tickets/{ticket_id}/generate-prompt", response_class=HTMLResponse)
async def generate_prompt_endpoint(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: write a kind-aware, notes-aware Claude Code prompt for this ticket.

    Persists any posted ``admin_notes`` first (so the prompt reflects the latest notes
    even before the notes field is separately saved), then generates and stores the
    prompt. Returns the #ticket-prompt copy box for an HTMX swap.
    """
    from ..services.ticket_prompt_service import generate_ticket_prompt
    from ..utils.claude_errors import ClaudeError, ClaudeUnavailableError

    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    # Persist notes riding along on the request (hx-include of the notes textarea).
    form = await request.form()
    posted_notes = form.get("admin_notes")
    if posted_notes is not None:
        ticket.admin_notes = (posted_notes or "").strip() or None
        ticket.updated_at = datetime.now(UTC)
        db.commit()

    try:
        prompt = await generate_ticket_prompt(db, ticket)
    except (ClaudeUnavailableError, ClaudeError) as e:
        logger.warning("Create-prompt failed for ticket {}: {}", ticket.ticket_number, e)
        return HTMLResponse(
            '<div class="p-3 text-sm text-amber-600 bg-amber-50 border border-amber-200 rounded-lg">'
            "Prompt generation is unavailable right now. Please try again later.</div>"
        )

    if not prompt:
        return HTMLResponse(
            '<div class="p-3 text-sm text-amber-600 bg-amber-50 border border-amber-200 rounded-lg">'
            "The AI returned no prompt. Please try again.</div>"
        )

    resp = template_response(
        "htmx/partials/tickets/_generated_prompt.html",
        {"request": request, "ticket": ticket},
    )
    resp.headers["HX-Trigger"] = "ticketsUpdated"
    return resp


@router.post("/api/trouble-tickets/diagnose-bulk", response_class=HTMLResponse)
async def diagnose_bulk_endpoint(
    body: DiagnoseBulkBody,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: AI-diagnose the selected tickets concurrently."""
    from ..services.ticket_diagnosis_service import diagnose_tickets_bulk

    tickets = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.id.in_(body.ticket_ids))
        .filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
        .all()
    )
    outcomes = await diagnose_tickets_bulk(db, tickets)
    ok = sum(1 for v in outcomes.values() if v == "ok")
    total = len(body.ticket_ids)

    resp = HTMLResponse(
        f'<div class="p-3 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg">'
        f"Diagnosed {ok} of {total} selected ticket{'s' if total != 1 else ''}.</div>"
    )
    resp.headers["HX-Trigger"] = "ticketsUpdated"
    return resp


@router.post("/api/trouble-tickets/bulk-status", response_class=HTMLResponse)
async def bulk_status_endpoint(
    body: BulkStatusBody,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin: bulk status change (resolve / wont_fix / in_progress / submitted)."""
    tickets = db.query(TroubleTicket).filter(TroubleTicket.id.in_(body.ticket_ids)).all()
    now = datetime.now(UTC)
    for ticket in tickets:
        ticket.status = body.status
        ticket.updated_at = now
        if body.status == TicketStatus.RESOLVED:
            ticket.resolved_at = now
            ticket.resolved_by_id = user.id
    db.commit()
    logger.info("Bulk status {} applied to {} tickets by user {}", body.status, len(tickets), user.id)

    resp = HTMLResponse("")
    resp.headers["HX-Trigger"] = "ticketsUpdated"
    return resp
