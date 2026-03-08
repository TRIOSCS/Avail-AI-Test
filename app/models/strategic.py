"""StrategicVendor — per-buyer strategic vendor assignments.

Each buyer can claim up to 10 strategic vendors. Vendors with no offer
within 39 days auto-expire back to the open pool. Only one buyer can
claim a given vendor at a time.

Called by: services/strategic_vendor_service.py, routers/strategic.py
Depends on: models/base.py, models/auth.py, models/vendors.py
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .base import Base


class StrategicVendor(Base):
    __tablename__ = "strategic_vendors"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"), nullable=False)
    claimed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_offer_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    released_at = Column(DateTime(timezone=True), nullable=True)
    release_reason = Column(String(20), nullable=True)

    # Relationships
    user = relationship("User", backref="strategic_vendors")
    vendor_card = relationship("VendorCard", backref="strategic_vendors")

    __table_args__ = (
        UniqueConstraint("user_id", "vendor_card_id", name="uq_user_vendor_strategic"),
        Index("ix_strategic_user_released", "user_id", "released_at"),
        Index("ix_strategic_expires_released", "expires_at", "released_at"),
        Index("ix_strategic_vendor_released", "vendor_card_id", "released_at"),
    )

    def __repr__(self):
        return f"<StrategicVendor user={self.user_id} vendor={self.vendor_card_id}>"
