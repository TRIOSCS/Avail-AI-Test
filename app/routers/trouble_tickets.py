"""Trouble ticket router -- unified CRUD endpoints for the self-heal pipeline.

POST /api/trouble-tickets/find-trouble       -- start Find Trouble loop (admin)
POST /api/trouble-tickets/find-trouble/stop  -- cancel running loop (admin)
GET  /api/trouble-tickets/find-trouble/stream-- SSE progress stream (admin)
GET  /api/trouble-tickets/find-trouble/prompts-- agent test prompts (admin)
POST /api/trouble-tickets            -- create (any authenticated user)
GET  /api/trouble-tickets            -- list all (admin, status/source filter + pagination)
GET  /api/trouble-tickets/my-tickets -- current user's tickets
GET  /api/trouble-tickets/stats      -- weekly stats + health (admin)
GET  /api/trouble-tickets/export/xlsx-- Excel export (admin)
GET  /api/trouble-tickets/active-areas -- areas under automated test (admin)
GET  /api/trouble-tickets/similar    -- check for similar open tickets (admin)
GET  /api/trouble-tickets/{id}       -- single ticket (admin or submitter)
PATCH /api/trouble-tickets/{id}      -- update (admin only)
POST /api/trouble-tickets/{id}/verify -- user confirms fix or reports still broken
POST /api/trouble-tickets/{id}/diagnose -- trigger AI diagnosis (admin)
POST /api/trouble-tickets/{id}/execute -- approve and execute fix (admin)
POST /api/trouble-tickets/{id}/regenerate-prompt -- regenerate AI prompt (admin)

Called by: main.py (app.include_router)
Depends on: services/trouble_ticket_service.py, dependencies.py
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import require_admin, require_user
from app.models import User
from app.models.trouble_ticket import TroubleTicket
from app.schemas.trouble_ticket import TroubleTicketCreate, TroubleTicketUpdate
from app.services import trouble_ticket_service as svc
from app.services.diagnosis_service import diagnose_full
from app.services.execution_service import execute_fix
from app.services.find_trouble_service import get_find_trouble_service
from app.services.pattern_tracker import get_health_status, get_weekly_stats

router = APIRouter(tags=["trouble-tickets"])


# ── Find Trouble endpoints (must be before /{ticket_id} routes) ────────

@router.post("/api/trouble-tickets/find-trouble")
async def start_find_trouble(
    request: Request,
    user: User = Depends(require_admin),
):
    """Launch the Find Trouble test loop (admin only)."""
    svc_ft = get_find_trouble_service()
    session_cookie = request.cookies.get("session", "")
    # Always use localhost — Playwright runs inside the same container
    base_url = "http://localhost:8000"

    result = svc_ft.try_start(base_url, session_cookie)
    if result is None:
        raise HTTPException(409, "Find Trouble is already running")
    return result


@router.post("/api/trouble-tickets/find-trouble/stop")
async def stop_find_trouble(
    user: User = Depends(require_admin),
):
    """Cancel the running Find Trouble loop."""
    svc_ft = get_find_trouble_service()
    if svc_ft.stop():
        return {"ok": True, "message": "Stop requested"}
    raise HTTPException(404, "No Find Trouble job running")


@router.get("/api/trouble-tickets/find-trouble/stream")
async def find_trouble_stream(
    user: User = Depends(require_admin),
):
    """SSE stream of Find Trouble progress events."""
    svc_ft = get_find_trouble_service()

    async def event_generator():
        cursor = 0
        while True:
            events = svc_ft.consume_events(after=cursor)
            for evt in events:
                yield f"data: {json.dumps(evt)}\n\n"
                cursor += 1

            status = svc_ft.get_status()
            if not status["running"] and cursor >= len(svc_ft._events):
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/trouble-tickets/find-trouble/prompts")
async def find_trouble_prompts(
    user: User = Depends(require_admin),
):
    """Get Claude agent test prompts for all areas."""
    from app.services.test_prompts import generate_all_prompts

    return {"prompts": generate_all_prompts()}


@router.post("/api/trouble-tickets")
async def create_ticket(
    body: TroubleTicketCreate,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a new trouble ticket. Any authenticated user."""
    # Validate screenshot size
    if body.screenshot_b64 and len(body.screenshot_b64) > svc.MAX_SCREENSHOT_SIZE:
        raise HTTPException(400, "Screenshot too large (max 2 MB)")

    ticket = svc.create_ticket(
        db=db,
        user_id=user.id,
        title=body.title,
        description=body.description,
        current_page=body.current_page,
        user_agent=request.headers.get("user-agent"),
        frontend_errors=body.frontend_errors,
        source=body.source or "ticket_form",
        screenshot_b64=body.screenshot_b64,
        browser_info=body.browser_info,
        screen_size=body.screen_size,
        console_errors=body.console_errors,
        page_state=body.page_state,
        current_view=body.current_view,
        tested_area=getattr(body, "tested_area", None),
        dom_snapshot=getattr(body, "dom_snapshot", None),
        network_errors=getattr(body, "network_errors", None),
        performance_timings=getattr(body, "performance_timings", None),
        reproduction_steps=getattr(body, "reproduction_steps", None),
    )

    # For report_button tickets, generate AI prompt
    if (body.source or "").lower() == "report_button":
        try:
            from app.services.ai_trouble_prompt import generate_trouble_prompt

            result = await generate_trouble_prompt(
                user_message=body.message or body.description or body.title,
                current_url=body.current_page,
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
                    db=db,
                    ticket_id=ticket.id,
                    title=result["title"],
                    ai_prompt=result["prompt"],
                )
                logger.info("AI prompt generated for ticket #{}", ticket.id)
        except Exception as e:
            logger.warning("AI prompt generation failed for ticket #{}: {}", ticket.id, e)

    # Fire-and-forget: auto-diagnose and auto-execute in background
    asyncio.create_task(svc.auto_process_ticket(ticket.id))

    # Thread consolidation — link to similar open ticket if found
    async def _consolidate_bg(tid: int):
        from app.database import SessionLocal
        from app.services.ticket_consolidation import consolidate_ticket

        _db = SessionLocal()
        try:
            await consolidate_ticket(tid, _db)
        except Exception:
            logger.warning("Background consolidation failed for ticket {}", tid)
        finally:
            _db.close()

    asyncio.create_task(_consolidate_bg(ticket.id))

    return {"ok": True, "id": ticket.id, "ticket_number": ticket.ticket_number}


