"""Execution service — orchestrates fix generation for the self-heal pipeline.

Flow: approve → budget check → file lock → generate patches (Claude API) → write to fix queue.
The host-side watcher (scripts/self_heal_watcher.sh) picks up fix files and applies them.

Called by: routers/trouble_tickets.py
Depends on: services/cost_controller.py, services/trouble_ticket_service.py,
            services/notification_service.py, services/patch_generator.py
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket
from app.services.cost_controller import check_budget, record_cost
from app.services.notification_service import create_notification
from app.services.trouble_ticket_service import check_file_lock, update_ticket

FIX_QUEUE_DIR = os.environ.get("FIX_QUEUE_DIR", "/app/fix_queue")


async def execute_fix(ticket_id: int, db: Session) -> dict:
    """Full execution pipeline for a diagnosed ticket.

    Returns: {ok, status, message} or {error, reason}.
    """
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return {"error": "Ticket not found"}

    if not ticket.diagnosis:
        return {"error": "Ticket not yet diagnosed"}

    if ticket.status not in ("diagnosed", "prompt_ready"):
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
    max_iter = (
        settings.self_heal_max_iterations_low if risk_tier == "low"
        else settings.self_heal_max_iterations_medium
    )
    current_iter = ticket.iterations_used or 0
    if current_iter >= max_iter:
        _escalate(db, ticket, f"Max iterations ({max_iter}) reached")
        return {"error": f"Max iterations ({max_iter}) reached — escalated"}

    # Mark as in-progress
    update_ticket(db, ticket_id, status="in_progress", iterations_used=current_iter + 1)

    # Generate patches via Claude API
    result = await _generate_fix(ticket)

    if result["success"]:
        # Write fix to queue for host-side watcher
        payload = {
            "ticket_id": ticket_id,
            "ticket_number": ticket.ticket_number,
            "risk_tier": risk_tier,
            "category": ticket.category or "unknown",
            "test_area": ticket.tested_area or ticket.category or "unknown",
            "patches": result["patches"],
            "summary": result.get("summary", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_fix_queue(ticket_id, payload)

        update_ticket(db, ticket_id, status="fix_queued")
        record_cost(db, ticket_id, result.get("cost_usd", 0.05))
        _notify(db, ticket, "fixed", "Fix queued", result.get("summary", "Fix generated"))
        logger.info("Ticket {} fix queued ({} patches)", ticket_id, len(result["patches"]))
        return {"ok": True, "status": "fix_queued", "message": "Fix queued for application"}
    else:
        error_msg = result.get("error", "Unknown error")
        if current_iter + 1 >= max_iter:
            _escalate(db, ticket, f"Fix failed after {current_iter + 1} iterations: {error_msg}")
            return {"error": f"Fix failed and escalated: {error_msg}"}
        else:
            update_ticket(db, ticket_id, status="diagnosed")
            record_cost(db, ticket_id, result.get("cost_usd", 0.0))
            _notify(db, ticket, "failed", "Fix attempt failed", error_msg)
            return {"error": f"Fix failed (attempt {current_iter + 1}/{max_iter}): {error_msg}"}


async def _generate_fix(ticket: TroubleTicket) -> dict:
    """Generate patches using Claude API via patch_generator.

    Returns: {success: bool, patches?: list, summary?: str, error?: str, cost_usd?: float}
    """
    if os.getenv("TESTING"):
        return {"success": False, "error": "Execution disabled in test mode"}

    from app.services.patch_generator import generate_patches

    diagnosis = ticket.diagnosis or {}
    affected_files = diagnosis.get("affected_files", [])
    if not affected_files:
        return {"success": False, "error": "No affected files in diagnosis"}

    try:
        result = await generate_patches(
            title=ticket.title,
            diagnosis=diagnosis,
            category=ticket.category or "unknown",
            affected_files=affected_files,
        )
    except Exception as e:
        logger.error("Patch generation failed for ticket {}: {}", ticket.id, e)
        return {"success": False, "error": str(e), "cost_usd": 0.02}

    if not result or not result.get("patches"):
        return {"success": False, "error": "No patches generated", "cost_usd": 0.02}

    return {
        "success": True,
        "patches": result["patches"],
        "summary": result.get("summary", ""),
        "cost_usd": 0.05,
    }


def _write_fix_queue(ticket_id: int, payload: dict) -> None:
    """Write fix JSON to the shared fix_queue directory."""
    queue_dir = Path(FIX_QUEUE_DIR)
    queue_dir.mkdir(parents=True, exist_ok=True)
    fix_path = queue_dir / f"{ticket_id}.json"
    fix_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("Fix queue file written: {}", fix_path)


def _escalate(db: Session, ticket: TroubleTicket, reason: str) -> None:
    """Escalate a ticket to human review."""
    update_ticket(db, ticket.id, status="escalated", resolution_notes=reason)
    _notify(db, ticket, "escalated", "Ticket escalated", reason)
    logger.warning("Ticket {} escalated: {}", ticket.id, reason)


def _notify(db: Session, ticket: TroubleTicket, event: str, title: str, body: str) -> None:
    """Send notification to ticket submitter."""
    if ticket.submitted_by:
        create_notification(
            db, user_id=ticket.submitted_by, event_type=event,
            title=f"Ticket #{ticket.id}: {title}", body=body,
            ticket_id=ticket.id,
        )


def _notify_budget_exceeded(db: Session, ticket: TroubleTicket, reason: str) -> None:
    """Notify about budget exceeded and escalate."""
    _escalate(db, ticket, f"Budget exceeded: {reason}")
