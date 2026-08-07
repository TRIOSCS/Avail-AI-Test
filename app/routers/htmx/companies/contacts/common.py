"""routers/htmx/companies/contacts/common.py — shared helpers + router shim for the
contacts subpackage (W4.8 split of the 1,764-line contacts.py, P4.3).

Pure structural move: URLs and behavior unchanged. The single APIRouter every
route in the companies package attaches to is DEFINED in
``app.routers.htmx.companies.__init__`` (no prefix; mounted by app.main); this
module re-exports it so the contacts submodules can ``from .common import
router`` (sightings/core-package pattern). Also owns the helpers shared by 2+
submodules AND by sibling packages: ``_render_contacts_list`` is consumed by
``..sites`` / ``..merge`` (via the contacts package __init__ re-export) after
their own contact-affecting mutations.

Called by: .listing, .card, .crud, .discovery (router + helpers),
    ..sites, ..merge (``_render_contacts_list``), contacts/__init__ (re-export)
Depends on: app.routers.htmx.companies (parent package router),
    app.services.crm_service
"""

from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .....models import Company, User
from .....services.crm_service import company_contact_rows, customer_contacts_list_ctx
from .....template_env import template_response
from ..._shared import _base_ctx

# Parent-package router re-export — submodules do `from .common import router`.
from .. import router  # noqa: F401
from .._registries import (
    CANONICAL_ROLES,
)
from ..saved_views import _saved_views_ctx


def _form_int(form, key: str, default: int = 0) -> int:
    raw = (form.get(key) or "").strip()
    return int(raw) if raw.isdigit() else default


def _contacts_list_response(request: Request, user: User, db: Session, form, prefix: str = "") -> HTMLResponse:
    """Re-render the global contacts list from the hx-included filter fields.

    prefix: read filter values from prefixed form keys (e.g. "filter_search") — used by
    the origin=contacts edit-modal save, whose form carries its own contact_role field
    and so namespaces the list filters to avoid the name collision.
    """
    ctx = _base_ctx(request, user, "crm")
    ctx.update(
        customer_contacts_list_ctx(
            db,
            user,
            search=(form.get(f"{prefix}search") or "").strip(),
            company_id=_form_int(form, f"{prefix}company_id"),
            contact_role=(form.get(f"{prefix}contact_role") or "").strip(),
            cadence_state=(form.get(f"{prefix}cadence_state") or "").strip(),
            limit=_form_int(form, f"{prefix}limit", 50),
            offset=_form_int(form, f"{prefix}offset", 0),
        )
    )
    ctx["contact_roles"] = CANONICAL_ROLES
    ctx.update(_saved_views_ctx(request, user, db, "contacts"))
    return template_response("htmx/partials/customers/contacts_list.html", ctx)


def _render_contacts_list(request: Request, user: User, company: Company, db: Session) -> HTMLResponse:
    """Build and return the contacts grouped-list partial for the Contacts tab.

    Shared by create, add-suggested, delete, set-primary, and edit endpoints (in this
    module AND in .sites / .merge) so every swap path stays in sync with one another.
    """
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "contact_rows": company_contact_rows(db, company.id, viewer=user),
            "now_utc": datetime.now(UTC),
            "roles": CANONICAL_ROLES,
        }
    )
    return template_response("htmx/partials/customers/tabs/_contacts_grouped_list.html", ctx)
