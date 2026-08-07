"""Requisitions package — W4.8 split of the former 1,473-line
routers/htmx/requisitions.py.

Server-rendered HTML partials for the requisitions surface: the list + CSV export,
the unified create/import modal + AI parse/save, the AI customer lookup/quick-create
+ typeahead used by that modal, the requisition detail shell, requirement add,
search-all, the detail tabs, and the Task board mutations (same `/v2/partials` +
`/api/requisitions` paths, same `htmx-views` tag).

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it in the
original registration order. That order is LOAD-BEARING: detail's GET
/v2/partials/requisitions/{req_id} captures ANY same-arity GET registered after
it, so the static GET siblings — list's /export and create_import's /create-form,
/import-form, /customer-typeahead — must attach first; hence the `isort: split`
fence keeping .create_import/.detail/.tasks below the alphabetised common/list
block. Every former top-level name is re-exported here so existing imports
(`from app.routers.htmx.requisitions import ...` — main.py's router mount,
requisitions_edit.py, my_day.py, tests) keep working; test PATCH targets,
however, must point at the defining submodule (patching a package attribute
cannot intercept a submodule-local lookup).

Called by: app/main.py (router mount); requisitions_edit.py (_best_quote_status,
    requisitions_list_partial); my_day.py (_coerce_task_priority).
Depends on: .common, .list, .create_import, .detail, .tasks
"""

from .common import router  # noqa: F401
from .list import (  # noqa: F401
    _QUOTE_STATUS_PRIORITY,
    _REQ_EXPORT_COLUMNS,
    _REQ_EXPORT_SORT_COLUMNS,
    _best_quote_status,
    _requisition_export_rows,
    build_requisition_list_query,
    requisitions_export,
    requisitions_list_partial,
)

# isort: split
from .create_import import (  # noqa: F401
    MAX_IMPORT_UPLOAD_BYTES,
    _parse_xlsx_rows,
    customer_lookup,
    customer_quick_create,
    customers_typeahead_dropdown,
    requisition_create_form,
    requisition_import_form,
    requisition_import_parse,
    requisition_import_save,
)
from .detail import (  # noqa: F401
    add_requirement,
    requisition_detail_partial,
    requisition_search_all,
    requisition_tab,
)
from .tasks import (  # noqa: F401
    _coerce_task_priority,
    _get_board_task_or_403,
    _parse_int_or_none,
    complete_requisition_task_endpoint,
    create_requisition_task_endpoint,
    delete_requisition_task_endpoint,
    edit_requisition_task_endpoint,
    requisition_task_edit_form_endpoint,
    requisition_task_row_endpoint,
)
