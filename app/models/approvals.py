"""approvals.py — Approval Engine ORM models (6 tables).

Purpose: Core approval-workflow tables: ApprovalRequest, ApprovalStep,
         ApprovalStepRecipient, ApprovalEvent, ApprovalOutbox,
         ApprovalGateConfig.

Called by: services/approvals.py (not yet written), routers/approvals.py (Task 3+)
Depends on: models.base, app.constants (ApprovalGateType, ApprovalRequestStatus,
            ApprovalRecipientStatus, ApprovalStepRule), models.auth (User)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from ..constants import (
    ApprovalRecipientStatus,
    ApprovalRequestStatus,
    ApprovalStepRule,
)
from ..database import UTCDateTime
from .base import Base

__all__ = [
    "ApprovalRequest",
    "ApprovalStep",
    "ApprovalStepRecipient",
    "ApprovalEvent",
    "ApprovalOutbox",
    "ApprovalGateConfig",
]


# ── ApprovalRequest ────────────────────────────────────────────────────────────


class ApprovalRequest(Base):
    """Root record for a single approval workflow instance.

    One row per event requiring sign-off (e.g. a prepayment, a buy-plan). The gate_type
    column distinguishes which workflow this belongs to. Subject FKs link back to the
    entity being approved (quality plan or prepayment).
    """

    __tablename__ = "approval_requests"

    id = Column(Integer, primary_key=True)

    gate_type = Column(String(50), nullable=False)  # ApprovalGateType
    status = Column(String(50), nullable=False, default=ApprovalRequestStatus.REQUESTED)

    # Amount + currency for spend-gate decisions
    amount = Column(Numeric(12, 2), nullable=True)
    currency = Column(String(10), default="USD")

    # Who triggered this request + who owns the originating entity
    requested_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Subject entity (one of these is populated, rest NULL)
    subject_quality_plan_id = Column(Integer, ForeignKey("quality_plans.id", ondelete="SET NULL"), nullable=True)
    subject_prepayment_id = Column(Integer, ForeignKey("prepayments.id", ondelete="SET NULL"), nullable=True)

    # Resolution tracking
    resolved_at = Column(UTCDateTime, nullable=True)
    resolution_note = Column(Text, nullable=True)
    expires_at = Column(UTCDateTime, nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc))

    # ── Relationships
    requested_by = relationship("User", foreign_keys=[requested_by_id])
    owner = relationship("User", foreign_keys=[owner_id])
    steps = relationship("ApprovalStep", back_populates="request", cascade="all, delete-orphan")
    events = relationship("ApprovalEvent", back_populates="request", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_approval_req_owner", "owner_id"),
        Index("ix_approval_req_status", "status"),
        Index("ix_approval_req_gate_type", "gate_type"),
        Index("ix_approval_req_subject_qp", "subject_quality_plan_id"),
        Index("ix_approval_req_subject_pp", "subject_prepayment_id"),
    )


# ── ApprovalStep ───────────────────────────────────────────────────────────────


class ApprovalStep(Base):
    """One ordered step (stage) within an ApprovalRequest.

    Steps are processed in seq order. rule controls quorum: 'any' (one approval
    suffices) or 'all' (every recipient must approve).
    """

    __tablename__ = "approval_steps"

    id = Column(Integer, primary_key=True)

    request_id = Column(Integer, ForeignKey("approval_requests.id", ondelete="CASCADE"), nullable=False)
    seq = Column(Integer, nullable=False, default=1)
    rule = Column(String(20), nullable=False, default=ApprovalStepRule.ANY)  # any | all
    status = Column(String(50), nullable=False, default=ApprovalRecipientStatus.PENDING)

    resolved_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # ── Relationships
    request = relationship("ApprovalRequest", back_populates="steps")
    recipients = relationship("ApprovalStepRecipient", back_populates="step", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_approval_step_request", "request_id"),
        Index("ix_approval_step_status", "status"),
    )


# ── ApprovalStepRecipient ──────────────────────────────────────────────────────


class ApprovalStepRecipient(Base):
    """Per-user assignment within an ApprovalStep.

    Unique per (step, user) — a user can only appear once per step. status tracks their
    individual decision (pending → approved/rejected/reassigned).
    """

    __tablename__ = "approval_step_recipients"

    id = Column(Integer, primary_key=True)

    step_id = Column(Integer, ForeignKey("approval_steps.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    status = Column(String(50), nullable=False, default=ApprovalRecipientStatus.PENDING)
    decided_at = Column(UTCDateTime, nullable=True)
    decision_note = Column(Text, nullable=True)

    # If reassigned, tracks who took over
    reassigned_to_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # ── Relationships
    step = relationship("ApprovalStep", back_populates="recipients")
    user = relationship("User", foreign_keys=[user_id])
    reassigned_to = relationship("User", foreign_keys=[reassigned_to_id])

    __table_args__ = (
        UniqueConstraint("step_id", "user_id", name="uq_approval_step_recipient"),
        Index("ix_approval_recip_step", "step_id"),
        Index("ix_approval_recip_user", "user_id"),
        Index("ix_approval_recip_status", "status"),
    )


# ── ApprovalEvent ──────────────────────────────────────────────────────────────


class ApprovalEvent(Base):
    """Immutable audit trail for every state change within an ApprovalRequest.

    Append-only: never update or delete rows. event_type is a short label such
    as 'submitted', 'approved', 'rejected', 'step_advanced', 'cancelled'.
    """

    __tablename__ = "approval_events"

    id = Column(Integer, primary_key=True)

    request_id = Column(Integer, ForeignKey("approval_requests.id", ondelete="CASCADE"), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    event_type = Column(String(50), nullable=False)
    note = Column(Text, nullable=True)
    payload = Column(JSON, nullable=True)  # extra structured context

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # ── Relationships
    request = relationship("ApprovalRequest", back_populates="events")
    actor = relationship("User", foreign_keys=[actor_id])

    __table_args__ = (
        Index("ix_approval_event_request", "request_id"),
        Index("ix_approval_event_actor", "actor_id"),
        Index("ix_approval_event_type", "event_type"),
    )


# ── ApprovalOutbox ─────────────────────────────────────────────────────────────


class ApprovalOutbox(Base):
    """Transactional outbox for notifications triggered by approval state changes.

    A background worker polls for rows where sent_at IS NULL and dispatches them (email,
    in-app alert, etc.). On success it sets sent_at; on failure it increments fail_count
    and records last_error.
    """

    __tablename__ = "approval_outbox"

    id = Column(Integer, primary_key=True)

    request_id = Column(Integer, ForeignKey("approval_requests.id", ondelete="CASCADE"), nullable=False)
    recipient_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    channel = Column(String(50), nullable=False, default="email")  # email | in_app
    payload = Column(JSON, nullable=True)

    sent_at = Column(UTCDateTime, nullable=True)
    fail_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    # ── Relationships
    request = relationship("ApprovalRequest")
    recipient = relationship("User", foreign_keys=[recipient_user_id])

    __table_args__ = (
        Index("ix_approval_outbox_request", "request_id"),
        Index("ix_approval_outbox_recipient", "recipient_user_id"),
        Index("ix_approval_outbox_sent", "sent_at"),
    )


# ── ApprovalGateConfig ─────────────────────────────────────────────────────────


class ApprovalGateConfig(Base):
    """Per-gate configuration row: which user is the approver and up to what amount.

    Supports one active config per gate_type (active=True). max_amount=NULL means
    the gate applies to any amount. Multiple inactive rows can coexist for audit.
    """

    __tablename__ = "approval_gate_configs"

    id = Column(Integer, primary_key=True)

    gate_type = Column(String(50), nullable=False)  # ApprovalGateType
    approver_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    max_amount = Column(Numeric(12, 2), nullable=True)
    active = Column(Boolean, nullable=False, default=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(UTCDateTime, onupdate=lambda: datetime.now(timezone.utc))

    # ── Relationships
    approver = relationship("User", foreign_keys=[approver_user_id])

    __table_args__ = (
        Index("ix_approval_gate_cfg_type", "gate_type"),
        Index("ix_approval_gate_cfg_approver", "approver_user_id"),
        Index("ix_approval_gate_cfg_active", "active"),
    )
