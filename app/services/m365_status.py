"""m365_status.py — single source of truth for mailbox-sync error copy.

Background jobs that touch Microsoft 365 (token refresh, inbox scan, Graph
subscription renewal) record a failure reason on ``User.m365_error_reason``.
That field is surfaced verbatim in the Settings → Profile mailbox-sync card and
the disconnected banner, so it MUST be a user-meaningful, actionable sentence —
never a raw ``str(exception)`` stack message.

This module classifies an arbitrary failure into a small, stable vocabulary and
maps each category to (a) the friendly reason string we persist and (b) the
next step the user can actually take. The status reader
(``activity_service.get_inbox_sync_status``) reverse-maps the persisted reason
back to an action so the template can show a specific, accurate message.

Called by:
    app/utils/token_manager.py      (token refresh)
    app/jobs/core_jobs.py           (scheduled token refresh + inbox scan)
    app/services/webhook_service.py (Graph subscription renewal)
    app/services/activity_service.py (status reader → template)
Depends on: nothing app-specific (kept dependency-free to avoid import cycles).
"""

from __future__ import annotations

from enum import StrEnum


class M365ErrorCategory(StrEnum):
    """Why mailbox sync is unhealthy — drives the user-facing copy + next step."""

    # Sign-in / token is dead — only a reconnect fixes it.
    AUTH = "auth"
    # Transient network / timeout / Graph 5xx — self-heals on the next cycle.
    TRANSIENT = "transient"
    # Graph webhook subscription renewal failing — email tracking degraded.
    SUBSCRIPTION = "subscription"


# What the user should do about each category. ``None`` = nothing, it self-heals.
class M365ErrorAction(StrEnum):
    RECONNECT = "reconnect"  # send them to /auth/login
    WAIT = "wait"  # transient — syncing resumes automatically


# Canonical persisted reason strings. These are the EXACT values written to
# ``User.m365_error_reason`` so the reader can reverse-map them to an action and
# so a successful renewal can clear its own message without clobbering others.
REASON_AUTH = "Microsoft 365 sign-in expired — reconnect to resume mailbox sync."
REASON_TRANSIENT = "Temporary connection issue reaching Microsoft 365 — sync will resume automatically."
# Subscription copy is unchanged from its original wording for backward
# compatibility with webhook_service's clear-on-success guard and its tests.
REASON_SUBSCRIPTION = "Email tracking degraded — Graph subscription renewal failing"

_CATEGORY_REASON: dict[M365ErrorCategory, str] = {
    M365ErrorCategory.AUTH: REASON_AUTH,
    M365ErrorCategory.TRANSIENT: REASON_TRANSIENT,
    M365ErrorCategory.SUBSCRIPTION: REASON_SUBSCRIPTION,
}

_REASON_ACTION: dict[str, M365ErrorAction | None] = {
    REASON_AUTH: M365ErrorAction.RECONNECT,
    REASON_TRANSIENT: M365ErrorAction.WAIT,
    REASON_SUBSCRIPTION: None,  # informational — nothing the user can do
}

# Substrings (lowercased) in a raw error that signal a dead sign-in/token.
# Everything else is treated as transient (timeouts, 5xx, network blips, parse
# errors) — a scary raw message must never reach the UI.
_AUTH_SIGNALS = (
    "invalid_grant",
    "invalid_client",
    "token refresh failed",
    "token expired",
    "unauthorized",
    "aadsts",
    "401",
    "403",
    "interaction_required",
    "consent",
)


def classify_m365_error(err: object) -> M365ErrorCategory:
    """Map an exception or raw message to a stable error category.

    Anything that looks like a dead sign-in/token → AUTH (needs reconnect). Everything
    else (timeouts, network, Graph 5xx, parse errors, unknown) → TRANSIENT, because
    surfacing a raw stack string as a permanent error and telling the user to reconnect
    is both inaccurate and unactionable.
    """
    text = str(err).lower()
    if any(sig in text for sig in _AUTH_SIGNALS):
        return M365ErrorCategory.AUTH
    return M365ErrorCategory.TRANSIENT


def reason_for(err: object) -> str:
    """Friendly, user-facing reason string for a raw error — never raw text."""
    return _CATEGORY_REASON[classify_m365_error(err)]


def action_for_reason(reason: str | None) -> M365ErrorAction | None:
    """Reverse-map a persisted reason to the user's next step.

    Returns ``None`` for no-reason or any unrecognized legacy string. An
    unrecognized reason is treated as transient (WAIT) rather than RECONNECT so
    a stale legacy value can never wrongly tell the user to reconnect.
    """
    if not reason:
        return None
    if reason in _REASON_ACTION:
        return _REASON_ACTION[reason]
    return M365ErrorAction.WAIT
