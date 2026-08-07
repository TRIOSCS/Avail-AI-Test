"""Trouble-ticket partials — workspace, grouped list, detail (admin console).

W4.8 split of the 969-line app/routers/htmx/archive.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from
.common (registration assembled in __init__). Route order preserved from the
original file: the literal /workspace and /list routes MUST register before
/{ticket_id} — Starlette matches in registration order and {ticket_id} textually
matches both literals (int coercion 422s only AFTER the match).

Called by: app/main.py (router mount); error_reports.analyze_tickets re-imports
    _build_ticket_list_context via the package __init__.
Depends on: app.constants, app.database, app.dependencies, app.models,
    app.template_env, .._shared
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session, joinedload

from ....constants import (
    TicketSource,
    TicketStatus,
    TicketType,
)
from ....database import get_db
from ....dependencies import require_admin
from ....models import User
from ....template_env import template_response
from .._shared import _base_ctx
from .common import router

# ── Trouble Tickets ──────────────────────────────────────────────────────


@router.get("/v2/partials/trouble-tickets/workspace", response_class=HTMLResponse)
async def trouble_tickets_workspace(request: Request, user: User = Depends(require_admin)):
    """Trouble Tickets workspace — loaded into #settings-content (admin-only
    console)."""
    return template_response(
        "htmx/partials/tickets/workspace.html",
        {**_base_ctx(request, user, "tickets")},
    )


def _build_ticket_list_context(db: Session, status: str | None, ticket_type: str | None = None) -> dict:
    """Query + group report_button tickets for the list partial.

    Shared by trouble_tickets_list and error_reports.analyze_tickets so both
    render the same grouped view. A logical ``status == "open"`` expands to the
    (submitted, in_progress) set so in-progress tickets stay visible under the
    "Open" pill; any other truthy status is an exact match; falsy means "all".
    ``ticket_type`` ("bug" | "feature") narrows the one inbox to a single kind;
    falsy means both kinds.

    Called by: trouble_tickets_list, error_reports.analyze_tickets.
    Depends on: TroubleTicket / RootCauseGroup models.
    """
    from app.models.root_cause_group import RootCauseGroup
    from app.models.trouble_ticket import TroubleTicket

    q = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.source == TicketSource.REPORT_BUTTON)
    )
    if status == "open":
        q = q.filter(TroubleTicket.status.in_([TicketStatus.SUBMITTED, TicketStatus.IN_PROGRESS]))
    elif status:
        q = q.filter(TroubleTicket.status == status)
    # Only constrain by kind for a recognised value; anything else means "both".
    if ticket_type in (TicketType.BUG, TicketType.FEATURE):
        q = q.filter(TroubleTicket.ticket_type == ticket_type)
    q = q.order_by(desc(TroubleTicket.created_at))
    tickets = q.limit(200).all()
    total = len(tickets)

    # Build group lookup only from group IDs present in results
    group_ids = {t.root_cause_group_id for t in tickets if t.root_cause_group_id}
    groups = (
        db.query(RootCauseGroup).filter(RootCauseGroup.id.in_(group_ids)).order_by(RootCauseGroup.title).all()
        if group_ids
        else []
    )
    grouped: dict = {}
    ungrouped = []
    for t in tickets:
        if t.root_cause_group_id:
            grouped.setdefault(t.root_cause_group_id, []).append(t)
        else:
            ungrouped.append(t)

    return {
        "total": total,
        "groups": groups,
        "grouped": grouped,
        "ungrouped": ungrouped,
        "current_status": status or "",
        "current_type": ticket_type or "",
    }


@router.get("/v2/partials/trouble-tickets/list", response_class=HTMLResponse)
async def trouble_tickets_list(
    request: Request,
    status: str = "",
    type: str = "",
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trouble Tickets list partial — grouped by root cause, filterable by status +
    kind."""
    return template_response(
        "htmx/partials/tickets/list.html",
        {**_base_ctx(request, user, "tickets"), **_build_ticket_list_context(db, status, type)},
    )


@router.get("/v2/partials/trouble-tickets/{ticket_id}", response_class=HTMLResponse)
async def trouble_ticket_detail(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trouble Ticket detail partial — swapped into #main-content (admin-only
    console)."""
    from app.models.trouble_ticket import TroubleTicket

    ticket = (
        db.query(TroubleTicket)
        .options(joinedload(TroubleTicket.root_cause_group), joinedload(TroubleTicket.submitter))
        .filter(TroubleTicket.id == ticket_id)
        .first()
    )
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    return template_response(
        "htmx/partials/tickets/detail.html",
        {**_base_ctx(request, user, "tickets"), "ticket": ticket},
    )
