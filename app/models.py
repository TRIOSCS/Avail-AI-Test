"""Database models — Requisition-based sourcing with CRM."""

from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    JSON,
    Index,
    Numeric,
    Date,
    ARRAY,
)
from sqlalchemy.orm import DeclarativeBase, relationship

from .utils.encrypted_type import EncryptedText


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    role = Column(
        String(20), default="buyer"
    )  # buyer | sales | trader | manager | admin | dev_assistant
    is_active = Column(Boolean, default=True)
    azure_id = Column(String(255), unique=True)
    refresh_token = Column(EncryptedText)
    access_token = Column(EncryptedText)
    token_expires_at = Column(DateTime)
    email_signature = Column(Text)
    last_email_scan = Column(DateTime)
    last_inbox_scan = Column(DateTime)
    last_contacts_sync = Column(DateTime)
    m365_connected = Column(Boolean, default=False)
    m365_error_reason = Column(String(255))
    m365_last_healthy = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisitions = relationship("Requisition", back_populates="creator")
    contacts = relationship("Contact", back_populates="user")


# ── CRM: Companies & Sites ────────────────────────────────────────────


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

    __table_args__ = (Index("ix_companies_name", "name"),)


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


# ── Core: Requisitions & Requirements ─────────────────────────────────


class Requisition(Base):
    __tablename__ = "requisitions"
    __table_args__ = (
        Index("ix_requisitions_status", "status"),
        Index("ix_requisitions_created_by", "created_by"),
    )
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    customer_name = Column(String(255))  # Legacy — kept for migration
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"))
    status = Column(String(50), default="active")
    cloned_from_id = Column(Integer, ForeignKey("requisitions.id"))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_searched_at = Column(DateTime)
    offers_viewed_at = Column(DateTime)

    creator = relationship("User", back_populates="requisitions")
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    requirements = relationship(
        "Requirement", back_populates="requisition", cascade="all, delete-orphan"
    )
    contacts = relationship(
        "Contact", back_populates="requisition", cascade="all, delete-orphan"
    )
    offers = relationship(
        "Offer", back_populates="requisition", cascade="all, delete-orphan"
    )
    quotes = relationship(
        "Quote", back_populates="requisition", cascade="all, delete-orphan"
    )


