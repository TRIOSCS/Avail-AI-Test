"""InboundCustomerSource — FYI alert for new inbound communications from customers.

Surfaces inbound emails/calls/messages logged against a Customer-type account that
the current user can manage (account owner OR site owner), or ALL accounts for
manager/admin users. Drives the CRM nav badge + in-tab spotlight. As an FYI source
the count excludes alert_seen rows, so opening the conversation drains the badge.

Visibility mirrors the Phase 2 ownership model in cdm_company_query:
- manager/admin: see all inbound-customer alerts.
- rep: see alerts for accounts they own (account_owner_id) OR where they own a site.

The "new" timestamp is COALESCE(occurred_at, created_at): poll-logged rows often carry
a NULL occurred_at, so the row's created_at is the recency fallback.

Called by: services/alerts/registry.py (registered centrally by the parent).
Depends on: services/alerts/base.AlertSource, models/intelligence.ActivityLog,
            models/crm.{Company,CustomerSite}, constants.{AlertKind,Direction,Channel},
            dependencies.is_manager_or_admin.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.constants import AlertKind, Channel, Direction
from app.dependencies import is_manager_or_admin
from app.models.auth import User
from app.models.crm import Company, CustomerSite
from app.models.intelligence import ActivityLog

from ..base import AlertItem, AlertSource, Temperament

# Channels that count as a genuine inbound customer communication.
_INBOUND_CHANNELS = (Channel.EMAIL, Channel.PHONE, Channel.TEAMS, Channel.WECHAT)

# Company.account_type is a free-text column (no StrEnum); "Customer" is the canonical
# value (see crm_service.CDM_ACCOUNT_TYPES). Named here so the filter isn't a bare literal
# and a future rename/normalization has one place to change — a casing drift would
# otherwise silently zero this alert.
_CUSTOMER_ACCOUNT_TYPE = "Customer"


class InboundCustomerSource(AlertSource):
    """New inbound customer communications on accounts the user can manage (FYI)."""

    key = "crm_inbound"
    kind = AlertKind.INBOUND_CUSTOMER
    temperament = Temperament.FYI

    def _eligible_query(self, db: Session, user: User):
        """Inbound, recent, mine, undismissed, unseen activity — ordered oldest-first.

        Joins ActivityLog.company_id → Company and applies Phase 2 ownership visibility:
        - manager/admin: no ownership filter (see all Customer accounts).
        - rep: account_owner_id == user OR user owns a site under the company.

        The new-timestamp is ``COALESCE(occurred_at, created_at)`` — used both for the
        recency floor and the oldest-unseen-first ordering.
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
                Company.account_type == _CUSTOMER_ACCOUNT_TYPE,
                new_ts >= floor,
            )
        )

        if not is_manager_or_admin(user):
            # Reps see alerts for accounts they own directly OR where they own a site —
            # mirrors the my_only visibility rule in cdm_company_query.
            site_company_ids = select(CustomerSite.company_id).where(CustomerSite.owner_id == user.id)
            query = query.filter(
                or_(
                    Company.account_owner_id == user.id,
                    Company.id.in_(site_company_ids),
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
