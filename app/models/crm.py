"""CRM models — Companies, Sites, and Site Contacts."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from .base import Base


class Company(Base):
    """Parent company — umbrella for multiple sites."""

    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    website = Column(String(500))
    industry = Column(String(255))
    notes = Column(Text)
    is_active = Column(Boolean, default=True)

    # Enrichment fields (shared structure with VendorCard)
    domain = Column(String(255), index=True)
    linkedin_url = Column(String(500))
    legal_name = Column(String(500))
    employee_size = Column(String(50))  # Range: "1-10", "51-200", "10001+"
    hq_city = Column(String(255))
    hq_state = Column(String(100))
    hq_country = Column(String(100))
    last_enriched_at = Column(DateTime)
    enrichment_source = Column(String(50))  # "clay", "explorium", "manual"

    # v1.3.0: Customer ownership fields
    is_strategic = Column(Boolean, default=False)
    ownership_cleared_at = Column(DateTime)
    last_activity_at = Column(DateTime)
    account_owner_id = Column(Integer, ForeignKey("users.id"))

    # v1.4.0: Account management fields
    account_type = Column(String(50))  # Customer, Prospect, Partner, Competitor
    phone = Column(String(100))
    credit_terms = Column(String(100))  # Net 30, Net 60, COD, etc.
    tax_id = Column(String(100))  # EIN / VAT ID
    currency = Column(String(10), default="USD")
    preferred_carrier = Column(String(100))  # FedEx, UPS, DHL, etc.

    # Deep enrichment tracking
    deep_enrichment_at = Column(DateTime)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    sites = relationship(
        "CustomerSite", back_populates="company", cascade="all, delete-orphan"
    )
    account_owner = relationship("User", foreign_keys=[account_owner_id])

    __table_args__ = (
        Index("ix_companies_name", "name"),
        Index("ix_companies_account_owner", "account_owner_id"),
        Index("ix_companies_owner_created", "account_owner_id", "created_at"),
    )


class CustomerSite(Base):
    """Child site within a company — where ownership lives."""

    __tablename__ = "customer_sites"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    site_name = Column(String(255), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"))

    # Contact (one per site)
    contact_name = Column(String(255))
    contact_email = Column(String(255))
    contact_phone = Column(String(100))
    contact_title = Column(String(255))
    contact_linkedin = Column(String(500))

    # Address
    address_line1 = Column(String(500))
    address_line2 = Column(String(255))
    city = Column(String(255))
    state = Column(String(100))
    zip = Column(String(20))
    country = Column(String(100), default="US")

    # Default terms
    payment_terms = Column(String(100))
    shipping_terms = Column(String(100))

    # v1.4.0: Site operations fields
    site_type = Column(String(50))  # HQ, Branch, Warehouse, Manufacturing
    timezone = Column(String(50))  # e.g. "America/New_York"
    receiving_hours = Column(String(100))  # e.g. "Mon-Fri 8am-5pm"
    carrier_account = Column(String(100))  # Customer shipping account number

    notes = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    company = relationship("Company", back_populates="sites")
    owner = relationship("User", foreign_keys=[owner_id])
    site_contacts = relationship(
        "SiteContact", back_populates="customer_site", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_cs_company", "company_id"),
        Index("ix_cs_owner", "owner_id"),
    )


class SiteContact(Base):
    """Contact person at a customer site — multiple per site."""

    __tablename__ = "site_contacts"
    id = Column(Integer, primary_key=True)
    customer_site_id = Column(
        Integer, ForeignKey("customer_sites.id", ondelete="CASCADE"), nullable=False
    )
    full_name = Column(String(255), nullable=False)
    title = Column(String(255))
    email = Column(String(255))
    phone = Column(String(100))
    notes = Column(Text)
    is_primary = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    customer_site = relationship("CustomerSite", back_populates="site_contacts")

    __table_args__ = (
        Index("ix_site_contacts_site", "customer_site_id"),
        Index("ix_site_contacts_email", "email"),
    )
