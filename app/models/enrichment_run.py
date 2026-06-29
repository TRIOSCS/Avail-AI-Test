"""Enrichment run model — legacy autonomous-enrichment pipeline state.

Persisted batch IDs, request maps, and progress so the old phase-based
orchestrator could resume after container restarts. That orchestrator
(``scripts/enrich_orchestrator.py`` + ``enrichment-entrypoint.sh``) was removed
when the paced ``app.services.enrichment_worker`` superseded it, so nothing
writes these rows anymore.

The ``enrichment_runs`` table still exists in the live schema (migrations
``001`` / ``071``), so this model is retained purely to keep
``Base.metadata`` in sync with the database and the schema-drift gate
(``scripts/check_schema_matches_models.py``) green. Dropping the table is a
separate, riskier decision (a real DROP migration), deliberately out of scope
for the dead-code cleanup that removed the orchestrator.

Depends on: app.models.base.Base
"""

from datetime import datetime, timezone

from sqlalchemy import Column, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ..database import UTCDateTime
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
    started_at = Column(UTCDateTime)
    completed_at = Column(UTCDateTime)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_enrichment_runs_phase", "phase"),
        Index("ix_enrichment_runs_status", "status"),
        Index("ix_enrichment_runs_created_at", "created_at"),
    )
