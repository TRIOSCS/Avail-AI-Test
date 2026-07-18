"""Intelligence models — Materials, Proactive, Activity."""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import relationship, validates

from ..constants import ProactiveMatchStatus
from ..database import UTCDateTime
from .base import Base


class MaterialCard(Base):
    __tablename__ = "material_cards"
    id = Column(Integer, primary_key=True)
    normalized_mpn = Column(String(255), nullable=False, unique=True, index=True)
    display_mpn = Column(String(255), nullable=False)
    # Dual-brand semantics (migration 097): `manufacturer` = the ACTUAL MAKER (Seagate
    # Technology, Kingston Technology, Hitachi/IBM); `brand` = the OEM LABEL on the part
    # (IBM, Dell Technologies, Hewlett Packard Enterprise, Lenovo). Nullable — most cards
    # never get a brand. Both are written ONLY via spec_tiers.set_manufacturer/set_brand
    # (F1 ladder + normalize_brand_name); the combined "Brand" facet ORs across both.
    manufacturer = Column(String(255), index=True)
    brand = Column(String(255), index=True)
    description = Column(String(1000))
    search_count = Column(Integer, default=0)
    last_searched_at = Column(UTCDateTime)
    search_vector = Column(TSVECTOR)

    # Enrichment fields (populated by AI agent)
    lifecycle_status = Column(String(50), index=True)  # active, nrfnd, eol, obsolete, ltb
    package_type = Column(String(100))  # QFP-64, BGA-256, 0603, etc.
    category = Column(String(255))  # Microcontroller, Capacitor, Connector, etc.
    rohs_status = Column(String(50))  # compliant, non-compliant, exempt
    # Stock condition (broker facet): the constants.MaterialCondition StrEnum vocabulary
    # (New | Recertified | Refurbished | Used | Pulled | Unknown) — always write via that
    # enum, never raw strings. Application-validated (no DB CHECK), like lifecycle_status.
    # NULL until a source populates it ("no data" stays NULL, never "Unknown").
    condition = Column(String(20), index=True)
    pin_count = Column(Integer)
    datasheet_url = Column(String(1000))
    # Auto-datasheet capture: stamps drive the dossier UI + 30-day negative cache.
    datasheet_captured_at = Column(UTCDateTime, nullable=True)
    datasheet_searched_at = Column(UTCDateTime, nullable=True)
    cross_references = Column(JSONB, default=list)  # [{mpn, manufacturer}]
    specs_summary = Column(Text)  # Key electrical specs in plain text
    specs_structured = Column(
        JSONB
    )  # Structured specs: {"ddr_type": {"value": "DDR4", "source": "...", "confidence": 0.99, "updated_at": "..."}}
    enrichment_source = Column(String(50))  # "claude_ai", "manual", etc.
    enriched_at = Column(UTCDateTime)
    specs_enriched_at = Column(UTCDateTime, index=True)  # NULL = spec pass not yet run
    # Verification provenance (added 2026-06-04 — verified-enrichment feature)
    # enrichment_status: see constants.MaterialEnrichmentStatus (validated on write):
    # unenriched | verified | web_sourced | oem_sourced | ai_inferred | not_found | not_catalogued
    enrichment_status = Column(
        String(20),
        nullable=False,
        server_default="unenriched",
        index=True,
        comment=(
            "unenriched|verified|web_sourced|oem_sourced|ai_inferred|not_found|not_catalogued "
            "(see MaterialEnrichmentStatus)"
        ),
    )
    # Per-field provenance: {"<field>": {"source": "digikey", "confidence": 1.0,
    #                                    "fetched_at": "2026-06-04T..Z", "matched_mpn": "..."}}
    enrichment_provenance = Column(JSONB)

    # Category provenance (SP2/F2 — set via spec_tiers.set_category, governed by the F1
    # ladder). Through set_category, a lower-tier source can never overwrite a category
    # written by a higher-tier source; a category carrying a value but NULL provenance
    # (legacy data) ranks at the legacy_backfill mid-tier (50). All in-tree category
    # writers route through set_category; the @validates("category") guard below (SP3)
    # rejects any off-vocab direct assignment, so a future un-routed writer can no
    # longer persist junk past the ladder.
    category_source = Column(String(50))  # "mpn_decode", "digikey_api", "claude_opus_inferred", ...
    category_confidence = Column(Float)
    category_tier = Column(Integer)
    category_updated_at = Column(UTCDateTime)  # when the category was last (re)written via the ladder

    # Brand + manufacturer provenance (migration 097 — written via spec_tiers.set_brand /
    # set_manufacturer, governed by the same F1 ladder as category_*). A valued
    # manufacturer with NULL provenance (legacy data, or a writer that bypassed the
    # helpers) ranks at the legacy_backfill floor (tier 50, conf 0.5) at runtime — so
    # trio_source (95) maker evidence can displace an OEM name sitting in `manufacturer`
    # from legacy data, but a stray AI guess (40) cannot.
    brand_source = Column(String(50))
    brand_confidence = Column(Float)
    brand_tier = Column(Integer)
    brand_updated_at = Column(UTCDateTime)
    manufacturer_source = Column(String(50))
    manufacturer_confidence = Column(Float)
    manufacturer_tier = Column(Integer)
    manufacturer_updated_at = Column(UTCDateTime)

    is_internal_part = Column(Boolean, default=False, server_default="false")  # Internal/custom PN (not a standard MPN)

    # Worker priority-lane stamp (on-add enrichment, migration 099). Set ONLY by the
    # single-add endpoint (a user is actively waiting); the enrichment worker's
    # select_batch orders stamped cards first (FIFO by stamp) and run_one_batch clears
    # the stamp on every batch card so a terminal not_found card cannot pin the lane.
    # Bulk/stock/email/search creation paths never stamp (created_at fast lane instead).
    enrich_requested_at = Column(UTCDateTime, index=True)

    # Validation conflicts (migration 099): evidence from a tier>=80 authoritative
    # source that contradicted a manual (tier 100) value. The ladder KEEPS the manual
    # value; spec_tiers.record_validation_conflict persists the contradiction here.
    # List entries: {"key": <spec_key|"category">, "manual": {"value", "updated_at"},
    #   "evidence": {"source", "tier", "confidence", "value", "observed_at"}}
    # — de-duped per (key, evidence.source), newest evidence replaces.
    validation_conflicts = Column(JSONB)
    # Review-queue filter flag — True iff validation_conflicts is non-empty. Backed by
    # the partial index ix_material_cards_needs_review (WHERE has_validation_conflict).
    has_validation_conflict = Column(Boolean, nullable=False, default=False, server_default="false")

    # Demand telemetry (migration 105 — TRIO's SFDC Weekly Export, one-shot backfill
    # via app/management/import_demand_telemetry.py; NO recurring refresh — the export
    # is a static snapshot, re-import is an explicit operator step). Prioritization
    # signal ONLY, never a displayed fact: worker select_batch and the spec-pass
    # selection order by (sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS
    # LAST, id) so enrichment slots land on parts TRIO actually trades. NULL = no
    # telemetry row matched this card's normalized_mpn. Served on PG by the partial
    # index ix_mc_demand_queue (migration-only — its DESC NULLS LAST keys are not
    # valid SQLite index DDL, so it is deliberately NOT declared on the model).
    sourced_qty_90d = Column(Integer)
    last_sourced_at = Column(UTCDateTime)

    deleted_at = Column(UTCDateTime, nullable=True, index=True)  # NULL = active, non-NULL = soft-deleted

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    datasheets = relationship(
        "MaterialCardDatasheet",
        back_populates="material_card",
        cascade="all, delete-orphan",
        order_by="desc(MaterialCardDatasheet.captured_at)",
    )
    attachments = relationship("MaterialCardAttachment", back_populates="material_card", cascade="all, delete-orphan")

    __table_args__ = (
        # Partial index for the review-queue filter (conflicted cards are a tiny
        # minority — a full index would be ~all-false dead weight). sqlite_where keeps
        # the test engine's create_all in step with migration 099.
        Index(
            "ix_material_cards_needs_review",
            "has_validation_conflict",
            postgresql_where=Column("has_validation_conflict"),
            sqlite_where=Column("has_validation_conflict"),
        ),
        # Raw-DDL indexes reconciled into the model so the drift gate sees them (#464):
        # pg_trgm GIN (fuzzy MPN/description/manufacturer search), the FTS tsvector GIN,
        # and a partial last-searched index.
        Index("ix_material_cards_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_material_cards_trgm_mpn",
            "display_mpn",
            postgresql_using="gin",
            postgresql_ops={"display_mpn": "gin_trgm_ops"},
        ),
        Index(
            "ix_mc_trgm_description",
            "description",
            postgresql_using="gin",
            postgresql_ops={"description": "gin_trgm_ops"},
        ),
        Index(
            "ix_mc_trgm_manufacturer",
            "manufacturer",
            postgresql_using="gin",
            postgresql_ops={"manufacturer": "gin_trgm_ops"},
        ),
        Index(
            "ix_mc_trgm_norm_mpn",
            "normalized_mpn",
            postgresql_using="gin",
            postgresql_ops={"normalized_mpn": "gin_trgm_ops"},
        ),
        Index("ix_mc_last_searched", "last_searched_at", postgresql_where=text("last_searched_at IS NOT NULL")),
    )

    # --- Validators ---
    @validates("search_count")
    def _validate_search_count(self, _key, value):
        if value is not None and value < 0:
            raise ValueError(f"search_count must be >= 0, got {value}")
        return value

    @validates("enrichment_status")
    def _validate_enrichment_status(self, _key, value):
        from ..constants import MaterialEnrichmentStatus

        if value is None:
            return MaterialEnrichmentStatus.UNENRICHED.value
        return MaterialEnrichmentStatus(value).value  # raises ValueError on unknown

    @validates("category")
    def _validate_category(self, _key, value):
        """SP3 ladder hardening: only NULL or a canonical commodity key may be assigned.

        set_category (spec_tiers) is the single routed writer and only ever assigns
        canonical keys, so it passes untouched; an off-vocab direct assignment (the
        pre-#267 bypass-writer class that minted 61 junk categories) raises instead of
        silently persisting junk with stale provenance columns. Lazy import — the
        registry imports app.models at module level.
        """
        if value is None:
            return None
        from app.services.commodity_registry import CANONICAL_COMMODITY_KEYS

        if value not in CANONICAL_COMMODITY_KEYS:
            raise ValueError(
                f"material_cards.category must be a canonical commodity key or None, got {value!r} — "
                "route the write through spec_tiers.set_category (which normalizes via "
                "category_normalizer and arbitrates via the F1 ladder)"
            )
        return value


