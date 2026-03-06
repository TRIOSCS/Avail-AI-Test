"""AI thread consolidation — detect and link duplicate trouble tickets.

Finds open tickets with similar root causes using Claude AI, then links
child tickets to parent tickets via parent_ticket_id + similarity_score.

Called by: scheduler (batch_consolidate), routers/trouble_tickets.py (on create)
Depends on: app.utils.claude_client (claude_structured), app.models.trouble_ticket
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.models.trouble_ticket import TroubleTicket
from app.utils.claude_client import claude_structured

SIMILARITY_THRESHOLD = 0.9

OPEN_STATUSES = ("submitted", "diagnosed", "escalated", "in_progress", "open")

_SYSTEM_PROMPT = (
    "You are a conservative bug triage assistant. Given a target ticket and a list of "
    "open tickets, determine if the target has the SAME root cause as any existing ticket. "
    "Only match when you are highly confident (>0.9) they share the same underlying bug. "
    "Do NOT match tickets that are merely in the same area — they must be the same defect."
)

_MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "match_id": {"type": ["integer", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["match_id", "confidence"],
}


async def find_similar_ticket(ticket: TroubleTicket, db: Session) -> dict | None:
    """Find an existing open ticket with the same root cause.

    Returns {"match_id": int, "confidence": float} if a high-confidence
    match is found, otherwise None.
    """
    try:
        candidates = (
            db.query(TroubleTicket)
            .filter(
                TroubleTicket.status.in_(OPEN_STATUSES),
                TroubleTicket.id != ticket.id,
            )
            .order_by(TroubleTicket.created_at.desc())
            .limit(50)
            .all()
        )

        if not candidates:
            return None

        candidate_lines = "\n".join(
            f"- ID {c.id}: {c.title} — {c.description}" for c in candidates
        )
        prompt = (
            f"Target ticket (ID {ticket.id}):\n"
            f"Title: {ticket.title}\n"
            f"Description: {ticket.description}\n\n"
            f"Open tickets:\n{candidate_lines}\n\n"
            "If the target shares the same root cause as one of the open tickets, "
            "return its ID and your confidence. Otherwise return match_id=null."
        )

        result = await claude_structured(
            prompt=prompt,
            schema=_MATCH_SCHEMA,
            system=_SYSTEM_PROMPT,
            model_tier="smart",
            max_tokens=256,
        )

        if not result:
            return None

        match_id = result.get("match_id")
        confidence = result.get("confidence", 0.0)

        if match_id is None or confidence < SIMILARITY_THRESHOLD:
            return None

        # Validate that match_id is actually one of the open candidates
        valid_ids = {c.id for c in candidates}
        if match_id not in valid_ids:
            logger.warning("AI returned invalid match_id={} not in open tickets", match_id)
            return None

        return {"match_id": match_id, "confidence": confidence}

    except Exception:
        logger.warning("find_similar_ticket failed for ticket {}", ticket.id, exc_info=True)
        return None


async def consolidate_ticket(ticket_id: int, db: Session) -> None:
    """Check a single ticket for duplicates and link if found."""
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return

    # Skip if already linked
    if ticket.parent_ticket_id is not None:
        return

    match = await find_similar_ticket(ticket, db)
    if not match:
        return

    ticket.parent_ticket_id = match["match_id"]
    ticket.similarity_score = match["confidence"]
    db.commit()

    logger.info(
        "Linked ticket {} -> parent {} (confidence {:.2f})",
        ticket_id,
        match["match_id"],
        match["confidence"],
    )


async def batch_consolidate(db: Session) -> int:
    """Scan all unlinked open tickets and attempt to find duplicates.

    Returns the count of newly linked tickets.
    """
    tickets = (
        db.query(TroubleTicket)
        .filter(
            TroubleTicket.status.in_(OPEN_STATUSES),
            TroubleTicket.parent_ticket_id.is_(None),
        )
        .order_by(TroubleTicket.created_at.asc())
        .all()
    )

    linked = 0
    for t in tickets:
        before = t.parent_ticket_id
        await consolidate_ticket(t.id, db)
        db.refresh(t)
        if t.parent_ticket_id is not None and before is None:
            linked += 1

    logger.info("batch_consolidate: linked {} of {} tickets", linked, len(tickets))
    return linked
