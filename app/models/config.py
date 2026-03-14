"""System configuration models — API sources, config, Graph subscriptions."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .base import Base


class ApiSource(Base):
    __tablename__ = "api_sources"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    category = Column(String(50), nullable=False)
    source_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    is_active = Column(Boolean, default=False, server_default="false")
    description = Column(String(500))
    setup_notes = Column(Text)
    signup_url = Column(String(500))
    env_vars = Column(JSON, default=list)
    credentials = Column(JSONB, default=dict)
    last_success = Column(DateTime)
    last_error = Column(String(500))
    last_error_at = Column(DateTime)
    error_count_24h = Column(Integer, default=0, nullable=False, server_default="0")
    total_searches = Column(Integer, default=0)
    total_results = Column(Integer, default=0)
    avg_response_ms = Column(Integer, default=0)
    monthly_quota = Column(Integer, nullable=True)
    calls_this_month = Column(Integer, default=0, server_default="0")
    last_ping_at = Column(DateTime)
    last_deep_test_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SystemConfig(Base):
    """Key-value runtime configuration. Survives restarts, auditable."""

    __tablename__ = "system_config"
    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=False)
    description = Column(String(500))
    updated_by = Column(String(255))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class GraphSubscription(Base):
    """Tracks active Graph API webhook subscriptions per user."""

    __tablename__ = "graph_subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subscription_id = Column(String(255), nullable=False, unique=True)
    resource = Column(String(255), nullable=False)
    change_type = Column(String(100), nullable=False)
    expiration_dt = Column(DateTime, nullable=False)
    client_state = Column(String(255))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_graphsub_user", "user_id"),
        Index("ix_graphsub_expiry", "expiration_dt"),
    )


class ApiUsageLog(Base):
    """Tracks individual API calls for usage monitoring and health history."""

    __tablename__ = "api_usage_log"
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("api_sources.id", ondelete="CASCADE"), nullable=False)
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    endpoint = Column(String(200))
    status_code = Column(Integer)
    response_ms = Column(Integer)
    success = Column(Boolean, nullable=False)
    error_message = Column(String(500))
    check_type = Column(String(20), nullable=False)

    __table_args__ = (Index("ix_usage_log_source_ts", "source_id", "timestamp"),)
