"""BuyplanActionSource — ACTION alert for buy-plan steps that need MY action.

Surfaces the open work the current user personally owns across three buy-plan roles,
unioned into one count + spotlight list:

  1. Buyer PO step — a BuyPlanLine assigned to me that is still AWAITING_PO.
  2. Manager approval — a PENDING buy plan WITH NO OPEN BUY_PLAN ApprovalRequest, where I
     hold can_approve_buy_plans. Post-C1 the engine owns the gate, so a plan that opened an
     engine request is counted by the approvals badge instead (counting it here too would
     double-count); only a pre-C1 transition-window plan (no engine request) still surfaces
     here so it never goes invisible.
  3. Ops verify — a buy plan whose SO is still PENDING and unverified, where I am an
     active member of the ops verification group. (Phase D folded SO verification into the
     single approval, so newly-approved plans are stamped so_status=approved and never
     surface here; this remains only for any legacy active+pending plan.)

As an ACTION source the count derives PURELY from work-state: it does NOT subtract
seen_ref_ids. ``alert_seen`` only gates the cosmetic one-time in-tab pulse (handled
elsewhere); the item leaves the count only when the underlying work is done. Hence
``count_for_user`` == ``len(new_items_for_user(...))`` — both delegate to one helper.

Called by: services/alerts/registry.py (registered centrally by the parent).
Depends on: services/alerts/base.AlertSource, models/buy_plan.{BuyPlan,BuyPlanLine,
            VerificationGroupMember}, constants.{AlertKind,BuyPlanStatus,
            BuyPlanLineStatus,SOVerificationStatus}, dependencies.can_approve_buy_plans.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import (
    AlertKind,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
)
from app.dependencies import can_approve_buy_plans
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember

from ..base import AlertItem, AlertSource, Temperament


class BuyplanActionSource(AlertSource):
    """Open buy-plan steps the user must act on (ACTION)."""

    key = "buy_plans_action"
    kind = AlertKind.BUYPLAN_ACTION
    temperament = Temperament.ACTION

    def _actionable_items(self, db: Session, user: User) -> list[AlertItem]:
        """The union of open buy-plan steps this user personally owns.

        ACTION semantics: this is the single source of truth for both public
        methods (count = len). It never consults seen_ref_ids — seen only gates the
        cosmetic pulse.

        Anchor convention: the Buy Plans list renders one row PER PLAN, so every item
        anchors to its plan's row as ``f"bp-{buy_plan_id}"`` — a buyer-PO line anchors to
        the plan that owns it (not the line). The ``ref_id`` is still the precise thing we
        mark seen (the BuyPlanLine id for a PO step, the BuyPlan id for a plan step).

        NOTE on ref_id collisions: a line step uses the BuyPlanLine id and a plan step the
        BuyPlan id, so two AlertItems under this one kind can share a ref_id (e.g. line 7
        and plan 7). Acceptable for an ACTION source — the count is work-state-derived and
        ``alert_seen`` only suppresses the one-time pulse, so a coincidental shared ref_id
        at worst stops one row pulsing a beat early.
        """
        items: list[AlertItem] = []

        # 1. Buyer PO step — lines I own that are awaiting their PO, on an ACTIVE plan.
        #    confirm_po requires plan.status == ACTIVE (buyplan_workflow), so a line on a
        #    draft/pending/halted/completed plan is NOT actionable and must not count.
        #    Anchor to the owning plan's row (the list is per-plan); ref_id stays the line.
        po_lines = (
            db.query(BuyPlanLine.id, BuyPlanLine.buy_plan_id)
            .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
            .filter(
                BuyPlanLine.buyer_id == user.id,
                BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
                BuyPlan.status == BuyPlanStatus.ACTIVE,
            )
            .all()
        )
        items.extend(AlertItem(ref_id=line_id, anchor=f"bp-{plan_id}") for (line_id, plan_id) in po_lines)

        # 2. Approval — pending plans with no approver, if I hold the buy-plan approval
        #    right. Single source of truth: can_approve_buy_plans (the per-user column the
        #    approve route + service enforce), NOT a role set — so the badge never counts a
        #    step the user could not actually act on, and always counts one they can.
        #
        #    Post-C1, the approvals engine OWNS the buy-plan gate: a PENDING plan opens a
        #    BUY_PLAN ApprovalRequest that the dedicated approvals badge counts. Counting
        #    that same plan here too would DOUBLE-count it (buy-plans badge + approvals
        #    badge). So this branch counts ONLY pending plans with NO open engine request —
        #    i.e. post-C1 plans surface on the approvals badge alone, while a pre-C1
        #    transition-window plan (PENDING but never got an engine request) still surfaces
        #    here so it never goes invisible.
        if can_approve_buy_plans(user):
            from app.constants import ApprovalRequestStatus, ApprovalSubjectType
            from app.models.approvals import ApprovalRequest

            open_req_subq = (
                db.query(ApprovalRequest.subject_id)
                .filter(
                    ApprovalRequest.subject_type == ApprovalSubjectType.BUY_PLAN,
                    ApprovalRequest.status == ApprovalRequestStatus.REQUESTED,
                )
                .subquery()
            )
            approval_plans = (
                db.query(BuyPlan.id)
                .filter(
                    BuyPlan.status == BuyPlanStatus.PENDING,
                    BuyPlan.approved_by_id.is_(None),
                    BuyPlan.id.not_in(db.query(open_req_subq.c.subject_id)),
                )
                .all()
            )
            items.extend(AlertItem(ref_id=plan_id, anchor=f"bp-{plan_id}") for (plan_id,) in approval_plans)

        # 3. Ops verify — an ACTIVE plan whose SO is still pending + unverified, if I'm an
        #    active ops member. The ACTIVE gate matters: so_status DEFAULTS to "pending" at
        #    creation, so without it every draft/pending plan would falsely count (SO
        #    verification is only meaningful + surfaced once a plan is active). Gating here
        #    also prevents a PENDING plan double-counting for an admin who is also an ops
        #    member (branch 2 counts it as an approval; this branch no longer also counts it).
        is_ops_member = (
            db.query(VerificationGroupMember.id)
            .filter(
                VerificationGroupMember.user_id == user.id,
                VerificationGroupMember.is_active.is_(True),
            )
            .first()
            is not None
        )
        if is_ops_member:
            verify_plans = (
                db.query(BuyPlan.id)
                .filter(
                    BuyPlan.status == BuyPlanStatus.ACTIVE,
                    BuyPlan.so_status == SOVerificationStatus.PENDING,
                    BuyPlan.so_verified_by_id.is_(None),
                )
                .all()
            )
            items.extend(AlertItem(ref_id=plan_id, anchor=f"bp-{plan_id}") for (plan_id,) in verify_plans)

        return items

    def count_for_user(self, db: Session, user: User) -> int:
        return len(self._actionable_items(db, user))

    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        return self._actionable_items(db, user)
