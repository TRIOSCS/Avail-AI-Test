"""Settings package — W4.8 split of the former 1,032-line htmx/settings.py router.

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it. Every former
top-level name is re-exported here so existing imports
(`from app.routers.htmx.settings import ...`) keep working; test PATCH targets,
however, must point at the defining submodule (patching a package attribute cannot
intercept a submodule-local lookup).
"""

from .common import (  # noqa: F401
    router,
    settings_toast,
)
from .connectors import (  # noqa: F401
    _DEAD_CONNECTORS,
    _TEST_ALL_DISCONNECT_POLL_S,
    _TEST_ALL_MAX_PER_MIN,
    _TEST_ALL_OVERALL_BUDGET_S,
    _TEST_ALL_PROBE_TIMEOUT_S,
    _build_connector_field,
    _build_connector_groups,
    _enrich_source,
    _worker_status_row,
    clay_oauth,
    connector_card_partial,
    connectors_test_all,
    settings_connectors_tab,
)
from .dedup_admin import (  # noqa: F401
    _MAX_DEDUP_PAIRS,
    _dedup_bulk,
    _parse_dedup_pairs,
    _render_data_ops,
    admin_api_health,
    admin_company_bulk,
    admin_company_delete_both,
    admin_company_merge,
    admin_vendor_bulk,
    admin_vendor_delete_both,
    admin_vendor_merge,
    settings_data_ops_tab,
)
from .profile import (  # noqa: F401
    _run_inbox_scan_now,
    settings_profile_tab,
    settings_scan_now,
    toggle_8x8,
    toggle_buyplan_email,
    toggle_new_offer_alert,
    toggle_resource_alert,
    update_display_timezone,
    update_user_profile,
)
from .tabs import (  # noqa: F401
    settings_api_keys_tab,
    settings_data_export_tab,
    settings_ops_group_tab,
    settings_partial,
    settings_sources_tab,
    settings_system_tab,
    settings_users_tab,
)
