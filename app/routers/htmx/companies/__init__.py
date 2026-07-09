"""routers/htmx/companies/__init__.py — Company/customer & contact partial views (HTMX
+ Alpine) — package entry point (P4.3 split).

Server-rendered HTML partials for the company/customer + contact CRM surface, split
along its audited seams into cohesive sibling modules: ``saved_views`` (filter
presets), ``tags`` (segment tags), ``custom_fields``, ``merge`` (company + contact
duplicate merge), ``sites`` (CustomerSite + site-contact CRUD), ``contacts``
(Contacts-tab CRUD, bulk actions, suggested-contacts discovery, notes/history/files),
``detail`` (the company-detail shell / ``company_tab`` render path), and ``core``
(list/create/lifecycle basics). Every submodule imports the SAME ``router`` instance
defined here and decorates it directly — so ``router`` below is byte-for-byte the one
FastAPI object every route in this package is registered on, exactly like the single-
file module this package replaces.

This ``__init__.py`` re-exports every name that was previously importable/patchable
off ``app.routers.htmx.companies`` (tests patch several of these; ``app.main`` imports
only ``router``) so no caller outside this package needs to change.

Import order below: leaves first (no dependency on sibling companies submodules), then
``.contacts`` (depended on by ``.sites``/``.merge``), then ``.sites``/``.merge``, then
``.detail``, then ``._registries``, then ``.core`` (depends on ``.detail`` +
``.saved_views``). Each submodule's ``@router...`` decorators register its routes onto
the shared ``router`` created below as a side effect of import.

Called by: app.main (router mount), app.routers.htmx._shared_tabs (lazily imports
    CANONICAL_ROLES / FIELD_LABELS / _company_quotes_query / _company_buy_plans_query
    off this package to avoid a load-time cycle), tests/ (see __all__ below)
Depends on: .saved_views, .tags, .custom_fields, .contacts, .sites, .merge, .detail,
    .core, ._registries, app.services.crm_service
"""

from fastapi import APIRouter

from ....services.crm_service import staleness_tier as _staleness_tier

router: APIRouter = APIRouter(tags=["htmx-views"])

from . import custom_fields, merge, saved_views, sites, tags
from ._registries import (
    _VALID_ROLES,
    CANONICAL_ROLES,
    FIELD_LABELS,
    apply_company_field,
    apply_contact_field,
)
from .contacts import _run_contact_discovery, contacts_tab_suggested
from .core import _manageable_company_ids, create_company, edit_company
from .detail import (
    _company_buy_plans_query,
    _company_quotes_query,
    _render_company_detail,
    company_detail_partial,
    company_tab,
)
from .sites import edit_site

__all__ = [
    "router",
    "company_detail_partial",
    "company_tab",
    "create_company",
    "edit_company",
    "edit_site",
    "apply_company_field",
    "apply_contact_field",
    "CANONICAL_ROLES",
    "_VALID_ROLES",
    "FIELD_LABELS",
    "contacts_tab_suggested",
    "_run_contact_discovery",
    "_manageable_company_ids",
    "_company_quotes_query",
    "_company_buy_plans_query",
    "_render_company_detail",
    "_staleness_tier",
    # Re-exported (not directly patched/imported by tests today, but part of the
    # module's public surface before the split — kept importable):
    "custom_fields",
    "saved_views",
    "tags",
    "merge",
    "sites",
]
