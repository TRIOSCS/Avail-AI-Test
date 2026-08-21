"""AI diagnosis for trouble tickets — structured root-cause + ready-to-paste fix prompt.

Builds a text prompt from a ticket's runtime context (description, page, JS/console
errors, network log, browser info, AI summary) and calls Claude (SMART/Sonnet) to
produce {root_cause, severity, affected_areas, reproduction_steps, fix_prompt}. The
fix_prompt is a self-contained prompt the admin pastes into a Claude Code session to
implement the fix — no code is changed automatically.

Persists into the TroubleTicket columns that already exist for this:
``diagnosis`` (JSON), ``generated_prompt`` (Text), ``diagnosed_at``, and best-effort
``cost_tokens``/``cost_usd``.

Called by: routers/error_reports.py (diagnose / diagnose-bulk endpoints)
Depends on: utils/claude_client.claude_structured_with_usage, models/trouble_ticket
"""

import asyncio
import json
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from ..models.trouble_ticket import TroubleTicket
from ..utils.claude_client import claude_structured_with_usage

# ── Tunables ─────────────────────────────────────────────────────────────────
DIAGNOSE_MODEL_TIER = "smart"  # Sonnet — diagnosis/fix-prompt quality is the whole value
DIAGNOSE_MAX_TOKENS = 2000
BULK_CONCURRENCY = 4  # bounded fan-out for interactive "Diagnose selected"
DESCRIPTION_TRUNC = 2000
CONSOLE_ERRORS_TRUNC = 4000
NETWORK_ERRORS_TRUNC = 2000

# Rough per-million-token prices (USD) for cost_usd estimation. Informational only.
_PRICE_PER_MTOK = {
    "fast": (1.0, 5.0),
    "smart": (3.0, 15.0),
    "opus": (15.0, 75.0),
}

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {
            "type": "string",
            "description": "Concise, most-likely root cause inferred from the runtime context.",
        },
        "severity": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "affected_areas": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Best-effort guesses of the files, routes, templates, or modules likely "
                "involved. You have NO repo access — infer from the page URL, stack traces, "
                "and the stack (FastAPI / SQLAlchemy / HTMX / Alpine.js / Jinja2)."
            ),
        },
        "reproduction_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered steps a developer would follow to reproduce the issue.",
        },
        "fix_prompt": {
            "type": "string",
            "description": (
                "A complete, self-contained prompt a developer can paste into a Claude Code "
                "session to implement the fix. Reference the likely files/areas, the symptom, "
                "and a concrete acceptance check. Do not include code blocks unless essential."
            ),
        },
    },
    "required": ["root_cause", "severity", "affected_areas", "reproduction_steps", "fix_prompt"],
}

DIAGNOSIS_SYSTEM = (
    "You are a senior full-stack engineer triaging bug reports for a FastAPI + SQLAlchemy + "
    "HTMX + Alpine.js + Jinja2 + Tailwind web app. You see only RUNTIME context (no repo "
    "access), so affected_areas are best-effort guesses — qualify them when the context is "
    "sparse rather than fabricating specifics. A screenshot may exist but is not provided to "
    "you. Produce a precise, self-contained Claude Code prompt a developer can paste to "
    "implement the fix."
)


def _estimate_cost_usd(usage: dict, model_tier: str) -> float | None:
    """Estimate USD spend from a usage dict.

    Returns None if usage is empty.
    """
    if not usage:
        return None
    in_price, out_price = _PRICE_PER_MTOK.get(model_tier, _PRICE_PER_MTOK["smart"])
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    return round((in_tok * in_price + out_tok * out_price) / 1_000_000, 6)


def _build_diagnosis_prompt(ticket: TroubleTicket) -> str:
    """Assemble the user prompt from the ticket's captured runtime context."""
    lines: list[str] = [f"Trouble ticket {ticket.ticket_number}."]
    if ticket.description:
        lines.append(f"User description: {ticket.description[:DESCRIPTION_TRUNC]}")
    if ticket.ai_summary:
        lines.append(f"AI summary: {ticket.ai_summary}")
    if ticket.current_page:
        lines.append(f"Page URL: {ticket.current_page}")
    if ticket.browser_info:
        lines.append(f"Browser: {ticket.browser_info}")
    if ticket.console_errors:
        lines.append(f"JS/console errors: {ticket.console_errors[:CONSOLE_ERRORS_TRUNC]}")
    if ticket.network_errors:
        net = json.dumps(ticket.network_errors)[:NETWORK_ERRORS_TRUNC]
        lines.append(f"Network log: {net}")
    if len(lines) == 1:
        lines.append("(no runtime context captured beyond the report itself)")
    return "\n".join(lines)


