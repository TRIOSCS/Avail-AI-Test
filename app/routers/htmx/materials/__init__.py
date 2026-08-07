"""Materials package — W4.8 split of the former 1,483-line routers/htmx/materials.py.

Server-rendered HTML partials for the materials surface: the faceted list + filter
sidebars (manufacturers/global/tree/sub), manufacturer search/add, AI interpret,
faceted results, add-form, enrich-status poller, conflict-accept, FRU lookup, the
material detail panel + tabs, card update, and the enrich/find-crosses/insights
actions (same `/v2/partials/materials` + `/v2/partials/manufacturers` paths, same
`htmx-views` tag). NB: distinct from the domain router app/routers/materials.py.

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it in the
original registration order. That order is LOAD-BEARING: cards' GET
/v2/partials/materials/{card_id} captures ANY 4-segment GET registered after it,
so browse's static siblings (workspace/ai-interpret/faceted) must attach first —
hence the `isort: split` fence keeping .actions (which imports .cards) below the
alphabetised browse/cards/common block. Every former top-level name is re-exported
here so existing imports (`from app.routers.htmx.materials import ...`) keep
working; test PATCH targets, however, must point at the defining submodule
(patching a package attribute cannot intercept a submodule-local lookup).

Called by: app/main.py (router mount).
Depends on: .common, .browse, .cards, .actions
"""

from .browse import (  # noqa: F401
    manufacturer_add,
    manufacturer_search,
    materials_ai_interpret_partial,
    materials_faceted_partial,
    materials_filters_global_partial,
    materials_filters_manufacturers_partial,
    materials_filters_sub_partial,
    materials_filters_tree_partial,
    materials_list_partial,
    materials_workspace_partial,
)
from .cards import (  # noqa: F401
    _MATERIALS_EXPORT_COLUMNS,
    _materials_export_rows,
    fru_lookup_partial,
    material_add_form_partial,
    material_conflict_accept,
    material_detail_partial,
    material_enrich_status_partial,
    material_tab_partial,
    materials_export,
    update_material_card,
)
from .common import (  # noqa: F401
    _parse_card_filter_params,
    _parse_filter_json,
    _pop_manufacturers,
    router,
)

# isort: split
from .actions import (  # noqa: F401
    _run_card_crosses,
    _run_card_enrichment,
    enrich_material,
    find_crosses,
    material_crosses_status_partial,
    material_insights,
)
