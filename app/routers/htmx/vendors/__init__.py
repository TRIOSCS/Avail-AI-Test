# ruff: noqa: I001 — import order below IS the route registration order; sorting it
# would register GET /v2/partials/vendors/{vendor_id} (crud) before the static
# GET /v2/partials/vendors/export (listing) and break the export URL (422).
"""Vendors package — W4.8 split of the former 1,475-line routers/htmx/vendors.py.

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it in the
original registration order (listing → crud → contacts → profile → prospects —
order matters: the static /export, /create-form, and /check-duplicate routes must
register before GET /{vendor_id}). Every former top-level name is re-exported here
so existing imports (`from app.routers.htmx.vendors import ...`) keep working;
test PATCH targets, however, must point at the defining submodule (patching a
package attribute cannot intercept a submodule-local lookup).
"""

from .common import router  # noqa: F401
from .listing import (  # noqa: F401
    _VENDOR_EXPORT_COLUMNS,
    _VENDOR_EXPORT_SORT_COLUMNS,
    _vendor_export_rows,
    build_vendor_list_query,
    vendor_contacts_partial,
    vendors_export,
    vendors_list_partial,
)
from .crud import (  # noqa: F401
    archive_vendor,
    create_vendor_partial_early,
    delete_vendor_partial,
    edit_vendor,
    toggle_vendor_blacklist,
    unarchive_vendor,
    vendor_check_duplicate_partial,
    vendor_create_form_early,
    vendor_detail_partial,
    vendor_edit_form,
    vendor_tab,
)
from .contacts import (  # noqa: F401
    _render_contact_row,
    _render_contact_rows,
    vendor_contact_add,
    vendor_contact_delete,
    vendor_contact_edit,
    vendor_contact_set_primary,
)
from .profile import (  # noqa: F401
    _render_vendor_custom_fields,
    _render_vendor_ownership_badge,
    add_vendor_review,
    delete_vendor_review,
    vendor_add_custom_field,
    vendor_claim,
    vendor_delete_custom_field,
    vendor_ownership_badge,
    vendor_release,
    vendor_reviews,
)
from .prospects import (  # noqa: F401
    _run_vendor_find_contacts,
    vendor_find_contacts,
    vendor_find_contacts_status,
    vendor_prospect_delete,
    vendor_prospect_promote,
    vendor_prospect_save,
)
