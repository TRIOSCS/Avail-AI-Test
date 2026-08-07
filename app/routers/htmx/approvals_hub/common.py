"""Shared state for the approvals_hub package — router, tab vocabulary, label maps,
notes ctx.

W4.8 split of the 1,250-line app/routers/htmx/approvals_hub.py — pure structural move:
URLs and behavior unchanged; every route attaches to the shared router imported from
.common (registration assembled in __init__).
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from ....constants import BuyPlanLineStatus, SalesOrderType
from ....models import User

router = APIRouter(tags=["htmx-views"])

# The three workspace tabs (dash-cased URL segments), in display order. Deals is the
# one SO/BP surface — the quote-less BuyPlan IS the deal (W4.3 tabs merge).
_TABS = ("deals", "purchase-orders", "prepayments")
DEFAULT_TAB = "deals"

# Legacy tab keys → workspace tabs. Old pushed URLs (?tab=sales-orders / ?tab=buy-plan)
# and the origin=approvals_hub decide handlers resolve through this map.
LEGACY_TAB_ALIASES = {
    "sales-orders": "deals",
    "buy-plans": "deals",
    "buy-plan": "deals",
    "po-approval": "purchase-orders",
    "prepayment": "prepayments",
}

_TAB_LABELS = {
    "deals": "Deals",
    "purchase-orders": "Purchase Orders",
    "prepayments": "Prepayments",
}

# PO decision vocabulary (spec §5): the UI says Approve / Approved / Pending approval
# everywhere users see a per-line PO state. Backend names stay pending_verify/verified —
# this is a DISPLAY map only, never a code rename.
PO_DECISION_LABELS = {
    BuyPlanLineStatus.AWAITING_PO.value: "Awaiting PO",
    BuyPlanLineStatus.PENDING_VERIFY.value: "Pending approval",
    BuyPlanLineStatus.VERIFIED.value: "Approved",
    BuyPlanLineStatus.ISSUE.value: "Issue",
    BuyPlanLineStatus.CANCELLED.value: "Cancelled",
    BuyPlanLineStatus.RESOURCING.value: "Re-sourcing",
}

# Order-type badge labels (SalesOrderType → short display).
ORDER_TYPE_LABELS = {
    SalesOrderType.NEW.value: "New",
    SalesOrderType.REVISION.value: "Revision",
    SalesOrderType.TESTING_SERVICE.value: "Testing Service",
    SalesOrderType.COMPS.value: "Comps",
    SalesOrderType.STOCK_SALE.value: "Stock Sale",
}


def _resolve_tab(tab: str) -> str | None:
    """Map *tab* (new key or legacy alias) to a canonical workspace tab, or None."""
    tab = LEGACY_TAB_ALIASES.get(tab, tab)
    return tab if tab in _TABS else None


# ── Notes + attachments (2.4 — one thread per item, never status-locked) ─


def _notes_ctx(
    db: Session, user: User, *, plan_id: int, line_id: int | None = None, prepayment_id: int | None = None
) -> dict:
    """Template context for _notes_thread.html on one subject (plan / line / prepay).

    ``thread_subject`` carries exactly the narrowest subject id — the add-note and
    upload forms round-trip it as hidden inputs. ``can_manage_files`` gates delete
    buttons for non-uploaders (manager/admin); the uploader always sees their own.
    """
    from ....dependencies import is_manager_or_admin
    from ....models.buy_plan import BuyPlanAttachment
    from ....services.workspace_notes import notes_thread

    if line_id is not None:
        notes = notes_thread(db, buy_plan_line_id=line_id)
        files_filter = BuyPlanAttachment.buy_plan_line_id == line_id
        thread_subject = {"buy_plan_line_id": line_id}
    elif prepayment_id is not None:
        notes = notes_thread(db, prepayment_id=prepayment_id)
        files_filter = BuyPlanAttachment.prepayment_id == prepayment_id
        thread_subject = {"prepayment_id": prepayment_id}
    else:
        notes = notes_thread(db, buy_plan_id=plan_id)
        files_filter = BuyPlanAttachment.buy_plan_id == plan_id
        thread_subject = {"buy_plan_id": plan_id}

    files = db.scalars(select(BuyPlanAttachment).where(files_filter).order_by(BuyPlanAttachment.id.asc())).all()
    return {
        "thread_notes": notes,
        "thread_files": files,
        "thread_subject": thread_subject,
        "viewer_id": user.id,
        "can_manage_files": is_manager_or_admin(user),
    }
