"""Shared state for the settings package — the single APIRouter + settings_toast.

W4.8 split of the 1,032-line app/routers/htmx/settings.py — pure structural move: URLs
and behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).

Called by: every settings submodule; external importers app/routers/admin/buy_plan_ops.py
    and app/routers/sources.py (settings_toast); app/main.py mounts router via the
    package __init__.
Depends on: fastapi
"""

import json

from fastapi import APIRouter

router = APIRouter(tags=["htmx-views"])


def settings_toast(response, message: str, kind: str = "success") -> None:
    """Attach a showToast HX-Trigger for settings mutation responses.

    Called by settings mutation handlers to surface success/error feedback via the
    Alpine $store.toast. Mirrors _prospect_toast but is scoped to settings so later
    tasks can import it cleanly.
    """
    response.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": kind}})
