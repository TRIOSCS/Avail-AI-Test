"""ApprovalRequestActionSource — ACTION alert for engine approvals awaiting MY decision.

Surfaces the open approval work the current user personally owns: every REQUESTED
ApprovalRequest on which the user holds a PENDING recipient row — exactly the set the
engine's ``decide`` would let them act on. Covers ALL engine gates (buy_plan, prepayment,
…) since C1 routes the live buy-plan gate through the engine, so a pending buy plan now
counts here for its assigned approvers (the buy-plans tab still counts the buyer-PO and
ops-verify steps; the approval decision moved to this Approvals tab).

As an ACTION source the count derives PURELY from work-state: it does NOT subtract
seen_ref_ids. ``alert_seen`` only gates the cosmetic one-time pulse; the item leaves the
count only when the request is decided / cancelled. Hence ``count_for_user`` ==
``len(new_items_for_user(...))`` — both delegate to one helper.

Called by: services/alerts/sources/__init__.py (registered centrally under the "approvals"
           tab).
Depends on: services/alerts/base.AlertSource, services/approvals/queue
            (_actionable_request_ids — the ONE owner of the awaiting-me join, shared
            with the queue view-model and mirroring service.decide's recipient check),
            constants.AlertKind.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import AlertKind
from app.models.auth import User
from app.services.approvals.queue import _actionable_request_ids

from ..base import AlertItem, AlertSource, Temperament


class ApprovalRequestActionSource(AlertSource):
    """Open engine approval requests this user must decide (ACTION)."""

    key = "approval_action"
    kind = AlertKind.APPROVAL_ACTION
    temperament = Temperament.ACTION

    def _actionable_items(self, db: Session, user: User) -> list[AlertItem]:
        """REQUESTED requests where the user holds a PENDING recipient row.

        ACTION semantics: the single source of truth for both public methods (count = len);
        never consults seen_ref_ids. Each item anchors to its request row (``ar-{id}``) and
        carries the request id as ref_id. The eligibility join itself is owned by
        ``services/approvals/queue._actionable_request_ids`` so the nav badge can never
        desynchronize from the queue's can_act flags.
        """
        ids = _actionable_request_ids(db, user)
        return [AlertItem(ref_id=req_id, anchor=f"ar-{req_id}") for req_id in sorted(ids)]

    def count_for_user(self, db: Session, user: User) -> int:
        return len(self._actionable_items(db, user))

    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        return self._actionable_items(db, user)