class Requirement(Base):
    __tablename__ = "requirements"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    primary_mpn = Column(String(255))
    oem_pn = Column(String(255))
    brand = Column(String(255))
    sku = Column(String(255))
    target_qty = Column(Integer, default=1)
    target_price = Column(Numeric(12, 4))
    substitutes = Column(JSON, default=list)
    notes = Column(Text)
    firmware = Column(String(100))
    date_codes = Column(String(100))
    hardware_codes = Column(String(100))
    packaging = Column(String(100))
    condition = Column(String(50))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", back_populates="requirements")
    sightings = relationship(
        "Sighting", back_populates="requirement", cascade="all, delete-orphan"
    )
    offers = relationship(
        "Offer", back_populates="requirement", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_req_requisition", "requisition_id"),)


class Sighting(Base):
    __tablename__ = "sightings"
    id = Column(Integer, primary_key=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    vendor_email = Column(String(255))
    vendor_phone = Column(String(100))
    mpn_matched = Column(String(255))
    manufacturer = Column(String(255))
    qty_available = Column(Integer)
    unit_price = Column(Numeric(12, 4))
    currency = Column(String(10), default="USD")
    moq = Column(Integer)
    source_type = Column(String(50))
    is_authorized = Column(Boolean, default=False)
    confidence = Column(Float, default=0.0)
    score = Column(Float, default=0.0)
    raw_data = Column(JSON)
    is_unavailable = Column(Boolean, default=False)

    # Richer attachment parsing (Email Mining v2 Upgrade 2)
    date_code = Column(String(50))
    packaging = Column(String(50))
    condition = Column(String(50))
    lead_time_days = Column(Integer)
    lead_time = Column(String(100))

    # v2.0: Excess list differentiation — links sighting to originating customer company
    source_company_id = Column(Integer, ForeignKey("companies.id"))

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requirement = relationship("Requirement", back_populates="sightings")
    source_company = relationship("Company", foreign_keys=[source_company_id])

    __table_args__ = (
        Index("ix_sightings_vendor_name", "vendor_name"),
        Index("ix_sight_req", "requirement_id"),
    )


# ── CRM: Offers ───────────────────────────────────────────────────────


class Offer(Base):
    """Vendor offer logged by a buyer for a specific MPN on a requisition."""

    __tablename__ = "offers"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"))

    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"))
    vendor_name = Column(String(255), nullable=False)

    mpn = Column(String(255), nullable=False)
    manufacturer = Column(String(255))
    qty_available = Column(Integer)
    unit_price = Column(Numeric(12, 4))
    currency = Column(String(10), default="USD")
    lead_time = Column(String(100))
    date_code = Column(String(100))
    condition = Column(String(50))
    packaging = Column(String(100))
    firmware = Column(String(100))
    hardware_code = Column(String(100))
    moq = Column(Integer)
    valid_until = Column(Date)

    source = Column(String(50), default="manual")
    vendor_response_id = Column(Integer, ForeignKey("vendor_responses.id"))
    entered_by_id = Column(Integer, ForeignKey("users.id"))

    notes = Column(Text)
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # v1.3.0: Attribution fields — 14-day TTL with reconfirmation
    expires_at = Column(DateTime)
    reconfirmed_at = Column(DateTime)
    reconfirm_count = Column(Integer, default=0)
    attribution_status = Column(
        String(20), default="active"
    )  # active, expired, converted

    requisition = relationship("Requisition", back_populates="offers")
    requirement = relationship("Requirement", back_populates="offers")
    entered_by = relationship("User", foreign_keys=[entered_by_id])
    attachments = relationship(
        "OfferAttachment", back_populates="offer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_offers_req", "requisition_id"),
        Index("ix_offers_requirement", "requirement_id"),
        Index("ix_offers_vendor", "vendor_card_id"),
        Index("ix_offers_mpn", "mpn"),
    )


class OfferAttachment(Base):
    """File attachment on a vendor offer (stored in OneDrive)."""

    __tablename__ = "offer_attachments"
    id = Column(Integer, primary_key=True)
    offer_id = Column(
        Integer, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False
    )
    file_name = Column(String(500), nullable=False)
    onedrive_item_id = Column(String(500))
    onedrive_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    offer = relationship("Offer", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (Index("ix_offer_attachments_offer", "offer_id"),)


# ── CRM: Quotes ───────────────────────────────────────────────────────


class Quote(Base):
    """Quote built by salesperson from selected offers."""

    __tablename__ = "quotes"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id"), nullable=False)

    quote_number = Column(String(50), nullable=False, unique=True)
    revision = Column(Integer, default=1)

    line_items = Column(JSON, nullable=False, default=list)

    subtotal = Column(Numeric(12, 2))
    total_cost = Column(Numeric(12, 2))
    total_margin_pct = Column(Numeric(5, 2))

    payment_terms = Column(String(100))
    shipping_terms = Column(String(100))
    validity_days = Column(Integer, default=7)
    notes = Column(Text)

    status = Column(String(20), default="draft")
    sent_at = Column(DateTime)
    result = Column(String(20))
    result_reason = Column(String(255))
    result_notes = Column(Text)
    result_at = Column(DateTime)
    won_revenue = Column(Numeric(12, 2))

    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    requisition = relationship("Requisition", back_populates="quotes")
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("ix_quotes_req", "requisition_id"),
        Index("ix_quotes_site", "customer_site_id"),
        Index("ix_quotes_status", "status"),
    )


class BuyPlan(Base):
    """Purchase plan submitted after a quote is won — requires manager approval."""

    __tablename__ = "buy_plans"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(
        Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False
    )
    quote_id = Column(
        Integer, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False
    )

    status = Column(String(30), default="pending_approval")
    # pending_approval | approved | rejected | po_entered | po_confirmed | complete | cancelled

    line_items = Column(JSON, nullable=False, default=list)
    # [{offer_id, mpn, vendor_name, qty, cost_price, lead_time, condition,
    #   entered_by_id, po_number, po_sent_at, po_recipient, po_verified}]

    manager_notes = Column(Text)
    salesperson_notes = Column(Text)
    rejection_reason = Column(Text)
    sales_order_number = Column(String(100))

    submitted_by_id = Column(Integer, ForeignKey("users.id"))
    approved_by_id = Column(Integer, ForeignKey("users.id"))

    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    approved_at = Column(DateTime)
    rejected_at = Column(DateTime)
    completed_at = Column(DateTime)
    completed_by_id = Column(Integer, ForeignKey("users.id"))
    cancelled_at = Column(DateTime)
    cancelled_by_id = Column(Integer, ForeignKey("users.id"))
    cancellation_reason = Column(Text)

    approval_token = Column(String(100), unique=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    quote = relationship("Quote", foreign_keys=[quote_id])
    submitted_by = relationship("User", foreign_keys=[submitted_by_id])
    approved_by = relationship("User", foreign_keys=[approved_by_id])
    completed_by = relationship("User", foreign_keys=[completed_by_id])
    cancelled_by = relationship("User", foreign_keys=[cancelled_by_id])

    __table_args__ = (
        Index("ix_buyplans_req", "requisition_id"),
        Index("ix_buyplans_quote", "quote_id"),
        Index("ix_buyplans_status", "status"),
        Index("ix_buyplans_token", "approval_token"),
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
    status = Column(String(20), default="sent")  # sent | replied | converted | expired
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


# ── Performance Tracking ─────────────────────────────────────────────


class VendorMetricsSnapshot(Base):
    """Daily snapshot of vendor performance metrics (90-day rolling window)."""

    __tablename__ = "vendor_metrics_snapshot"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(
        Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False
    )
    snapshot_date = Column(Date, nullable=False)

    response_rate = Column(Float)
    quote_accuracy = Column(Float)
    on_time_delivery = Column(Float)
    cancellation_rate = Column(Float)
    rma_rate = Column(Float)
    lead_time_accuracy = Column(Float)
    quote_conversion = Column(Float)
    po_conversion = Column(Float)
    avg_review_rating = Column(Float)

    composite_score = Column(Float)
    interaction_count = Column(Integer, default=0)
    is_sufficient_data = Column(Boolean, default=False)

    rfqs_sent = Column(Integer, default=0)
    rfqs_answered = Column(Integer, default=0)
    pos_in_window = Column(Integer, default=0)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])

    __table_args__ = (
        Index("ix_vms_vendor_date", "vendor_card_id", "snapshot_date", unique=True),
        Index("ix_vms_date", "snapshot_date"),
        Index("ix_vms_composite", "composite_score"),
    )


class BuyerLeaderboardSnapshot(Base):
    """Monthly buyer leaderboard snapshot with multiplier scoring."""

    __tablename__ = "buyer_leaderboard_snapshot"
    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    month = Column(Date, nullable=False)

    offers_logged = Column(Integer, default=0)
    offers_quoted = Column(Integer, default=0)
    offers_in_buyplan = Column(Integer, default=0)
    offers_po_confirmed = Column(Integer, default=0)
    stock_lists_uploaded = Column(Integer, default=0)

    points_offers = Column(Integer, default=0)
    points_quoted = Column(Integer, default=0)
    points_buyplan = Column(Integer, default=0)
    points_po = Column(Integer, default=0)
    points_stock = Column(Integer, default=0)
    total_points = Column(Integer, default=0)

    rank = Column(Integer)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_bls_user_month", "user_id", "month", unique=True),
        Index("ix_bls_month_rank", "month", "rank"),
        Index("ix_bls_month_points", "month", "total_points"),
    )


