"""AlertSeen model — per-user read-state for cross-app alerts.

Records that a user has SEEN a specific alert item (an offer, an inbound activity,
a buy-plan step). FYI alert counts EXCLUDE seen items (seeing drains the badge);
ACTION alert counts ignore this table for counting and use it only to suppress
re-pulsing a row the user has already looked at.

Called by: models/__init__.py (re-exported for DB schema), services/alerts/*.
Depends on: models/base.py, models/auth.py, database.py (UTCDateTime).
"""

from datetime import UTC, datetime

from sqlalchemy import Column, ForeignKey, Index, Integer, String, UniqueConstraint

from ..database import UTCDateTime
from .base import Base


class AlertSeen(Base):
    __tablename__ = "alert_seen"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # AlertKind value — offer_confirmed, inbound_customer, inbound_vendor, buyplan_action
    alert_kind = Column(String(40), nullable=False)
    ref_id = Column(Integer, nullable=False)  # the source item's id (offer.id, activity_log.id, ...)
    seen_at = Column(UTCDateTime, default=lambda: datetime.now(UTC), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "alert_kind", "ref_id", name="uq_alert_seen_user_kind_ref"),
        Index("ix_alert_seen_user_kind", "user_id", "alert_kind"),
    )
