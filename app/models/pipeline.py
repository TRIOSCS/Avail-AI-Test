"""Email pipeline models — message dedup, sync state, column mapping, batches."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)

from .base import Base


class ProcessedMessage(Base):
    """H2: Deduplication — track messages already processed."""

    __tablename__ = "processed_messages"
    message_id = Column(Text, primary_key=True)
    processing_type = Column(Text, primary_key=True)
    processed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SyncState(Base):
    """H8: Delta Query state per user per folder."""

    __tablename__ = "sync_state"
    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    folder = Column(String(100), nullable=False)
    delta_token = Column(Text)
    last_sync_at = Column(DateTime)

    __table_args__ = (
        Index("ix_sync_state_user_folder", "user_id", "folder", unique=True),
    )


class ColumnMappingCache(Base):
    """Upgrade 2: Cache AI-detected column mappings for vendor attachments."""

    __tablename__ = "column_mapping_cache"
    id = Column(Integer, primary_key=True)
    vendor_domain = Column(Text, nullable=False)
    file_fingerprint = Column(Text, nullable=False)
    mapping = Column(JSON, nullable=False)
    confidence = Column(Float, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_colmap_domain_fp", "vendor_domain", "file_fingerprint", unique=True),
    )


class PendingBatch(Base):
    """Tracks Anthropic Batch API submissions for async AI processing."""

    __tablename__ = "pending_batches"
    id = Column(Integer, primary_key=True)
    batch_id = Column(String, nullable=False, index=True)
    batch_type = Column(String(50), default="inbox_parse")
    request_map = Column(JSON)
    status = Column(String(20), default="processing")
    submitted_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True))
    result_count = Column(Integer)
    error_message = Column(String)

    __table_args__ = (
        Index("ix_pending_batches_status", "status", "submitted_at"),
    )