class StockListHash(Base):
    """Deduplication hashes for uploaded stock lists."""

    __tablename__ = "stock_list_hashes"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content_hash = Column(String(64), nullable=False)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"))
    file_name = Column(String(500))
    row_count = Column(Integer)
    first_seen_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    upload_count = Column(Integer, default=1)

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_slh_hash", "content_hash"),
        Index("ix_slh_user_hash", "user_id", "content_hash", unique=True),
        Index("ix_slh_vendor", "vendor_card_id"),
    )


# ── Existing Models (unchanged) ───────────────────────────────────────


class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    contact_type = Column(String(20), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    vendor_contact = Column(String(255))
    parts_included = Column(JSON, default=list)
    subject = Column(String(500))
    details = Column(Text)
    status = Column(String(50), default="sent")
    status_updated_at = Column(DateTime)
    graph_message_id = Column(String(500))
    graph_conversation_id = Column(String(500))
    needs_review = Column(Boolean, default=False)
    parse_result_json = Column(JSON)
    parse_confidence = Column(Float)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisition = relationship("Requisition", back_populates="contacts")
    user = relationship("User", back_populates="contacts")

    __table_args__ = (
        Index("ix_contact_req", "requisition_id"),
        Index("ix_contact_status", "status"),
        Index("ix_contact_user_status", "user_id", "status", "created_at"),
    )


class VendorResponse(Base):
    __tablename__ = "vendor_responses"
    id = Column(Integer, primary_key=True)
    contact_id = Column(Integer, ForeignKey("contacts.id"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id"), nullable=True)
    vendor_name = Column(String(255))
    vendor_email = Column(String(255))
    subject = Column(String(500))
    body = Column(Text)
    received_at = Column(DateTime)
    parsed_data = Column(JSON)
    confidence = Column(Float)
    classification = Column(String(50))
    needs_action = Column(Boolean, default=False)
    action_hint = Column(String(255))
    status = Column(String(50), default="new")
    message_id = Column(String(255), unique=True, index=True, nullable=True)
    graph_conversation_id = Column(String(500))
    scanned_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    match_method = Column(
        String(50)
    )  # conversation_id, subject_token, email_exact, domain, unmatched
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("ix_vr_classification", "classification"),)


class VendorCard(Base):
    __tablename__ = "vendor_cards"
    id = Column(Integer, primary_key=True)
    normalized_name = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=False)
    domain = Column(String(255), index=True)
    domain_aliases = Column(JSON, default=list)
    website = Column(String(500))
    emails = Column(JSON, default=list)
    phones = Column(JSON, default=list)
    contacts = Column(JSON, default=list)
    alternate_names = Column(JSON, default=list)
    sighting_count = Column(Integer, default=0)
    is_blacklisted = Column(Boolean, default=False)
    source = Column(String(50))
    raw_response = Column(Text)

    # Enrichment fields (shared structure with Company)
    linkedin_url = Column(String(500))
    legal_name = Column(String(500))
    employee_size = Column(String(50))
    hq_city = Column(String(255))
    hq_state = Column(String(100))
    hq_country = Column(String(100))
    industry = Column(String(255))

    last_enriched_at = Column(DateTime)
    enrichment_source = Column(String(50))

    # Acctivate sync fields — behavioral truth
    acctivate_vendor_id = Column(String(255), index=True)  # For reconciliation
    cancellation_rate = Column(Float)  # cancelled / total orders
    rma_rate = Column(Float)  # units returned / units received
    acctivate_total_orders = Column(Integer)
    acctivate_total_units = Column(Integer)
    acctivate_last_order_date = Column(Date)
    last_synced_at = Column(DateTime)

    # Engagement scoring (Email Mining v2 Upgrade 4)
    total_outreach = Column(Integer, default=0)
    total_responses = Column(Integer, default=0)
    total_wins = Column(Integer, default=0)
    ghost_rate = Column(Float)
    response_velocity_hours = Column(Float)
    last_contact_at = Column(DateTime)
    relationship_months = Column(Integer)
    engagement_score = Column(Float)
    engagement_computed_at = Column(DateTime)

    # v1.3.0: Vendor scorecard fields
    avg_response_hours = Column(Float)
    overall_win_rate = Column(Float)
    total_pos = Column(Integer, default=0)
    total_revenue = Column(Numeric(14, 2), default=0)
    last_activity_at = Column(DateTime)

    # AI-generated material intelligence
    brand_tags = Column(JSON, default=list)  # ["IBM", "Dell", "HP"]
    commodity_tags = Column(JSON, default=list)  # ["CPU", "HDD", "DDR", "LCD"]
    material_tags_updated_at = Column(DateTime)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    reviews = relationship(
        "VendorReview", back_populates="vendor_card", cascade="all, delete-orphan"
    )
    vendor_contacts = relationship(
        "VendorContact", back_populates="vendor_card", cascade="all, delete-orphan"
    )


class VendorContact(Base):
    __tablename__ = "vendor_contacts"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(
        Integer, ForeignKey("vendor_cards.id", ondelete="CASCADE"), nullable=False
    )
    contact_type = Column(String(20), default="company")
    full_name = Column(String(255))
    title = Column(String(255))
    label = Column(String(100))
    email = Column(String(255))
    phone = Column(String(100))
    phone_type = Column(String(20))
    linkedin_url = Column(String(500))
    source = Column(String(50), nullable=False)
    is_verified = Column(Boolean, default=False)
    confidence = Column(Integer, default=50)
    interaction_count = Column(Integer, default=0)
    last_interaction_at = Column(DateTime)
    first_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", back_populates="vendor_contacts")

    __table_args__ = (
        Index("ix_vendor_contacts_card", "vendor_card_id"),
        Index("ix_vendor_contacts_email", "email"),
        Index("ix_vendor_contacts_card_email", "vendor_card_id", "email", unique=True),
    )


class VendorReview(Base):
    __tablename__ = "vendor_reviews"
    id = Column(Integer, primary_key=True)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(String(500))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    vendor_card = relationship("VendorCard", back_populates="reviews")
    user = relationship("User")

    __table_args__ = (Index("ix_review_vendor", "vendor_card_id"),)


class MaterialCard(Base):
    __tablename__ = "material_cards"
    id = Column(Integer, primary_key=True)
    normalized_mpn = Column(String(255), nullable=False, unique=True, index=True)
    display_mpn = Column(String(255), nullable=False)
    manufacturer = Column(String(255))
    description = Column(String(1000))
    search_count = Column(Integer, default=0)
    last_searched_at = Column(DateTime)
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


class ApiSource(Base):
    __tablename__ = "api_sources"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    category = Column(String(50), nullable=False)
    source_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    description = Column(String(500))
    setup_notes = Column(Text)
    signup_url = Column(String(500))
    env_vars = Column(JSON, default=list)
    credentials = Column(
        JSON, default=dict
    )  # encrypted credential values keyed by env var name
    last_success = Column(DateTime)
    last_error = Column(String(500))
    total_searches = Column(Integer, default=0)
    total_results = Column(Integer, default=0)
    avg_response_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class SystemConfig(Base):
    """Key-value runtime configuration. Survives restarts, auditable."""

    __tablename__ = "system_config"
    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=False)
    description = Column(String(500))
    updated_by = Column(String(255))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
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

    # Acctivate transaction truth (separate from API sighting prices)
    acctivate_last_price = Column(Float)
    acctivate_last_date = Column(Date)
    acctivate_rma_rate = Column(Float)  # Per-part RMA rate for this vendor+part
    source = Column(
        String(50), default="api_sighting"
    )  # "api_sighting", "acctivate", "salesforce"

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    material_card = relationship("MaterialCard", back_populates="vendor_history")

    __table_args__ = (
        Index("ix_mvh_card_vendor", "material_card_id", "vendor_name", unique=True),
        Index("ix_mvh_vendor", "vendor_name"),
    )


