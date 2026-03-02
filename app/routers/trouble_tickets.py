"""Trouble ticket router -- CRUD endpoints for the self-heal pipeline.

POST /api/trouble-tickets            -- create (any authenticated user)
GET  /api/trouble-tickets            -- list all (admin, status filter + pagination)
GET  /api/trouble-tickets/my-tickets -- current user's tickets
GET  /api/trouble-tickets/{id}       -- single ticket (admin or submitter)
PATCH /api/trouble-tickets/{id}      -- update (admin only)
POST /api/trouble-tickets/{id}/verify -- user confirms fix or reports still broken
POST /api/trouble-tickets/{id}/diagnose -- trigger AI diagnosis (planned, not yet implemented)

Called by: main.py (app.include_router)
Depends on: services/trouble_ticket_service.py, dependencies.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import require_admin, require_user
from app.models import User
from app.schemas.trouble_ticket import TroubleTicketCreate, TroubleTicketUpdate
from app.services import trouble_ticket_service as svc
from app.services.diagnosis_service import diagnose_full

router = APIRouter(tags=["trouble-tickets"])


@router.post("/api/trouble-tickets")
async def create_ticket(
    body: TroubleTicketCreate,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a new trouble ticket. Any authenticated user."""
    ticket = svc.create_ticket(
        db=db,
        user_id=user.id,
        title=body.title,
        description=body.description,
        current_page=body.current_page,
        user_agent=request.headers.get("user-agent"),
        frontend_errors=body.frontend_errors,
    )
    return {"ok": True, "id": ticket.id, "ticket_number": ticket.ticket_number}


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
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in tickets
        ]
    }


@router.get("/api/trouble-tickets")
async def list_tickets(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all tickets (admin only). Optional status filter and pagination."""
    return svc.list_tickets(db=db, status_filter=status, limit=limit, offset=offset)


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
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "diagnosed_at": ticket.diagnosed_at.isoformat() if ticket.diagnosed_at else None,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
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
    if ticket.status != "awaiting_verification":
        raise HTTPException(400, "Ticket is not awaiting verification")

    is_fixed = body.get("is_fixed", True)

    if is_fixed:
        svc.update_ticket(
            db=db, ticket_id=ticket_id,
            status="resolved", resolution_notes="User verified fix",
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
            db=db, ticket_id=child.id,
            risk_tier=child_risk, parent_ticket_id=ticket.id,
        )
        svc.update_ticket(
            db=db, ticket_id=ticket_id,
            status="escalated", resolution_notes="User reported still broken",
        )
        return {"ok": True, "status": "escalated", "child_ticket_id": child.id}
