"""models/dedup_decision.py — persisted dedup decisions for the Data Ops surface.

DedupDecision: one row per DISMISSED candidate pair (vendor/company/contact). Pairs
are stored canonically as (id_a, id_b) = (min, max) — the UI emits keeper-first
tokens, so dedup_decision_service.canonical_pair MUST run on every write and lookup
or one pair would store under two keys. Dismissals only; no decision column — human
merges/deletes are audited in DedupMergeAudit instead. Deliberately NO FKs to the
entity tables: three different tables (vendor_cards / companies / site_contacts)
share this seam, and stale rows are pruned app-side on merge/delete.

DedupMergeAudit: append-only audit trail for the human dedup merge / delete-both
actions (UserAdminAudit shape — app/models/auth.py). Names are denormalized at
action time because the loser row is deleted by the merge. Explicit rows, not
audit_listeners: bulk paths bypass listener stamping (see
app/services/audit_listeners.py:10-16).

Called by: services/dedup_decision_service.py (sole writer/reader)
Depends on: app.database (UTCDateTime), app.models.base (Base)
"""

from datetime import UTC, datetime

from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint

from ..database import UTCDateTime
from .base import Base


class DedupDecision(Base):
    __tablename__ = "dedup_decisions"
    __table_args__ = (UniqueConstraint("entity_type", "id_a", "id_b", name="uq_dedup_decision_pair"),)

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(16), nullable=False)  # 'vendor' | 'company' | 'contact'
    id_a = Column(Integer, nullable=False)  # canonical: always min(pair)
    id_b = Column(Integer, nullable=False)  # canonical: always max(pair)
    decided_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))


class DedupMergeAudit(Base):
    __tablename__ = "dedup_merge_audit"

    id = Column(Integer, primary_key=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    entity_type = Column(String(16), nullable=False)  # 'vendor' | 'company' | 'contact'
    action = Column(String(16), nullable=False)  # 'merge' | 'delete_both'
    kept_id = Column(Integer, nullable=True)  # NULL for delete_both (nothing kept)
    kept_name = Column(String(255), nullable=True)
    removed_id = Column(Integer, nullable=False)
    removed_name = Column(String(255), nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC), index=True)
