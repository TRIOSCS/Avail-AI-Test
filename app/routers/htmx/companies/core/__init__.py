"""routers/htmx/companies/core/__init__.py — W4.8 split of the former 1,407-line core.py
(company list/create/edit/lifecycle basics, P4.3) into a package.

Pure structural move: URLs and behavior identical. The shared APIRouter still
lives in the PARENT package (``app.routers.htmx.companies.__init__``); .common
re-exports it and each submodule below decorates it directly, exactly like the
single module this package replaces (sightings-package pattern).

Import order below is REGISTRATION order and is load-bearing:
``lists`` must register ``POST /v2/partials/customers/bulk/{action}`` BEFORE
``lifecycle``'s ``POST /v2/partials/customers/{company_id}/deactivate`` and
``/{company_id}/send-to-prospecting`` — "deactivate" and "send-to-prospecting"
are both live bulk action names, so ``/bulk/deactivate`` must not match with
``company_id='bulk'`` (422). Additionally, NO submodule here may import
``..detail`` at module scope (see the NOTE in .lifecycle): rendered-detail calls
go through the ``_pkg._render_company_detail`` / ``_pkg.company_detail_partial``
package-attribute indirection so core's static routes keep registering before
..detail's ``/v2/partials/customers/{company_id}`` catch-all (the parent
__init__ imports .core before .detail).

Every former top-level name of core.py is re-exported here so existing imports
(``from .core import ...`` in the parent __init__, and the package-level
re-export surface tests rely on) keep working unchanged.

Called by: app.routers.htmx.companies (package __init__ re-export, route
    registration)
Depends on: .common, .lists, .lifecycle, .editing
"""

# Registration order (load-bearing — see module docstring): lists → lifecycle → editing.
# isort: off
from .common import router  # noqa: F401
from .lists import (  # noqa: F401
    _VALID_BULK_COMPANY_ACTIONS,
    _manageable_company_ids,
    archived_companies_list,
    companies_account_list_partial,
    companies_list_partial,
    companies_redirect,
    customers_bulk_action,
    import_companies_confirm,
    import_companies_preview,
    partials_companies_redirect,
)
from .lifecycle import (  # noqa: F401
    _VALID_DISPOSITIONS,
    _VALID_TIERS,
    check_company_duplicate,
    company_apply_name,
    company_create_form,
    company_dup_suggestion,
    company_name_suggestion,
    company_typeahead,
    create_company,
    deactivate_company,
    reactivate_company,
    send_company_to_prospecting_htmx,
    set_company_disposition,
    set_company_tier,
)
from .editing import (  # noqa: F401
    _collaborators_partial,
    _set_parent_company,
    add_account_collaborator,
    company_edit_form,
    company_field_display,
    company_field_edit_form,
    company_field_post,
    edit_company,
    remove_account_collaborator,
    set_account_primary_contact,
    set_parent_company,
)
# isort: on
