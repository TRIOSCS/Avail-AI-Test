"""Resell package — W4.8 split of the former 2,830-line resell.py router.

Pure structural move: URLs and behavior identical. common.py owns the single
APIRouter; importing the submodules below attaches their routes to it. board's
literal routes (/workspace, /lists, /list-rows, /create-form) must register
before detail's GET /v2/partials/resell/{list_id} — alphabetical module order
preserves that. Every former top-level name is re-exported here so existing
imports (`from app.routers.resell import ...`) keep working; test PATCH
targets, however, must point at the defining submodule (patching a package
attribute cannot intercept a submodule-local lookup).

Customer hiding remains VIEW DISCIPLINE (single-tenant): the "Open to Me" lens
and non-owner detail render only MPN / qty / condition, never the seller
company — enforced by the `can_see_customer` flag + owner-only list query.

Called by: app/main.py (router mount).
"""

from .bids import (  # noqa: F401
    _build_bid_context,
    _latest_bid,
    resell_accept_bid,
    resell_assemble_bid,
    resell_bid_csv,
    resell_bid_pdf,
    resell_bid_sheet_export,
    resell_build_bid,
    resell_reject_bid,
    resell_send_bid,
)
from .board import (  # noqa: F401
    _LIST_PAGE_SIZE,
    _list_cards,
    _list_rows_context,
    _parse_close_at,
    _stat_strip,
    resell_create_form,
    resell_create_list,
    resell_list_rows,
    resell_lists,
    resell_workspace,
)
from .common import (  # noqa: F401
    _LIVE_STATUSES,
    _POSTED_STATUSES,
    _UNACTIONED_OFFER_STATUSES,
    _VISIBLE_OFFER_STATUSES,
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    _close_at_display,
    _detail_context,
    _display_title,
    _file_extension,
    _fmt_dt,
    _get_list_for_user,
    _hours_until,
    _is_live,
    _offer_coverage,
    _require_owner,
    _to_decimal,
    _to_int,
    _toast,
    router,
)
from .detail import (  # noqa: F401
    resell_add_line,
    resell_add_line_form,
    resell_close,
    resell_close_awarded,
    resell_close_without_bid,
    resell_delete_line,
    resell_delete_list,
    resell_detail,
    resell_edit_line_form,
    resell_edit_list_form,
    resell_import_confirm,
    resell_import_preview,
    resell_lines,
    resell_publish,
    resell_update_line,
    resell_update_list,
)
from .offers import (  # noqa: F401
    _WITHDRAWABLE_OFFER_STATUSES,
    _award_response_context,
    _offer_broker_label,
    _offers_context,
    resell_assign_offer_line,
    resell_award_offer,
    resell_bids_upload_confirm,
    resell_bids_upload_form,
    resell_bids_upload_preview,
    resell_line_offer_compare,
    resell_offer_form,
    resell_offers,
    resell_offers_export,
    resell_submit_offer,
    resell_unaward_offer,
    resell_withdraw_offer,
)
from .outreach_send import (  # noqa: F401
    _RETRY_BODY,
    _RETRYABLE_OUTREACH,
    _buyer_panel_context,
    _neutral_outreach_subject,
    _no_contact_buyers,
    _suggestion_rows,
    resell_not_yet_strip,
    resell_offer_buyers_form,
    resell_retry_outreach,
    resell_submit_outreach,
)
from .outreach_track import (  # noqa: F401
    _RESPONDED_OUTREACH,
    _conversation_replies,
    _load_manual_outreach_for_owner,
    _load_outreach_for_owner,
    _outreach_tracker_context,
    resell_outreach_convert_offer,
    resell_outreach_export,
    resell_outreach_log_bid,
    resell_outreach_log_bid_form,
    resell_outreach_log_response,
    resell_outreach_reply,
    resell_outreach_tracker,
)
