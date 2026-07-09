"""routers/htmx/companies/saved_views.py — per-user saved filter presets for the
customers/contacts lists (P4.3 split).

Per-user named snapshots of a list's filter query params (list_key='customers' |
'contacts'). The control lives in both filter bars (list.html / contacts_list.html);
these routes refresh the ``#saved-views-<list_key>`` wrapper. ``_saved_views_ctx`` is
also called by ``.core`` (companies_list_partial) and ``.contacts``
(customer_contacts_partial / contacts bulk re-render) to embed the saved-views control
into their own list partials.

Called by: app.routers.htmx.companies (package __init__ re-export, route registration),
    .core, .contacts
Depends on: app.services.saved_views_service, app.routers.htmx.companies (shared router)
"""

import json

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy.orm import Session

from ....database import get_db
from ....dependencies import require_user
from ....models import User
from ....services.saved_views_service import create_saved_view, delete_saved_view, list_saved_views, valid_list_key
from ....template_env import template_response
from . import router

# Map list_key → the filter <form> id its presets apply onto (used by the partial).
_SAVED_VIEW_FORMS = {"customers": "cdm-filters", "contacts": "contacts-filters"}


def _saved_views_ctx(request: Request, user: User, db: Session, list_key: str) -> dict:
    """Context for the _saved_views.html partial (and the list shells that embed it)."""
    return {
        "request": request,
        "user": user,
        "saved_views": list_saved_views(db, user, list_key),
        "list_key": list_key,
        "target_form": _SAVED_VIEW_FORMS.get(list_key, "cdm-filters"),
    }


@router.get("/v2/partials/customers/saved-views", response_class=HTMLResponse)
async def saved_views_list(
    request: Request,
    list_key: str = "customers",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Render the saved-views control for *list_key* ('customers' | 'contacts')."""
    if not valid_list_key(list_key):
        raise HTTPException(400, "Unknown list_key")
    ctx = _saved_views_ctx(request, user, db, list_key)
    return template_response("htmx/partials/customers/_saved_views.html", ctx)


@router.post("/v2/partials/customers/saved-views", response_class=HTMLResponse)
async def saved_views_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Save the submitted filter set as a named view (upsert on name).

    Reads list_key + name + the hx-included filter form fields; the service whitelists
    which keys are persisted.
    """
    form = await request.form()
    list_key = (form.get("list_key") or "customers").strip()
    if not valid_list_key(list_key):
        raise HTTPException(400, "Unknown list_key")
    name = form.get("name") or ""
    raw_filters = {k: v for k, v in form.items() if k not in ("list_key", "name")}
    try:
        view = create_saved_view(db, user, list_key, name, raw_filters)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    logger.info("Saved view {!r} ({}) created by {}", view.name, list_key, user.email)
    ctx = _saved_views_ctx(request, user, db, list_key)
    resp = template_response("htmx/partials/customers/_saved_views.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": f"Saved view '{view.name}'"}})
    return resp


@router.delete("/v2/partials/customers/saved-views/{view_id}", response_class=HTMLResponse)
async def saved_views_delete(
    request: Request,
    view_id: int,
    list_key: str = "customers",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete one of the caller's saved views, then re-render the control."""
    if not valid_list_key(list_key):
        raise HTTPException(400, "Unknown list_key")
    deleted = delete_saved_view(db, user, view_id)
    ctx = _saved_views_ctx(request, user, db, list_key)
    resp = template_response("htmx/partials/customers/_saved_views.html", ctx)
    if deleted:
        resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": "View deleted"}})
    return resp
