"""InboundCustomerSource — FYI alert for new inbound communications from customers.

Surfaces inbound emails/calls/messages logged against a Customer-type account the
current user OWNS (Company.account_owner_id), that they haven't seen yet. Drives the
CRM nav badge + in-tab spotlight. As an FYI source the count excludes alert_seen rows,
so opening the conversation drains the badge.

The "new" timestamp is COALESCE(occurred_at, created_at): poll-logged rows often carry
a NULL occurred_at, so the row's created_at is the recency fallback.

Called by: services/alerts/registry.py (registered centrally by the parent).
Depends on: services/alerts/base.AlertSource, models/intelligence.ActivityLog,
            models/crm.Company, constants.{AlertKind,Direction,Channel}.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import AlertKind, Channel, Direction
from app.models.auth import User
from app.models.crm import Company
from app.models.intelligence import ActivityLog

from ..base import AlertItem, AlertSource, Temperament

# Channels that count as a genuine inbound customer communication.
_INBOUND_CHANNELS = (Channel.EMAIL, Channel.PHONE, Channel.TEAMS, Channel.WECHAT)


class InboundCustomerSource(AlertSource):
    """New inbound customer communications on accounts the user owns (FYI)."""

    key = "crm_inbound"
    kind = AlertKind.INBOUND_CUSTOMER
    temperament = Temperament.FYI

    def _eligible_query(self, db: Session, user: User):
        """Inbound, recent, mine, undismissed, unseen activity — ordered oldest-first.

        Joins ActivityLog.company_id → Company so ownership reads
        ``Company.account_owner_id`` and the account is a Customer
        (``Company.account_type == "Customer"``). The new-timestamp is
        ``COALESCE(occurred_at, created_at)`` — used both for the recency floor and
        the oldest-unseen-first ordering.
        """

        floor = self.recency_floor()
        seen = self.seen_ref_ids(db, user)
        new_ts = func.coalesce(ActivityLog.occurred_at, ActivityLog.created_at)

        query = (
            db.query(ActivityLog)
            .join(Company, ActivityLog.company_id == Company.id)
            .filter(
                ActivityLog.direction == Direction.INBOUND,
                ActivityLog.channel.in_(_INBOUND_CHANNELS),
                ActivityLog.company_id.isnot(None),
                ActivityLog.dismissed_at.is_(None),
                Company.account_type == "Customer",
                Company.account_owner_id == user.id,
                new_ts >= floor,
            )
        )
        if seen:
            query = query.filter(ActivityLog.id.notin_(seen))
        return query.order_by(new_ts.asc())

    def count_for_user(self, db: Session, user: User) -> int:
        return self._eligible_query(db, user).count()

    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        activities = self._eligible_query(db, user).all()
        return [AlertItem(ref_id=a.id, anchor=f"company-{a.company_id}") for a in activities]