class MaterialCardDatasheet(Base):
    """A permanent datasheet copy stored in the company SharePoint library, attached to
    a MaterialCard.

    Unlike MaterialCard.datasheet_url (an external link that rots when vendors pull EOL
    datasheets), this is our own copy: download → verify → store in the company library.
    """

    __tablename__ = "material_card_datasheets"

    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))  # Graph driveItem id of the stored copy
    library_web_url = Column(Text)  # Graph webUrl (convenience; the in-app download is the primary access path)
    library_drive_id = Column(String(200))  # Graph drive id of the company library this copy lives in
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    source = Column(String(50))  # "connector" | "web"
    original_url = Column(Text)  # where the copy came from (provenance/audit)
    verified = Column(Boolean, nullable=False, default=False)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    captured_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    material_card = relationship("MaterialCard", back_populates="datasheets")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])


class MaterialVendorHistory(Base):
    __tablename__ = "material_vendor_history"
    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False)
    vendor_name = Column(String(255), nullable=False)
    vendor_name_normalized = Column(String(255))
    source_type = Column(String(50))
    is_authorized = Column(Boolean, default=False)
    first_seen = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    last_seen = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    times_seen = Column(Integer, default=1)
    last_qty = Column(Integer)
    last_price = Column(Numeric(12, 4))
    last_currency = Column(String(10), default="USD")
    last_manufacturer = Column(String(255))
    vendor_sku = Column(String(255))

    source = Column(String(50), default="api_sighting")

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    material_card = relationship("MaterialCard")

    __table_args__ = (
        Index("ix_mvh_card_vendor", "material_card_id", "vendor_name", unique=True),
        Index("ix_mvh_vendor", "vendor_name"),
        Index("ix_mvh_vendor_norm", "vendor_name_normalized"),
    )


