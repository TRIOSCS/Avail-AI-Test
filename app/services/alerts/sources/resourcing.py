"""BuyplanResourcingSource — ACTION alert for the open re-sourcing claim pool.

Surfaces buy-plan lines whose cut PO was cancelled (vendor fell down) and are now
unassigned in the open pool, awaiting any buyer to claim + backfill. Unlike
BuyplanActionSource (which counts the work the user PERSONALLY owns), this is a
POOL-WIDE count shown to every PO-cutter (buyer/manager/admin) — it is a SEPARATE
source precisely because its ownership semantics differ. Registered on the same
"buy-plans" tab, so its count adds to that tab's badge.

ACTION temperament: the count derives purely from work-state (unclaimed RESOURCING
lines on ACTIVE plans); alert_seen only gates the one-time in-tab spotlight pulse.

Called by: services/alerts/sources/__init__.py (registered centrally).
Depends on: services/alerts/base.AlertSource, models/buy_plan.{BuyPlan,BuyPlanLine},
            constants.{AlertKind,BuyPlanStatus,BuyPlanLineStatus,UserRole}.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import AlertKind, BuyPlanLineStatus, BuyPlanStatus, UserRole
from app.models.auth import User
from app.models.buy_plan import BuyPlan, BuyPlanLine

from ..base import AlertItem, AlertSource, Temperament

# Only roles that can cut/claim POs see the open pool (sales/trader cannot).
_PO_CUTTER_ROLES = (UserRole.BUYER, UserRole.MANAGER, UserRole.ADMIN)


class BuyplanResourcingSource(AlertSource):
    """Open re-sourcing pool lines any PO-cutter can claim (ACTION, pool-wide)."""

    key = "buy_plans_resourcing"
    kind = AlertKind.BUYPLAN_RESOURCING
    temperament = Temperament.ACTION

    def _pool(self, db: Session, user: User) -> list[AlertItem]:
        if user.role not in _PO_CUTTER_ROLES:
            return []
        rows = (
            db.query(BuyPlanLine.id, BuyPlanLine.buy_plan_id)
            .join(BuyPlan, BuyPlanLine.buy_plan_id == BuyPlan.id)
            .filter(
                BuyPlanLine.status == BuyPlanLineStatus.RESOURCING,
                BuyPlanLine.buyer_id.is_(None),
                BuyPlan.status == BuyPlanStatus.ACTIVE,
            )
            .all()
        )
        return [AlertItem(ref_id=line_id, anchor=f"bp-{plan_id}") for (line_id, plan_id) in rows]

    def count_for_user(self, db: Session, user: User) -> int:
        return len(self._pool(db, user))

    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        return self._pool(db, user)
