"""OfferConfirmedSource — FYI alert for new confirmed (approved + qualified) offers.

Surfaces APPROVED offers that the buyer hasn't seen yet, on requirements the buyer
owns (assigned to them, or unassigned on a requisition they created). Drives the
sales-hub nav badge + in-tab spotlight. As an FYI source the count excludes
alert_seen rows, so opening an offer drains the badge. Honors the per-user
Profile toggle ``User.notify_new_offer_alert_enabled``: when off the count/items
are forced to 0/empty so the badge is suppressed for that user.

Called by: services/alerts/registry.py (registered centrally by the parent).
Depends on: services/alerts/base.AlertSource, models/offers.Offer,
            models/sourcing.{Requirement,Requisition}, constants.{AlertKind,
            OfferStatus,QualificationStatus}.
"""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import Query, Session

from app.constants import AlertKind, OfferStatus, QualificationStatus
from app.models.auth import User
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition

from ..base import AlertItem, AlertSource, Temperament


class OfferConfirmedSource(AlertSource):
    """New confirmed offers on the buyer's requirements (FYI)."""

    key = "sales_hub_offers"
    kind = AlertKind.OFFER_CONFIRMED
    temperament = Temperament.FYI

    def _eligible_query(self, db: Session, user: User) -> Query[Offer]:
        """Confirmed, qualified, recent, mine, unseen offers — ordered oldest-first.

        Ownership ("mine") joins each Offer → its Requirement → that Requirement's
        Requisition so the unassigned fallback can read ``Requisition.created_by``.
        The OR expresses: the requirement is assigned to me, OR it is unassigned
        (assigned_buyer_id IS NULL) and I created the parent requisition.
        """

        floor = self.recency_floor()
        seen = self.seen_ref_ids(db, user)

        query = (
            db.query(Offer)
            .join(Requirement, Offer.requirement_id == Requirement.id)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(
                Offer.status == OfferStatus.APPROVED,
                Offer.qualification_status.in_((QualificationStatus.ESSENTIALS, QualificationStatus.COMPLETE)),
                Offer.approved_at.isnot(None),
                Offer.approved_at >= floor,
                or_(
                    Requirement.assigned_buyer_id == user.id,
                    and_(
                        Requirement.assigned_buyer_id.is_(None),
                        Requisition.created_by == user.id,
                    ),
                ),
            )
        )
        if seen:
            query = query.filter(Offer.id.notin_(seen))
        return query.order_by(Offer.approved_at.asc())

    def count_for_user(self, db: Session, user: User) -> int:
        if not getattr(user, "notify_new_offer_alert_enabled", True):
            return 0
        return self._eligible_query(db, user).count()

    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        if not getattr(user, "notify_new_offer_alert_enabled", True):
            return []
        offers = self._eligible_query(db, user).all()
        return [AlertItem(ref_id=o.id, anchor=f"req-{o.requirement_id}") for o in offers]