def _apply_diagnosis(ticket: TroubleTicket, result: dict, usage: dict, model_tier: str) -> None:
    """Persist a structured diagnosis onto the ticket (does not commit)."""
    now = datetime.now(UTC)
    ticket.diagnosis = {
        "root_cause": result.get("root_cause"),
        "severity": result.get("severity"),
        "affected_areas": result.get("affected_areas") or [],
        "reproduction_steps": result.get("reproduction_steps") or [],
        "model_tier": model_tier,
    }
    ticket.generated_prompt = result.get("fix_prompt")
    if result.get("severity"):
        ticket.risk_tier = result["severity"]
    ticket.diagnosed_at = now
    ticket.updated_at = now
    if usage:
        ticket.cost_tokens = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        ticket.cost_usd = _estimate_cost_usd(usage, model_tier)


async def diagnose_ticket(db: Session, ticket: TroubleTicket) -> dict:
    """Diagnose one ticket, persist the result, and return the diagnosis dict.

    Returns ``{}`` if Claude returned no structured result. Raises
    ClaudeUnavailableError / ClaudeError on AI failure (the caller maps these to a
    friendly inline message).
    """
    result, usage = await claude_structured_with_usage(
        _build_diagnosis_prompt(ticket),
        DIAGNOSIS_SCHEMA,
        system=DIAGNOSIS_SYSTEM,
        model_tier=DIAGNOSE_MODEL_TIER,
        max_tokens=DIAGNOSE_MAX_TOKENS,
    )
    if not result:
        return {}
    _apply_diagnosis(ticket, result, usage, DIAGNOSE_MODEL_TIER)
    db.commit()
    logger.info("Diagnosed trouble ticket {}", ticket.ticket_number)
    return ticket.diagnosis


async def diagnose_tickets_bulk(db: Session, tickets: list[TroubleTicket]) -> dict[int, str]:
    """Diagnose many tickets concurrently; persist all in one commit.

    Network calls run concurrently under a semaphore; ORM writes are applied
    sequentially on the request session (never from inside the gathered coroutines) and
    committed once. Per-ticket failures are isolated. Returns {ticket_id: 'ok'|'error'}.
    """
    if not tickets:
        return {}
    sem = asyncio.Semaphore(BULK_CONCURRENCY)

    async def _call(ticket: TroubleTicket):
        async with sem:
            result, usage = await claude_structured_with_usage(
                _build_diagnosis_prompt(ticket),
                DIAGNOSIS_SCHEMA,
                system=DIAGNOSIS_SYSTEM,
                model_tier=DIAGNOSE_MODEL_TIER,
                max_tokens=DIAGNOSE_MAX_TOKENS,
            )
            return ticket, result, usage

    settled = await asyncio.gather(*(_call(t) for t in tickets), return_exceptions=True)

    outcomes: dict[int, str] = {}
    for ticket, item in zip(tickets, settled):
        if isinstance(item, BaseException) or not isinstance(item, tuple):
            logger.warning("Bulk diagnose failed for ticket {}: {}", ticket.ticket_number, item)
            outcomes[ticket.id] = "error"
            continue
        _t, result, usage = item
        if not result:
            outcomes[ticket.id] = "error"
            continue
        _apply_diagnosis(ticket, result, usage, DIAGNOSE_MODEL_TIER)
        outcomes[ticket.id] = "ok"

    db.commit()
    ok = sum(1 for v in outcomes.values() if v == "ok")
    logger.info("Bulk-diagnosed {}/{} trouble tickets", ok, len(tickets))
    return outcomes


async def analyze_open_tickets(db: Session) -> str:
    """Batch AI root-cause analysis: group open report-button tickets.

    Projects the 50 newest SUBMITTED/IN_PROGRESS tickets, asks Claude to group them
    by root cause, then upserts RootCauseGroup rows (identity = exact title match,
    suggested_fix backfilled when empty) and links each ticket to its group.

    Returns "no_tickets" (nothing to analyze), "no_result" (AI unavailable or empty
    answer; nothing persisted), or "ok" (groups committed). Moved from
    routers/error_reports.analyze_tickets, which delegates here and renders.
    """
    from sqlalchemy import desc

    from ..constants import TicketSource, TicketStatus
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
        return "no_tickets"

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
        return "no_result"

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
    return "ok"