class MaterialCardAudit(Base):
    """Audit log for material card lifecycle events."""

    __tablename__ = "material_card_audit"
    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, index=True)  # No FK — survives card deletion
    # created, linked, unlinked, deleted, merged, healed, restored,
    # category_cleanup / facet_cleanup (app/management/cleanup_known_bad.py),
    # category_recategorize (app/services/spec_tiers.recategorize)
    action = Column(String(50), nullable=False)
    entity_type = Column(String(50))  # requirement, sighting, offer
    entity_id = Column(Integer)
    old_card_id = Column(Integer)
    new_card_id = Column(Integer)
    normalized_mpn = Column(String(255), index=True)
    details = Column(JSON)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    created_by = Column(String(255))  # system, user email, scheduler

    __table_args__ = (Index("ix_mca_card_action", "material_card_id", "action"),)


class ProactiveMatch(Base):
    """A match between a new vendor offer and an archived customer requirement."""

    __tablename__ = "proactive_matches"
    id = Column(Integer, primary_key=True)
    offer_id = Column(Integer, ForeignKey("offers.id", ondelete="CASCADE"), nullable=False)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"), nullable=True)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"), nullable=True)
    salesperson_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    mpn = Column(String(255), nullable=False)
    status = Column(String(20), default=ProactiveMatchStatus.NEW)  # new | sent | dismissed | converted

    # CPH-enriched fields (populated by matching engine)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="SET NULL"))
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"))
    match_score = Column(Integer, default=0)  # 0-100 composite score
    margin_pct = Column(Numeric(5, 2))  # Potential margin %
    customer_purchase_count = Column(Integer, default=0)
    customer_last_price = Column(Numeric(12, 4))
    customer_last_purchased_at = Column(UTCDateTime)
    our_cost = Column(Numeric(12, 4))
    dismiss_reason = Column(String(255))

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    offer = relationship("Offer", foreign_keys=[offer_id])
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])

    # --- Validators ---
    @validates("match_score")
    def _validate_match_score(self, _key, value):
        if value is not None and not (0 <= value <= 100):
            raise ValueError(f"match_score must be 0-100, got {value}")
        return value

    __table_args__ = (
        Index("ix_pm_offer", "offer_id"),
        Index("ix_pm_req", "requisition_id"),
        Index("ix_pm_site", "customer_site_id"),
        Index("ix_pm_sales", "salesperson_id"),
        Index("ix_pm_status", "status"),
        Index("ix_pm_mpn_site", "mpn", "customer_site_id"),
        Index("ix_pm_material_card", "material_card_id"),
        Index("ix_pm_score", "match_score"),
        Index("ix_pm_status_sales", "status", "salesperson_id"),
        Index("ix_pm_company", "company_id"),
    )