# ── Acctivate Sync ────────────────────────────────────────────────────


class InventorySnapshot(Base):
    """Current inventory from Acctivate — refreshed daily."""

    __tablename__ = "inventory_snapshots"
    id = Column(Integer, primary_key=True)
    product_id = Column(String(255), nullable=False, index=True)
    warehouse_id = Column(String(100))
    qty_on_hand = Column(Integer, default=0)
    synced_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_inv_product_warehouse", "product_id", "warehouse_id", unique=True),
    )


class SyncLog(Base):
    """Log of each data sync run."""

    __tablename__ = "sync_logs"
    id = Column(Integer, primary_key=True)
    source = Column(
        String(50), nullable=False
    )  # "acctivate", "salesforce", "quickbooks"
    status = Column(
        String(50), nullable=False
    )  # "success", "error", "connection_failed"
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    duration_seconds = Column(Float)
    row_counts = Column(JSON)
    errors = Column(JSON)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("ix_sync_source_time", "source", "started_at"),)


# ── Email Mining v2: Pipeline Infrastructure ──────────────────────────


class ProcessedMessage(Base):
    """H2: Deduplication — track messages already processed."""

    __tablename__ = "processed_messages"
    message_id = Column(Text, primary_key=True)
    processing_type = Column(
        Text, primary_key=True
    )  # mining, response, attachment, sent
    processed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SyncState(Base):
    """H8: Delta Query state per user per folder."""

    __tablename__ = "sync_state"
    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    folder = Column(String(100), nullable=False)  # Inbox, SentItems
    delta_token = Column(Text)
    last_sync_at = Column(DateTime)

    __table_args__ = (
        Index("ix_sync_state_user_folder", "user_id", "folder", unique=True),
    )


