"""General CRM task — scoped to a requisition, company, contact, or vendor.

Table stays named requisition_tasks for backwards compatibility. At least one
parent FK must be set (enforced by CHECK ck_task_has_parent).

Tracks sourcing, sales, and general tasks through pipeline stages.
Auto-generated from system events (offers, RFQs, quotes) and manually
by buyers.

Called by: services/task_service.py, routers/htmx/* task endpoints
Depends on: models/base.py, models/auth.py, models/sourcing.py, models/vendors.py
"""

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from ..database import UTCDateTime
from .base import Base


class RequisitionTask(Base):
    __tablename__ = "requisition_tasks"

    id = Column(Integer, primary_key=True, index=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=True)
    site_contact_id = Column(Integer, ForeignKey("site_contacts.id", ondelete="CASCADE"), nullable=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=True)
    vendor_contact_id = Column(Integer, ForeignKey("vendor_contacts.id", ondelete="CASCADE"), nullable=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # sourcing | sales | general
    task_type = Column(String(20), nullable=False, default="general")
    # todo | in_progress | done
    status = Column(String(20), nullable=False, default="todo")
    # 1=low, 2=medium, 3=high
    priority = Column(Integer, nullable=False, default=2)

    # Assignment
    assigned_to_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Origin tracking
    source = Column(String(20), nullable=False, default="manual")  # manual | system | ai
    source_ref = Column(String(100), nullable=True)  # e.g. "offer:123", "rfq:456"

    # Completion
    completion_note = Column(Text, nullable=True)  # note from assignee on task resolution

    # Dates
    due_at = Column(UTCDateTime, nullable=True)
    completed_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    assignee = relationship("User", foreign_keys=[assigned_to_id])
    creator = relationship("User", foreign_keys=[created_by])
    company = relationship("Company", foreign_keys=[company_id])
    site_contact = relationship("SiteContact", foreign_keys=[site_contact_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    vendor_contact = relationship("VendorContact", foreign_keys=[vendor_contact_id])

    __table_args__ = (
        Index("ix_rt_req_status", "requisition_id", "status"),
        Index("ix_rt_assignee_status", "assigned_to_id", "status"),
        Index("ix_rt_status_due", "status", "due_at"),
        Index("ix_rt_creator_status", "created_by", "status"),
        Index("ix_rt_requirement", "requirement_id"),
        Index("ix_rt_company_status", "company_id", "status"),
        Index("ix_rt_contact_status", "site_contact_id", "status"),
        Index("ix_rt_vendor_card_status", "vendor_card_id", "status"),
        Index("ix_rt_vendor_contact_status", "vendor_contact_id", "status"),
        CheckConstraint(
            "requisition_id IS NOT NULL OR company_id IS NOT NULL"
            " OR site_contact_id IS NOT NULL"
            " OR vendor_card_id IS NOT NULL OR vendor_contact_id IS NOT NULL",
            name="ck_task_has_parent",
        ),
    )