class ProactiveOffer(Base):
    """A proactive offer email sent to a customer with selected match items."""

    __tablename__ = "proactive_offers"
    id = Column(Integer, primary_key=True)
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"), nullable=True)
    salesperson_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    line_items = Column(JSON, nullable=False, default=list)
    recipient_contact_ids = Column(JSON, default=list)
    recipient_emails = Column(JSON, default=list)
    subject = Column(String(500))
    email_body_html = Column(Text)
    graph_message_id = Column(String(500))
    status = Column(String(20), default="sent")
    sent_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    converted_requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"))
    converted_quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="SET NULL"))
    converted_at = Column(UTCDateTime)
    total_sell = Column(Numeric(12, 2))
    total_cost = Column(Numeric(12, 2))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])

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
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="CASCADE"), nullable=False)
    last_offered_at = Column(UTCDateTime, nullable=False)
    proactive_offer_id = Column(Integer, ForeignKey("proactive_offers.id", ondelete="SET NULL"))

    __table_args__ = (
        Index("ix_pt_mpn_site", "mpn", "customer_site_id", unique=True),
        Index("ix_pt_last_offered", "last_offered_at"),
    )


class ProactiveDoNotOffer(Base):
    """Permanent suppression: salesperson marks an MPN as 'do not offer' to a company."""

    __tablename__ = "proactive_do_not_offer"
    id = Column(Integer, primary_key=True)
    mpn = Column(String(255), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason = Column(String(255))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    company = relationship("Company", foreign_keys=[company_id])
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (Index("ix_pdno_mpn_company", "mpn", "company_id", unique=True),)


class ChangeLog(Base):
    """Field-level change log for audit trail on offers, requirements, requisitions."""

    __tablename__ = "change_log"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(50), nullable=False)  # offer, requirement, requisition
    entity_id = Column(Integer, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text)
    new_value = Column(Text)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_changelog_entity", "entity_type", "entity_id"),
        Index("ix_changelog_user", "user_id"),
    )


