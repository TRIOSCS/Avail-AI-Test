"""MaterialPriceSnapshot — records price observations over time for trend tracking.

Called by: price_snapshot_service.record_price_snapshot()
Depends on: MaterialCard (FK)
"""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.sql import func

from .base import Base


class MaterialPriceSnapshot(Base):
    __tablename__ = "material_price_snapshots"
    __table_args__ = (Index("ix_price_snap_card_time", "material_card_id", "recorded_at"),)

    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), index=True, nullable=False)
    vendor_name = Column(String(200), nullable=False)
    price = Column(Numeric(12, 4), nullable=False)
    currency = Column(String(3), default="USD")
    quantity = Column(Integer, nullable=True)
    source = Column(String(50), nullable=False)
    recorded_at = Column(DateTime, server_default=func.now(), index=True)
