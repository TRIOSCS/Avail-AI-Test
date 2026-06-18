"""BuyplanActionSource — ACTION alert for buy-plan steps that need MY action.

Surfaces the open work the current user personally owns across three buy-plan roles,
unioned into one count + spotlight list:

  1. Buyer PO step — a BuyPlanLine assigned to me that is still AWAITING_PO.
  2. Manager approval — a PENDING buy plan with no approver yet, where my role lets
     me approve (manager/admin — the same rule buyplan_workflow.approve_buy_plan enforces).
  3. Ops verify — a buy plan whose SO is still PENDING and unverified, where I am an
     active member of the ops verification group (the same rule verify_so enforces).

As an ACTION source the count derives PURELY from work-state: it does NOT subtract
seen_ref_ids. ``alert_seen`` only gates the cosmetic one-time in-tab pulse (handled
elsewhere); the item leaves the count only when the underlying work is done. Hence
``count_for_user`` == ``len(new_items_for_user(...))`` — both delegate to one helper.

Called by: services/alerts/registry.py (registered centrally by the parent).
Depends on: services/alerts/base.AlertSource, models/buy_plan.{BuyPlan,BuyPlanLine,
            VerificationGroupMember}, constants.{AlertKind,BuyPlanStatus,
            BuyPlanLineStatus,SOVerificationStatus,UserRole}.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import (
    AlertKind,
    BuyPlanLineStatus,
    BuyPlanStatus,
    SOVerificationStatus,
    UserRole,
)
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine, VerificationGroupMember

from ..base import AlertItem, AlertSource, Temperament

# Roles permitted to approve a pending buy plan. Single source of truth mirrors
# buyplan_workflow.approve_buy_plan's allowed set ({"manager", "admin"}) — if the
# approval rule changes there, change it here too so the badge can never count a
# step the user could not actually act on.
_APPROVER_ROLES = frozenset({UserRole.MANAGER, UserRole.ADMIN})


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

        # 1. Buyer PO step — lines assigned to me, still awaiting their PO. Anchor to the
        #    owning plan's row (the list is per-plan); ref_id stays the line id.
        po_lines = (
            db.query(BuyPlanLine.id, BuyPlanLine.buy_plan_id)
            .filter(
                BuyPlanLine.buyer_id == user.id,
                BuyPlanLine.status == BuyPlanLineStatus.AWAITING_PO,
            )
            .all()
        )
        items.extend(AlertItem(ref_id=line_id, anchor=f"bp-{plan_id}") for (line_id, plan_id) in po_lines)

        # 2. Manager approval — pending plans with no approver, if my role can approve.
        if user.role in _APPROVER_ROLES:
            approval_plans = (
                db.query(BuyPlan.id)
                .filter(
                    BuyPlan.status == BuyPlanStatus.PENDING,
                    BuyPlan.approved_by_id.is_(None),
                )
                .all()
            )
            items.extend(AlertItem(ref_id=plan_id, anchor=f"bp-{plan_id}") for (plan_id,) in approval_plans)

        # 3. Ops verify — SO still pending + unverified, if I'm an active ops member.
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
