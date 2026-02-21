"""AI Trouble Prompt Generator — Translates user trouble reports into Claude Code prompts.

Uses Gradient (Sonnet) to convert a plain-language trouble report + auto-captured context
into a structured prompt that an admin can paste into Claude Code CLI.

Called by: app/routers/error_reports.py
Depends on: app/services/gradient_service.py
"""

from loguru import logger

from .gradient_service import gradient_json

# Map frontend views to the files an engineer would need to look at
VIEW_FILE_MAP = {
    "rfq": "app/static/app.js (RFQ section), app/routers/requisitions.py, app/routers/rfq.py",
    "sourcing": "app/static/app.js (sourcing section), app/routers/sources.py, app/services/search_service.py",
    "archive": "app/static/app.js (archive section), app/routers/requisitions.py",
    "crm": "app/static/crm.js, app/routers/crm.py",
    "companies": "app/static/crm.js (companies section), app/routers/crm.py",
    "quotes": "app/static/crm.js (quotes section), app/routers/crm.py",
    "vendors": "app/static/app.js (vendors section), app/routers/vendors.py",
    "settings": "app/static/crm.js (settings section), app/routers/admin.py",
    "pipeline": "app/static/crm.js (pipeline section), app/routers/crm.py",
    "activity": "app/static/crm.js (activity section), app/services/activity_service.py",
}

SYSTEM_PROMPT = """\
You are a senior engineer triaging trouble reports for AvailAI, an electronic component \
sourcing platform. Your job is to translate a user's plain-language trouble report into a \
concise, actionable prompt that can be pasted into Claude Code CLI to investigate and fix the issue.

Architecture context:
- Stack: FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Jinja2 + vanilla JS
- Frontend: two JS files — app/static/app.js (search, RFQ, vendors, upload) and \
app/static/crm.js (CRM, quotes, activity, settings). Single template: app/templates/index.html
- Backend: app/routers/ for HTTP endpoints, app/services/ for business logic, app/models/ for ORM
- Tests: pytest with in-memory SQLite, run with TESTING=1

Return ONLY valid JSON with exactly two keys:
- "title": a short (max 80 chars) summary of the issue suitable for a ticket title
- "prompt": a multi-line Claude Code prompt (200-500 words) that includes:
  1. What the user reported (in their words, summarized)
  2. Relevant file paths to investigate based on the view/context
  3. Console errors if present (quoted)
  4. Suggested investigation steps
  5. Any reproduction context (URL, view, browser)
"""


async def generate_trouble_prompt(
    *,
    user_message: str,
    current_url: str | None = None,
    current_view: str | None = None,
    browser_info: str | None = None,
    screen_size: str | None = None,
    console_errors: str | None = None,
    page_state: str | None = None,
    has_screenshot: bool = False,
    reporter_name: str | None = None,
) -> dict | None:
    """Generate a Claude Code prompt from a trouble report.

    Returns {"title": str, "prompt": str} or None on failure.
    """
    # Build the relevant files hint from the view
    relevant_files = ""
    if current_view:
        view_key = current_view.lower().split("/")[-1] if "/" in (current_view or "") else (current_view or "").lower()
        relevant_files = VIEW_FILE_MAP.get(view_key, "")

    parts = [f"User ({reporter_name or 'unknown'}) reported:\n\"{user_message}\""]

    if current_url:
        parts.append(f"URL: {current_url}")
    if current_view:
        parts.append(f"View: {current_view}")
    if relevant_files:
        parts.append(f"Relevant files: {relevant_files}")
    if browser_info:
        parts.append(f"Browser: {browser_info}")
    if screen_size:
        parts.append(f"Screen: {screen_size}")
    if console_errors and console_errors != "[]":
        parts.append(f"Console errors: {console_errors}")
    if page_state:
        parts.append(f"Page state: {page_state}")
    if has_screenshot:
        parts.append("A screenshot was captured with the report.")

    user_prompt = "\n".join(parts)

    try:
        result = await gradient_json(
            user_prompt,
            system=SYSTEM_PROMPT,
            model_tier="default",
            temperature=0.3,
            max_tokens=1500,
        )
        if not result or not isinstance(result, dict):
            logger.warning("Gradient returned non-dict for trouble prompt")
            return None
        title = result.get("title")
        prompt = result.get("prompt")
        if not title or not prompt:
            logger.warning("Gradient response missing title or prompt: {}", result)
            return None
        return {"title": str(title)[:255], "prompt": str(prompt)}
    except Exception as e:
        logger.warning("Failed to generate trouble prompt: {}", e)
        return None
