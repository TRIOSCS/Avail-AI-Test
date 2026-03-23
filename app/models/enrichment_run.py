"""Enrichment run model — tracks autonomous enrichment pipeline state.

Persists batch IDs, request maps, and progress so the pipeline can resume
after container restarts. Each phase creates one or more EnrichmentRun rows.

Called by: scripts/enrich_orchestrator.py
Depends on: app.models.base.Base
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class EnrichmentRun(Base):
    __tablename__ = "enrichment_runs"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(100), unique=True, nullable=False)
    phase = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    batch_ids = Column(JSONB, default=list)
    request_map = Column(JSONB, default=dict)
    progress = Column(JSONB, default=dict)
    stats = Column(JSONB, default=dict)
    error_message = Column(Text)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_enrichment_runs_phase", "phase"),
        Index("ix_enrichment_runs_status", "status"),
        Index("ix_enrichment_runs_created_at", "created_at"),
    )
