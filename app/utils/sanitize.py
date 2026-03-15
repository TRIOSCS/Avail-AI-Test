"""sanitize.py — Input sanitization utilities for XSS prevention.

Purpose: Strip dangerous HTML/script content from user-entered and ingested data
         before storing in the database. Prevents stored XSS payload persistence.

Called by: routers (offers, quotes, requisitions), connectors (email_mining)
Depends on: standard library only (no external dependencies)
"""

import html
import re

# Pattern to match HTML tags including script, style, event handlers
_TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE)
# Pattern to match javascript: and data: URIs
_JS_URI_RE = re.compile(r"(javascript|data)\s*:", re.IGNORECASE)
# Pattern to match event handler attributes
_EVENT_RE = re.compile(r"\bon\w+\s*=", re.IGNORECASE)


def sanitize_text(value: str | None) -> str | None:
    """Sanitize a text string by escaping HTML entities and stripping tags.

    Returns None if input is None, otherwise returns a safe string. Does NOT modify
    numeric strings or purely alphanumeric content.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    # Strip HTML tags
    cleaned = _TAG_RE.sub("", value)
    # Neutralize javascript: and data: URIs
    cleaned = _JS_URI_RE.sub("_blocked_:", cleaned)
    # Neutralize event handlers
    cleaned = _EVENT_RE.sub("_blocked_=", cleaned)
    # Escape remaining HTML entities
    cleaned = html.escape(cleaned, quote=True)
    return cleaned


def sanitize_dict(data: dict, fields: list[str]) -> dict:
    """Sanitize specific string fields in a dictionary.

    Only processes fields that exist and are strings. Returns the dict with sanitized
    values (mutates in place for efficiency).
    """
    for field in fields:
        if field in data and isinstance(data[field], str):
            data[field] = sanitize_text(data[field])
    return data
