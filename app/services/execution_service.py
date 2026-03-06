"""Execution service — orchestrates fix execution for the self-heal pipeline.

Flow: approve → budget check → file lock → generate patches (Claude API) → queue fix.
Patches are written to FIX_QUEUE_DIR as JSON for human review before application.

Called by: routers/trouble_tickets.py
Depends on: services/cost_controller.py, services/trouble_ticket_service.py,
            services/notification_service.py, services/patch_generator.py
"""

import json
import os
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models.trouble_ticket import TroubleTicket
from app.services.cost_controller import check_budget, record_cost
from app.services.notification_service import create_notification
from app.services.patch_generator import generate_patches
from app.services.trouble_ticket_service import check_file_lock, update_ticket

FIX_QUEUE_DIR = "/app/fix_queue"


async def execute_fix(ticket_id: int, db: Session) -> dict:
    """Full execution pipeline for a diagnosed ticket.

    Returns: {ok, status, message} or {error, reason}.
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    if not ticket.diagnosis:
        return {"error": "Ticket not yet diagnosed"}

    if ticket.status not in ("diagnosed", "prompt_ready", "fix_queued"):
        return {"error": f"Ticket status '{ticket.status}' cannot be executed"}

    risk_tier = ticket.risk_tier or "medium"
    if risk_tier == "high":
        return {"error": "High-risk tickets require human intervention"}

    # Budget check
    budget = check_budget(db, ticket_id)
    if not budget["allowed"]:
        _notify_budget_exceeded(db, ticket, budget["reason"])
        return {"error": budget["reason"]}

    # File lock check
    file_mapping = ticket.file_mapping or []
    if file_mapping:
        blocking = check_file_lock(db, file_mapping)
        if blocking and blocking.id != ticket_id:
            reason = f"File lock conflict with ticket #{blocking.id}"
            return {"error": reason}

    # Max iterations check
    max_iter = settings.self_heal_max_iterations_low if risk_tier == "low" else settings.self_heal_max_iterations_medium
    current_iter = ticket.iterations_used or 0
    if current_iter >= max_iter:
        _escalate(db, ticket, f"Max iterations ({max_iter}) reached")
        return {"error": f"Max iterations ({max_iter}) reached — escalated"}

    # Extract diagnosis text
    diag = ticket.diagnosis
    if isinstance(diag, dict):
        diagnosis = diag.get("detailed") or diag
    else:
        diagnosis = diag

    # Mark as in-progress
    update_ticket(db, ticket_id, status="in_progress", iterations_used=current_iter + 1)

    # Generate patches via Claude API
    category = ticket.category or "other"
    affected_files = file_mapping or []
    result = await generate_patches(
        title=ticket.title,
        diagnosis=diagnosis,
        category=category,
        affected_files=affected_files,
    )

    # Handle: generation failed entirely
    if result is None:
        record_cost(db, ticket_id, 0.03)
        if current_iter + 1 >= max_iter:
            _escalate(db, ticket, f"Patch generation failed after {current_iter + 1} iterations")
            return {"error": f"Patch generation failed and escalated"}
        else:
            update_ticket(db, ticket_id, status="diagnosed")
            _notify(db, ticket, "failed", "Fix attempt failed", "Patch generation failed")
            return {"error": f"Patch generation failed (attempt {current_iter + 1}/{max_iter})"}

    # Handle: empty patches
    patches = result.get("patches", [])
    if not patches:
        _notify(db, ticket, "failed", "Fix attempt failed", "No patches generated")
        update_ticket(db, ticket_id, status="diagnosed")
        return {"error": "No patches generated"}

    # Success — write fix to queue
    test_area = getattr(ticket, "tested_area", None) or category
    fix_payload = {
        "ticket_id": ticket_id,
        "ticket_number": ticket.ticket_number,
        "risk_tier": risk_tier,
        "category": category,
        "test_area": test_area,
        "patches": patches,
        "summary": result.get("summary", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_fix_queue(ticket_id, fix_payload)

    fix_branch = f"fix/ticket-{ticket_id}"
    update_ticket(db, ticket_id, status="fix_queued", fix_branch=fix_branch)
    record_cost(db, ticket_id, 0.05)
    _notify(db, ticket, "fixed", "Fix queued", result.get("summary", "Patches generated"))
    logger.info("Ticket {} fix queued ({} patches)", ticket_id, len(patches))
    return {"ok": True, "status": "fix_queued", "message": f"Fix queued with {len(patches)} patch(es)"}


def _write_fix_queue(ticket_id: int, payload: dict) -> None:
    """Write fix JSON to the queue directory."""
    os.makedirs(FIX_QUEUE_DIR, exist_ok=True)
    path = os.path.join(FIX_QUEUE_DIR, f"{ticket_id}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info("Fix queue file written: {}", path)


def _escalate(db: Session, ticket: TroubleTicket, reason: str) -> None:
    """Escalate a ticket to human review."""
    update_ticket(db, ticket.id, status="escalated", resolution_notes=reason)
    _notify(db, ticket, "escalated", "Ticket escalated", reason)
    logger.warning("Ticket {} escalated: {}", ticket.id, reason)


def _notify(db: Session, ticket: TroubleTicket, event: str, title: str, body: str) -> None:
    """Send notification to ticket submitter."""
    if ticket.submitted_by:
        create_notification(
            db,
            user_id=ticket.submitted_by,
            event_type=event,
            title=f"Ticket #{ticket.id}: {title}",
            body=body,
            ticket_id=ticket.id,
        )


def _notify_budget_exceeded(db: Session, ticket: TroubleTicket, reason: str) -> None:
    """Notify about budget exceeded and escalate."""
    _escalate(db, ticket, f"Budget exceeded: {reason}")
