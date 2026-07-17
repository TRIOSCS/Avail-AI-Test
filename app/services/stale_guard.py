"""stale_guard.py — Stale-edit guard for the Approvals Workspace (design D5).

Purpose: Optimistic-concurrency check for edit forms. Forms embed a hidden
         expected_updated_at token (stale_token — the NARROWEST edited object's
         updated_at as ISO-8601 UTC, exposed to templates as the ``stale_token``
         Jinja2 global); handlers call ensure_not_stale before applying edits.
         A mismatch raises StaleEditError, which the route turns into
         stale_conflict_response(): a non-destructive 409 (HX-Reswap: none) whose
         HX-Trigger showToast tells the user "This changed — refresh." Nothing is
         written, nothing is swapped.

         Tokens serialize identically on SQLite (naive datetimes) and PostgreSQL
         (aware) — naive values are assumed UTC (risk 8). An empty/missing token
         SKIPS the check (legacy forms and first-render races never hard-fail).

Called by: buy-plan / QP / prepayment edit routes (Phase 2 wiring), templates via
           the ``stale_token`` Jinja2 global (app/template_env.py)
Depends on: fastapi.responses.HTMLResponse (conflict response helper only)
"""

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import HTMLResponse

STALE_TOAST_MESSAGE = "This changed — refresh."


class StaleEditError(Exception):
    """The object changed since the form was rendered — the edit must not apply."""

    def __init__(self, expected: str, actual: str):
        self.expected = expected
        self.actual = actual
        super().__init__(f"Stale edit: expected updated_at {expected!r}, actual {actual!r}")


def stale_token(obj: Any) -> str:
    """The object's optimistic-concurrency token: updated_at as ISO-8601 UTC.

    "" when the object has no updated_at (never updated / legacy row) — which
    ensure_not_stale treats as skip-the-check on round-trip. Naive datetimes are
    assumed UTC so SQLite and PostgreSQL produce the same token for the same instant.
    """
    updated_at = getattr(obj, "updated_at", None)
    if not isinstance(updated_at, datetime):
        return ""
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at.astimezone(UTC).isoformat()


def ensure_not_stale(obj: Any, expected: str | None) -> None:
    """Raise StaleEditError when *obj* changed since *expected* was rendered.

    expected empty/None SKIPS the check entirely (D5 — legacy forms, objects that
    have never been updated, and callers that opt out never false-positive).
    """
    if not expected:
        return
    actual = stale_token(obj)
    if actual != expected:
        raise StaleEditError(expected=expected, actual=actual)


def stale_conflict_response() -> HTMLResponse:
    """The canonical 409 for a caught StaleEditError.

    Non-destructive: HX-Reswap none (the form stays on screen, nothing swaps) +
    HX-Trigger showToast (the bridge in htmx_app.js renders it; the generic
    htmx:responseError handler skips 409 so the toast isn't doubled).
    """
    return HTMLResponse(
        content="",
        status_code=409,
        headers={
            "HX-Reswap": "none",
            "HX-Trigger": json.dumps({"showToast": {"message": STALE_TOAST_MESSAGE, "type": "warning"}}),
        },
    )