class ColumnMappingCache(Base):
    """Upgrade 2: Cache AI-detected column mappings for vendor attachments."""

    __tablename__ = "column_mapping_cache"
    id = Column(Integer, primary_key=True)
    vendor_domain = Column(Text, nullable=False)
    file_fingerprint = Column(Text, nullable=False)
    mapping = Column(JSON, nullable=False)
    confidence = Column(Float, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_colmap_domain_fp", "vendor_domain", "file_fingerprint", unique=True),
    )


# ── Intelligence Layer: Contact Enrichment ────────────────────────────


class ProspectContact(Base):
    """Enriched contacts found via Apollo/web search for customers and vendors."""

    __tablename__ = "prospect_contacts"
    id = Column(Integer, primary_key=True)
    customer_site_id = Column(
        Integer, ForeignKey("customer_sites.id", ondelete="SET NULL")
    )
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"))

    full_name = Column(String(255), nullable=False)
    title = Column(String(255))
    email = Column(String(255))
    email_status = Column(String(20))  # verified, guessed, unavailable, bounced
    phone = Column(String(100))
    linkedin_url = Column(String(500))

    source = Column(
        String(50), nullable=False
    )  # apollo, web_search, email_reply, manual, import
    confidence = Column(String(10), nullable=False)  # high, medium, low
    found_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    verified_at = Column(DateTime)

    is_saved = Column(Boolean, default=False)
    saved_by_id = Column(Integer, ForeignKey("users.id"))
    notes = Column(Text)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_prospect_contacts_site", "customer_site_id"),
        Index("ix_prospect_contacts_vendor", "vendor_card_id"),
        Index("ix_prospect_contacts_email", "email"),
    )


