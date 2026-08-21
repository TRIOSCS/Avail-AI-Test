"""Shared mixins for the marketplace worker model triples (TBF / NC / ICS).

The Broker Forum, NetComponents, and ICsource workers each own a search_queue,
search_log, and worker_status table that are identical modulo the tbf/nc/ics
prefix. These mixins declare the shared columns once and derive the historical
constraint/index names (uq_tbf_queue_requirement_mpn, ix_nc_queue_poll,
ck_ics_worker_status_singleton, ...) from each concrete class's __tablename__,
so the emitted DDL is byte-identical to the previous hand-copied models.

The search-log queue FK stays on the concrete classes: its FK target and index
naming differ per marketplace (tbf/nc use column-level index=True; ics has the
named ix_ics_log_queue index).

Called by: app/models/{tbf,nc,ics}_search_queue.py, *_search_log.py,
           *_worker_status.py
Depends on: database.UTCDateTime, models.base.Base (concrete classes)
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declared_attr

from ..database import UTCDateTime


def _prefix(cls: Any) -> str:
    """Marketplace prefix from the table name: 'tbf_search_queue' → 'tbf'."""
    return str(cls.__tablename__).split("_", 1)[0]


class SearchQueueMixin:
    """Parts pending marketplace search — one row per (requirement, MPN)."""

    @declared_attr
    def id(cls):
        return Column(Integer, primary_key=True)

    # Dedup is keyed on (requirement_id, normalized_mpn) so one requirement
    # can have multiple queue rows when the spec-code resolver enqueues
    # additional AVL MPNs alongside the primary MPN. Application-level
    # check lives in QueueManager.enqueue_search.
    @declared_attr
    def requirement_id(cls):
        return Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)

    @declared_attr
    def requisition_id(cls):
        return Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)

    @declared_attr
    def mpn(cls):
        return Column(String(100), nullable=False)

    @declared_attr
    def normalized_mpn(cls):
        return Column(String(100), nullable=False)

    @declared_attr
    def manufacturer(cls):
        return Column(String(200))

    @declared_attr
    def description(cls):
        return Column(Text)

    @declared_attr
    def commodity_class(cls):
        return Column(String(50))

    @declared_attr
    def gate_decision(cls):
        return Column(String(20))

    @declared_attr
    def gate_reason(cls):
        return Column(String(200))

    @declared_attr
    def priority(cls):
        return Column(SmallInteger, default=3)

    @declared_attr
    def status(cls):
        return Column(String(20), default="pending")

    @declared_attr
    def search_count(cls):
        return Column(Integer, default=0)

    @declared_attr
    def last_searched_at(cls):
        return Column(UTCDateTime)

    @declared_attr
    def results_count(cls):
        return Column(Integer)

    @declared_attr
    def error_message(cls):
        return Column(Text)

    # Spec-code resolver lineage — populated when this queue row was created
    # for an AVL MPN resolved from an OEM spec code (see SpecCodeResolver).
    @declared_attr
    def resolved_via_spec_code(cls):
        return Column(String(64), nullable=True)

    @declared_attr
    def created_at(cls):
        return Column(UTCDateTime, default=lambda: datetime.now(UTC))

    @declared_attr
    def updated_at(cls):
        return Column(UTCDateTime, default=lambda: datetime.now(UTC))

    @declared_attr.directive
    def __table_args__(cls):
        p = _prefix(cls)
        return (
            # DB-level backing for the (requirement_id, normalized_mpn) dedup that
            # QueueManager.enqueue_search checks in Python. The app check loses to a
            # concurrent enqueue race; this constraint makes the duplicate insert
            # fail loudly (caught as IntegrityError) instead of silently doubling.
            UniqueConstraint("requirement_id", "normalized_mpn", name=f"uq_{p}_queue_requirement_mpn"),
            Index(
                f"ix_{p}_queue_poll",
                "status",
                "priority",
                "created_at",
                postgresql_where=(Column("status") == "queued"),
            ),
            Index(
                f"ix_{p}_queue_dedup",
                "normalized_mpn",
                cls.last_searched_at.desc(),
                postgresql_where=(Column("status") == "completed"),
            ),
        )


class SearchLogMixin:
    """Audit trail for every marketplace search attempt.

    Concrete classes declare id + their queue_id FK (per-marketplace index naming) and
    inherit the shared measurement columns here.
    """

    @declared_attr
    def searched_at(cls):
        return Column(UTCDateTime, default=lambda: datetime.now(UTC))

    @declared_attr
    def duration_ms(cls):
        return Column(Integer)

    @declared_attr
    def results_found(cls):
        return Column(Integer)

    @declared_attr
    def sightings_created(cls):
        return Column(Integer)

    @declared_attr
    def page_html_hash(cls):
        return Column(String(64))

    @declared_attr
    def error(cls):
        return Column(Text)


class WorkerStatusMixin:
    """Singleton worker-health row (id=1, enforced via CHECK constraint).

    Business Rules:
    - Exactly one row exists (id=1), inserted by migration
    - Worker updates this row periodically with heartbeat and stats
    - API server reads it for the worker health/status endpoints
    """

    @declared_attr
    def id(cls):
        return Column(Integer, primary_key=True, default=1)

    @declared_attr
    def is_running(cls):
        return Column(Boolean, default=False)

    @declared_attr
    def last_heartbeat(cls):
        return Column(UTCDateTime)

    @declared_attr
    def last_search_at(cls):
        return Column(UTCDateTime)

    @declared_attr
    def searches_today(cls):
        return Column(Integer, default=0)

    @declared_attr
    def sightings_today(cls):
        return Column(Integer, default=0)

    @declared_attr
    def circuit_breaker_open(cls):
        return Column(Boolean, default=False)

    @declared_attr
    def circuit_breaker_reason(cls):
        return Column(Text)

    @declared_attr
    def daily_stats_json(cls):
        return Column(JSON)

    @declared_attr
    def updated_at(cls):
        return Column(UTCDateTime, default=lambda: datetime.now(UTC))

    @declared_attr.directive
    def __table_args__(cls):
        return (CheckConstraint("id = 1", name=f"ck_{_prefix(cls)}_worker_status_singleton"),)
