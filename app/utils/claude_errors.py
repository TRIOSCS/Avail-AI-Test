"""Claude API error types for distinguishable failure handling.

Called by: claude_client.py, all AI feature callers
Depends on: nothing
"""


class ClaudeError(Exception):
    """Base exception for Claude API failures."""

    pass


class ClaudeAuthError(ClaudeError):
    """API key missing or invalid (401/403)."""

    pass


class ClaudeRateLimitError(ClaudeError):
    """Rate limited (429) — caller should back off."""

    pass


class ClaudeServerError(ClaudeError):
    """Claude API returned 5xx — transient, may retry."""

    pass


class ClaudeUnavailableError(ClaudeError):
    """API key not configured — feature should degrade gracefully."""

    pass