# ── Intelligence Layer: Intel Cache ───────────────────────────────────


class IntelCache(Base):
    """Cached intelligence data with TTL."""

    __tablename__ = "intel_cache"
    id = Column(Integer, primary_key=True)
    cache_key = Column(String(500), nullable=False, unique=True, index=True)
    data = Column(JSON, nullable=False)
    ttl_days = Column(Integer, nullable=False, default=7)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=False)


# ═══════════════════════════════════════════════════════════════════════
#  v1.3.0 — Activity Logging, Buyer Routing & Customer Ownership
# ═══════════════════════════════════════════════════════════════════════


class ActivityLog(Base):
    """Activity log — system events (email, phone) and manual entries (call, note)."""

    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    activity_type = Column(
        String(20), nullable=False
    )  # email_sent, email_received, call_outbound, call_inbound, note
    channel = Column(String(20), nullable=False)  # email, phone, manual

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
    external_id = Column(String(255))  # Graph message ID or 8x8 call ID
    notes = Column(Text)  # manual call/note text
    dismissed_at = Column(DateTime)  # v2.0: admin dismissed unmatched activity

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
    )


class BuyerProfile(Base):
    """Buyer routing attributes — commodity, geography, brand assignments."""

    __tablename__ = "buyer_profiles"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)

    primary_commodity = Column(String(100))  # semiconductors, pc_server_parts
    secondary_commodity = Column(String(100))
    primary_geography = Column(String(50))  # apac, emea, americas

    brand_specialties = Column(ARRAY(Text))  # ['IBM']
    brand_material_types = Column(ARRAY(Text))  # ['systems', 'parts']
    brand_usage_types = Column(ARRAY(Text))  # ['sourcing_to_buy', 'backup_buying']

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])


