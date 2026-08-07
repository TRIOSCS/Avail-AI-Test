"""Archive package — W4.8 split of the former 969-line routers/htmx/archive.py.

Tasks / tickets lifecycle partials (HTMX + Alpine): trouble-ticket
workspace/list/detail, account + contact + vendor tasks
(add-form/create/list, complete/delete/edit/snooze), and the account + vendor
activity add-note forms — same `/v2/partials/...` paths, same `htmx-views` tag.

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it. Every
former top-level name is re-exported here so existing imports
(`from app.routers.htmx.archive import ...` — main.py's router mount,
error_reports' _build_ticket_list_context) keep working; test PATCH targets,
however, must point at the defining submodule (patching a package attribute
cannot intercept a submodule-local lookup).
"""

# Submodule import order = route REGISTRATION order (mirrors the original file's
# route order), deliberately not alphabetical — hence the isort exemption. The
# only order-critical pair lives inside tickets.py (literal /workspace and /list
# before /{ticket_id}), preserved by that file's intra-module order.
# ruff: noqa: I001

from .common import (  # noqa: F401
    _active_users,
    _coerce_task_priority,
    router,
)
from .tickets import (  # noqa: F401
    _build_ticket_list_context,
    trouble_ticket_detail,
    trouble_tickets_list,
    trouble_tickets_workspace,
)
from .tasks_crud import (  # noqa: F401
    account_task_add_form,
    account_tasks_partial,
    contact_task_add_form,
    contact_tasks_partial,
    create_account_task,
    create_contact_task_endpoint,
    create_vendor_task_endpoint,
    vendor_task_add_form,
    vendor_tasks_partial,
)
from .tasks_lifecycle import (  # noqa: F401
    _render_task_edit_form,
    complete_task_endpoint,
    delete_task_endpoint,
    edit_task_endpoint,
    snooze_task_endpoint,
    task_edit_form,
)
from .notes import (  # noqa: F401
    activity_add_note,
    activity_add_note_form,
    vendor_activity_add_note,
    vendor_activity_add_note_form,
)
