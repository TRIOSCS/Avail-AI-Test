"""Routers/htmx/ — per-domain split of the monolithic htmx_views.py.

Each module here owns one cohesive slice of the HTMX/Alpine frontend surface
(same `/v2/...` URL space, same `htmx-views` tag) with its own APIRouter that
app/main.py mounts alongside the legacy htmx_views router. Shared helpers live
in _shared.py.

Called by: app/main.py (sub-router registration)
Depends on: ._shared
"""