class ActivityLog(Base):
    """Activity log — system events (email, phone) and manual entries (call, note)."""

    __tablename__ = "activity_log"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    activity_type = Column(String(20), nullable=False)
    channel = Column(String(20), nullable=False)

    # Polymorphic link — at most one set
    company_id = Column(Integer, ForeignKey("companies.id", ondelete="SET NULL"))
    vendor_card_id = Column(Integer, ForeignKey("vendor_cards.id", ondelete="SET NULL"))
    vendor_contact_id = Column(Integer, ForeignKey("vendor_contacts.id", ondelete="SET NULL"))
    requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="SET NULL"))
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True)
    quote_id = Column(Integer, ForeignKey("quotes.id", ondelete="SET NULL"))
    customer_site_id = Column(Integer, ForeignKey("customer_sites.id", ondelete="SET NULL"))
    site_contact_id = Column(Integer, ForeignKey("site_contacts.id", ondelete="SET NULL"))

    buy_plan_id = Column(Integer, ForeignKey("buy_plans_v3.id", ondelete="SET NULL"), nullable=True)
    # Approvals Workspace (migration 196): per-line / per-prepayment notes threads and
    # field-diff audit rows key on these. Nullable + SET NULL like the other
    # polymorphic-scope FKs — the timeline row outlives its subject.
    buy_plan_line_id = Column(Integer, ForeignKey("buy_plan_lines.id", ondelete="SET NULL"), nullable=True)
    prepayment_id = Column(Integer, ForeignKey("prepayments.id", ondelete="SET NULL"), nullable=True)
    # Resell-outreach scope: outreach/touch events on an excess list write to the same
    # immutable timeline + cadence clocks (resell-outreach Chunk A; CRM Phase 3 generalizes
    # the activity layer). Nullable + SET NULL like the other polymorphic-scope FKs.
    excess_list_id = Column(Integer, ForeignKey("excess_lists.id", ondelete="SET NULL"), nullable=True)

    # Contact snapshot
    contact_email = Column(String(255))
    contact_phone = Column(String(100))
    contact_name = Column(String(255))

    # Metadata
    subject = Column(String(500))
    duration_seconds = Column(Integer)
    external_id = Column(String(255))
    notes = Column(Text)
    dismissed_at = Column(UTCDateTime)
    auto_logged = Column(Boolean, default=False)
    occurred_at = Column(UTCDateTime)

    # Communication Intelligence columns (migration 058)
    direction = Column(String(20))  # "inbound" | "outbound"
    event_type = Column(String(30))  # "email" | "call" | "note" | "meeting"
    summary = Column(String(500))
    details = Column(JSON)

    # AI Quality Scoring (Phase 2b)
    quality_score = Column(Float)
    quality_classification = Column(String(30))
    quality_assessed_at = Column(UTCDateTime)
    is_meaningful = Column(Boolean)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    user = relationship("User", foreign_keys=[user_id])
    company = relationship("Company", foreign_keys=[company_id])
    vendor_card = relationship("VendorCard", foreign_keys=[vendor_card_id])
    vendor_contact = relationship("VendorContact", foreign_keys=[vendor_contact_id])
    requisition = relationship("Requisition", foreign_keys=[requisition_id])
    requirement = relationship("Requirement", foreign_keys=[requirement_id])
    quote = relationship("Quote", foreign_keys=[quote_id])
    customer_site = relationship("CustomerSite", foreign_keys=[customer_site_id])
    excess_list = relationship("ExcessList", foreign_keys=[excess_list_id])

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
        Index(
            "ix_activity_site_contact",
            "site_contact_id",
            "created_at",
            postgresql_where=Column("site_contact_id").isnot(None),
        ),
        Index("ix_activity_user", "user_id", "created_at"),
        Index(
            "ix_activity_external",
            "external_id",
            unique=False,
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
        Index(
            "ix_activity_req_channel",
            "requisition_id",
            "channel",
            "created_at",
            postgresql_where=Column("requisition_id").isnot(None),
        ),
        Index("ix_activity_created_at", "created_at"),
        Index(
            "ix_activity_requirement",
            "requirement_id",
            "created_at",
            postgresql_where=Column("requirement_id").isnot(None),
        ),
        Index(
            "ix_activity_excess_list",
            "excess_list_id",
            "created_at",
            postgresql_where=Column("excess_list_id").isnot(None),
        ),
        # Approvals Workspace (migration 196): per-line / per-prepayment thread reads.
        Index(
            "ix_activity_buy_plan_line",
            "buy_plan_line_id",
            "created_at",
            postgresql_where=Column("buy_plan_line_id").isnot(None),
        ),
        Index(
            "ix_activity_prepayment",
            "prepayment_id",
            "created_at",
            postgresql_where=Column("prepayment_id").isnot(None),
        ),
        # Raw-DDL indexes reconciled into the model so the drift gate sees them (#464):
        # a composite user/channel index + a partial index over un-scored activity rows.
        Index("ix_activity_user_channel_created", "user_id", "channel", "created_at"),
        Index("ix_activity_unscored", "quality_assessed_at", postgresql_where=text("quality_assessed_at IS NULL")),
    )


