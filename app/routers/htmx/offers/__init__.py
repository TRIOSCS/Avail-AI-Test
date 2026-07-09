"""Routers/htmx/offers/ — Offer / RFQ / follow-up partial views (HTMX + Alpine).

Package split of the monolithic offers.py (P4.3), along the audited seams:

  crud.py       — AI offer parsing (parse-email/paste/parse-offer/save), offer
                  CRUD + review/promote/reject/changelog, quote-from-offers.
  rfq.py        — RFQ compose form, AI cleanup/rephrase, RFQ send.
  follow_ups.py — Cross-requisition follow-up queue, single/batch send, AI draft,
                  nav badge.
  replies.py    — Vendor response review/reply, manual activity/phone-call logging.

Re-exports a single router that includes all sub-routers so that app/main.py can
continue to do:

    from .routers.htmx.offers import router as htmx_offers_router

without any changes (same `/v2/partials` paths, same `htmx-views` tag).

Test-patch note: `template_response` / `requisition_tab` / `maybe_release_on_offer` /
`offer_review_queue` are re-exported here (from their real sources, or from .crud
for `offer_review_queue`, which is defined there). Every sub-module call site pulls
them back via a FUNCTION-LOCAL ``from . import X`` (not a module-level import) so
`patch("app.routers.htmx.offers.X")` still intercepts every call site post-split —
a module-level `from . import X` would bind the pre-patch object permanently at
import time.

Called by: app/main.py (router mount).
Depends on: .crud, .rfq, .follow_ups, .replies sub-modules
"""

from fastapi import APIRouter

# Re-export names that tests patch/import at "app.routers.htmx.offers.X". Sub-modules
# pull these back via a function-local `from . import X` — see the note above.
from ....services.vendor_unavailability import maybe_release_on_offer  # noqa: F401
from ....template_env import template_response  # noqa: F401
from .._shared_tabs import requisition_tab  # noqa: F401

# Re-export route functions tests import directly (e.g. `from app.routers.htmx.offers
# import rfq_send`) so callers keep working unchanged.
from .crud import (  # noqa: F401
    add_offer,
    create_quote_from_offers,
    edit_offer,
    offer_changelog,
    offer_review_queue,
    promote_offer_htmx,
    reject_offer_htmx,
    review_offer,
    save_parsed_offers,
)
from .crud import router as crud_router
from .follow_ups import router as follow_ups_router
from .follow_ups import send_follow_up_htmx  # noqa: F401
from .replies import log_activity, log_phone_call, review_response_htmx  # noqa: F401
from .replies import router as replies_router
from .rfq import ai_cleanup_email, rfq_compose, rfq_send  # noqa: F401
from .rfq import router as rfq_router

router = APIRouter(tags=["htmx-views"])
router.include_router(crud_router)
router.include_router(rfq_router)
router.include_router(follow_ups_router)
router.include_router(replies_router)
