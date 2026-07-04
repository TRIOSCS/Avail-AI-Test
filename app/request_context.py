# Request-scoped contextvar holding the current authenticated user's id.
#
# What: Provides a ContextVar that middleware sets on every authenticated request,
#       allowing SQLAlchemy event listeners to stamp created_by/modified_by on CRM
#       entities without threading explicit user_id through every call site.
# Called by: app/main.py (middleware), app/audit_listeners.py (event listeners)
# Depends on: nothing (pure stdlib)

import contextvars

# Holds the authenticated user's id for the duration of the current request.
# None when no request is in scope (background jobs, CLI commands, test fixtures
# that create records directly).
current_user_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_user_id_var", default=None)

# Holds the current viewer's IANA display timezone (e.g. "Asia/Tokyo") for the duration
# of the request, so the |localtime/|localdate Jinja filters and _task_due_state can
# render UTC timestamps in the viewer's own zone without threading the user through every
# call. AuditUserMiddleware establishes a per-request baseline (None) + reset; require_user
# overrides it with the loaded user's display_timezone. None → callers fall back to
# app.utils.timezones.DEFAULT_DISPLAY_TZ.
current_user_display_tz_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_display_tz_var", default=None
)
