# Request-scoped contextvar holding the current authenticated user's id.
#
# What: Provides a ContextVar that middleware sets on every authenticated request,
#       allowing SQLAlchemy event listeners to stamp created_by/modified_by on CRM
#       entities without threading explicit user_id through every call site. Also holds
#       the viewer's display timezone plus a small TTL-cached resolver (resolve_display_tz)
#       the middleware uses to populate that contextvar in the ASYNC event-loop context so
#       it propagates to both async and threadpool (sync) endpoints + template renders.
# Called by: app/main.py (middleware), app/audit_listeners.py (event listeners),
#       app/routers/htmx/settings.py (cache invalidation on timezone change)
# Depends on: stdlib only at module scope; SessionLocal/User/select imported lazily inside
#       resolve_display_tz to avoid a circular import at load time.

import contextvars
import threading
import time

# Holds the authenticated user's id for the duration of the current request.
# None when no request is in scope (background jobs, CLI commands, test fixtures
# that create records directly).
current_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user_id_var", default=None)

# Holds the current viewer's IANA display timezone (e.g. "Asia/Tokyo") for the duration
# of the request, so the |localtime/|localdate Jinja filters and _task_due_state can
# render UTC timestamps in the viewer's own zone without threading the user through every
# call. AuditUserMiddleware resolves it from the session uid (via resolve_display_tz) and
# sets it in the ASYNC request context — so it propagates to both async endpoints and the
# threadpool that runs sync dependencies/endpoints (a sync dependency's .set() runs in a
# discarded thread-context copy, which is why require_user must NOT set it). None → callers
# fall back to app.utils.timezones.DEFAULT_DISPLAY_TZ.
current_user_display_tz_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_display_tz_var", default=None
)


# ── Display-timezone resolver (TTL-cached, hot-path safe) ─────────────────────────────
#
# The middleware needs the viewer's display_timezone on EVERY request, but an unbounded
# per-request SELECT on the hot path is unacceptable. This tiny in-process cache maps
# uid -> (tz, expiry_monotonic); a lookup costs one dict read within the TTL window and at
# most one lightweight SELECT per user per TTL otherwise. A timezone write (settings) calls
# invalidate_display_tz(uid) so the next request re-reads immediately.

_DISPLAY_TZ_TTL_SECONDS = 60.0
_display_tz_cache: dict[int, tuple[str | None, float]] = {}
_display_tz_cache_lock = threading.Lock()


def _query_display_tz(uid: int) -> str | None:
    """Best-effort SELECT of a user's stored display_timezone (None on any error).

    Lazy imports avoid a circular import (database/models import this module's contextvars
    transitively). Opens its own short-lived session — the middleware runs outside FastAPI
    dependency injection, so there is no request-scoped session to borrow. A DB error must
    never propagate into the request: callers then fall back to DEFAULT_DISPLAY_TZ.
    """
    try:
        from sqlalchemy import select

        from .database import SessionLocal
        from .models import User

        with SessionLocal() as db:
            return db.execute(select(User.display_timezone).where(User.id == uid)).scalar_one_or_none()
    except Exception:
        from loguru import logger

        logger.debug("resolve_display_tz: lookup failed for uid={} — falling back to default", uid)
        return None


def resolve_display_tz(uid: int | None) -> str | None:
    """Return user *uid*'s stored IANA display timezone, or None (→ business default).

    Backed by a ~60s in-process TTL cache keyed by uid so the middleware does not issue
    a per-request query. None uid (logged-out / agent / background) short-circuits to
    None.
    """
    if not uid:
        return None
    now = time.monotonic()
    with _display_tz_cache_lock:
        hit = _display_tz_cache.get(uid)
        if hit is not None and hit[1] > now:
            return hit[0]
    tz = _query_display_tz(uid)
    with _display_tz_cache_lock:
        _display_tz_cache[uid] = (tz, time.monotonic() + _DISPLAY_TZ_TTL_SECONDS)
    return tz


def invalidate_display_tz(uid: int | None) -> None:
    """Drop uid's cached timezone so the next request re-reads it (call after a tz
    write)."""
    if not uid:
        return
    with _display_tz_cache_lock:
        _display_tz_cache.pop(uid, None)


def clear_display_tz_cache() -> None:
    """Clear the entire display-tz cache (test isolation / global reset)."""
    with _display_tz_cache_lock:
        _display_tz_cache.clear()
