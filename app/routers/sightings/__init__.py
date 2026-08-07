"""Sightings package — W4.1 split of the former 3,811-line sightings.py router.

Pure structural move (spec §5.1/§10): URLs and behavior identical. common.py owns
the single APIRouter; importing the submodules below attaches their routes to it
in the original registration order. Every former top-level name is re-exported
here so existing imports (`from app.routers.sightings import ...`) keep working;
test PATCH targets, however, must point at the defining submodule (patching a
package attribute cannot intercept a submodule-local lookup).
"""

from .board import (  # noqa: F401
    _EXPORT_COLUMNS,
    _SORT_COLUMNS,
    _fmt_seen_date,
    _render_sightings_table,
    _rerender_board_with_toast,
    _sighting_export_rows,
    build_board_requirement_query,
    sightings_batch_assign,
    sightings_batch_notes,
    sightings_batch_status,
    sightings_export,
    sightings_list,
    sightings_workspace,
)
from .common import (  # noqa: F401
    _EXCLUDED_REQ_STATUSES,
    _EXCLUDED_SOURCING_STATUSES,
    _SEARCH_FANOUT_LIMIT,
    MAX_BATCH_SIZE,
    _active_sourcing_status_clause,
    _append_oob_toast,
    _best_contacts_by_card,
    _cache,
    _get_cached,
    _invalidate_cache,
    _mpn_link_map,
    _oob_toast,
    _oob_toast_html,
    _publish_if_user_source,
    _refresh_offers_panel,
    _render_offers_panel,
    _toast_suppressed_for_sse,
    _with_toast,
    router,
)
from .composer import (  # noqa: F401
    _cards_with_resolvable_email,
    _dnc_emails_for_cards,
    sightings_composer_vendor,
    sightings_vendor_affinity,
    sightings_vendor_modal,
    sightings_vendor_search,
)
from .coverage import (  # noqa: F401
    CoverageEntry,
    RankedVendor,
    SuggestedVendor,
    _coverage_ranked_vendor_rows,
    _find_affinity_in_thread,
    _vss_vendor_card_join,
)
from .detail import (  # noqa: F401
    _annotated_unavailability,
    _mark_error_response,
    _run_search_and_publish,
    sightings_advance_status,
    sightings_batch_refresh,
    sightings_detail,
    sightings_log_activity,
    sightings_mark_available,
    sightings_mark_unavailable,
    sightings_refresh,
    sightings_unavailable_form,
)
from .offers_actions import (  # noqa: F401
    _offer_for_requirement_or_404,
    sightings_delete_offer,
    sightings_mark_offer_sold,
    sightings_offer_request,
    sightings_offer_request_send,
    sightings_qualify_ai,
    sightings_qualify_ai_draft,
    sightings_reconfirm_offer,
    sightings_review_offer,
)
from .offers_forms import (  # noqa: F401
    _echo_prefill,
    _parse_iso_date,
    _qual_dict,
    sightings_create_offer,
    sightings_offer_edit_form,
    sightings_offer_form,
    sightings_update_offer,
)
from .rfq_send import (  # noqa: F401
    RFQ_DATASHEETS_DROPPED_HEADER,
    RFQ_SENT_HEADER,
    RFQ_SKIPPED_HEADER,
    RFQ_TOTAL_HEADER,
    RFQ_UNAVAILABLE_HEADER,
    _partition_by_unavailability,
    sightings_preview_inquiry,
    sightings_send_inquiry,
)
