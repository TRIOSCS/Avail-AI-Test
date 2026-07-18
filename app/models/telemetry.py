"""Trust-telemetry table — durable reconcile tallies.

What: ``ReconcileRun`` persists one row per ``reconcile_decoded_facets`` execution
      (dry-run AND apply) so the per-class tallies survive container rotation — both
      pre-migration reconcile rounds' apply tallies were runtime-log-only and are
      already unrecoverable.
Called by: app/management/reconcile_decoded_facets.py (record_reconcile_run writes
      ReconcileRun); ad-hoc trust reporting reads it.
Depends on: Base, UTCDateTime (app/database.py); migration
      alembic/versions/104_trust_telemetry.py.

NOTE: this module used to also define ``FacetAudit`` (table ``facet_audits``,
created alongside ``reconcile_runs`` in the same 104_trust_telemetry migration
for a planned Phase-2.2 volume-weighted accuracy audit harness). That harness
(``app/management/audit_facets.py``) was never built, so the model had zero
readers/writers; it was removed as dead code (see
docs/audit/2026-07-18-non-production-code-audit.md ss1) and the table dropped by
alembic/versions/197_drop_facet_audits_kconfig.py.
"""

from datetime import UTC, datetime

from sqlalchemy import Column, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from ..database import UTCDateTime
from .base import Base


class ReconcileRun(Base):
    """One reconcile_decoded_facets execution: scope + per-class tallies, durable."""

    __tablename__ = "reconcile_runs"

    id = Column(Integer, primary_key=True)
    ran_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(UTC), index=True)
    mode = Column(String(8), nullable=False)  # 'dry-run' | 'apply'
    sources = Column(JSONB, nullable=False)  # list[str] — the facet sources reconciled
    keys = Column(JSONB, nullable=False)  # list[str] — the spec_keys reconciled
    by_class = Column(JSONB, nullable=False)  # {failure_class: {action: count}}
    totals = Column(JSONB, nullable=False)  # {cards, facets, corrected, deleted, unchanged, skipped, failed}
