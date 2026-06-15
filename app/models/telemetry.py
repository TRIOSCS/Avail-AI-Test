"""Trust-telemetry tables — durable reconcile tallies + facet-audit verdicts.

What: ``ReconcileRun`` persists one row per ``reconcile_decoded_facets`` execution
      (dry-run AND apply) so the per-class tallies survive container rotation — both
      pre-migration reconcile rounds' apply tallies were runtime-log-only and are
      already unrecoverable. ``FacetAudit`` stores one verdict per audited facet row
      (correct / wrong / unverifiable) for the volume-weighted accuracy audits; it is
      created in the same migration (104_trust_telemetry) so Phase 2.2's audit harness
      needs no second migration.
Called by: app/management/reconcile_decoded_facets.py (record_reconcile_run writes
      ReconcileRun); the Phase-2.2 audit harness (app/management/audit_facets.py,
      future) writes FacetAudit; ad-hoc trust reporting reads both.
Depends on: Base, UTCDateTime (app/database.py); migration
      alembic/versions/104_trust_telemetry.py.
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from ..database import UTCDateTime
from .base import Base

# The closed verdict vocabulary — mirrored by ck_facet_audits_verdict at the DB level
# so a bypassing writer cannot persist a fourth state.
FACET_AUDIT_VERDICTS: frozenset[str] = frozenset({"correct", "wrong", "unverifiable"})


class ReconcileRun(Base):
    """One reconcile_decoded_facets execution: scope + per-class tallies, durable."""

    __tablename__ = "reconcile_runs"

    id = Column(Integer, primary_key=True)
    ran_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    mode = Column(String(8), nullable=False)  # 'dry-run' | 'apply'
    sources = Column(JSONB, nullable=False)  # list[str] — the facet sources reconciled
    keys = Column(JSONB, nullable=False)  # list[str] — the spec_keys reconciled
    by_class = Column(JSONB, nullable=False)  # {failure_class: {action: count}}
    totals = Column(JSONB, nullable=False)  # {cards, facets, corrected, deleted, unchanged, skipped, failed}


class FacetAudit(Base):
    """One audited facet row's verdict (Phase 2.2 volume-weighted accuracy audits)."""

    __tablename__ = "facet_audits"

    id = Column(Integer, primary_key=True)
    audited_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    card_id = Column(Integer, index=True)  # No FK — the audit record survives card deletion
    category = Column(String(64))
    spec_key = Column(String(64))
    value = Column(Text)  # the audited value as displayed (text projection)
    source = Column(String(32))  # facet provenance at audit time (desc_parse, mpn_decode, …)
    verdict = Column(String(16), nullable=False)  # FACET_AUDIT_VERDICTS member
    notes = Column(Text)

    @validates("verdict")
    def _validate_verdict(self, _key: str, value: str) -> str:
        if value not in FACET_AUDIT_VERDICTS:
            raise ValueError(f"facet_audits.verdict must be one of {sorted(FACET_AUDIT_VERDICTS)}, got {value!r}")
        return value

    __table_args__ = (
        Index("ix_facet_audits_category_key", "category", "spec_key"),
        CheckConstraint(
            "verdict IN ('correct', 'wrong', 'unverifiable')",
            name="ck_facet_audits_verdict",
        ),
    )
