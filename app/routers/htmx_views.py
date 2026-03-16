"""
routers/htmx_views.py — Thin re-export after domain split.

All HTMX route handlers now live in app/routers/htmx/ sub-modules.
This file simply imports them (registering routes on the shared router)
and re-exports `router` so main.py doesn't need to change.

Called by: main.py (router mount)
Depends on: app/routers/htmx/ sub-modules
"""

# Import the shared router — sub-module imports register their routes on it.
# Import each domain module to trigger route registration on the shared router.
from .htmx import activity as _activity  # noqa: F401
from .htmx import buy_plans as _buy_plans  # noqa: F401
from .htmx import companies as _companies  # noqa: F401
from .htmx import core as _core  # noqa: F401
from .htmx import prospecting as _prospecting  # noqa: F401
from .htmx import quotes as _quotes  # noqa: F401
from .htmx import requisitions as _requisitions  # noqa: F401
from .htmx import sourcing as _sourcing  # noqa: F401
from .htmx import vendors as _vendors  # noqa: F401
from .htmx._helpers import router  # noqa: F401
