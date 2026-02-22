"""Intelligence models — Materials, Proactive, Activity."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import relationship

from .base import Base


class MaterialCard(Base):
    __tablename__ = "material_cards"
    id = Column(Integer, primary_key=True)
    normalized_mpn = Column(String(255), nullable=False, unique=True, index=True)
    display_mpn = Column(String(255), nullable=False)
    manufacturer = Column(String(255))
    description = Column(String(1000))
    search_count = Column(Integer, default=0)
    last_searched_at = Column(DateTime)
    search_vector = Column(TSVECTOR)

    # Enrichment fields (populated by AI agent)
    lifecycle_status = Column(String(50))  # active, nrfnd, eol, obsolete, ltb
    package_type = Column(String(100))  # QFP-64, BGA-256, 0603, etc.
    category = Column(String(255))  # Microcontroller, Capacitor, Connector, etc.
    rohs_status = Column(String(50))  # compliant, non-compliant, exempt
    pin_count = Column(Integer)
    datasheet_url = Column(String(1000))
    cross_references = Column(JSON, default=list)  # [{mpn, manufacturer}]
    specs_summary = Column(Text)  # Key electrical specs in plain text
    enrichment_source = Column(String(50))  # "gradient_agent", "manual", etc.
    enriched_at = Column(DateTime)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    vendor_history = relationship(
        "MaterialVendorHistory",
        back_populates="material_card",
        cascade="all, delete-orphan",
    )


class MaterialVendorHistory(Base):
    __tablename__ = "material_vendor_history"
    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id"), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    source_type = Column(String(50))
    is_authorized = Column(Boolean, default=False)
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    times_seen = Column(Integer, default=1)
    last_qty = Column(Integer)
    last_price = Column(Float)
    last_currency = Column(String(10), default="USD")
    last_manufacturer = Column(String(255))
    vendor_sku = Column(String(255))

    source = Column(String(50), default="api_sighting")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    material_card = relationship("MaterialCard", back_populates="vendor_history")

    __table_args__ = (
        Index("ix_mvh_card_vendor", "material_card_id", "vendor_name", unique=True),
        Index("ix_mvh_vendor", "vendor_name"),
    )


class ProactiveMatch(Base):
    """A match between a new vendor offer and an archived customer requirement."""

    __tablename__ = "proactive_matches"
    id = Column(Integer, primary_key=True)
    offer_id = Column(
        Integer, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id = Column(
        Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False
    )
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id"), nullable=False)
    salesperson_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    mpn = Column(String(255), nullable=False)
    status = Column(String(20), default="new")  # new | sent | dismissed | converted
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    offer = relationship("Offer", foreign_keys=[offer_id])
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    salesperson = relationship("User", foreign_keys=[salesperson_id])

    __table_args__ = (
        Index("ix_pm_offer", "offer_id"),
        Index("ix_pm_req", "requisition_id"),
        Index("ix_pm_site", "customer_site_id"),
        Index("ix_pm_sales", "salesperson_id"),
        Index("ix_pm_status", "status"),
        Index("ix_pm_mpn_site", "mpn", "customer_site_id"),
    )


class ProactiveOffer(Base):
    """A proactive offer email sent to a customer with selected match items."""

    __tablename__ = "proactive_offers"
    id = Column(Integer, primary_key=True)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id"), nullable=False)
    salesperson_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    line_items = Column(JSON, nullable=False, default=list)
    recipient_contact_ids = Column(JSON, default=list)
    recipient_emails = Column(JSON, default=list)
    subject = Column(String(500))
    email_body_html = Column(Text)
    graph_message_id = Column(String(500))
    status = Column(String(20), default="sent")
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    converted_requisition_id = Column(Integer, ForeignKey("requisitions.id"))
    converted_quote_id = Column(Integer, ForeignKey("quotes.id"))
    converted_at = Column(DateTime)
    total_sell = Column(Numeric(12, 2))
    total_cost = Column(Numeric(12, 2))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    salesperson = relationship("User", foreign_keys=[salesperson_id])

    __table_args__ = (
        Index("ix_poff_site", "customer_site_id"),
        Index("ix_poff_sales", "salesperson_id"),
        Index("ix_poff_status", "status"),
        Index("ix_poff_sent", "sent_at"),
    )


class ProactiveThrottle(Base):
    """Tracks when an MPN was last proactively offered to a customer site."""

    __tablename__ = "proactive_throttle"
    id = Column(Integer, primary_key=True)
    mpn = Column(String(255), nullable=False)
    customer_site_id = Column(
        Integer, ForeignKey("customer_sites.id", ondelete="CASCADE"), nullable=False
    )
    last_offered_at = Column(DateTime, nullable=False)
    proactive_offer_id = Column(Integer, ForeignKey("proactive_offers.id"))

    __table_args__ = (
        Index("ix_pt_mpn_site", "mpn", "customer_site_id", unique=True),
        Index("ix_pt_last_offered", "last_offered_at"),
    )


class ActivityLog(Base):
    """Activity log — system events (email, phone) and manual entries (call, note)."""

    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    activity_type = Column(String(20), nullable=False)
    channel = Column(String(20), nullable=False)

    # Polymorphic link — at most one set
    company_id = Column(Integer, ForeignKey("companies.id"))
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"))
    vendor_contact_id = Column(Integer, ForeignKey("vendor_contacts.id"))
    requisition_id = Column(Integer, ForeignKey("requisitions.id"))

    # Contact snapshot
    contact_email = Column(String(255))
    contact_phone = Column(String(100))
    contact_name = Column(String(255))

    # Metadata
    subject = Column(String(500))
    duration_seconds = Column(Integer)
    external_id = Column(String(255))
    notes = Column(Text)
    dismissed_at = Column(DateTime)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", foreign_keys=[user_id])
    company = relationship("Company", foreign_keys=[company_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    vendor_contact = relationship("VendorContact", foreign_keys=[vendor_contact_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])

    __table_args__ = (
        Index(
            "ix_activity_company",
            "company_id",
            "created_at",
            postgresql_where=Column("company_id").isnot(None),
        ),
        Index(
            "ix_activity_vendor",
            "vendor_card_id",
            "created_at",
            postgresql_where=Column("vendor_card_id").isnot(None),
        ),
        Index(
            "ix_activity_vendor_contact",
            "vendor_contact_id",
            "created_at",
            postgresql_where=Column("vendor_contact_id").isnot(None),
        ),
        Index("ix_activity_user", "user_id", "created_at"),
        Index(
            "ix_activity_external",
            "external_id",
            unique=True,
            postgresql_where=Column("external_id").isnot(None),
        ),
        Index(
            "ix_activity_requisition",
            "requisition_id",
            "vendor_card_id",
            "created_at",
            postgresql_where=Column("requisition_id").isnot(None),
        ),
        Index(
            "ix_activity_user_notif",
            "user_id",
            "activity_type",
            "created_at",
            postgresql_where=Column("dismissed_at").is_(None),
        ),
    )
