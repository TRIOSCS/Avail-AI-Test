"""Patch generator — uses Claude API to generate search/replace patches for bug fixes.

Takes a diagnosed trouble ticket and produces structured patches (file, search,
replace, explanation) that can be reviewed and applied by the execution service.

Called by: services/execution_service.py (during self-heal execution)
Depends on: utils/claude_client.py (claude_structured), services/prompt_generator.py (constraints/rules)
"""

import os
from typing import Any

from loguru import logger

from app.services.prompt_generator import BASE_CONSTRAINTS, CATEGORY_RULES
from app.utils.claude_client import claude_structured

PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Relative file path to modify"},
                    "search": {"type": "string", "description": "Exact text to find in the file"},
                    "replace": {"type": "string", "description": "Replacement text"},
                    "explanation": {"type": "string", "description": "Why this change fixes the bug"},
                },
                "required": ["file", "search", "replace", "explanation"],
            },
        },
        "summary": {"type": "string", "description": "One-line summary of the overall fix"},
    },
    "required": ["patches", "summary"],
}

_MAX_FILES = 5
_MAX_FILE_CHARS = 15000


def _read_file(path: str) -> str | None:
    """Read a source file, trying Docker path first then cwd."""
    candidates = [
        os.path.join("/app", path),
        os.path.join(os.getcwd(), path),
    ]
    for candidate in candidates:
        try:
            with open(candidate) as f:
                content = f.read()
            if len(content) > _MAX_FILE_CHARS:
                content = content[:_MAX_FILE_CHARS] + "\n... (truncated)"
            return content
        except (OSError, IOError):
            continue
    logger.warning("patch_generator: could not read file {}", path)
    return None


async def generate_patches(
    title: str,
    diagnosis: dict,
    category: str,
    affected_files: list[str],
) -> dict | None:
    """Generate search/replace patches for a diagnosed bug.

    Args:
        title: Ticket title describing the bug
        diagnosis: Dict with root_cause, fix_approach, etc.
        category: Bug category (ui, api, data, performance, other)
        affected_files: List of relative file paths to examine

    Returns:
        Dict with 'patches' array and 'summary', or None on failure.
    """
    # Limit files and read contents
    files_to_read = affected_files[:_MAX_FILES]
    file_contents: dict[str, str] = {}
    for path in files_to_read:
        content = _read_file(path)
        if content:
            file_contents[path] = content

    # Build file context section
    file_section = ""
    for path, content in file_contents.items():
        file_section += f"\n### {path}\n```\n{content}\n```\n"

    # Get category-specific rules
    category_rules = CATEGORY_RULES.get(category, CATEGORY_RULES["other"])

    root_cause = diagnosis.get("root_cause", "Unknown")
    fix_approach = diagnosis.get("fix_approach", "Not specified")

    prompt = f"""# Bug Fix Request: {title}

## Diagnosis
**Root Cause:** {root_cause}
**Fix Approach:** {fix_approach}

## Source Files
{file_section}

{BASE_CONSTRAINTS}
{category_rules}

## Task
Generate minimal search/replace patches to fix this bug. Each patch must contain
the EXACT text to search for (copy-paste from the source above) and the replacement.
Keep changes minimal — fix the bug, nothing more.
"""

    logger.info("patch_generator: generating patches for '{}' ({} files)", title, len(file_contents))

    result = await claude_structured(
        prompt=prompt,
        schema=PATCH_SCHEMA,
        system="You are a senior Python/FastAPI developer. Generate precise search/replace patches to fix bugs. Only output valid patches with exact string matches from the source code provided.",
        model_tier="smart",
        max_tokens=4096,
    )

    if result is None:
        logger.warning("patch_generator: Claude API returned None for '{}'", title)
        return None

    logger.info(
        "patch_generator: generated {} patches for '{}'",
        len(result.get("patches", [])),
        title,
    )
    return result