class ActivityDigest(Base):
    """AI-generated digest of an entity's activity timeline (cache).

    One row per (entity_type, entity_id). Regenerated lazily on view when the timeline
    basis changes; see app/services/activity_digest_service.py.
    """

    __tablename__ = "activity_digest"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(50), nullable=False)  # DigestEntityType
    entity_id = Column(Integer, nullable=False)

    headline = Column(String(300))
    narrative = Column(Text)
    highlights = Column(JSON)  # list[{"label": str, "value": str}]
    next_step = Column(String(500))
    status_signal = Column(String(20))  # DigestStatusSignal

    generated_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))
    basis_last_activity_at = Column(UTCDateTime)
    basis_activity_count = Column(Integer, default=0)
    cooldown_until = Column(UTCDateTime)
    model = Column(String(50))

    @validates("entity_type")
    def _validate_entity_type(self, _key, value):
        from ..constants import DigestEntityType

        return DigestEntityType(value).value  # raises ValueError on unknown

    @validates("status_signal")
    def _validate_status_signal(self, _key, value):
        from ..constants import DigestStatusSignal

        return DigestStatusSignal(value).value if value is not None else None

    __table_args__ = (UniqueConstraint("entity_type", "entity_id", name="uq_activity_digest_entity"),)


class MaterialCardAttachment(Base):
    """User-uploaded file attachment on a material card part dossier.

    Distinct from MaterialCardDatasheet (system-captured PDFs). These are user files:
    drawings, test reports, photos, POs, anything the buyer wants to pin to the part.

    library_drive_id NULL  → OneDrive fallback row (user token, item in /me/drive)
    library_drive_id set   → company SharePoint library row (app token)

    Called by: app/routers/attachments_extra.py, app/services/attachment_service.py
    Depends on: MaterialCard, User
    """

    __tablename__ = "material_card_attachments"
    id = Column(Integer, primary_key=True)
    material_card_id = Column(Integer, ForeignKey("material_cards.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(500), nullable=False)
    library_item_id = Column(String(500))
    library_drive_id = Column(String(200))
    library_web_url = Column(Text)
    thumbnail_url = Column(Text)
    content_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    material_card = relationship("MaterialCard", back_populates="attachments")
    uploaded_by = relationship("User", foreign_keys=[uploaded_by_id])

    __table_args__ = (Index("ix_material_card_attachments_card", "material_card_id"),)
