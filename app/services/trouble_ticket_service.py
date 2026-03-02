"""Trouble ticket service -- CRUD, auto-context capture, and sanitization.

Handles the full ticket lifecycle: creation with auto-context capture,
sanitization of sensitive data before AI processing, listing, updating,
and file lock checking for concurrent fix prevention.

Called by: routers/trouble_tickets.py
Depends on: models/trouble_ticket.py, models/auth.py, config.py
"""

import re
import sys
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.config import APP_VERSION
from app.models.trouble_ticket import TroubleTicket
from app.models import User


_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{10,}"),
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"postgres(ql)?://\S+"),
    re.compile(r'password["\s:=]+\S+', re.IGNORECASE),
    re.compile(r'api[_-]?key["\s:=]+\S+', re.IGNORECASE),
    re.compile(r'secret["\s:=]+\S+', re.IGNORECASE),
]
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+")


def create_ticket(
    db: Session,
    user_id: int,
    title: str,
    description: str,
    current_page: str | None = None,
    user_agent: str | None = None,
    frontend_errors: list[dict] | None = None,
) -> TroubleTicket:
    """Create a trouble ticket with auto-captured context."""
    ticket_number = _generate_ticket_number(db)

    auto_ctx = _capture_auto_context(
        db=db,
        user_id=user_id,
        current_page=current_page,
        frontend_errors=frontend_errors,
    )

    user = db.get(User, user_id)
    submitter_email = user.email if user else ""
    sanitized = _sanitize_context(auto_ctx, submitter_email=submitter_email)

    ticket = TroubleTicket(
        ticket_number=ticket_number,
        submitted_by=user_id,
        title=title.strip(),
        description=description.strip(),
        current_page=current_page,
        user_agent=user_agent,
        auto_captured_context=auto_ctx,
        sanitized_context=sanitized,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    logger.info(
        "Ticket {ticket_number} created by user {user_id}",
        ticket_number=ticket_number,
        user_id=user_id,
    )
    return ticket


def get_ticket(db: Session, ticket_id: int) -> TroubleTicket | None:
    """Get a single ticket by ID."""
    return db.get(TroubleTicket, ticket_id)


def list_tickets(
    db: Session,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Paginated list of tickets, optionally filtered by status."""
    query = db.query(TroubleTicket)
    if status_filter:
        query = query.filter(TroubleTicket.status == status_filter)
    total = query.count()
    tickets = (
        query.order_by(TroubleTicket.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = []
    for t in tickets:
        submitter = db.get(User, t.submitted_by) if t.submitted_by else None
        items.append({
            "id": t.id,
            "ticket_number": t.ticket_number,
            "title": t.title,
            "status": t.status,
            "risk_tier": t.risk_tier,
            "category": t.category,
            "submitted_by": t.submitted_by,
            "submitted_by_name": submitter.name if submitter else None,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_tickets_by_user(db: Session, user_id: int) -> list[TroubleTicket]:
    """List tickets submitted by a specific user."""
    return (
        db.query(TroubleTicket)
        .filter(TroubleTicket.submitted_by == user_id)
        .order_by(TroubleTicket.created_at.desc())
        .all()
    )


def update_ticket(db: Session, ticket_id: int, **kwargs) -> TroubleTicket | None:
    """Update ticket fields. Auto-sets diagnosed_at/resolved_at on transitions."""
    ticket = db.get(TroubleTicket, ticket_id)
    if not ticket:
        return None

    for key, value in kwargs.items():
        if hasattr(ticket, key):
            setattr(ticket, key, value)

    if "status" in kwargs:
        if kwargs["status"] == "diagnosed" and not ticket.diagnosed_at:
            ticket.diagnosed_at = datetime.now(timezone.utc)
        elif kwargs["status"] == "resolved" and not ticket.resolved_at:
            ticket.resolved_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(ticket)
    return ticket


def check_file_lock(db: Session, file_paths: list[str]) -> TroubleTicket | None:
    """Check if any active fix overlaps with the given files."""
    active = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.status == "fix_in_progress")
        .filter(TroubleTicket.file_mapping.isnot(None))
        .all()
    )
    file_set = set(file_paths)
    for ticket in active:
        if ticket.file_mapping and set(ticket.file_mapping) & file_set:
            return ticket
    return None


def _generate_ticket_number(db: Session) -> str:
    """Generate TT-YYYYMMDD-NNN ticket number."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"TT-{today}-"
    last = (
        db.query(TroubleTicket)
        .filter(TroubleTicket.ticket_number.like(f"{prefix}%"))
        .order_by(TroubleTicket.id.desc())
        .first()
    )
    if last:
        last_num = int(last.ticket_number.split("-")[-1])
        seq = last_num + 1
    else:
        seq = 1
    return f"{prefix}{seq:03d}"


def _capture_auto_context(
    db: Session,
    user_id: int,
    current_page: str | None = None,
    frontend_errors: list[dict] | None = None,
) -> dict:
    """Build auto-captured context dict from user session and server state."""
    user = db.get(User, user_id)
    user_role = user.role if user else "unknown"

    page_route = current_page or ""
    if page_route:
        page_route = _NUMERIC_SEGMENT_RE.sub("/{id}", page_route)

    return {
        "recent_api_errors": [],  # TODO: integrate with Sentry API
        "recent_frontend_errors": frontend_errors or [],
        "user_role": user_role,
        "server_info": {
            "python_version": sys.version.split()[0],
            "app_version": APP_VERSION,
        },
        "page_route": page_route,
    }


def _sanitize_context(context: dict, submitter_email: str = "") -> dict:
    """Strip sensitive data from context before AI processing."""
    sanitized = {}
    any_stripped = False

    for key, value in context.items():
        if isinstance(value, str):
            clean = _sanitize_string(value, submitter_email)
            sanitized[key] = clean
            if clean != value:
                any_stripped = True
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_context(value, submitter_email)
        elif isinstance(value, list):
            sanitized[key] = [
                _sanitize_context(item, submitter_email) if isinstance(item, dict)
                else _sanitize_string(item, submitter_email) if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            sanitized[key] = value

    if any_stripped:
        logger.warning("Sensitive data stripped from ticket context")

    return sanitized


def _sanitize_string(value: str, submitter_email: str) -> str:
    """Sanitize a single string value."""
    result = value
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub("[REDACTED]", result)

    def _replace_email(match):
        email = match.group(0)
        if email.lower() == submitter_email.lower():
            return email
        return "[EMAIL]"

    result = _EMAIL_RE.sub(_replace_email, result)
    result = _IP_RE.sub("[IP]", result)

    if len(result) > 500:
        result = result[:500]

    return result
