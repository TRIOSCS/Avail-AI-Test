"""Patch generator — uses Claude API to generate search/replace patches.

Given a ticket's diagnosis and affected files, reads the source files and
asks Claude to produce exact search/replace patches.

Called by: services/execution_service.py
Depends on: utils/claude_client.py (claude_structured), app source files
"""

import os
from pathlib import Path

from loguru import logger

from app.utils.claude_client import claude_structured

# Base directory for reading source files inside Docker
SOURCE_DIR = Path(os.environ.get("APP_SOURCE_DIR", "/app"))

PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Relative path from project root"},
                    "search": {"type": "string", "description": "Exact string to find (copy-paste from source)"},
                    "replace": {"type": "string", "description": "Replacement string"},
                    "explanation": {"type": "string", "description": "Why this change fixes the issue"},
                },
                "required": ["file", "search", "replace", "explanation"],
            },
        },
        "summary": {"type": "string", "description": "One-line summary of the fix"},
    },
    "required": ["patches", "summary"],
}

SYSTEM_PROMPT = """You are a precise code fixer. Given a bug diagnosis and the current source files,
produce exact search/replace patches. Rules:
- The 'search' string must be an EXACT copy-paste from the current file content
- Keep patches minimal — change only what's needed to fix the issue
- Never add comments like "// fixed" or "# patched"
- If you can't find the right code to change, return an empty patches array
- Each patch targets ONE contiguous block of code"""


def validate_patches(patches: list[dict], source_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Validate that all patch search strings exist in their target files.

    Returns (ok, errors) where ok=True means all patches are valid.
    """
    base = source_dir or SOURCE_DIR
    errors = []

    for i, patch in enumerate(patches):
        rel_path = patch.get("file", "")
        search = patch.get("search", "")

        if not rel_path:
            errors.append(f"Patch [{i}]: missing 'file' field")
            continue

        target = base / rel_path
        if not target.is_file():
            errors.append(f"Patch [{i}]: file not found: {rel_path}")
            continue

        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"Patch [{i}]: cannot read {rel_path}: {exc}")
            continue

        if search and search not in content:
            preview = search[:80].replace("\n", "\\n")
            errors.append(
                f"Patch [{i}]: search string not found in {rel_path}: '{preview}...'"
            )

    return (len(errors) == 0, errors)


async def generate_patches(
    title: str,
    diagnosis: dict,
    category: str,
    affected_files: list[str],
) -> dict | None:
    """Generate search/replace patches for a diagnosed ticket.

    Returns dict with 'patches' and 'summary', or None on failure.
    """
    # Read source files
    file_contents = {}
    for rel_path in affected_files[:5]:  # Cap at 5 files
        full_path = SOURCE_DIR / rel_path
        if full_path.is_file():
            try:
                content = full_path.read_text(encoding="utf-8")
                if len(content) > 50_000:
                    content = content[:50_000] + "\n... (truncated)"
                file_contents[rel_path] = content
            except Exception:
                logger.warning("patch_generator: cannot read {}", rel_path)

    if not file_contents:
        logger.warning("patch_generator: no readable files for {}", title)
        return None

    # Build prompt
    files_section = ""
    for path, content in file_contents.items():
        files_section += f"\n### {path}\n```\n{content}\n```\n"

    user_prompt = f"""## Bug Report
**Title:** {title}
**Category:** {category}

## Diagnosis
**Root cause:** {diagnosis.get('root_cause', 'Unknown')}
**Fix approach:** {diagnosis.get('fix_approach', 'Unknown')}

## Current Source Files
{files_section}

Generate the minimal search/replace patches to fix this issue."""

    logger.info("patch_generator: generating patches for '{}' ({} files)", title, len(file_contents))

    try:
        result = await claude_structured(
            user_prompt,
            PATCH_SCHEMA,
            system=SYSTEM_PROMPT,
            model_tier="smart",
        )
    except Exception as e:
        logger.error("patch_generator: Claude API call failed: {}", e)
        return None

    if not result or not isinstance(result, dict):
        logger.warning("patch_generator: empty or invalid response")
        return None

    patches = result.get("patches", [])
    if patches:
        ok, errors = validate_patches(patches)
        if not ok:
            for err in errors:
                logger.warning("patch_generator: validation failed: {}", err)
            logger.warning("patch_generator: rejecting {} invalid patches for '{}'", len(patches), title)
            return None

    logger.info("patch_generator: generated {} patches for '{}'", len(patches), title)
    return result
