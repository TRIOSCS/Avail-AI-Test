"""Create Prompt — turn a trouble/feature ticket into a ready-to-paste Claude Code
prompt.

Given a ticket's captured runtime context plus the admin's (Michael's) notes, this
asks Claude to WRITE a complete, self-contained prompt a developer pastes into a
Claude Code CLI session. The prompt is kind-aware:

  - BUG      → a fix task: page/route, console + network errors, page_state, a
               screenshot reference, reproduction, the reporter's description, and
               Michael's notes.
  - FEATURE  → a build task: the page/surface the request came from, the feature
               description + the "why", and Michael's notes — nudging the
               brainstorm → plan → build flow rather than diving straight to code.

Reuses the same Anthropic client + model routing as the diagnosis service
(claude_text, "smart" tier → settings.anthropic_model — never a hardcoded model
string) and persists into the existing ``generated_prompt`` column, so the ticket
detail's copy-to-clipboard box renders it for either kind.

Called by: routers/error_reports.py (generate-prompt endpoint)
Depends on: utils/claude_client.claude_text, constants.TicketType, models/trouble_ticket
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import TicketType
from ..models.trouble_ticket import TroubleTicket
from ..utils.claude_client import claude_text

# ── Tunables ─────────────────────────────────────────────────────────────────
PROMPT_MODEL_TIER = "smart"  # Sonnet — the prompt quality is the whole value
PROMPT_MAX_TOKENS = 1500
DESCRIPTION_TRUNC = 2000
NOTES_TRUNC = 2000
CONSOLE_ERRORS_TRUNC = 4000
NETWORK_ERRORS_TRUNC = 2000
PAGE_STATE_TRUNC = 2000

_BUG_SYSTEM = (
    "You are a senior full-stack engineer writing a task brief for another engineer who "
    "will fix a bug in a FastAPI + SQLAlchemy + HTMX + Alpine.js + Jinja2 + Tailwind web "
    "app using the Claude Code CLI. Write ONE complete, self-contained prompt they can "
    "paste directly into Claude Code. Frame it as a fix task: state the symptom, the page/"
    "route, the observed console/network errors, reproduction steps, and a concrete "
    "acceptance check. Weave in the admin's notes as the authoritative intent. Be precise "
    "and actionable; do not include large code blocks. Output only the prompt text."
)

_FEATURE_SYSTEM = (
    "You are a senior product engineer writing a task brief for another engineer who will "
    "build a new feature in a FastAPI + SQLAlchemy + HTMX + Alpine.js + Jinja2 + Tailwind "
    "web app using the Claude Code CLI. Write ONE complete, self-contained prompt they can "
    "paste directly into Claude Code. Frame it as a build task and explicitly nudge the "
    "brainstorm → plan → build flow: understand the request and its 'why', explore the "
    "existing page/surface it was requested from, propose a plan, then implement. Weave in "
    "the admin's notes as the authoritative intent. Be precise and actionable; do not "
    "include large code blocks. Output only the prompt text."
)


def _build_bug_prompt(ticket: TroubleTicket) -> str:
    """Assemble the user message for a BUG ticket from its captured context + notes."""
    lines: list[str] = [f"Bug ticket {ticket.ticket_number}."]
    if ticket.description:
        lines.append(f"Reporter's description: {ticket.description[:DESCRIPTION_TRUNC]}")
    if ticket.current_page:
        lines.append(f"Page/route: {ticket.current_page}")
    if ticket.current_view:
        lines.append(f"Current view: {ticket.current_view}")
    if ticket.browser_info:
        lines.append(f"Browser: {ticket.browser_info}")
    if ticket.console_errors:
        lines.append(f"JS/console errors: {ticket.console_errors[:CONSOLE_ERRORS_TRUNC]}")
    if ticket.network_errors:
        lines.append(f"Network log: {str(ticket.network_errors)[:NETWORK_ERRORS_TRUNC]}")
    if ticket.page_state:
        lines.append(f"Page state: {ticket.page_state[:PAGE_STATE_TRUNC]}")
    if ticket.screenshot_path or ticket.screenshot_b64:
        lines.append("A screenshot of the page at the time of the report is attached to the ticket.")
    if ticket.admin_notes:
        lines.append(f"Admin notes (authoritative intent): {ticket.admin_notes[:NOTES_TRUNC]}")
    return "\n".join(lines)


def _build_feature_prompt(ticket: TroubleTicket) -> str:
    """Assemble the user message for a FEATURE ticket from its context + notes."""
    lines: list[str] = [f"Feature request {ticket.ticket_number}."]
    if ticket.description:
        lines.append(f"Requester's description (what + why): {ticket.description[:DESCRIPTION_TRUNC]}")
    if ticket.current_page:
        lines.append(f"Requested from page/surface: {ticket.current_page}")
    if ticket.current_view:
        lines.append(f"Current view: {ticket.current_view}")
    if ticket.screenshot_path or ticket.screenshot_b64:
        lines.append("A screenshot of the page it was requested from is attached to the ticket.")
    if ticket.admin_notes:
        lines.append(f"Admin notes (authoritative intent): {ticket.admin_notes[:NOTES_TRUNC]}")
    return "\n".join(lines)


async def generate_ticket_prompt(db: Session, ticket: TroubleTicket) -> str | None:
    """Generate a kind-aware, notes-aware Claude Code prompt and persist it.

    Writes ``ticket.generated_prompt`` and commits. Returns the prompt text, or
    ``None`` if Claude returned nothing. Raises ClaudeUnavailableError / ClaudeError
    on AI failure (the caller maps these to a friendly inline message).
    """
    is_feature = ticket.ticket_type == TicketType.FEATURE
    system = _FEATURE_SYSTEM if is_feature else _BUG_SYSTEM
    user_prompt = _build_feature_prompt(ticket) if is_feature else _build_bug_prompt(ticket)

    text = await claude_text(
        user_prompt,
        system=system,
        model_tier=PROMPT_MODEL_TIER,
        max_tokens=PROMPT_MAX_TOKENS,
    )
    if not text:
        return None

    ticket.generated_prompt = text.strip()
    ticket.updated_at = datetime.now(UTC)
    db.commit()
    logger.info("Generated {} prompt for ticket {}", ticket.ticket_type, ticket.ticket_number)
    return ticket.generated_prompt
