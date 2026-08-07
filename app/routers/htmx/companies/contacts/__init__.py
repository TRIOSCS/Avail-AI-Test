"""routers/htmx/companies/contacts/__init__.py — W4.8 split of the former 1,764-line
contacts.py (Contacts-tab CRUD, bulk actions, suggested-contacts discovery,
notes/history/files, P4.3) into a package.

Pure structural move: URLs and behavior identical. The shared APIRouter still
lives in the PARENT package (``app.routers.htmx.companies.__init__``); .common
re-exports it and each submodule below decorates it directly, exactly like the
single module this package replaces (sightings/core-package pattern).

Import order below is REGISTRATION order. It is NOT load-bearing for routing —
all 26 routes were checked pairwise (same method + arity + static-vs-param
overlap): zero collisions — but it mirrors the original file's layout:
``listing`` (static global-contacts routes first), then ``card``, ``crud``,
``discovery``.

Every former top-level name of contacts.py is re-exported here so existing
imports keep working unchanged: the parent ``__init__`` re-exports
``_run_contact_discovery`` / ``contacts_tab_suggested`` (tests monkeypatch the
former off the package), and ``..sites`` / ``..merge`` import
``_render_contacts_list`` from this package.

Called by: app.routers.htmx.companies (package __init__ re-export, route
    registration), ..sites, ..merge (``_render_contacts_list``)
Depends on: .common, .listing, .card, .crud, .discovery
"""

# Registration order (mirrors the original single-file layout — see module
# docstring): listing → card → crud → discovery.
# isort: off
from .common import (  # noqa: F401
    _contacts_list_response,
    _form_int,
    _render_contacts_list,
    router,
)
from .listing import (  # noqa: F401
    _VALID_BULK_CONTACT_ACTIONS,
    contacts_bulk_action,
    customer_contacts_partial,
    get_company_contacts_for_select,
    import_contacts_confirm,
    import_contacts_preview,
)
from .card import (  # noqa: F401
    _contact_under_company,
    _render_contact_notes_modal,
    add_contact_note,
    contact_field_display,
    contact_field_edit_form,
    contact_field_post,
    contact_files_modal,
    contact_history_modal,
    contact_notes_modal,
    set_contact_archive,
    set_contact_dnc,
    set_contact_priority,
    set_contact_role,
)
from .crud import (  # noqa: F401
    company_sites_options,
    contact_edit_form_company_scoped,
    contact_move,
    contact_move_form,
    contacts_tab_add_form,
    contacts_tab_create,
    edit_site_contact,
)
from .discovery import (  # noqa: F401
    _run_contact_discovery,
    contacts_tab_add_suggested,
    contacts_tab_suggested,
    contacts_tab_suggested_status,
)
# isort: on
