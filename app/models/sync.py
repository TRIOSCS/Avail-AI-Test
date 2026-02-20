"""Sync models — Acctivate inventory snapshots and sync logs."""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Index, Integer, JSON, String

from .base import Base


class InventorySnapshot(Base):
    """Current inventory from Acctivate — refreshed daily."""

    __tablename__ = "inventory_snapshots"
    id = Column(Integer, primary_key=True)
    product_id = Column(String(255), nullable=False, index=True)
    warehouse_id = Column(String(100))
    qty_on_hand = Column(Integer, default=0)
    synced_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_inv_product_warehouse", "product_id", "warehouse_id", unique=True),
    )


class SyncLog(Base):
    """Log of each data sync run."""

    __tablename__ = "sync_logs"
    id = Column(Integer, primary_key=True)
    source = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    duration_seconds = Column(Float)
    row_counts = Column(JSON)
    errors = Column(JSON)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("ix_sync_source_time", "source", "started_at"),)
