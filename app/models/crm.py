"""CRM models — Companies, Sites, and Site Contacts."""

import re
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from .base import Base


class Company(Base):
    """Parent company — umbrella for multiple sites."""

    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)

    # AI organization (Increment 3) — durable dedup foundation, mirrors VendorCard.
    # normalized_name is the suffix-stripped/lowercased match key, kept in sync with
    # `name` by the @validates hook below. Unlike VendorCard it is NULLABLE and NOT
    # unique: companies legitimately share a normalized form across the dedup window
    # (e.g. different-owner accounts the policy keeps separate). The pg_trgm GIN index
    # (migration 120, Postgres-only) is for similarity scanning, not a constraint.
    normalized_name = Column(String(255), index=True)
    # Names this company has been known by (loser names absorbed on merge), so a
    # re-import of the old name fuzzy-matches here instead of recreating the dupe.
    alternate_names = Column(JSON, default=list)

    website = Column(String(500))
    industry = Column(String(255))
    notes = Column(Text)
    is_active = Column(Boolean, default=True, index=True)

    # Enrichment fields (shared structure with VendorCard)
    domain = Column(String(255), index=True)
    linkedin_url = Column(String(500))
    legal_name = Column(String(500))
    employee_size = Column(String(50))  # Range: "1-10", "51-200", "10001+"
    hq_city = Column(String(255))
    hq_state = Column(String(100))
    hq_country = Column(String(100))
    last_enriched_at = Column(UTCDateTime)
    enrichment_source = Column(String(50))  # "explorium", "lusha", "clay", "manual"

    # Firmographic / provenance enrichment (Explorium+Clay blending)
    ticker = Column(String(20))
    naics = Column(String(20))
    revenue_range = Column(String(50))
    enrichment_provenance = Column(JSONB, default=dict, server_default="{}")
    custom_fields = Column(JSONB, default=dict, server_default="{}")

    # v1.3.0: Customer ownership fields
    is_strategic = Column(Boolean, default=False, index=True)
    ownership_cleared_at = Column(UTCDateTime)
    last_activity_at = Column(UTCDateTime, index=True)
    account_owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    # Step 3: Account-level primary contact (distinct from per-site is_primary)
    primary_contact_id = Column(Integer, ForeignKey("site_contacts.id", ondelete="SET NULL"))
    # Step 3: Parent company (self-referential hierarchy)
    parent_company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"))

    # CRM cadence — two clocks + tier (see docs/superpowers/plans/2026-06-17-crm-data-foundation.md)
    last_outbound_at = Column(UTCDateTime, index=True)
    last_reply_at = Column(UTCDateTime, index=True)
    tier = Column(String(20), index=True)  # key | core | standard | prospect (NULL => standard)

    # Account disposition (Increment 1) — salesperson-set lifecycle.
    # active | bucket (NULL => active, like tier). NOT is_active (would vanish).
    disposition = Column(String(20), index=True)
    disposition_reason = Column(String)  # optional, free text
    disposition_set_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    disposition_set_at = Column(UTCDateTime)

    # v1.4.0: Account management fields
    account_type = Column(String(50))  # Customer, Prospect, Partner, Competitor
    phone = Column(String(100))
    normalized_phone = Column(String(20), index=True)
    credit_terms = Column(String(100))  # Net 30, Net 60, COD, etc.
    tax_id = Column(String(100))  # EIN / VAT ID
    currency = Column(String(10), default="USD")
    preferred_carrier = Column(String(100))  # FedEx, UPS, DHL, etc.

    # AI-generated material intelligence (mirrors VendorCard pattern)
    brand_tags = Column(JSON, default=list)
    commodity_tags = Column(JSON, default=list)
    material_tags_updated_at = Column(UTCDateTime)

    # Denormalized counts (kept in sync by PostgreSQL triggers)
    site_count = Column(Integer, default=0, server_default="0", nullable=False)
    open_req_count = Column(Integer, default=0, server_default="0", nullable=False)

    # Record origin tracking
    source = Column(String(50), default="manual")

    # Salesforce import fields
    sf_account_id = Column(String(255), unique=True)
    import_priority = Column(String(20))  # "priority", "standard", "dismissed"

    # Deep enrichment tracking
    deep_enrichment_at = Column(UTCDateTime)

    # Customer enrichment waterfall tracking
    customer_enrichment_at = Column(UTCDateTime)
    customer_enrichment_status = Column(String(20))  # complete, partial, missing, stale

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    sites = relationship("CustomerSite", back_populates="company", cascade="all, delete-orphan")
    account_owner = relationship("User", foreign_keys=[account_owner_id])
    attachments = relationship("CompanyAttachment", back_populates="company", cascade="all, delete-orphan")
    collaborators = relationship("AccountCollaborator", back_populates="company", cascade="all, delete-orphan")
    primary_contact = relationship("SiteContact", foreign_keys=[primary_contact_id])
    parent_company = relationship(
        "Company",
        foreign_keys=[parent_company_id],
        backref="child_companies",
        remote_side="Company.id",
    )

    @validates("currency")
    def _validate_currency(self, _key, value):
        if value is not None and not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError(f"Invalid ISO 4217 currency code: {value}")
        return value

    @validates("name")
    def _sync_normalized_name(self, _key, value):
        """Keep normalized_name in lockstep with name on every create/rename.

        Uses the same normalizer the dedup scanner uses
        (vendor_utils.normalize_vendor_name via company_utils), so the durable match key
        is identical to scan-time scoring. Covers all create paths (API create,
        prospect-claim, importers) without touching each one. Empty/whitespace names
        normalize to None (no spurious '' match key).
        """
        from ..vendor_utils import normalize_vendor_name

        self.normalized_name = normalize_vendor_name(value) or None
        return value

    @validates("phone")
    def _sync_normalized_phone(self, _key, value):
        """Keep normalized_phone (E.164) in sync with phone on every write."""
        from ..utils.phone import normalize_e164

        self.normalized_phone = normalize_e164(value)
        return value

    @validates("custom_fields")
    def _validate_custom_fields(self, _key, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("custom_fields must be a dict")
        if len(value) > 30:
            raise ValueError("custom_fields: max 30 keys")
        for k, v in value.items():
            if len(str(k)) > 60:
                raise ValueError(f"custom_fields key too long (max 60 chars): {k!r}")
            if len(str(v)) > 500:
                raise ValueError(f"custom_fields value too long (max 500 chars) for key {k!r}")
        return value

    __table_args__ = (
        Index("ix_companies_name", "name"),
        Index("ix_companies_account_owner", "account_owner_id"),
        Index("ix_companies_owner_created", "account_owner_id", "created_at"),
        Index("ix_companies_sf_account_id", "sf_account_id", unique=True),
    )


class CustomerSite(Base):
    """Child site within a company — where ownership lives."""

    __tablename__ = "customer_sites"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    site_name = Column(String(255), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    # Contact (one per site)
    contact_name = Column(String(255))
    contact_email = Column(String(255))
    contact_phone = Column(String(100))
    contact_phone_2 = Column(String(100))
    normalized_phone = Column(String(20), index=True)
    normalized_phone_2 = Column(String(20), index=True)
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
    is_active = Column(Boolean, default=True, index=True)

    # v2.10: Prospecting pool fields
    last_activity_at = Column(UTCDateTime)
    ownership_cleared_at = Column(UTCDateTime)
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    company = relationship("Company", back_populates="sites")
    owner = relationship("User", foreign_keys=[owner_id])
    site_contacts = relationship("SiteContact", back_populates="customer_site", cascade="all, delete-orphan")

    @validates("contact_email")
    def _validate_contact_email(self, _key, value):
        if value and "@" not in value:
            raise ValueError(f"Invalid contact email: {value}")
        return value

    @validates("contact_phone")
    def _sync_normalized_phone(self, _key, value):
        """Keep normalized_phone (E.164) in sync with contact_phone on every write."""
        from ..utils.phone import normalize_e164

        self.normalized_phone = normalize_e164(value)
        return value

    @validates("contact_phone_2")
    def _sync_normalized_phone_2(self, _key, value):
        """Keep normalized_phone_2 (E.164) in sync with contact_phone_2 on every
        write."""
        from ..utils.phone import normalize_e164

        self.normalized_phone_2 = normalize_e164(value)
        return value

    __table_args__ = (
        Index("ix_cs_company", "company_id"),
        Index("ix_cs_owner", "owner_id"),
    )


class SiteContact(Base):
    """Contact person at a customer site — multiple per site."""

    __tablename__ = "site_contacts"
    id = Column(Integer, primary_key=True)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="CASCADE"), nullable=False)
    full_name = Column(String(255), nullable=False)
    # Step 4: Name split — first_name/last_name are the editable sources of truth.
    # full_name is DERIVED (recomposed on every first_name/last_name write via the
    # form/inline edit path). Legacy writers that set full_name directly leave
    # first_name/last_name as-is (they were seeded by migration 134 backfill).
    first_name = Column(String(120))
    last_name = Column(String(120))
    # DEPRECATED / UNUSED — ownership flows via site → account owner (Phase 1 cleanup).
    # Column is retained to avoid a migration; it will always be NULL for new contacts.
    contact_owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    title = Column(String(255))
    email = Column(String(255))
    phone = Column(String(100))
    normalized_phone = Column(String(20), index=True)
    wechat_id = Column(String(100))
    notes = Column(Text)
    is_primary = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True, server_default=text("true"))
    contact_status = Column(String(20), default="new")
    do_not_contact = Column(Boolean, nullable=False, default=False, server_default="false")
    # Increment 1 — contact disposition. is_priority surfaces a contact to the
    # top of the roster; is_archived sorts it to the bottom (still shown — NOT
    # is_active, which would hide it). Both mirror do_not_contact exactly.
    is_priority = Column(Boolean, nullable=False, default=False, server_default="false")
    is_archived = Column(Boolean, nullable=False, default=False, server_default="false")

    # CRM cadence — contact-level clocks
    last_activity_at = Column(UTCDateTime)
    last_outbound_at = Column(UTCDateTime)
    last_reply_at = Column(UTCDateTime)

    # Customer enrichment fields
    phone_verified = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    email_verified_at = Column(UTCDateTime)
    email_verification_status = Column(String(20))  # valid, invalid, accept_all, unknown
    enrichment_source = Column(String(50))  # lusha, clay, hunter, explorium, manual
    contact_role = Column(String(50))  # buyer, technical, decision_maker, operations
    needs_refresh = Column(Boolean, default=False)
    last_enriched_at = Column(UTCDateTime)
    linkedin_url = Column(String(500))
    secondary_email = Column(String(255), nullable=True)
    secondary_phone = Column(String(100), nullable=True)
    reports_to_id = Column(Integer, ForeignKey("site_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    enrichment_field_sources = Column(JSON)  # Per-field source tracking
    custom_fields = Column(JSONB, default=dict, server_default="{}")

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    customer_site = relationship("CustomerSite", back_populates="site_contacts")
    attachments = relationship("SiteContactAttachment", back_populates="site_contact", cascade="all, delete-orphan")
    contact_owner = relationship("User", foreign_keys=[contact_owner_id])
    reports_to = relationship(
        "SiteContact", foreign_keys="[SiteContact.reports_to_id]", remote_side="SiteContact.id", lazy="joined"
    )

    @validates("email")
    def _validate_email(self, _key, value):
        if value and "@" not in value:
            raise ValueError(f"Invalid email: {value}")
        return value

    @validates("secondary_email")
    def _validate_secondary_email(self, _key, value):
        if value and "@" not in value:
            raise ValueError(f"Invalid secondary_email: {value}")
        return value

    @validates("phone")
    def _sync_normalized_phone(self, _key, value):
        """Keep normalized_phone (E.164) in sync with phone on every write."""
        from ..utils.phone import normalize_e164

        self.normalized_phone = normalize_e164(value)
        return value

    @validates("custom_fields")
    def _validate_custom_fields(self, _key, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("custom_fields must be a dict")
        if len(value) > 30:
            raise ValueError("custom_fields: max 30 keys")
        for k, v in value.items():
            if len(str(k)) > 60:
                raise ValueError(f"custom_fields key too long (max 60 chars): {k!r}")
            if len(str(v)) > 500:
                raise ValueError(f"custom_fields value too long (max 500 chars) for key {k!r}")
        return value

    __table_args__ = (
        Index("ix_site_contacts_site", "customer_site_id"),
        Index("ix_site_contacts_email", "email"),
        Index("ix_site_contacts_contact_owner_id", "contact_owner_id"),
        UniqueConstraint("customer_site_id", "email", name="uq_site_contacts_site_email"),
    )


class AccountCollaborator(Base):
    """Account-level collaborator — a user with helper access to a CRM company.

    A helper collaborator can view and work the account (can_manage_account=True) but
    cannot modify the team roster (add/remove collaborators or change the primary owner).
    That team-management gate is enforced by can_manage_account_team(), which requires
    is_manager_or_admin OR company.account_owner_id == user.id.

    Called by: app/dependencies.can_manage_account, app/services/crm_service.cdm_company_query,
        app/routers/htmx_views (collaborator add/remove endpoints)
    Depends on: Company, User
    """

    __tablename__ = "account_collaborators"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False, default="helper", server_default="helper")
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="collaborators")
    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_account_collaborators_company_user"),
        Index("ix_account_collaborators_company", "company_id"),
    )


class CompanyAttachment(Base):
    """File attachment on a CRM company (stored in OneDrive or company SharePoint
    library).

    library_drive_id NULL  → OneDrive fallback row (user token, item in /me/drive)
    library_drive_id set   → company SharePoint library row (app token)

    Called by: app/routers/attachments_extra.py, app/services/attachment_service.py
    Depends on: Company, User
    """

    __tablename__ = "company_attachments"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    company = relationship("Company", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (Index("ix_company_attachments_company", "company_id"),)


class SiteContactAttachment(Base):
    """File attachment on a CRM site contact (stored in OneDrive or company SharePoint
    library).

    library_drive_id NULL  → OneDrive fallback row (user token, item in /me/drive)
    library_drive_id set   → company SharePoint library row (app token)

    Called by: app/routers/attachments_extra.py, app/services/attachment_service.py
    Depends on: SiteContact, User
    """

    __tablename__ = "site_contact_attachments"
    id = Column(Integer, primary_key=True)
    site_contact_id = Column(Integer, ForeignKey("site_contacts.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    site_contact = relationship("SiteContact", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (Index("ix_site_contact_attachments_contact", "site_contact_id"),)