@router.get("/api/trouble-tickets/stats")
async def ticket_stats(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Weekly stats + health indicator for admin dashboard."""
    stats = get_weekly_stats(db, weeks_back=1)
    health = get_health_status(db)
    return {"stats": stats, "health": health}


@router.get("/api/trouble-tickets/my-tickets")
async def my_tickets(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List current user's tickets."""
    tickets = svc.get_tickets_by_user(db=db, user_id=user.id)
    return {
        "items": [
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "title": t.title,
                "status": t.status,
                "risk_tier": t.risk_tier,
                "category": t.category,
                "source": t.source,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tickets
        ]
    }


@router.get("/api/trouble-tickets/export/xlsx")
def export_tickets_xlsx(
    status: str | None = Query(None),
    source: str | None = Query(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Export tickets to Excel (admin)."""
    import io

    from openpyxl import Workbook

    query = db.query(svc.TroubleTicket)
    if status:
        query = query.filter(svc.TroubleTicket.status == status)
    if source:
        query = query.filter(svc.TroubleTicket.source == source)
    query = query.order_by(svc.TroubleTicket.created_at.desc())
    tickets = query.limit(2000).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Trouble Tickets"
    headers = [
        "ID",
        "Ticket #",
        "Title",
        "Description",
        "Status",
        "Source",
        "Risk Tier",
        "Category",
        "Reporter",
        "URL",
        "View",
        "Browser",
        "Screen",
        "Console Errors",
        "Admin Notes",
        "Created",
        "Diagnosed",
        "Resolved",
    ]
    ws.append(headers)
    for t in tickets:
        submitter = db.get(User, t.submitted_by) if t.submitted_by else None
        ws.append(
            [
                t.id,
                t.ticket_number,
                t.title,
                t.description or "",
                t.status,
                t.source or "",
                t.risk_tier or "",
                t.category or "",
                submitter.email if submitter else "",
                t.current_page or "",
                t.current_view or "",
                t.browser_info or "",
                t.screen_size or "",
                t.console_errors or "",
                t.admin_notes or "",
                t.created_at.isoformat() if t.created_at else "",
                t.diagnosed_at.isoformat() if t.diagnosed_at else "",
                t.resolved_at.isoformat() if t.resolved_at else "",
            ]
        )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=trouble_tickets.xlsx"},
    )


@router.get("/api/trouble-tickets")
async def list_tickets(
    status: str | None = None,
    source: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all tickets (admin only). Optional status/source filter and pagination."""
    return svc.list_tickets(
        db=db,
        status_filter=status,
        source_filter=source,
        limit=limit,
        offset=offset,
    )


@router.get("/api/trouble-tickets/active-areas")
async def active_areas(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Areas currently under automated test (last hour)."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    rows = (
        db.query(TroubleTicket.tested_area)
        .filter(
            TroubleTicket.tested_area.isnot(None),
            TroubleTicket.source.in_(["playwright", "agent"]),
            TroubleTicket.created_at >= cutoff,
        )
        .distinct()
        .all()
    )
    return {"areas": [r[0] for r in rows]}


@router.get("/api/trouble-tickets/similar")
async def check_similar(
    title: str = Query(..., min_length=3),
    description: str = Query(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Check for similar open tickets before submitting (agent pre-check)."""
    from app.services.ticket_consolidation import find_similar_ticket

    # Create a temporary ticket object for comparison (not persisted)
    temp = TroubleTicket(
        id=-1,
        title=title,
        description=description or title,
        status="submitted",
    )
    match = await find_similar_ticket(temp, db)
    if match:
        parent = db.get(TroubleTicket, match["match_id"])
        return {
            "matches": [
                {
                    "id": parent.id,
                    "ticket_number": parent.ticket_number,
                    "title": parent.title,
                    "confidence": match["confidence"],
                }
            ]
            if parent
            else [],
        }
    return {"matches": []}


@router.get("/api/trouble-tickets/{ticket_id}")
async def get_ticket(
    ticket_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get a single ticket. Admin sees any; users see only their own."""
    ticket = svc.get_ticket(db=db, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if user.role != "admin" and ticket.submitted_by != user.id:
        raise HTTPException(403, "Access denied")
    submitter = db.get(User, ticket.submitted_by) if ticket.submitted_by else None
    resolved_by = db.get(User, ticket.resolved_by_id) if ticket.resolved_by_id else None
    children = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.parent_ticket_id == ticket.id)
        .order_by(TroubleTicket.created_at.desc())
        .all()
    )
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "title": ticket.title,
        "description": ticket.description,
        "status": ticket.status,
        "risk_tier": ticket.risk_tier,
        "category": ticket.category,
        "submitted_by": ticket.submitted_by,
        "submitted_by_name": submitter.name if submitter else None,
        "current_page": ticket.current_page,
        "auto_captured_context": ticket.auto_captured_context,
        "sanitized_context": ticket.sanitized_context,
        "diagnosis": ticket.diagnosis,
        "generated_prompt": ticket.generated_prompt,
        "file_mapping": ticket.file_mapping,
        "fix_branch": ticket.fix_branch,
        "fix_pr_url": ticket.fix_pr_url,
        "iterations_used": ticket.iterations_used,
        "cost_usd": ticket.cost_usd,
        "resolution_notes": ticket.resolution_notes,
        "parent_ticket_id": ticket.parent_ticket_id,
        "similarity_score": ticket.similarity_score,
        "child_tickets": [
            {
                "id": c.id,
                "ticket_number": c.ticket_number,
                "title": c.title,
                "status": c.status,
                "similarity_score": c.similarity_score,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in children
        ],
        "child_count": len(children),
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "diagnosed_at": ticket.diagnosed_at.isoformat() if ticket.diagnosed_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        # Unified fields
        "source": ticket.source,
        "screenshot_b64": ticket.screenshot_b64,
        "ai_prompt": ticket.ai_prompt,
        "admin_notes": ticket.admin_notes,
        "browser_info": ticket.browser_info,
        "screen_size": ticket.screen_size,
        "console_errors": ticket.console_errors,
        "current_view": ticket.current_view,
        "has_screenshot": bool(ticket.screenshot_b64),
        "has_ai_prompt": bool(ticket.ai_prompt),
        "resolved_by_email": resolved_by.email if resolved_by else None,
        "reporter_email": submitter.email if submitter else None,
    }


@router.patch("/api/trouble-tickets/{ticket_id}")
async def update_ticket(
    ticket_id: int,
    body: TroubleTicketUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a ticket (admin only)."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    # If status being changed to resolved, set resolved_by
    if updates.get("status") == "resolved":
        updates["resolved_by_id"] = user.id
        updates["resolved_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    ticket = svc.update_ticket(db=db, ticket_id=ticket_id, **updates)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return {"ok": True}


@router.post("/api/trouble-tickets/{ticket_id}/diagnose")
async def diagnose_ticket(
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trigger AI diagnosis on a ticket (admin only)."""
    if not settings.self_heal_enabled:
        raise HTTPException(403, "Self-heal pipeline is disabled")
    ticket = svc.get_ticket(db=db, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.diagnosis:
        raise HTTPException(400, "Ticket already diagnosed")
    result = await diagnose_full(ticket_id, db)
    if "error" in result:
        raise HTTPException(500, result["error"])
    return result


@router.post("/api/trouble-tickets/{ticket_id}/execute")
async def execute_ticket_fix(
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Approve and execute AI-generated fix (admin only)."""
    if not settings.self_heal_enabled:
        raise HTTPException(403, "Self-heal pipeline is disabled")
    result = await execute_fix(ticket_id, db)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/trouble-tickets/{ticket_id}/regenerate-prompt")
async def regenerate_prompt(
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Re-generate the AI prompt for a ticket (admin only)."""
    ticket = svc.get_ticket(db=db, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    from app.services.ai_trouble_prompt import generate_trouble_prompt

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
        db=db,
        ticket_id=ticket.id,
        title=result["title"],
        ai_prompt=result["prompt"],
    )
    logger.info("AI prompt regenerated for ticket #{} by {}", ticket_id, user.email)
    return {"id": ticket.id, "ai_prompt": result["prompt"], "title": result["title"]}


@router.post("/api/trouble-tickets/{ticket_id}/verify")
async def verify_ticket(
    ticket_id: int,
    body: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """User confirms fix works or reports still broken."""
    ticket = svc.get_ticket(db=db, ticket_id=ticket_id)
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if ticket.submitted_by != user.id and user.role != "admin":
        raise HTTPException(403, "Access denied")
    if ticket.status not in ("awaiting_verification", "in_progress", "open"):
        raise HTTPException(400, "Ticket is not awaiting verification")

    is_fixed = body.get("is_fixed", True)

    if is_fixed:
        svc.update_ticket(
            db=db,
            ticket_id=ticket_id,
            status="resolved",
            resolution_notes="User verified fix",
        )
        return {"ok": True, "status": "resolved"}
    else:
        parent_risk = ticket.risk_tier or "low"
        child_risk = "high" if parent_risk == "high" else "medium"
        child_desc = body.get("description", f"Follow-up: {ticket.title}")
        child = svc.create_ticket(
            db=db,
            user_id=user.id,
            title=f"Follow-up: {ticket.title}",
            description=child_desc,
        )
        svc.update_ticket(
            db=db,
            ticket_id=child.id,
            risk_tier=child_risk,
            parent_ticket_id=ticket.id,
        )
        svc.update_ticket(
            db=db,
            ticket_id=ticket_id,
            status="escalated",
            resolution_notes="User reported still broken",
        )
        return {"ok": True, "status": "escalated", "child_ticket_id": child.id}


@router.post("/api/trouble-tickets/{ticket_id}/verify-retest")
async def verify_retest_ticket(
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trigger automated retest via SiteTester after fix deployment (admin only)."""
    from app.services.rollback_service import verify_and_retest

    result = await verify_and_retest(ticket_id, db)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router.post("/api/internal/verify-retest/{ticket_id}")
async def internal_verify_retest(
    ticket_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Internal-only retest endpoint (localhost only, no auth).

    Called by: scripts/self_heal_watcher.sh after applying patches and rebuilding.
    """
    client = request.client
    if not client or client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Internal endpoint — localhost only")

    from app.services.rollback_service import verify_and_retest

    # Generate session cookie for SiteTester
    session_cookie = None
    try:
        from itsdangerous import URLSafeTimedSerializer
        from app.config import settings as cfg
        signer = URLSafeTimedSerializer(cfg.secret_key)
        session_cookie = signer.dumps({"user_id": 1})
    except Exception:
        logger.warning("Could not generate session cookie for retest")

    result = await verify_and_retest(
        ticket_id, db,
        base_url="http://localhost:8000",
        session_cookie=session_cookie,
    )
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result