class BuyerVendorStats(Base):
    """Per-buyer performance with a specific vendor. Auto-populated."""

    __tablename__ = "buyer_vendor_stats"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"), nullable=False)

    rfqs_sent = Column(Integer, default=0)
    responses_received = Column(Integer, default=0)
    response_rate = Column(Float)
    offers_logged = Column(Integer, default=0)
    offers_won = Column(Integer, default=0)
    win_rate = Column(Float)
    avg_response_hours = Column(Float)
    last_contact_at = Column(DateTime)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", foreign_keys=[user_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])

    __table_args__ = (
        Index("ix_bvs_vendor", "vendor_card_id"),
        Index("ix_bvs_user", "user_id"),
        Index("ix_bvs_unique", "user_id", "vendor_card_id", unique=True),
    )


class GraphSubscription(Base):
    """Tracks active Graph API webhook subscriptions per user."""

    __tablename__ = "graph_subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subscription_id = Column(String(255), nullable=False, unique=True)
    resource = Column(String(255), nullable=False)  # /me/messages
    change_type = Column(String(100), nullable=False)  # created
    expiration_dt = Column(DateTime, nullable=False)
    client_state = Column(String(255))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_graphsub_user", "user_id"),
        Index("ix_graphsub_expiry", "expiration_dt"),
    )


class RoutingAssignment(Base):
    """Tracks buyer routing for a requirement+vendor pair with 48-hour waterfall."""

    __tablename__ = "routing_assignments"
    id = Column(Integer, primary_key=True)
    requirement_id = Column(
        Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False
    )
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id"), nullable=False)

    buyer_1_id = Column(Integer, ForeignKey("users.id"))
    buyer_2_id = Column(Integer, ForeignKey("users.id"))
    buyer_3_id = Column(Integer, ForeignKey("users.id"))

    buyer_1_score = Column(Float)
    buyer_2_score = Column(Float)
    buyer_3_score = Column(Float)
    scoring_details = Column(JSON)

    assigned_at = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    expires_at = Column(DateTime, nullable=False)
    claimed_by_id = Column(Integer, ForeignKey("users.id"))
    claimed_at = Column(DateTime)
    status = Column(String(20), default="active")  # active, claimed, expired

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    buyer_1 = relationship("User", foreign_keys=[buyer_1_id])
    buyer_2 = relationship("User", foreign_keys=[buyer_2_id])
    buyer_3 = relationship("User", foreign_keys=[buyer_3_id])
    claimed_by = relationship("User", foreign_keys=[claimed_by_id])

    __table_args__ = (
        Index("ix_routing_req", "requirement_id"),
        Index("ix_routing_vendor", "vendor_card_id"),
        Index("ix_routing_expires", "expires_at"),
    )


class PendingBatch(Base):
    """Tracks Anthropic Batch API submissions for async AI processing."""

    __tablename__ = "pending_batches"
    id = Column(Integer, primary_key=True)
    batch_id = Column(String, nullable=False, index=True)
    batch_type = Column(String(50), default="inbox_parse")
    request_map = Column(JSON)  # {custom_id: vendor_response_id}
    status = Column(String(20), default="processing")  # processing | completed | failed
    submitted_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True))
    result_count = Column(Integer)
    error_message = Column(String)
