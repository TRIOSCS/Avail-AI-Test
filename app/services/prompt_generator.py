"""Prompt generator — builds category-specific fix prompts for the self-heal agent.

Takes a diagnosed ticket and produces a structured prompt with base safety
constraints plus category-specific guidance. The prompt is stored on the
ticket for review before execution.

Called by: services/diagnosis_service.py (after diagnosis)
Depends on: models/trouble_ticket.py, services/file_mapper.py
"""

from app.services.file_mapper import STABLE_FILES


# Files the agent is never allowed to modify
_BLOCKED_FILES = ", ".join(sorted(STABLE_FILES))

BASE_CONSTRAINTS = f"""## Constraints (MUST follow)
- NEVER modify these files: {_BLOCKED_FILES}
- NEVER run destructive operations (DROP TABLE, DELETE FROM without WHERE, rm -rf)
- NEVER modify authentication, session, or encryption logic
- ALWAYS write or update tests for every change
- ALWAYS use existing patterns from the codebase (check similar files first)
- If the fix requires a database migration, create an Alembic revision
- Keep changes minimal — fix the bug, nothing more
- Use loguru for logging, never print()
"""

CATEGORY_RULES = {
    "ui": """## UI Bug Rules
- Check app/static/app.js, app/static/crm.js, app/static/tickets.js for JS issues
- Check app/static/styles.css for CSS issues
- Check app/templates/index.html for HTML structure issues
- Use DOM methods (createElement, textContent) — never innerHTML with user data
- Test across the main views (list, detail, form) after changes
- Preserve existing CSS variable usage (--blue, --red, --border, etc.)
""",
    "api": """## API Bug Rules
- Router files are thin — business logic goes in app/services/
- Check request validation in Pydantic schemas (app/schemas/)
- Error responses use: {{"error": str, "status_code": int, "request_id": str}}
- Use db.get(Model, id) not db.query(Model).get(id) (SQLAlchemy 2.0)
- Check for N+1 queries — use joinedload or subqueryload where needed
- Verify the endpoint works with the TestClient in tests
""",
    "data": """## Data Bug Rules
- Check model definitions in app/models/ for column types and constraints
- If adding/modifying columns, create an Alembic migration
- Always include a downgrade function in migrations
- Check for FK constraints and cascade behavior
- Test with the SQLite test DB (some PG features need adaptation)
- Verify data integrity after the fix
""",
    "performance": """## Performance Bug Rules
- Profile the slow path before changing code
- Add database indexes via Alembic migration if needed
- Consider caching with @cached_endpoint decorator (app/cache/decorators.py)
- Use asyncio.to_thread() for blocking DB calls in async endpoints
- Check for missing pagination (default 100/page pattern)
- Avoid loading large result sets into memory
""",
    "other": """## General Bug Rules
- Identify the exact file and function causing the issue
- Check git blame for recent changes that may have introduced the bug
- Look for similar patterns in the codebase to guide the fix
- When uncertain, prefer the safer/simpler approach
""",
}


def generate_fix_prompt(
    ticket_id: int,
    title: str,
    description: str,
    category: str,
    diagnosis: dict,
    relevant_files: list[dict] | None = None,
) -> str:
    """Build a fix prompt from diagnosis results.

    Returns a structured prompt string ready for the AI agent.
    """
    category_rules = CATEGORY_RULES.get(category, CATEGORY_RULES["other"])

    # Build file context section
    file_section = ""
    if relevant_files:
        lines = []
        for f in relevant_files:
            flag = " [STABLE — DO NOT MODIFY]" if f.get("stable") else ""
            lines.append(f"- {f['path']} ({f['role']}, confidence: {f['confidence']:.1f}){flag}")
        file_section = "## Relevant Files\n" + "\n".join(lines) + "\n"

    # Build diagnosis context
    root_cause = diagnosis.get("root_cause", "Unknown")
    fix_approach = diagnosis.get("fix_approach", "Not specified")
    test_strategy = diagnosis.get("test_strategy", "Write appropriate tests")
    affected = diagnosis.get("affected_files", [])
    affected_section = ""
    if affected:
        affected_section = "## Affected Files (from diagnosis)\n" + "\n".join(f"- {f}" for f in affected) + "\n"

    prompt = f"""# Fix: {title}

## Ticket #{ticket_id}
**Description:** {description}

## Diagnosis
**Root Cause:** {root_cause}
**Fix Approach:** {fix_approach}
**Test Strategy:** {test_strategy}

{file_section}
{affected_section}
{BASE_CONSTRAINTS}
{category_rules}
## Instructions
1. Read the affected files to understand current behavior
2. Write a failing test that reproduces the bug
3. Implement the minimal fix described above
4. Run the test to verify it passes
5. Run the full test suite: pytest tests/ -v
6. If all tests pass, respond with <promise>FIXED</promise>
7. If you cannot fix it safely, respond with <promise>ESCALATE</promise>
"""
    return prompt.strip()


def generate_prompt_for_ticket(ticket) -> str:
    """Convenience wrapper that takes a TroubleTicket ORM object."""
    diagnosis = ticket.diagnosis or {}
    relevant_files = []
    if ticket.file_mapping:
        relevant_files = [{"path": f, "role": "mapped", "confidence": 0.7, "stable": f in STABLE_FILES}
                          for f in ticket.file_mapping]

    return generate_fix_prompt(
        ticket_id=ticket.id,
        title=ticket.title,
        description=ticket.description or "",
        category=ticket.category or "other",
        diagnosis=diagnosis,
        relevant_files=relevant_files,
    )
