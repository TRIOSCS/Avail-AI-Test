"""Execution service — orchestrates fix execution for the self-heal pipeline.

Flow: approve → budget check → file lock → run fix (subprocess) → handle result.
The _run_fix() method is pluggable — local subprocess in v1, GitHub Actions later.

Called by: routers/trouble_tickets.py
Depends on: services/cost_controller.py, services/trouble_ticket_service.py,
            services/notification_service.py, services/prompt_generator.py
"""

import asyncio
import os
import subprocess
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models.self_heal_log import SelfHealLog
from app.models.trouble_ticket import TroubleTicket
from app.services.cost_controller import check_budget, record_cost
from app.services.notification_service import create_notification
from app.services.prompt_generator import generate_prompt_for_ticket
from app.services.trouble_ticket_service import check_file_lock, update_ticket


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

    # Get or generate prompt
    prompt = ticket.generated_prompt
    if not prompt:
        prompt = generate_prompt_for_ticket(ticket)
        update_ticket(db, ticket_id, generated_prompt=prompt)

    # Mark as in-progress
    update_ticket(db, ticket_id, status="fix_in_progress", iterations_used=current_iter + 1)

    # Execute
    result = await _run_fix(prompt, ticket)

    # Handle result
    if result["success"]:
        update_ticket(
            db, ticket_id,
            status="awaiting_verification",
            fix_branch=result.get("branch"),
        )
        record_cost(db, ticket_id, result.get("cost_usd", 0.0))
        _notify(db, ticket, "fixed", "Fix applied", result.get("summary", "Fix completed"))
        logger.info("Ticket {} fix applied successfully", ticket_id)
        return {"ok": True, "status": "awaiting_verification", "message": "Fix applied"}
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


async def _run_fix(prompt: str, ticket: TroubleTicket) -> dict:
    """Execute the fix via local subprocess.

    Returns: {success: bool, error?: str, summary?: str, branch?: str, cost_usd?: float}
    """
    if os.getenv("TESTING"):
        return {"success": False, "error": "Execution disabled in test mode"}

    try:  # pragma: no cover
        result = await asyncio.to_thread(
            _subprocess_fix, prompt, ticket.id,
        )
        return result
    except Exception as e:  # pragma: no cover
        logger.error("Execution subprocess failed for ticket {}: {}", ticket.id, e)
        return {"success": False, "error": str(e), "cost_usd": 0.0}


def _subprocess_fix(prompt: str, ticket_id: int) -> dict:  # pragma: no cover
    """Run claude CLI as subprocess with the fix prompt."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=300,
            cwd="/root/availai",
        )
        if result.returncode == 0:
            return {
                "success": True,
                "summary": result.stdout[:500] if result.stdout else "Fix applied",
                "branch": f"fix/ticket-{ticket_id}",
                "cost_usd": 0.10,  # estimated per-run cost
            }
        else:
            return {
                "success": False,
                "error": result.stderr[:500] if result.stderr else "Process exited with error",
                "cost_usd": 0.05,
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Execution timed out (5min)", "cost_usd": 0.05}


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
