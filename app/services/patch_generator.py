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


def _resolve_file(rel_path: str, base: Path | None = None) -> str | None:
    """Resolve a possibly-incorrect relative path to an actual file.

    Handles common diagnosis mistakes: singular/plural, file-vs-package.
    Returns the corrected relative path or None.
    """
    src = base or SOURCE_DIR
    candidate = src / rel_path
    if candidate.is_file():
        return rel_path

    parent = candidate.parent
    stem = candidate.stem
    suffix = candidate.suffix or ".py"

    if not parent.is_dir():
        return None

    # Check if it's a package (directory with __init__.py)
    pkg_init = candidate.with_suffix("") / "__init__.py"
    if pkg_init.is_file():
        return str(pkg_init.relative_to(src))

    # Try singular/plural variants
    variants = []
    if stem.endswith("s"):
        variants.append(stem[:-1])
        if stem.endswith("ies"):
            variants.append(stem[:-3] + "y")
        if stem.endswith("es"):
            variants.append(stem[:-2])
    else:
        variants.append(stem + "s")
        if stem.endswith("y"):
            variants.append(stem[:-1] + "ies")

    for v in variants:
        alt = parent / (v + suffix)
        if alt.is_file():
            return str(alt.relative_to(src))

    # Case-insensitive match in the same directory
    for f in parent.iterdir():
        if f.is_file() and f.stem.lower() == stem.lower() and f.suffix == suffix:
            return str(f.relative_to(src))

    return None


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
- CRITICAL: The 'search' string must be COPIED VERBATIM from the source file shown below — character for character, including whitespace and indentation. Do NOT paraphrase, summarize, or guess at the code.
- Before writing a search string, find the exact lines in the provided source and copy them exactly.
- Keep patches minimal — change only what's needed to fix the issue
- Never add comments like "// fixed" or "# patched" or "// BUG:"
- If the source file is truncated and you cannot find the exact code to change, return an empty patches array rather than guessing
- Each patch targets ONE contiguous block of code
- The search string should be short (5-15 lines) but unique enough to match only once in the file"""


def validate_patches(patches: list[dict], source_dir: Path | None = None) -> tuple[bool, list[str]]:
    """Validate that all patch search strings exist in their target files.

    Returns (ok, errors) where ok=True means all patches are valid.
    """
    base = source_dir or SOURCE_DIR
    errors = []

    for i, patch in enumerate(patches):
        if not isinstance(patch, dict):
            errors.append(f"Patch [{i}]: expected dict, got {type(patch).__name__}")
            continue
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
    # Read source files, resolving possibly-incorrect paths
    file_contents = {}
    for rel_path in affected_files[:5]:  # Cap at 5 files
        resolved = _resolve_file(rel_path)
        if not resolved:
            logger.warning("patch_generator: cannot resolve path {}", rel_path)
            continue
        if resolved != rel_path:
            logger.info("patch_generator: resolved {} -> {}", rel_path, resolved)
        full_path = SOURCE_DIR / resolved
        if full_path.is_file():
            try:
                content = full_path.read_text(encoding="utf-8")
                if len(content) > 50_000:
                    content = content[:50_000] + "\n... (truncated)"
                file_contents[resolved] = content
            except Exception:
                logger.warning("patch_generator: cannot read {}", resolved)

    if not file_contents:
        logger.warning("patch_generator: no readable files for {}", title)
        return None

    # Build prompt with line numbers for reference (but search strings must NOT include line numbers)
    files_section = ""
    for path, content in file_contents.items():
        numbered = "\n".join(f"{i+1:4d}| {line}" for i, line in enumerate(content.split("\n")))
        files_section += f"\n### {path}\n```\n{numbered}\n```\n"

    user_prompt = f"""## Bug Report
**Title:** {title}
**Category:** {category}

## Diagnosis
**Root cause:** {diagnosis.get('root_cause', 'Unknown')}
**Fix approach:** {diagnosis.get('fix_approach', 'Unknown')}

## Current Source Files
{files_section}

Generate the minimal search/replace patches to fix this issue.

IMPORTANT: The source files above show line numbers (e.g. "  42| code here") for reference only.
Your 'search' strings must contain ONLY the raw code WITHOUT line number prefixes.
Copy the exact code from after the "| " separator."""

    logger.info("patch_generator: generating patches for '{}' ({} files)", title, len(file_contents))

    try:
        result = await claude_structured(
            user_prompt,
            PATCH_SCHEMA,
            system=SYSTEM_PROMPT,
            model_tier="smart",
            max_tokens=4096,
            timeout=120,
        )
    except Exception as e:
        logger.error("patch_generator: Claude API call failed: {}", e)
        return None

    if not result or not isinstance(result, dict):
        logger.warning("patch_generator: empty or invalid response")
        return None

    patches = result.get("patches", [])
    logger.debug("patch_generator: raw result keys={}, patches_count={}", list(result.keys()), len(patches))
    if patches:
        ok, errors = validate_patches(patches)
        if not ok:
            for err in errors:
                logger.warning("patch_generator: validation failed: {}", err)
            logger.warning("patch_generator: rejecting {} invalid patches for '{}'", len(patches), title)
            return None

    logger.info("patch_generator: generated {} patches for '{}'", len(patches), title)
    return result
